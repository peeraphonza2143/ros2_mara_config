"""
ทดสอบ NavGoal (ส่งหุ่นไปจุดหมาย) ด้วย client ปลอม — ไม่ต้องมี ROS
เน้น: ผิดกติกาข้อไหน "ไม่ส่งอะไรให้ Nav2 เลย" · ทีละ goal · สถานะมาจาก feedback/result จริง · waiting เป็นข้อสันนิษฐาน

รัน:  cd ~/ros2_ws/src/agv_web_app && python3 -m unittest discover -s test -v
"""
import threading
import unittest

from agv_web_app.live_map import LiveMap
import math

from agv_web_app.nav_goal import (ACCEPT_TIMEOUT_S, CLEARANCE_M, SOFT_CLEARANCE_M, STALL_PROGRESS_M, STALL_S,
                                  STRAIGHT_MIN_M, GoalError, NavGoal)


class FakeClient:
    """action server ปลอม: ตอบรับ/ปฏิเสธ/เงียบ ตามที่ตั้ง ; ทดสอบจำลอง callback เองได้"""

    def __init__(self, ready=True, accept='yes'):
        self.is_ready, self.accept = ready, accept
        self.sent, self.cancels, self.listener = [], 0, None
        self.lines, self.line_is_ready = [], True
        self.fail_send = False

    def ready(self):
        return self.is_ready

    def send(self, x, y, yaw, listener):
        if self.fail_send:
            raise RuntimeError('boom')
        self.sent.append((x, y, yaw))
        self.listener = listener
        if self.accept == 'yes':
            listener.on_accepted(True)
        elif self.accept == 'no':
            listener.on_accepted(False)

    def line_ready(self):
        return self.line_is_ready

    def send_line(self, points, listener):
        if self.fail_send:
            raise RuntimeError('boom')
        self.lines.append(points)
        self.listener = listener
        if self.accept == 'yes':
            listener.on_accepted(True)
        elif self.accept == 'no':
            listener.on_accepted(False)

    def cancel(self):
        self.cancels += 1


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def ok_probe(x, y, radius):
    return {'status': 'ok', 'clearance': None}


def make(**kw):
    client = kw.pop('client', None) or FakeClient()
    clock = kw.pop('clock', None) or Clock()
    events = []
    goal = NavGoal(client, on_event=events.append, clock=clock, accept_timeout=kw.pop('accept_timeout', 0.2), **kw)
    return goal, client, clock, events


def send(goal, x=1.0, y=2.0, yaw=0.5, probe=ok_probe, localized=True, confirmed=True):
    return goal.send(x, y, yaw, probe, localized, confirmed)


class RulesTest(unittest.TestCase):

    def refused(self, code, **kw):
        goal, client, _, _ = make(client=kw.pop('client', None))
        with self.assertRaises(GoalError) as cm:
            send(goal, **kw)
        self.assertEqual(cm.exception.code, code)
        self.assertEqual(client.sent, [], 'ผิดกติกาแล้วต้องไม่ส่งอะไรให้ Nav2')
        self.assertEqual(goal.status()['state'], 'idle')

    def test_needs_confirmed_true_exactly(self):
        for bad in (False, None, 1, 'true', 'yes', [True]):
            self.refused('nav.err.goal_confirm_required', confirmed=bad)

    def test_invalid_numbers(self):
        for bad in ('a', None, True, [1], float('nan'), float('inf'), 1e9):
            self.refused('nav.err.goal_invalid', x=bad)
            self.refused('nav.err.goal_invalid', yaw=bad)

    def test_not_localized(self):
        self.refused('nav.err.goal_not_localized', localized=False)

    def test_unsafe_goal_statuses(self):
        for status, code in (('outside', 'nav.err.goal_outside'), ('unknown', 'nav.err.goal_unknown'),
                             ('occupied', 'nav.err.goal_unknown'), ('tight', 'nav.err.goal_tight'),
                             ('no_map', 'nav.err.no_map')):
            self.refused(code, probe=lambda x, y, r, s=status: {'status': s, 'clearance': 0.1})

    def test_probe_gets_the_configured_clearance(self):
        seen = []
        goal, _, _, _ = make(clearance=0.55)
        send(goal, probe=lambda x, y, r: seen.append(r) or {'status': 'ok', 'clearance': None})
        self.assertEqual(seen, [0.55])
        self.assertEqual(CLEARANCE_M, 0.40)

    def test_no_action_server(self):
        self.refused('nav.err.goal_no_server', client=FakeClient(ready=False))

    def test_send_exception_leaves_no_goal(self):
        client = FakeClient()
        client.fail_send = True
        goal, _, _, _ = make(client=client)
        with self.assertRaises(GoalError) as cm:
            send(goal)
        self.assertEqual(cm.exception.code, 'nav.err.goal_no_server')
        self.assertEqual(goal.status()['state'], 'failed')
        client.fail_send = False
        self.assertEqual(send(goal)['state'], 'active')          # ไม่ค้าง: ส่งใหม่ได้

    def test_one_goal_at_a_time(self):
        goal, client, _, _ = make()
        send(goal)
        with self.assertRaises(GoalError) as cm:
            send(goal, x=3.0)
        self.assertEqual(cm.exception.code, 'nav.err.goal_busy')
        self.assertEqual(len(client.sent), 1)

    def test_yaw_is_normalized(self):
        goal, client, _, _ = make()
        send(goal, yaw=7.0)
        self.assertAlmostEqual(client.sent[0][2], 7.0 - 2 * 3.141592653589793)


