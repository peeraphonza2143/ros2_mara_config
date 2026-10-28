"""
ทดสอบตัวรัน Route (RouteRunner) กับ NavGoal จริง + Nav2 ปลอม (FakeClient) — ไม่ต้องมี ROS
เน้น: ผิดกติกา = ไม่ส่งอะไรให้ Nav2 · ล้มที่จุดไหนหยุดทั้ง Route · ยกเลิก/พัก/ต่อ · รอที่จุด · วนรอบ · เพดานเวลา

รัน:  cd ~/ros2_ws/src/agv_web_app && python3 -m unittest discover -s test -v
"""
import threading
import time
import unittest

from test_nav_goal import Clock, FakeClient, ok_probe

from agv_web_app.nav_goal import GoalError, NavGoal
from agv_web_app.route_run import RouteRunner, RunError
from agv_web_app.routes import RouteError


def P(name, x, y, wait=0):
    return {'name': name, 'x': x, 'y': y, 'yaw': 0.0, 'wait': wait}


ROUTE = {'loop': 1, 'points': [P('A', 1.0, 1.0), P('B', 2.0, 1.0, wait=5), P('C', 3.0, 1.0)]}


class Rig:
    def __init__(self, **kw):
        threaded = kw.pop('threaded', False)
        self.clock = Clock()
        self.client = FakeClient()
        self.goal = NavGoal(self.client, clock=self.clock, accept_timeout=0.2)
        self.localized, self.nav = True, True
        self.stops, self.rounds, self.refreshes, self.events = 0, 0, 0, []
        self.refresh_error = None
        kw.setdefault('loc_grace', 0.0)           # เทสต์ทั่วไปใช้แบบเข้ม (ไม่รอ) ; เทสต์ช่วง locating ระบุเอง
        self.runner = RouteRunner(self.goal, ok_probe, lambda: self.localized, lambda: self.nav, self._stop,
                                  on_round=self._round, on_event=self.events.append, clock=self.clock,
                                  sleep=kw.pop('sleep', lambda s: None), threaded=threaded, refresh=self._refresh, **kw)

    def _refresh(self):
        self.refreshes += 1
        if self.refresh_error:
            raise self.refresh_error

    def _stop(self):
        self.stops += 1

    def _round(self):
        self.rounds += 1

    def start(self, route=ROUTE, **kw):
        args = dict(name='R', map_name='lab', route=route, check_probe=ok_probe, clearance=0.4, confirmed=True)
        args.update(kw)
        return self.runner.start(**args)

    def sent(self):
        return [(x, y) for x, y, _ in self.client.sent]

    def arrive(self):
        """Nav2 รายงานว่าถึง goal ปัจจุบันแล้ว แล้วให้ตัวรันก้าวหนึ่งรอบ"""
        self.client.listener.on_result('succeeded')
        return self.runner.tick()

    def status(self):
        return self.runner.status()


