"""
ทดสอบ ModeManager ด้วยระบบปลอม (ไม่ต้องมีหุ่น/ROS)

รัน:  cd ~/ros2_ws/src/agv_web_app && python3 -m unittest discover -s test -v
"""
import os
import shutil
import sys
import tempfile
import time
import unittest

from agv_web_app.mode_manager import (
    ERROR, MANUAL, MAPPING, NAVIGATION, RUNNING, STANDBY, STARTING, ModeManager)

FAKE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fake_system.py')
NODE_OF = {MAPPING: 'slam_toolbox', MANUAL: 'ekf_filter_node', NAVIGATION: 'bt_navigator'}


def alive(pid):
    """process ยังมีชีวิตอยู่จริง (ไม่นับ zombie)"""
    try:
        with open(f'/proc/{pid}/stat') as f:
            return f.read().rsplit(')', 1)[1].split()[0] != 'Z'
    except (FileNotFoundError, ProcessLookupError):
        return False


class ModeManagerTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='modemgr_')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state_file = os.path.join(self.tmp, 'graph.txt')
        open(self.state_file, 'w').close()
        self.overlap = False          # เคยเห็นระบบของสองโหมดพร้อมกันหรือไม่

    # ── ตัวช่วย ────────────────────────────────────────────────────────
    def list_nodes(self):
        with open(self.state_file) as f:
            names = f.read().split()
        if len(set(names) & set(NODE_OF.values())) > 1:
            self.overlap = True
        return names

    def make(self, behaviors=None, **kw):
        behaviors = behaviors or {}
        commands = {
            mode: [sys.executable, FAKE, self.state_file, name,
                   behaviors.get(mode, 'normal'), '0.2']
            for mode, name in NODE_OF.items()
        }
        args = dict(sigint_timeout=1.0, sigterm_timeout=1.0, settle_timeout=2.0,
                    poll_interval=0.05, ready_timeout={m: 3.0 for m in NODE_OF})
        args.update(kw)
        mgr = ModeManager(commands, self.list_nodes, os.path.join(self.tmp, 'logs'), **args)
        self.addCleanup(mgr.shutdown, 10.0)
        return mgr

    def wait_for(self, mgr, predicate, timeout=10.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            snap = mgr.snapshot()
            if predicate(snap):
                return snap
            time.sleep(0.05)
        self.fail(f'หมดเวลารอ; snapshot ล่าสุด = {mgr.snapshot()}')

    def settle(self, mgr, mode):
        mgr.request_mode(mode)
        return self.wait_for(mgr, lambda s: s['state'] in (RUNNING, ERROR) and s['target'] is None)

    # ── กรณีปกติ ───────────────────────────────────────────────────────
    def test_start_and_stop(self):
        mgr = self.make()
        ok, _ = mgr.request_mode(NAVIGATION)
        self.assertTrue(ok)
        self.assertEqual(self.wait_for(mgr, lambda s: s['state'] == STARTING)['target'], NAVIGATION)
        snap = self.wait_for(mgr, lambda s: s['mode'] == NAVIGATION and s['state'] == RUNNING)
        self.assertEqual(snap['target'], None)
        self.assertTrue(snap['managed'])
        self.assertIn('bt_navigator', self.list_nodes())

        snap = self.settle(mgr, STANDBY)
        self.assertEqual((snap['mode'], snap['state']), (STANDBY, RUNNING))
        self.assertEqual(self.list_nodes(), [])
        self.assertEqual(mgr.last_stop, 'SIGINT')      # ปิดแบบสุภาพ ไม่ต้องใช้สัญญาณแรง

    def test_switch_closes_old_before_starting_new(self):
        mgr = self.make()
        self.settle(mgr, NAVIGATION)
        snap = self.settle(mgr, MAPPING)
        self.assertEqual((snap['mode'], snap['state']), (MAPPING, RUNNING))
        self.assertEqual(self.list_nodes(), ['slam_toolbox'])
        self.assertFalse(self.overlap, 'พบระบบสองโหมดรันพร้อมกัน')

    def test_waits_for_graph_to_drop_old_nodes(self):
        # process จบแล้ว แต่ชื่อยังค้างใน graph ~0.8 s → ต้องรอ ไม่เปิดโหมดใหม่ทับ
        mgr = self.make({NAVIGATION: 'lingering'}, settle_timeout=4.0)
        self.settle(mgr, NAVIGATION)
        snap = self.settle(mgr, MAPPING)
        self.assertEqual((snap['mode'], snap['state']), (MAPPING, RUNNING))
        self.assertEqual(self.list_nodes(), ['slam_toolbox'])
        self.assertFalse(self.overlap, 'เปิดโหมดใหม่ทั้งที่ node เก่ายังค้างใน graph')

    def test_same_mode_is_noop(self):
        mgr = self.make()
        self.settle(mgr, MANUAL)
        ok, msg = mgr.request_mode(MANUAL)
        self.assertTrue(ok)
        self.assertEqual(msg['code'], 'mode.already')
        self.assertEqual(mgr.snapshot()['state'], RUNNING)

    def test_standby_when_nothing_running(self):
        mgr = self.make()
        ok, _ = mgr.request_mode(STANDBY)
        self.assertTrue(ok)
        self.assertEqual(mgr.snapshot()['mode'], STANDBY)

    # ── กติกาความปลอดภัย ───────────────────────────────────────────────
    def test_unknown_mode_rejected(self):
        ok, msg = self.make().request_mode('TURBO')
        self.assertFalse(ok)
        self.assertEqual(msg, {'code': 'mode.err.unknown', 'params': {'mode': 'TURBO'}})

    def test_busy_rejects_second_request(self):
        mgr = self.make()
        self.assertTrue(mgr.request_mode(NAVIGATION)[0])
        ok, msg = mgr.request_mode(MAPPING)
        self.assertFalse(ok)
        self.assertEqual(msg['code'], 'mode.err.busy')
        self.wait_for(mgr, lambda s: s['mode'] == NAVIGATION)

    def test_external_system_blocks_start(self):
        with open(self.state_file, 'w') as f:
            f.write('bt_navigator')            # มีคนเปิด Nav2 เองจาก terminal
        mgr = self.make()
        ok, msg = mgr.request_mode(MAPPING)
        self.assertFalse(ok)
        self.assertEqual(msg, {'code': 'mode.err.external', 'params': {'nodes': ['bt_navigator']}})
        snap = mgr.snapshot()
        self.assertEqual(snap['external'], ['bt_navigator'])
        self.assertFalse(snap['managed'])
        self.assertEqual(snap['mode'], STANDBY)

    def test_never_ready_times_out_and_cleans_up(self):
        mgr = self.make({NAVIGATION: 'never_ready'}, ready_timeout={NAVIGATION: 1.0})
        snap = self.settle(mgr, NAVIGATION)
        self.assertEqual((snap['mode'], snap['state']), (STANDBY, ERROR))
        self.assertEqual(snap['message']['code'], 'mode.err.timeout')
        self.assertEqual((snap['message']['params']['mode'], snap['message']['params']['seconds']),
                         (NAVIGATION, 1))
        self.assertTrue(snap['message']['params']['log'].endswith('.log'))
        self.assertTrue(os.path.exists(snap['log']))
        self.assertEqual(mgr._proc, None)

    def test_crash_after_running_is_reported(self):
        mgr = self.make({MANUAL: 'crash'})
        self.settle(mgr, MANUAL)
        snap = self.wait_for(mgr, lambda s: s['state'] == ERROR, timeout=8.0)
        self.assertEqual(snap['mode'], STANDBY)
        self.assertEqual(snap['message']['code'], 'mode.err.crashed')
        self.assertEqual(snap['message']['params']['exit_code'], 3)

    # ── การปิดแบบไล่ระดับสัญญาณ ────────────────────────────────────────
    def test_stubborn_needs_sigterm(self):
        mgr = self.make({NAVIGATION: 'stubborn'})
        self.settle(mgr, NAVIGATION)
        snap = self.settle(mgr, STANDBY)
        self.assertEqual((snap['mode'], snap['state']), (STANDBY, RUNNING))
        self.assertEqual(mgr.last_stop, 'SIGTERM')

    def test_immortal_escalates_to_sigkill_and_reports_leftover(self):
        mgr = self.make({NAVIGATION: 'immortal'}, sigint_timeout=0.3, sigterm_timeout=0.3,
                        settle_timeout=0.5)
        self.settle(mgr, NAVIGATION)
        snap = self.settle(mgr, STANDBY)
        self.assertEqual(mgr.last_stop, 'SIGKILL')
        # SIGKILL ลบชื่อใน "graph" ไม่ได้ → ต้องรายงานว่ามี node ค้าง ไม่ใช่ประกาศว่าปิดสำเร็จ
        self.assertEqual(snap['state'], ERROR)
        self.assertEqual(snap['message']['code'], 'mode.err.leftover')
        self.assertIn('bt_navigator', snap['message']['params']['nodes'])

    def test_stop_kills_whole_process_group(self):
        mgr = self.make({NAVIGATION: 'child'})
        self.settle(mgr, NAVIGATION)
        with open(self.state_file + '.child') as f:
            child_pid = int(f.read())
        self.assertTrue(alive(child_pid))
        self.settle(mgr, STANDBY)
        end = time.monotonic() + 3.0
        while alive(child_pid) and time.monotonic() < end:
            time.sleep(0.05)
        self.assertFalse(alive(child_pid), 'process ลูกยังค้างอยู่หลังปิดโหมด')

    def test_shutdown_stops_managed_system(self):
        mgr = self.make()
        self.settle(mgr, NAVIGATION)
        mgr.shutdown(10.0)
        self.assertEqual(self.list_nodes(), [])
        self.assertEqual(mgr.snapshot()['mode'], STANDBY)


if __name__ == '__main__':
    unittest.main()