class LifecycleTest(unittest.TestCase):

    def test_success_flow_with_feedback(self):
        goal, client, clock, events = make()
        st = send(goal)
        self.assertEqual((st['state'], st['goal']), ('active', {'x': 1.0, 'y': 2.0, 'yaw': 0.5}))
        client.listener.on_feedback({'distance_remaining': 3.0, 'eta': 12.0, 'recoveries': 0, 'elapsed': 1.0})
        self.assertEqual(goal.status()['feedback']['distance_remaining'], 3.0)
        client.listener.on_result('succeeded')
        st = goal.status()
        self.assertEqual((st['state'], st['elapsed']), ('succeeded', None))
        self.assertTrue(any('succeeded' in e for e in events))
        self.assertEqual(send(goal, x=2.0)['state'], 'active')    # จบแล้วส่งใหม่ได้

    def test_rejected_by_nav2(self):
        goal, client, _, _ = make(client=FakeClient(accept='no'))
        with self.assertRaises(GoalError) as cm:
            send(goal)
        self.assertEqual(cm.exception.code, 'nav.err.goal_rejected')
        self.assertEqual(goal.status()['state'], 'failed')
        self.assertEqual(goal.status()['reason'], 'nav.err.goal_rejected')

    def test_accept_timeout_cancels_and_fails(self):
        goal, client, _, _ = make(client=FakeClient(accept='silent'), accept_timeout=0.05)
        with self.assertRaises(GoalError) as cm:
            send(goal)
        self.assertEqual(cm.exception.code, 'nav.err.goal_timeout')
        self.assertEqual(client.cancels, 1)
        self.assertEqual(goal.status()['state'], 'failed')
        self.assertGreater(ACCEPT_TIMEOUT_S, 0)

    def test_aborted_is_failed(self):
        goal, client, _, _ = make()
        send(goal)
        client.listener.on_result('failed')
        st = goal.status()
        self.assertEqual((st['state'], st['reason']), ('failed', 'nav.goal.aborted'))

    def test_cancel(self):
        goal, client, _, _ = make()
        self.assertFalse(goal.cancel())                            # ไม่มี goal = ไม่มีอะไรให้ยกเลิก
        self.assertEqual(client.cancels, 0)
        send(goal)
        self.assertTrue(goal.cancel())
        self.assertEqual((goal.status()['state'], client.cancels), ('canceling', 1))
        with self.assertRaises(GoalError):                         # ระหว่างยกเลิก ยังถือว่า "มี goal" ห้ามส่งซ้อน
            send(goal)
        client.listener.on_result('canceled')
        self.assertEqual(goal.status()['state'], 'canceled')
        self.assertFalse(goal.cancel())

    def test_cancel_then_success_is_reported_honestly(self):
        goal, client, _, _ = make()
        send(goal)
        goal.cancel()
        client.listener.on_result('succeeded')                     # ถึงเป้าก่อนยกเลิกทัน
        self.assertEqual(goal.status()['state'], 'succeeded')

    def test_late_messages_of_an_old_goal_are_ignored(self):
        goal, client, _, _ = make()
        send(goal)
        old = client.listener
        old.on_result('succeeded')
        send(goal, x=2.0)
        old.on_result('failed')
        old.on_feedback({'distance_remaining': 99.0})
        st = goal.status()
        self.assertEqual((st['state'], st['feedback']), ('active', None))

    def test_cancel_while_sending_is_honoured(self):
        client = FakeClient(accept='silent')
        goal, _, _, _ = make(client=client, accept_timeout=2.0)
        errors = []

        def run():
            try:
                send(goal)
            except GoalError as e:
                errors.append(e.code)
        t = threading.Thread(target=run)
        t.start()
        for _ in range(200):
            if client.listener:
                break
            threading.Event().wait(0.01)
        self.assertEqual(goal.status()['state'], 'sending')
        self.assertTrue(goal.cancel())
        client.listener.on_accepted(True)                          # Nav2 รับหลังสั่งยกเลิก
        t.join(3)
        self.assertEqual(goal.status()['state'], 'canceling')      # ห้ามกลับเป็น active
        client.listener.on_result('canceled')
        self.assertEqual((goal.status()['state'], errors), ('canceled', []))

    def test_nav_going_down_marks_goal_lost(self):
        goal, client, _, _ = make()
        send(goal)
        st = goal.status(nav_up=False)
        self.assertEqual((st['state'], st['reason']), ('failed', 'nav.goal.lost'))
        send(goal)                                                 # ไม่ค้าง
        goal2, _, _, _ = make()
        send(goal2)
        goal2.cancel()
        self.assertEqual(goal2.status(nav_up=False)['state'], 'failed')

    def test_finished_goal_stays_finished_when_nav_down(self):
        goal, client, _, _ = make()
        send(goal)
        client.listener.on_result('succeeded')
        self.assertEqual(goal.status(nav_up=False)['state'], 'succeeded')

    def test_concurrent_sends_only_one_wins(self):
        client = FakeClient()
        goal, _, _, _ = make(client=client)
        codes, barrier = [], threading.Barrier(6)

        def run():
            barrier.wait()
            try:
                send(goal)
                codes.append('ok')
            except GoalError as e:
                codes.append(e.code)
        threads = [threading.Thread(target=run) for _ in range(6)]
        [t.start() for t in threads]
        [t.join(5) for t in threads]
        self.assertEqual(codes.count('ok'), 1, codes)
        self.assertEqual(len(client.sent), 1)