class StartRulesTest(unittest.TestCase):

    def refused(self, exc, code=None, **kw):
        rig = kw.pop('rig', None) or Rig()
        with self.assertRaises(exc) as cm:
            rig.start(**kw)
        if code:
            self.assertEqual(cm.exception.code, code)
        self.assertEqual(rig.client.sent, [], 'ผิดกติกาแล้วต้องไม่ส่งอะไรให้ Nav2')
        self.assertEqual(rig.status()['state'], 'idle')
        self.assertFalse(rig.runner.active())

    def test_needs_confirmed_true_exactly(self):
        for bad in (False, None, 1, 'true', 'yes', [True]):
            self.refused(RunError, 'route.run.confirm_required', confirmed=bad)

    def test_navigation_must_be_up(self):
        rig = Rig()
        rig.nav = False
        self.refused(RunError, 'route.run.no_nav', rig=rig)

    def test_must_be_localized(self):
        rig = Rig()
        rig.localized = False
        self.refused(RunError, 'route.run.not_localized', rig=rig)

    def test_no_second_goal_or_route_at_the_same_time(self):
        rig = Rig()
        rig.goal.send(5.0, 5.0, 0.0, ok_probe, True, True)                 # มี goal เดี่ยวค้างอยู่
        sent = list(rig.client.sent)
        with self.assertRaises(RunError) as cm:
            rig.start()
        self.assertEqual(cm.exception.code, 'route.run.goal_busy')
        self.assertEqual(rig.client.sent, sent)
        rig2 = Rig()
        rig2.start()
        with self.assertRaises(RunError) as cm:
            rig2.start()
        self.assertEqual(cm.exception.code, 'route.run.busy')
        self.assertEqual(len(rig2.client.sent), 1)

    def test_forever_needs_its_own_confirmation(self):
        forever = {'loop': 0, 'points': ROUTE['points']}
        self.refused(RunError, 'route.run.forever_confirm_required', route=forever)
        self.refused(RunError, 'route.run.forever_confirm_required', route=forever, forever_confirmed='true')
        rig = Rig()
        rig.start(route=forever, forever_confirmed=True)
        self.assertEqual(rig.status()['loop'], 0)

    def test_every_point_is_checked_before_the_robot_moves(self):
        def probe(x, y, r):
            return {'status': 'tight' if x == 3.0 else 'ok', 'clearance': 0.1}
        rig = Rig()
        with self.assertRaises(RouteError) as cm:
            rig.start(check_probe=probe)
        self.assertEqual((cm.exception.code, cm.exception.params['n']), ('route.err.point_tight', 3))
        self.assertEqual(rig.client.sent, [])                                 # จุดสุดท้ายไม่ผ่าน = ไม่ปล่อยหุ่นออกเลย

    def test_two_starts_overlapping_never_overwrite_each_other(self):
        rig = Rig()
        inner = []

        def probe(x, y, r):
            if not inner:                                                    # คนที่สองเริ่มสำเร็จก่อนระหว่างตรวจจุด
                inner.append(True)
                rig.start(name='Second', route={'loop': 1, 'points': [P('S', 9.0, 9.0)]})
            return {'status': 'ok', 'clearance': None}
        with self.assertRaises(RunError) as cm:
            rig.start(check_probe=probe)
        self.assertEqual(cm.exception.code, 'route.run.busy')
        self.assertEqual(rig.sent(), [(9.0, 9.0)], 'ต้องมีเพียงงานของคนที่เริ่มสำเร็จก่อน')
        self.assertEqual(rig.status()['route'], 'Second')

    def test_invalid_routes_are_refused(self):
        for route in ({'loop': 1, 'points': []}, {'loop': -1, 'points': ROUTE['points']}, {'loop': 1, 'points': 'x'}):
            self.refused(RouteError, route=route)

    def test_first_goal_refused_by_nav_goal_rules_means_never_started(self):
        rig = Rig()
        rig.client.is_ready = False
        with self.assertRaises(GoalError) as cm:
            rig.start()
        self.assertEqual(cm.exception.code, 'nav.err.goal_no_server')
        self.assertEqual(rig.status()['state'], 'idle')
        rig.client.is_ready = True
        rig.start()                                                           # ไม่ค้างสถานะ: เริ่มใหม่ได้
        self.assertEqual(rig.status()['state'], 'running')


