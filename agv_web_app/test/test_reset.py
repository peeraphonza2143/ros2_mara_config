"""
ทดสอบ Reset all + บันทึกกลุ่ม process ที่ค้าง (managed.json) ของ ModeManager ด้วยระบบปลอม

⚠ ไม่เรียก sweep จริง (จะไปฆ่าระบบหุ่นจริงบนเครื่อง) — ทุกเทสต์ฉีด sweep ปลอมเข้าไป

รัน:  cd ~/ros2_ws/src/agv_web_app && python3 -m unittest discover -s test -v
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from agv_web_app import mode_manager
from agv_web_app.mode_manager import ERROR, MANUAL, MAPPING, NAVIGATION, RUNNING, STANDBY, STOPPING, ModeManager
from agv_web_app.sweep import Proc

FAKE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fake_system.py')
NODE_OF = {MAPPING: 'slam_toolbox', MANUAL: 'ekf_filter_node', NAVIGATION: 'bt_navigator'}


def alive(pid):
    try:
        with open(f'/proc/{pid}/stat') as f:
            return f.read().rsplit(')', 1)[1].split()[0] != 'Z'
    except (FileNotFoundError, ProcessLookupError):
        return False


class ResetBase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='reset_')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state_file = os.path.join(self.tmp, 'graph.txt')
        open(self.state_file, 'w').close()
        self.run_dir = os.path.join(self.tmp, 'run')
        self.record = os.path.join(self.run_dir, 'managed.json')
        self.orphans = []
        self.addCleanup(self.kill_orphans)

    def kill_orphans(self):
        for pid in self.orphans:
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    def list_nodes(self):
        with open(self.state_file) as f:
            return f.read().split()

    def make(self, behaviors=None, **kw):
        behaviors = behaviors or {}
        commands = {m: [sys.executable, FAKE, self.state_file, n, behaviors.get(m, 'normal'), '0.2']
                    for m, n in NODE_OF.items()}
        args = dict(sigint_timeout=1.0, sigterm_timeout=1.0, settle_timeout=2.0, poll_interval=0.05,
                    ready_timeout={m: 3.0 for m in NODE_OF}, run_dir=self.run_dir)
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

    def settled(self, snap):
        return snap['state'] in (RUNNING, ERROR) and snap['target'] is None

    def run_mode(self, mgr, mode):
        mgr.request_mode(mode)
        return self.wait_for(mgr, lambda s: s['mode'] == mode and self.settled(s))

    def spawn_orphan(self, node='slam_toolbox', behavior='normal', write_record=True):
        """เปิดระบบปลอมแล้วทำให้ "กำพร้า" จริง (ผู้ปกครองตายทันที → ถูกรับเลี้ยงโดย init) เหมือนเว็บแอปที่ตายไปแล้ว"""
        code = ('import subprocess, sys\n'
                f'p = subprocess.Popen([sys.executable, {FAKE!r}, {self.state_file!r}, {node!r}, {behavior!r}, "0.2"],'
                ' start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,'
                ' stderr=subprocess.DEVNULL)\nprint(p.pid)\n')
        pid = int(subprocess.run([sys.executable, '-c', code], capture_output=True, text=True).stdout)
        self.orphans.append(pid)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and node not in self.list_nodes():
            time.sleep(0.05)
        if write_record:
            self.write_record(pid, MAPPING)
        return pid

    def write_record(self, pid, mode, starttime='auto', pgid=None):
        os.makedirs(self.run_dir, exist_ok=True)
        with open(self.record, 'w') as f:
            json.dump({'pid': pid, 'pgid': pgid or pid, 'mode': mode, 'argv': ['x'],
                       'starttime': mode_manager._proc_starttime(pid) if starttime == 'auto' else starttime}, f)


class ExtraArgsTest(ResetBase):
    """extra_args: อาร์กิวเมนต์ที่คำนวณใหม่ทุกครั้งที่เปิดโหมด (ใช้ส่ง slam_params_file:= ของ Mapping)"""

    def make_dumper(self, extra, **kw):
        out = os.path.join(self.tmp, 'argv.txt')
        code = 'import sys, time; open(sys.argv[1], "w").write("|".join(sys.argv[2:])); time.sleep(30)'
        commands = {MAPPING: [sys.executable, '-c', code, out, 'base1', 'base2']}
        mgr = ModeManager(commands, lambda: ['slam_toolbox'] if os.path.exists(out) else [],
                          os.path.join(self.tmp, 'logs'), sigint_timeout=1.0, sigterm_timeout=1.0, settle_timeout=2.0,
                          poll_interval=0.05, ready_timeout={MAPPING: 3.0}, extra_args=extra, **kw)
        self.addCleanup(mgr.shutdown, 10.0)
        return mgr, out

    def test_extra_args_are_appended_each_time_the_mode_starts(self):
        calls = []

        def extra():
            calls.append(1)
            return [f'slam_params_file:=/x/{len(calls)}.yaml']
        mgr, out = self.make_dumper({MAPPING: extra})
        self.run_mode(mgr, MAPPING)
        self.assertEqual(open(out).read(), 'base1|base2|slam_params_file:=/x/1.yaml')
        os.remove(out)
        self.run_mode(mgr, STANDBY)
        self.run_mode(mgr, MAPPING)
        self.assertEqual(open(out).read(), 'base1|base2|slam_params_file:=/x/2.yaml')      # คำนวณใหม่ ไม่ใช้ค่าเก่า

    def test_no_extra_args_means_the_command_is_unchanged(self):
        mgr, out = self.make_dumper({MAPPING: lambda: []})
        self.run_mode(mgr, MAPPING)
        self.assertEqual(open(out).read(), 'base1|base2')
        mgr2, out2 = self.make_dumper(None)
        os.remove(out)
        self.run_mode(mgr2, MAPPING)
        self.assertEqual(open(out).read(), 'base1|base2')

    def test_failing_extra_args_reports_an_error_and_starts_nothing(self):
        def boom():
            raise OSError('read-only disk')
        mgr, out = self.make_dumper({MAPPING: boom})
        mgr.request_mode(MAPPING)
        snap = self.wait_for(mgr, lambda s: s['state'] == ERROR)
        self.assertEqual(snap['message']['code'], 'mode.err.internal')
        self.assertIn('read-only disk', snap['message']['params']['error'])
        self.assertFalse(os.path.exists(out))                      # ไม่เปิด SLAM ด้วยค่าที่ผู้ใช้ไม่ได้ตั้ง


class RecordTest(ResetBase):

    def test_record_is_written_on_start_and_removed_after_clean_stop(self):
        mgr = self.make()
        self.assertFalse(os.path.exists(self.record))
        self.run_mode(mgr, MAPPING)
        rec = json.load(open(self.record))
        self.assertEqual((rec['mode'], rec['pid'] == rec['pgid']), (MAPPING, True))
        self.assertEqual(rec['starttime'], mode_manager._proc_starttime(rec['pid']))
        self.run_mode(mgr, STANDBY)
        self.assertFalse(os.path.exists(self.record))

    def test_record_survives_if_app_dies_but_is_removed_by_shutdown(self):
        mgr = self.make()
        self.run_mode(mgr, MANUAL)
        self.assertTrue(os.path.exists(self.record))
        mgr.shutdown(10.0)
        self.assertFalse(os.path.exists(self.record))

    def test_record_removed_when_start_fails(self):
        mgr = self.make(behaviors={MAPPING: 'never_ready'}, ready_timeout={MAPPING: 0.5, MANUAL: 3.0, NAVIGATION: 3.0})
        mgr.request_mode(MAPPING)
        self.wait_for(mgr, lambda s: s['state'] == ERROR)
        self.assertFalse(os.path.exists(self.record))

    def test_without_run_dir_nothing_is_written(self):
        mgr = self.make(run_dir=None)
        self.run_mode(mgr, MAPPING)
        self.assertFalse(os.path.exists(self.record))
        self.assertIsNone(mgr.snapshot()['leftover'])


class LeftoverTest(ResetBase):

    def test_orphan_group_from_previous_session_is_detected(self):
        pid = self.spawn_orphan()
        mgr = self.make()
        snap = mgr.snapshot()
        self.assertEqual(snap['leftover'], {'mode': MAPPING})
        self.assertTrue(alive(pid))

    def test_starting_a_mode_is_refused_while_leftover_exists(self):
        self.spawn_orphan(node='nothing_in_graph_yet')
        open(self.state_file, 'w').close()                    # ยังไม่ขึ้น graph (เช่น launch ค้างก่อนสร้าง node)
        mgr = self.make()
        ok, message = mgr.request_mode(NAVIGATION)
        self.assertFalse(ok)
        self.assertEqual(message['code'], 'mode.err.leftover_session')
        self.assertEqual(message['params'], {'mode': MAPPING})
        ok, message = mgr.request_mode(STANDBY)               # Standby ซ้ำ = รับทราบเฉยๆ (เหมือนกรณี external)
        self.assertEqual((ok, message['code']), (True, 'mode.already'))
        self.assertIsNotNone(mgr.snapshot()['leftover'])      # ของค้างยังอยู่ → ต้องใช้ Reset all

    def test_record_of_dead_group_is_discarded(self):
        pid = self.spawn_orphan()
        os.killpg(pid, signal.SIGKILL)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and alive(pid):
            time.sleep(0.05)
        mgr = self.make()
        self.assertIsNone(mgr.snapshot()['leftover'])
        self.assertFalse(os.path.exists(self.record))

    def test_reused_pid_is_not_mistaken_for_ours(self):
        pid = self.spawn_orphan(write_record=False)
        self.write_record(pid, MAPPING, starttime=12345)       # เวลาเริ่มไม่ตรง = เป็น process อื่นที่ได้ pid เดียวกัน
        mgr = self.make()
        self.assertIsNone(mgr.snapshot()['leftover'])
        self.assertFalse(os.path.exists(self.record))
        self.assertTrue(alive(pid))                            # และห้ามไปแตะมัน

    def test_corrupt_record_is_discarded(self):
        os.makedirs(self.run_dir)
        for bad in ('{ nope', '[]', '{"pid": "x"}', '{"pid": 1, "pgid": 1}'):
            open(self.record, 'w').write(bad)
            self.assertIsNone(self.make().snapshot()['leftover'], bad)
            self.assertFalse(os.path.exists(self.record), bad)

    def test_leftover_disappears_when_user_kills_it_by_hand(self):
        pid = self.spawn_orphan()
        mgr = self.make()
        self.assertIsNotNone(mgr.snapshot()['leftover'])
        os.killpg(pid, signal.SIGKILL)
        self.wait_for(mgr, lambda s: s['leftover'] is None)
        self.assertFalse(os.path.exists(self.record))

    def test_leader_dead_but_robot_process_alive_in_group_is_still_ours(self):
        """ผู้นำกลุ่ม (ros2 launch) ตายแล้ว แต่ node ลูกยังอยู่ในกลุ่มเดิม"""
        script = os.path.join(self.tmp, 'ekf_node')
        open(script, 'w').write('import time\ntime.sleep(60)\n')
        code = ('import subprocess, sys\n'
                f'subprocess.Popen([sys.executable, {script!r}], stdin=subprocess.DEVNULL,'
                ' stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n')       # ลูกอยู่ในกลุ่มผู้นำ แล้วผู้นำจบเลย
        launcher = subprocess.Popen([sys.executable, '-c', code], start_new_session=True)
        launcher.wait()
        self.orphans.append(launcher.pid)
        time.sleep(0.3)
        self.write_record(launcher.pid, MAPPING, starttime=None)
        self.assertFalse(alive(launcher.pid))
        mgr = self.make()
        self.assertEqual(mgr.snapshot()['leftover'], {'mode': MAPPING})


class ResetTest(ResetBase):

    def reset(self, mgr):
        ok, message = mgr.request_reset()
        self.assertTrue(ok, message)
        return self.wait_for(mgr, lambda s: self.settled(s))

    def test_reset_stops_the_system_the_app_started(self):
        mgr = self.make()
        self.run_mode(mgr, NAVIGATION)
        self.assertEqual(mgr.snapshot()['state'], RUNNING)
        snap = self.reset(mgr)
        self.assertEqual((snap['mode'], snap['state']), (STANDBY, RUNNING))
        self.assertEqual(snap['message'], {'code': 'mode.reset.done', 'params': {'count': 1}})
        self.assertEqual(self.list_nodes(), [])
        self.assertFalse(os.path.exists(self.record))

    def test_reset_reports_progress_while_working(self):
        mgr = self.make()
        self.run_mode(mgr, NAVIGATION)
        ok, message = mgr.request_reset()
        self.assertEqual(message['code'], 'mode.resetting')
        snap = mgr.snapshot()
        self.assertEqual((snap['state'], snap['message']['code']), (STOPPING, 'mode.resetting')) \
            if snap['state'] == STOPPING else None
        self.wait_for(mgr, self.settled)

    def test_reset_kills_orphan_group_left_by_a_dead_app(self):
        pid = self.spawn_orphan()
        mgr = self.make()
        snap = self.reset(mgr)
        self.assertEqual((snap['mode'], snap['state'], snap['leftover']), (STANDBY, RUNNING, None))
        self.assertEqual(snap['message']['code'], 'mode.reset.done')
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and alive(pid):
            time.sleep(0.05)
        self.assertFalse(alive(pid))
        self.assertEqual(self.list_nodes(), [])
        self.assertFalse(os.path.exists(self.record))

    def test_reset_works_after_the_stuck_state_can_start_modes_again(self):
        self.spawn_orphan()
        mgr = self.make()
        self.assertFalse(mgr.request_mode(MAPPING)[0])
        self.reset(mgr)
        ok, _ = mgr.request_mode(MAPPING)
        self.assertTrue(ok)
        self.wait_for(mgr, lambda s: s['mode'] == MAPPING and self.settled(s))

    def test_reset_when_nothing_is_running(self):
        mgr = self.make()
        snap = self.reset(mgr)
        self.assertEqual(snap['message'], {'code': 'mode.reset.nothing', 'params': {}})
        self.assertEqual((snap['mode'], snap['state']), (STANDBY, RUNNING))

    def test_reset_is_refused_while_another_change_is_in_progress(self):
        mgr = self.make()
        mgr.request_mode(NAVIGATION)
        ok, message = mgr.request_reset()
        self.assertFalse(ok)
        self.assertEqual(message['code'], 'mode.err.busy')
        self.wait_for(mgr, lambda s: s['mode'] == NAVIGATION and self.settled(s))

    def test_second_reset_while_resetting_is_refused(self):
        mgr = self.make(sweep=lambda: (time.sleep(0.5), ([], []))[1])
        self.assertTrue(mgr.request_reset()[0])
        ok, message = mgr.request_reset()
        self.assertEqual((ok, message['code']), (False, 'mode.err.busy'))
        self.wait_for(mgr, self.settled)

    def test_reset_clears_an_error_state(self):
        mgr = self.make(behaviors={MAPPING: 'never_ready'},
                        ready_timeout={MAPPING: 0.5, MANUAL: 3.0, NAVIGATION: 3.0})
        mgr.request_mode(MAPPING)
        self.wait_for(mgr, lambda s: s['state'] == ERROR)
        snap = self.reset(mgr)
        self.assertEqual(snap['state'], RUNNING)

    def test_order_is_stop_motion_then_stop_systems_then_sweep_then_stop_motion_again(self):
        events = []
        mgr = self.make(stop_motion=lambda: events.append('motion'),
                        sweep=lambda: (events.append('sweep'), ([111, 222], []))[1])
        self.run_mode(mgr, NAVIGATION)
        pid = json.load(open(self.record))['pid']
        orig = mgr._stop_process

        def traced(proc):
            events.append('stop_managed')
            return orig(proc)
        mgr._stop_process = traced
        snap = self.reset(mgr)
        self.assertEqual(events, ['motion', 'stop_managed', 'sweep', 'motion'])
        self.assertEqual(snap['message']['params']['count'], 3)          # ระบบของแอป 1 + กวาด 2
        self.assertFalse(alive(pid))

    def test_failing_stop_motion_does_not_block_reset(self):
        def boom():
            raise RuntimeError('publisher closed')
        mgr = self.make(stop_motion=boom)
        self.run_mode(mgr, MANUAL)
        snap = self.reset(mgr)
        self.assertEqual((snap['mode'], snap['state']), (STANDBY, RUNNING))
        self.assertEqual(self.list_nodes(), [])

    def test_sweep_stops_a_system_started_outside_the_app(self):
        open(self.state_file, 'w').write('bt_navigator amcl')           # ระบบที่เปิดจาก terminal ของผู้ใช้

        def sweep():
            open(self.state_file, 'w').close()
            return [4001, 4002], []
        mgr = self.make(sweep=sweep)
        self.assertEqual(mgr.snapshot()['external'], ['amcl', 'bt_navigator'])
        snap = self.reset(mgr)
        self.assertEqual((snap['external'], snap['state']), ([], RUNNING))
        self.assertEqual(snap['message']['params']['count'], 2)

    def test_processes_that_cannot_be_stopped_are_reported_by_name(self):
        remaining = [Proc(9001, ['x'], 'sllidar_node'), Proc(9002, ['y'], 'sllidar_node'),
                     Proc(9003, ['z'], 'amcl')]
        mgr = self.make(sweep=lambda: ([], remaining))
        snap = self.reset(mgr)
        self.assertEqual((snap['mode'], snap['state']), (STANDBY, ERROR))
        self.assertEqual(snap['message'], {'code': 'mode.err.reset_remaining',
                                           'params': {'procs': ['amcl', 'sllidar_node']}})

    def test_node_that_stays_in_graph_is_reported(self):
        mgr = self.make(behaviors={NAVIGATION: 'immortal'})              # SIGKILL ก็ลบชื่อจาก graph ไม่ได้
        self.run_mode(mgr, NAVIGATION)
        snap = self.reset(mgr)
        self.assertEqual(snap['state'], ERROR)
        self.assertEqual(snap['message']['code'], 'mode.err.leftover')
        self.assertEqual(snap['message']['params']['nodes'], ['bt_navigator'])

    def test_shutdown_stops_motion_before_closing_systems(self):
        events = []
        mgr = self.make(stop_motion=lambda: events.append('motion'))
        self.run_mode(mgr, MANUAL)
        pid = json.load(open(self.record))['pid']
        mgr.shutdown(10.0)
        self.assertEqual(events, ['motion'])
        self.assertFalse(alive(pid))


if __name__ == '__main__':
    unittest.main()