class WaitingAssumptionTest(unittest.TestCase):

    def setUp(self):
        self.goal, self.client, self.clock, _ = make()
        send(self.goal)

    def fb(self, d):
        self.client.listener.on_feedback({'distance_remaining': d})

    def test_progress_keeps_it_not_waiting(self):
        for i in range(10):
            self.clock.now += STALL_S - 1
            self.fb(5.0 - i * 0.5)
            self.assertFalse(self.goal.status()['waiting_assumed'])

    def test_no_progress_is_assumed_waiting_then_recovers(self):
        self.fb(5.0)
        self.clock.now += STALL_S + 1
        self.assertTrue(self.goal.status()['waiting_assumed'])
        self.fb(4.0)
        self.assertFalse(self.goal.status()['waiting_assumed'])

    def test_tiny_changes_are_not_progress(self):
        self.fb(5.0)
        self.clock.now += STALL_S - 1
        self.fb(5.0 - STALL_PROGRESS_M / 2)
        self.clock.now += 2
        self.assertTrue(self.goal.status()['waiting_assumed'])

    def test_never_waiting_when_not_active(self):
        self.client.listener.on_result('succeeded')
        self.clock.now += 100
        self.assertFalse(self.goal.status()['waiting_assumed'])


class ProbeTest(unittest.TestCase):
    """LiveMap.probe: จุดหมายต้อง "รู้ว่าว่าง" และห่างผนัง"""

    def setUp(self):
        self.map = LiveMap()
        grid = [0] * (60 * 40)                                    # 3.0 × 2.0 m ที่ 0.05
        for row in range(40):
            grid[row * 60 + 40] = 100                             # ผนังที่ x = 2.0..2.05
        grid[10 * 60 + 5] = -1                                    # ช่องไม่รู้ที่ (0.25..0.30, 0.5..0.55)
        self.map.update(60, 40, 0.05, 0.0, 0.0, 0.0, grid)

    def test_statuses(self):
        r = 0.3
        self.assertEqual(self.map.probe(1.0, 1.0, r)['status'], 'ok')
        self.assertEqual(self.map.probe(2.02, 1.0, r)['status'], 'occupied')
        self.assertEqual(self.map.probe(0.27, 0.52, r)['status'], 'unknown')
        self.assertEqual(self.map.probe(9.0, 1.0, r)['status'], 'outside')
        self.assertEqual(self.map.probe(-0.01, 1.0, r)['status'], 'outside')
        self.assertEqual(self.map.probe(1.8, 1.0, r)['status'], 'tight')                      # ผนังห่าง 0.2
        self.assertEqual(self.map.probe(0.1, 1.5, r)['status'], 'tight')                      # ชิดขอบแผนที่
        self.assertEqual(self.map.probe(1.6, 1.0, r)['status'], 'ok')                         # ผนังห่าง 0.4

    def test_clearance_boundary(self):
        self.assertEqual(self.map.probe(1.7, 1.0, 0.25)['status'], 'ok')                      # ห่าง 0.3 ≥ 0.25
        self.assertEqual(self.map.probe(1.7, 1.0, 0.45)['status'], 'tight')

    def test_no_map_and_cleared(self):
        empty = LiveMap()
        self.assertEqual(empty.probe(0, 0, 0.3)['status'], 'no_map')
        self.map.clear()
        self.assertEqual(self.map.probe(1.0, 1.0, 0.3)['status'], 'no_map')

    def test_intermediate_cost_cells_are_not_free(self):
        m = LiveMap()
        grid = [0] * 400
        grid[10 * 20 + 10] = 50                                   # ค่ากลาง (ไม่ใช่ว่างแน่ชัด)
        m.update(20, 20, 0.05, 0.0, 0.0, 0.0, grid)
        self.assertEqual(m.probe(0.525, 0.525, 0.1)['status'], 'occupied')

    def test_rotated_origin(self):
        m = LiveMap()
        grid = [0] * 400
        m.update(20, 20, 0.05, 1.0, 1.0, 1.5707963267948966, grid)       # origin หมุน 90°
        self.assertEqual(m.probe(0.5, 1.5, 0.05)['status'], 'ok')         # u = 0.5, v = 0.5 ในกรอบแผนที่ที่หมุน
        self.assertEqual(m.probe(1.5, 1.5, 0.05)['status'], 'outside')