class RunFlowTest(unittest.TestCase):

    def test_visits_points_in_order_waits_and_succeeds(self):
        rig = Rig()
        rig.start()
        s = rig.status()
        self.assertEqual((s['state'], s['index'], s['round'], s['total']), ('running', 0, 1, 3))
        self.assertEqual(s['current']['name'], 'A')
        self.assertEqual(rig.sent(), [(1.0, 1.0)])
        self.assertTrue(rig.runner.tick())
        self.assertEqual(rig.sent(), [(1.0, 1.0)], 'ยังไม่ถึง = ไม่ส่งจุดถัดไป')
        self.assertTrue(rig.arrive())                                         # ถึง A (ไม่รอ) → ไป B
        self.assertEqual((rig.sent()[-1], rig.status()['index']), ((2.0, 1.0), 1))
        self.assertTrue(rig.arrive())                                         # ถึง B (รอ 5 วินาที)
        s = rig.status()
        self.assertEqual((s['phase'], s['wait_left'], len(rig.sent())), ('waiting', 5.0, 2))
        rig.clock.now += 4.9
        self.assertTrue(rig.runner.tick())
        self.assertEqual((len(rig.sent()), rig.status()['wait_left']), (2, 0.1))
        rig.clock.now += 0.2
        self.assertTrue(rig.runner.tick())                                    # รอครบ → ไป C
        self.assertEqual((rig.sent()[-1], rig.status()['phase']), ((3.0, 1.0), 'going'))
        self.assertFalse(rig.arrive())                                        # ถึง C = ครบ Route
        s = rig.status()
        self.assertEqual((s['state'], rig.rounds, len(rig.sent())), ('succeeded', 1, 3))
        self.assertFalse(rig.runner.active())
        self.assertEqual(rig.stops, 0, 'จบปกติไม่ต้องสั่งหยุดหุ่น')

    def test_loop_counts_rounds_and_restarts_from_the_first_point(self):
        rig = Rig()
        rig.start(route={'loop': 3, 'points': [P('A', 1.0, 1.0), P('B', 2.0, 2.0)]})
        for _ in range(4):                                                   # ครบ 2 รอบ (A,B,A,B)
            self.assertTrue(rig.arrive())
        self.assertEqual((rig.rounds, rig.status()['round'], rig.status()['index']), (2, 3, 0))
        self.assertEqual(rig.sent(), [(1.0, 1.0), (2.0, 2.0)] * 2 + [(1.0, 1.0)])   # รอบที่ 3 เริ่มที่จุดแรกอีกครั้ง
        self.assertTrue(rig.arrive())                                        # ถึง A ของรอบที่ 3 → ไป B
        self.assertEqual(rig.status()['index'], 1)
        self.assertFalse(rig.arrive())                                       # ถึง B = ครบรอบที่ 3
        self.assertEqual((rig.status()['state'], rig.rounds), ('succeeded', 3))
        self.assertEqual(len(rig.sent()), 6)

    def test_forever_keeps_going_until_stopped(self):
        rig = Rig()
        rig.start(route={'loop': 0, 'points': [P('A', 1.0, 1.0)]}, forever_confirmed=True)
        for _ in range(12):
            self.assertTrue(rig.arrive())
        self.assertEqual((rig.rounds, rig.status()['state']), (12, 'running'))
        self.assertTrue(rig.runner.stop())
        self.assertEqual(rig.status()['state'], 'canceled')

    def test_single_point_route(self):
        rig = Rig()
        rig.start(route={'loop': 1, 'points': [P('Only', 1.0, 1.0)]})
        self.assertFalse(rig.arrive())
        self.assertEqual((rig.status()['state'], rig.rounds), ('succeeded', 1))

    def test_final_state_stays_visible_until_next_run(self):
        rig = Rig()
        rig.start(route={'loop': 1, 'points': [P('Only', 1.0, 1.0)]})
        rig.arrive()
        s = rig.status()
        self.assertEqual((s['state'], s['route'], s['round']), ('succeeded', 'R', 1))
        rig.start(route={'loop': 1, 'points': [P('Only', 2.0, 2.0)]})
        self.assertEqual((rig.status()['state'], rig.status()['run']), ('running', 2))

    def test_status_shape_is_json_friendly(self):
        import json
        rig = Rig()
        json.dumps(rig.status())
        rig.start()
        json.dumps(rig.status())
        self.assertEqual(rig.status()['elapsed'], 0.0)
        rig.clock.now += 7.0
        self.assertEqual(rig.status()['elapsed'], 7.0)