def free_line(x0, y0, x1, y1, radius, soft):
    return {'ok': True, 'at': None, 'reason': None, 'min_clearance': None, 'soft_risk': False}


class StraightTest(unittest.TestCase):
    """Drive straight: จุดหมาย = ตำแหน่ง+ทิศหุ่น+ระยะ ; ตรวจทั้งแนว ; ผ่านกติกา send() ทุกข้อ"""

    def make(self, **kw):
        return make(max_straight=kw.pop('max_straight', 10.0), **kw)

    def test_goal_is_ahead_along_the_heading(self):
        goal, client, _, _ = self.make()
        seen = []
        status, check = goal.send_straight(4.0, (1.0, 2.0, math.pi / 2), lambda *a: seen.append(a) or free_line(*a),
                                           ok_probe, True, True)
        x, y, yaw = client.sent[0]
        self.assertAlmostEqual(x, 1.0)
        self.assertAlmostEqual(y, 6.0)
        self.assertAlmostEqual(yaw, math.pi / 2)
        self.assertEqual((status['state'], check['distance']), ('active', 4.0))
        self.assertEqual(seen[0][4:], (CLEARANCE_M, SOFT_CLEARANCE_M))      # ตรวจด้วย clearance และ soft ที่กำหนด
        self.assertAlmostEqual(seen[0][2], 1.0)
        self.assertAlmostEqual(seen[0][3], 6.0)

    def test_distance_limits(self):
        goal, client, _, _ = self.make(max_straight=10.0)
        for bad in (0.0, STRAIGHT_MIN_M - 0.01, 10.01, -3.0, 25.0):
            with self.assertRaises(GoalError) as cm:
                goal.check_straight(bad, (0, 0, 0), free_line)
            self.assertEqual(cm.exception.code, 'nav.err.straight_distance', bad)
        for bad in ('a', None, True, float('nan'), float('inf')):
            with self.assertRaises(GoalError) as cm:
                goal.check_straight(bad, (0, 0, 0), free_line)
            self.assertEqual(cm.exception.code, 'nav.err.goal_invalid')
        goal.check_straight(STRAIGHT_MIN_M, (0, 0, 0), free_line)
        goal.check_straight(10.0, (0, 0, 0), free_line)
        goal25, _, _, _ = self.make(max_straight=25.0)
        goal25.check_straight(20.0, (0, 0, 0), free_line)                          # เพดานตั้งค่าได้
        self.assertEqual(goal25.limits()['straight_max'], 25.0)
        self.assertEqual(client.sent, [])

    def test_no_pose(self):
        goal, client, _, _ = self.make()
        with self.assertRaises(GoalError) as cm:
            goal.send_straight(3.0, None, free_line, ok_probe, True, True)
        self.assertEqual(cm.exception.code, 'nav.err.straight_no_pose')
        self.assertEqual(client.sent, [])

    def test_blocked_line_is_refused_with_distance_and_reason(self):
        for reason, code in (('occupied', 'nav.err.straight_unknown'), ('unknown', 'nav.err.straight_unknown'),
                             ('tight', 'nav.err.straight_tight'), ('outside', 'nav.err.straight_outside'),
                             ('no_map', 'nav.err.no_map')):
            goal, client, _, _ = self.make()
            with self.assertRaises(GoalError) as cm:
                goal.send_straight(8.0, (0, 0, 0), lambda *a, r=reason: {'ok': False, 'at': 5.5, 'reason': r,
                                                                         'min_clearance': 0.1, 'soft_risk': True},
                                   ok_probe, True, True)
            self.assertEqual((cm.exception.code, cm.exception.params['at']), (code, 5.5))
            self.assertEqual(client.sent, [], reason)

    def test_same_rules_as_a_normal_goal(self):
        goal, client, _, _ = self.make()
        calls = []
        for localized, confirmed, code in ((True, None, 'nav.err.goal_confirm_required'),
                                           (True, 'true', 'nav.err.goal_confirm_required'),
                                           (False, True, 'nav.err.goal_not_localized')):
            with self.assertRaises(GoalError) as cm:
                goal.send_straight(3.0, (0, 0, 0), lambda *a: calls.append(a) or free_line(*a), ok_probe, localized,
                                   confirmed)
            self.assertEqual(cm.exception.code, code)
        self.assertEqual(calls, [])                                # ปฏิเสธก่อนตรวจแนว ไม่เสียงานเปล่า
        self.assertEqual(client.sent, [])
        goal.send_straight(3.0, (0, 0, 0), free_line, ok_probe, True, True)
        with self.assertRaises(GoalError) as cm:                                   # ทีละ goal
            goal.send_straight(3.0, (0, 0, 0), free_line, ok_probe, True, True)
        self.assertEqual(cm.exception.code, 'nav.err.goal_busy')
        self.assertEqual(len(client.sent), 1)

    def test_endpoint_must_also_pass_the_point_check(self):
        goal, client, _, _ = self.make()
        with self.assertRaises(GoalError) as cm:
            goal.send_straight(3.0, (0, 0, 0), free_line, lambda x, y, r: {'status': 'unknown', 'clearance': 0},
                               True, True)
        self.assertEqual(cm.exception.code, 'nav.err.goal_unknown')
        self.assertEqual(client.sent, [])

    def test_check_never_sends(self):
        goal, client, _, _ = self.make()
        result = goal.check_straight(5.0, (0, 0, 0), free_line)
        self.assertEqual((result['ok'], result['goal']['x']), (True, 5.0))
        self.assertEqual((client.sent, goal.status()['state']), ([], 'idle'))