class FailureTest(unittest.TestCase):

    def assert_failed(self, rig, code, sends):
        s = rig.status()
        self.assertEqual(s['state'], 'failed')
        self.assertEqual(s['reason']['code'], code)
        self.assertEqual(len(rig.client.sent), sends, 'ล้มแล้วห้ามส่งจุดถัดไป')
        self.assertFalse(rig.runner.active())

    def test_aborted_goal_stops_the_whole_route(self):
        rig = Rig()
        rig.start()
        rig.client.listener.on_result('failed')
        self.assertFalse(rig.runner.tick())
        self.assert_failed(rig, 'nav.goal.aborted', 1)
        self.assertEqual(rig.rounds, 0)
        self.assertGreaterEqual(rig.stops, 1)                                 # สั่งศูนย์เพื่อความแน่ใจ

    def test_rejected_goal_at_a_later_point_stops_the_route(self):
        rig = Rig()
        rig.start()
        rig.client.accept = 'no'
        self.assertFalse(rig.arrive())
        self.assert_failed(rig, 'nav.err.goal_rejected', 2)

    def test_lost_localization_between_points_stops_before_sending(self):
        rig = Rig()
        rig.start()
        rig.localized = False
        self.assertFalse(rig.arrive())
        self.assert_failed(rig, 'route.run.not_localized', 1)

    def test_point_became_blocked_stops_before_sending(self):
        rig = Rig()
        rig.start()
        rig.runner._probe = lambda x, y, r: {'status': 'tight', 'clearance': 0.1}     # แผนที่สดเปลี่ยนระหว่างทาง
        self.assertFalse(rig.arrive())
        self.assert_failed(rig, 'nav.err.goal_tight', 1)

    def test_navigation_going_down_fails_the_run_even_when_paused(self):
        rig = Rig()
        rig.start()
        rig.nav = False
        self.assertFalse(rig.runner.tick())
        self.assert_failed(rig, 'route.run.nav_lost', 1)
        rig2 = Rig()
        rig2.start()
        rig2.runner.pause()
        rig2.nav = False
        self.assertFalse(rig2.runner.tick())
        self.assertEqual(rig2.status()['reason']['code'], 'route.run.nav_lost')

    def test_goal_cancelled_elsewhere_ends_the_run_as_canceled(self):
        rig = Rig()
        rig.start()
        rig.client.listener.on_result('canceled')
        self.assertFalse(rig.runner.tick())
        s = rig.status()
        self.assertEqual((s['state'], s['reason']['code']), ('canceled', 'route.run.goal_canceled'))
        self.assertEqual(len(rig.client.sent), 1)

    def test_someone_else_sending_a_goal_is_a_conflict(self):
        rig = Rig()
        rig.start()
        rig.client.listener.on_result('succeeded')
        rig.goal.send(9.0, 9.0, 0.0, ok_probe, True, True)                   # goal อื่นแทรกก่อนตัวรันก้าว
        self.assertFalse(rig.runner.tick())
        self.assertEqual(rig.status()['reason']['code'], 'route.run.goal_conflict')

    def test_point_timeout_cancels_goal_and_fails(self):
        rig = Rig(point_timeout=60.0)
        rig.start()
        rig.clock.now += 59.0
        self.assertTrue(rig.runner.tick())
        rig.clock.now += 2.0
        self.assertFalse(rig.runner.tick())
        s = rig.status()
        self.assertEqual((s['state'], s['reason']['code']), ('failed', 'route.run.point_timeout'))
        self.assertEqual(s['reason']['params']['n'], 1)
        self.assertEqual(rig.client.cancels, 1)

    def test_max_run_time_applies_to_the_whole_route_even_when_waiting(self):
        rig = Rig(max_run=100.0)
        rig.start(route={'loop': 0, 'points': [P('A', 1.0, 1.0, wait=30)]}, forever_confirmed=True)
        rig.arrive()
        rig.clock.now += 101.0
        self.assertFalse(rig.runner.tick())
        self.assertEqual(rig.status()['reason']['code'], 'route.run.max_time')