class ProbeLineTest(unittest.TestCase):

    def setUp(self):
        self.map = LiveMap()
        grid = [0] * (400 * 100)                                  # 20 × 5 m ที่ 0.05
        for row in range(100):
            grid[row * 400 + 300] = 100                           # ผนังที่ x = 15.0 ขวางทั้งแถว
        for col in range(400):
            grid[0 * 400 + col] = 100                             # ผนังล่าง y = 0.0..0.05
            grid[99 * 400 + col] = 100                            # ผนังบน y = 4.95..5.0
        self.map.update(400, 100, 0.05, 0.0, 0.0, 0.0, grid)

    def test_clear_line_reports_min_clearance_and_soft_risk(self):
        r = self.map.probe_line(1.0, 2.5, 10.0, 2.5, 0.4, 0.55)
        self.assertTrue(r['ok'])
        self.assertIsNone(r['at'])
        self.assertFalse(r['soft_risk'])                          # ห่างผนัง ~2.45 m ทั้งสองด้าน
        near = self.map.probe_line(1.0, 0.8, 10.0, 0.8, 0.4, 0.55)   # ห่างผนังล่าง ~0.75 → ไม่เสี่ยง
        self.assertFalse(near['soft_risk'])
        close = self.map.probe_line(1.0, 0.5, 10.0, 0.5, 0.4, 0.55)  # ห่างผนังล่าง ~0.45 : ผ่านแต่เสี่ยงโค้ง
        self.assertTrue(close['ok'])
        self.assertTrue(close['soft_risk'])
        self.assertLess(close['min_clearance'], 0.55)
        self.assertGreaterEqual(close['min_clearance'], 0.4)

    def test_wall_across_the_line_blocks_at_the_right_distance(self):
        r = self.map.probe_line(10.0, 2.5, 18.0, 2.5, 0.4, 0.55)
        self.assertFalse(r['ok'])
        self.assertEqual(r['reason'], 'tight')                    # ติดที่ 0.4 ก่อนถึงผนัง ไม่ใช่ตอนชน
        self.assertAlmostEqual(r['at'], 15.0 - 0.4 - 10.0, delta=0.2)

    def test_too_close_to_side_wall_is_blocked_at_the_start(self):
        r = self.map.probe_line(1.0, 0.3, 5.0, 0.3, 0.4, 0.55)
        self.assertEqual((r['ok'], r['reason'], r['at']), (False, 'tight', 0.0))

    def test_line_leaving_the_map_and_no_map(self):
        r = self.map.probe_line(15.5, 2.5, 25.0, 2.5, 0.4, 0.55)
        self.assertFalse(r['ok'])
        through = self.map.probe_line(1.0, 2.5, 25.0, 2.5, 0.4, 0.55)             # ผนังที่ x=15 มาก่อนขอบแผนที่
        self.assertEqual(through['reason'], 'tight')
        self.assertAlmostEqual(through['at'], 15.0 - 0.4 - 1.0, delta=0.2)
        off = self.map.probe_line(19.0, 2.5, 25.0, 2.5, 0.2, 0.3)                  # ออกนอกขอบแผนที่ที่ x=20
        self.assertEqual((off['ok'], off['reason']), (False, 'tight'))      # ขอบแผนที่นับเป็น "ไม่รู้"
        empty = LiveMap()
        self.assertEqual(empty.probe_line(0, 0, 1, 0, 0.4)['reason'], 'no_map')

    def test_unknown_cells_on_the_line_block(self):
        grid = [0] * (200 * 60)
        for row in range(60):
            for col in range(80, 90):
                grid[row * 200 + col] = -1                        # แถบที่ยังไม่รู้ x = 4..4.5
        m = LiveMap()
        m.update(200, 60, 0.05, 0.0, 0.0, 0.0, grid)
        r = m.probe_line(1.0, 1.5, 9.0, 1.5, 0.2, 0.3)
        self.assertEqual(r['ok'], False)
        self.assertAlmostEqual(r['at'], 4.0 - 0.2 - 1.0, delta=0.15)

    def test_starting_inside_an_unknown_area_reports_unknown(self):
        grid = [0] * (200 * 60)
        for row in range(60):
            for col in range(80, 90):
                grid[row * 200 + col] = -1
        m = LiveMap()
        m.update(200, 60, 0.05, 0.0, 0.0, 0.0, grid)
        r = m.probe_line(4.2, 1.5, 8.0, 1.5, 0.2, 0.3)
        self.assertEqual((r['ok'], r['reason'], r['at']), (False, 'unknown', 0.0))
        wall = self.map.probe_line(15.02, 2.5, 18.0, 2.5, 0.4, 0.55)               # เริ่มบนผนังเอง
        self.assertEqual((wall['ok'], wall['reason']), (False, 'occupied'))

    def test_diagonal_and_rotated_map(self):
        m = LiveMap()
        m.update(100, 100, 0.05, 1.0, 1.0, math.pi / 2, [0] * 10000)
        self.assertEqual(m.probe_line(0.5, 1.5, 0.5, 4.0, 0.1, 0.2)['ok'], True)      # แนวตามแกน v ของแผนที่ที่หมุน
        self.assertEqual(m.probe_line(0.5, 1.5, 5.0, 1.5, 0.1, 0.2)['ok'], False)    # ออกนอกแผนที่

    def test_many_samples_on_a_big_map_are_fast(self):
        import time
        m = LiveMap()
        m.update(2000, 2000, 0.05, 0.0, 0.0, 0.0, [0] * (2000 * 2000))
        start = time.monotonic()
        self.assertTrue(m.probe_line(2.0, 50.0, 12.0, 50.0, 0.4, 0.55)['ok'])
        self.assertLess(time.monotonic() - start, 1.5)


if __name__ == '__main__':
    unittest.main()