class StopTest(unittest.TestCase):

    def test_stop_cancels_goal_zeroes_motion_and_blocks_next_points(self):
        rig = Rig()
        rig.start()
        self.assertTrue(rig.runner.stop())
        self.assertEqual((rig.status()['state'], rig.client.cancels, rig.stops), ('canceled', 1, 1))
        rig.client.listener.on_result('succeeded')                           # ผลของ goal เก่ามาช้า
        self.assertFalse(rig.runner.tick())
        self.assertEqual(len(rig.client.sent), 1)
        self.assertEqual(rig.rounds, 0)

    def test_stop_while_waiting_at_a_point(self):
        rig = Rig()
        rig.start()
        rig.arrive()
        rig.arrive()
        self.assertEqual(rig.status()['phase'], 'waiting')
        self.assertTrue(rig.runner.stop())
        rig.clock.now += 100
        self.assertFalse(rig.runner.tick())
        self.assertEqual(len(rig.client.sent), 2)

    def test_stop_is_idempotent_and_harmless_when_idle(self):
        rig = Rig()
        self.assertFalse(rig.runner.stop())
        rig.start()
        self.assertTrue(rig.runner.stop())
        self.assertFalse(rig.runner.stop())
        self.assertEqual(rig.client.cancels, 1)

    def test_stop_during_a_slow_send_still_halts_the_robot(self):
        rig = Rig()

        class SlowClient(FakeClient):
            def send(inner, x, y, yaw, listener):
                rig.runner.stop()                                            # ผู้ใช้กดหยุดระหว่างรอ Nav2 รับ goal
                super().send(x, y, yaw, listener)

        rig.client = SlowClient()
        rig.goal = NavGoal(rig.client, clock=rig.clock, accept_timeout=0.2)
        rig.runner._goal = rig.goal
        rig.start()
        self.assertEqual(rig.status()['state'], 'canceled')
        # ยกเลิก 2 ครั้ง: ตอนผู้ใช้กดหยุด + ตอนตัวรันเห็นว่าถูกสั่งหยุดไปแล้วหลังส่งเสร็จ
        # (goal ที่เพิ่งถูกรับหลังกดหยุดต้องถูกยกเลิกด้วย)
        self.assertEqual(rig.client.cancels, 2)
        self.assertGreaterEqual(rig.stops, 2)

    def test_can_start_again_after_stop(self):
        rig = Rig()
        rig.start()
        rig.runner.stop()
        rig.goal.status()
        rig.client.listener.on_result('canceled')                            # Nav2 ยืนยันว่ายกเลิกแล้ว
        rig.start()
        self.assertEqual(rig.status()['state'], 'running')


class PauseResumeTest(unittest.TestCase):

    def test_pause_cancels_goal_zeroes_motion_and_freezes(self):
        rig = Rig()
        rig.start()
        rig.runner.pause()
        s = rig.status()
        self.assertEqual((s['state'], rig.client.cancels, rig.stops, s['pause_settled']), ('paused', 1, 1, False))
        self.assertTrue(rig.runner.tick())                                   # พักอยู่ = ไม่ทำอะไร
        rig.client.listener.on_result('canceled')
        self.assertTrue(rig.runner.tick())
        self.assertEqual((rig.status()['state'], rig.status()['pause_settled']), ('paused', True))
        self.assertEqual(len(rig.client.sent), 1)
        rig.clock.now += 10000
        self.assertTrue(rig.runner.tick(), 'พักนานแค่ไหนก็ไม่หมดเวลา/ไม่ล้ม')
        self.assertEqual(rig.status()['state'], 'paused')

    def test_resume_before_the_old_goal_settled_is_refused(self):
        rig = Rig()
        rig.start()
        rig.runner.pause()
        with self.assertRaises(RunError) as cm:
            rig.runner.resume()
        self.assertEqual(cm.exception.code, 'route.run.pausing')
        self.assertEqual(rig.status()['state'], 'paused')

    def test_resume_resends_the_same_point_after_rechecking_localization(self):
        rig = Rig()
        rig.start()
        rig.arrive()                                                         # กำลังไป B
        rig.runner.pause()
        rig.client.listener.on_result('canceled')
        rig.localized = False
        with self.assertRaises(RunError) as cm:
            rig.runner.resume()
        self.assertEqual((cm.exception.code, rig.status()['state']), ('route.run.not_localized', 'paused'))
        rig.localized = True
        rig.runner.resume()
        self.assertEqual((rig.status()['state'], rig.status()['index']), ('running', 1))
        self.assertEqual(rig.sent(), [(1.0, 1.0), (2.0, 1.0), (2.0, 1.0)])   # ส่งจุด B ซ้ำ ไม่ใช่ไปจุดถัดไป
        self.assertTrue(rig.arrive())                                        # ถึง B → รอ → ... ใช้งานต่อได้ปกติ
        self.assertEqual(rig.status()['phase'], 'waiting')

    def test_pause_requires_running_and_resume_requires_paused(self):
        rig = Rig()
        for fn in (rig.runner.pause, rig.runner.resume):
            with self.assertRaises(RunError):
                fn()
        rig.start()
        with self.assertRaises(RunError) as cm:
            rig.runner.resume()
        self.assertEqual(cm.exception.code, 'route.run.not_paused')
        rig.runner.pause()
        with self.assertRaises(RunError) as cm:
            rig.runner.pause()
        self.assertEqual(cm.exception.code, 'route.run.not_running')

    def test_pause_while_waiting_freezes_the_countdown(self):
        rig = Rig()
        rig.start()
        rig.arrive()
        rig.arrive()                                                         # รอที่ B 5 วินาที
        rig.clock.now += 2.0
        rig.runner.pause()
        s = rig.status()
        self.assertEqual((s['state'], s['wait_left'], rig.client.cancels), ('paused', 3.0, 0))   # ไม่มี goal ให้ยกเลิก
        rig.clock.now += 500
        self.assertTrue(rig.runner.tick())
        self.assertEqual(rig.status()['wait_left'], 3.0)
        rig.runner.resume()
        self.assertEqual((rig.status()['state'], rig.status()['wait_left']), ('running', 3.0))
        rig.clock.now += 2.9
        self.assertTrue(rig.runner.tick())
        self.assertEqual(len(rig.sent()), 2)
        rig.clock.now += 0.2
        self.assertTrue(rig.runner.tick())
        self.assertEqual(rig.sent()[-1], (3.0, 1.0))

    def test_paused_time_does_not_count_toward_the_run_limit(self):
        rig = Rig(max_run=100.0)
        rig.start(route={'loop': 0, 'points': [P('A', 1.0, 1.0)]}, forever_confirmed=True)
        rig.clock.now += 60
        rig.runner.pause()
        rig.client.listener.on_result('canceled')
        rig.clock.now += 5000
        rig.runner.resume()
        rig.clock.now += 39
        self.assertTrue(rig.runner.tick())                                   # ทำงานจริงรวม 99 วินาที < 100
        rig.clock.now += 2
        self.assertFalse(rig.runner.tick())
        self.assertEqual(rig.status()['reason']['code'], 'route.run.max_time')

    def test_stop_while_paused(self):
        rig = Rig()
        rig.start()
        rig.runner.pause()
        self.assertTrue(rig.runner.stop())
        self.assertEqual(rig.status()['state'], 'canceled')
        self.assertFalse(rig.runner.tick())


class LocatingTest(unittest.TestCase):
    """ตำแหน่งไม่น่าเชื่อถือ (σ ของ AMCL เกินเกณฑ์) : ไม่ส่ง goal · สั่ง AMCL อัปเดตเอง · รอไม่เกิน loc_grace"""

    def rig(self, **kw):
        return Rig(loc_grace=15.0, **kw)

    def test_start_while_uncertain_sends_nothing_refreshes_and_waits(self):
        rig = self.rig()
        rig.localized = False
        rig.start()
        s = rig.status()
        self.assertEqual((s['state'], s['phase'], s['loc_left'], rig.refreshes), ('running', 'locating', 15.0, 1))
        self.assertEqual(rig.client.sent, [], 'ตำแหน่งยังไม่น่าเชื่อถือ = ห้ามส่ง goal')
        rig.clock.now += 1.0
        self.assertTrue(rig.runner.tick())
        self.assertEqual((rig.client.sent, rig.status()['loc_left']), ([], 14.0))
        rig.localized = True                                                 # AMCL ฟื้นแล้ว
        self.assertTrue(rig.runner.tick())
        self.assertEqual((rig.status()['phase'], rig.sent()), ('going', [(1.0, 1.0)]))

    def test_start_gives_up_after_the_grace_period_without_ever_sending(self):
        rig = self.rig()
        rig.localized = False
        rig.start()
        for _ in range(4):
            rig.clock.now += 3.0
            self.assertTrue(rig.runner.tick())
        rig.clock.now += 3.1                                                 # รวม 15.1 วินาที
        self.assertFalse(rig.runner.tick())
        s = rig.status()
        self.assertEqual((s['state'], s['reason']['code']), ('failed', 'route.run.not_localized'))
        self.assertEqual(rig.client.sent, [])
        self.assertGreaterEqual(rig.refreshes, 5, 'ระหว่างรอต้องสั่ง AMCL อัปเดตซ้ำเป็นระยะ ไม่ใช่ครั้งเดียว')
        self.assertLessEqual(rig.refreshes, 7)

    def test_arriving_while_uncertain_waits_then_continues_to_the_same_next_point(self):
        rig = self.rig()
        rig.start()
        rig.localized = False
        self.assertTrue(rig.arrive())                                        # ถึง A แต่ตำแหน่งไม่น่าเชื่อถือ
        s = rig.status()
        self.assertEqual((s['state'], s['phase'], s['index'], rig.refreshes), ('running', 'locating', 1, 1))
        self.assertEqual(len(rig.client.sent), 1, 'ไม่ส่งจุดถัดไปจนกว่าจะฟื้น')
        rig.clock.now += 4.0
        self.assertTrue(rig.runner.tick())
        self.assertEqual(len(rig.client.sent), 1)
        rig.localized = True
        self.assertTrue(rig.runner.tick())
        self.assertEqual((rig.sent()[-1], rig.status()['phase'], rig.status()['index']), ((2.0, 1.0), 'going', 1))

    def test_arriving_while_uncertain_that_never_recovers_fails_without_sending(self):
        rig = self.rig()
        rig.start()
        rig.localized = False
        rig.arrive()
        rig.clock.now += 16.0
        self.assertFalse(rig.runner.tick())
        s = rig.status()
        self.assertEqual((s['state'], s['reason']['code']), ('failed', 'route.run.not_localized'))
        self.assertEqual(len(rig.client.sent), 1)
        self.assertEqual(rig.rounds, 0)

    def test_round_is_counted_once_even_when_the_wait_for_localization_happens_after_it(self):
        rig = self.rig()
        rig.start(route={'loop': 2, 'points': [P('A', 1.0, 1.0), P('B', 2.0, 2.0)]})
        rig.arrive()
        rig.localized = False
        rig.arrive()                                                         # ถึง B = ครบรอบที่ 1 แต่ไม่ localize
        self.assertEqual((rig.rounds, rig.status()['round'], rig.status()['index']), (1, 2, 0))
        rig.clock.now += 5
        rig.runner.tick()
        self.assertEqual(rig.rounds, 1)
        rig.localized = True
        rig.runner.tick()
        self.assertEqual((rig.sent()[-1], rig.rounds), ((1.0, 1.0), 1))      # รอบที่ 2 เริ่มที่จุดแรก

    def test_refresh_errors_do_not_break_the_wait(self):
        rig = self.rig()
        rig.refresh_error = RuntimeError('service not ready')
        rig.localized = False
        rig.start()
        rig.clock.now += 4.0
        self.assertTrue(rig.runner.tick())
        rig.localized = True
        self.assertTrue(rig.runner.tick())
        self.assertEqual(len(rig.client.sent), 1)

    def test_pause_freezes_the_countdown_and_resume_does_not_demand_localization(self):
        rig = self.rig()
        rig.localized = False
        rig.start()
        rig.clock.now += 5.0
        rig.runner.pause()
        s = rig.status()
        self.assertEqual((s['state'], s['loc_left'], rig.client.cancels), ('paused', 10.0, 0))
        rig.clock.now += 500
        self.assertTrue(rig.runner.tick())
        rig.runner.resume()                                                  # ยังไม่ localize ก็ต่อได้ (กลับไปรอ)
        self.assertEqual((rig.status()['state'], rig.status()['loc_left']), ('running', 10.0))
        rig.clock.now += 10.1
        self.assertFalse(rig.runner.tick())
        self.assertEqual(rig.status()['reason']['code'], 'route.run.not_localized')

    def test_stop_while_locating(self):
        rig = self.rig()
        rig.localized = False
        rig.start()
        self.assertTrue(rig.runner.stop())
        self.assertEqual((rig.status()['state'], rig.client.sent), ('canceled', []))
        rig.localized = True
        self.assertFalse(rig.runner.tick())
        self.assertEqual(rig.client.sent, [], 'หยุดแล้วต่อให้ฟื้นก็ไม่ส่ง goal')

    def test_navigation_lost_while_locating_fails_with_nav_lost(self):
        rig = self.rig()
        rig.localized = False
        rig.start()
        rig.nav = False
        self.assertFalse(rig.runner.tick())
        self.assertEqual(rig.status()['reason']['code'], 'route.run.nav_lost')

    def test_zero_grace_keeps_the_strict_behaviour(self):
        rig = Rig(loc_grace=0.0)
        rig.localized = False
        with self.assertRaises(RunError) as cm:
            rig.start()
        self.assertEqual(cm.exception.code, 'route.run.not_localized')
        rig.localized = True
        rig.start()
        rig.localized = False
        self.assertFalse(rig.arrive())
        self.assertEqual(rig.status()['reason']['code'], 'route.run.not_localized')
        self.assertEqual(rig.refreshes, 0)

    def test_the_overall_run_limit_still_applies_while_locating(self):
        rig = Rig(loc_grace=1000.0, max_run=50.0)
        rig.localized = False
        rig.start()
        rig.clock.now += 51.0
        self.assertFalse(rig.runner.tick())
        self.assertEqual(rig.status()['reason']['code'], 'route.run.max_time')


class ThreadTest(unittest.TestCase):

    def test_background_thread_drives_the_route_to_the_end(self):
        rig = Rig(threaded=True, sleep=lambda s: time.sleep(0.005), poll=0.005)
        rig.start(route={'loop': 2, 'points': [P('A', 1.0, 1.0), P('B', 2.0, 2.0, wait=0.05)]})
        end = time.monotonic() + 5
        while rig.status()['state'] == 'running' and time.monotonic() < end:
            listener = rig.client.listener
            if listener is not None and rig.goal.status()['state'] == 'active':
                listener.on_result('succeeded')
            time.sleep(0.002)
            rig.clock.now += 0.05                                            # นาฬิกาปลอมเดินตามเวลาจริงคร่าวๆ
        self.assertEqual((rig.status()['state'], rig.rounds), ('succeeded', 2))
        rig.runner.thread.join(2)
        self.assertFalse(rig.runner.thread.is_alive())

    def test_stop_ends_the_thread_quickly(self):
        rig = Rig(threaded=True, sleep=lambda s: time.sleep(0.005), poll=0.005)
        rig.start()
        thread = rig.runner.thread
        rig.runner.stop()
        thread.join(2)
        self.assertFalse(thread.is_alive())

    def test_concurrent_starts_only_one_wins(self):
        rig = Rig()
        results = []

        def go():
            try:
                rig.start()
                results.append('ok')
            except RunError as e:
                results.append(e.code)
            except GoalError as e:
                results.append(e.code)
        threads = [threading.Thread(target=go) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        self.assertEqual(results.count('ok'), 1, results)
        self.assertEqual(len(rig.client.sent), 1)


if __name__ == '__main__':
    unittest.main()
