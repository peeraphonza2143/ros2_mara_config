"""
ทดสอบ "วิ่งตรงตามเส้น" (NavGoal.send_line / check_line / line_points) ด้วย client ปลอม — ไม่ต้องมี ROS
เน้น: เส้นเป็นเส้นตรงจริง · ผิดกติกาข้อไหน "ไม่ส่งอะไรให้ตัวควบคุมเลย" · หุ่นต้องอยู่ที่จุดเริ่มและหันตรงแนว ·
ใช้สถานะ/ยกเลิก/ทีละงานเดียวชุดเดียวกับ goal ปกติ

รัน:  cd ~/ros2_ws/src/agv_web_app && python3 -m unittest discover -s test -v
"""
import math
import unittest

from agv_web_app.nav_goal import (LINE_HEADING_TOL_RAD, LINE_MIN_M, LINE_START_TOL_M, LINE_STEP_M, GoalError,
                                  line_points)
from test_nav_goal import FakeClient, make, ok_probe


def free_line(*args):
    return {'ok': True, 'at': None, 'reason': None, 'min_clearance': None, 'soft_risk': False}


def blocked_line(reason='tight', at=1.5):
    return lambda *args: {'ok': False, 'at': at, 'reason': reason, 'min_clearance': 0.1, 'soft_risk': True}


def line(goal, start=(0.0, 0.0), end=(3.0, 0.0, 0.0), pose=(0.0, 0.0, 0.0), probe=free_line, localized=True,
         confirmed=True):
    return goal.send_line(start, end, pose, probe, localized, confirmed)


class LinePointsTest(unittest.TestCase):

    def test_points_lie_on_the_straight_line(self):
        pts = line_points(1.0, 2.0, 4.0, 6.0, 0.7)                     # ยาว 5 m ทิศ atan2(4, 3)
        d = math.atan2(4.0, 3.0)
        self.assertEqual((pts[0][0], pts[0][1]), (1.0, 2.0))
        self.assertEqual((pts[-1][0], pts[-1][1]), (4.0, 6.0))
        for x, y, _ in pts:                                             # ทุกจุดต้องอยู่บนเส้น (ระยะตั้งฉาก ≈ 0)
            cross = (x - 1.0) * math.sin(d) - (y - 2.0) * math.cos(d)
            self.assertAlmostEqual(cross, 0.0, places=9)

    def test_spacing_and_headings(self):
        pts = line_points(0.0, 0.0, 3.0, 0.0, 1.2)
        gaps = [math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(pts, pts[1:])]
        self.assertLessEqual(max(gaps), LINE_STEP_M + 1e-9)
        self.assertTrue(all(p[2] == 0.0 for p in pts[:-1]))            # ระหว่างทางหันตามเส้น
        self.assertEqual(pts[-1][2], 1.2)                              # จุดสุดท้ายหันตามทิศปลายทาง
        self.assertEqual(len(pts), 61)

    def test_short_line_has_both_ends(self):
        pts = line_points(0.0, 0.0, 0.01, 0.0, 0.0)
        self.assertEqual(len(pts), 2)


class CheckLineTest(unittest.TestCase):

    def check(self, **kw):
        goal, client, _, _ = make()
        args = dict(start=(0.0, 0.0), goal=(3.0, 0.0, 0.0), pose=(0.0, 0.0, 0.0), line_probe=free_line)
        args.update(kw)
        return goal.check_line(args['start'], args['goal'], args['pose'], args['line_probe']), client

    def test_ok_when_robot_is_at_the_start_facing_the_line(self):
        result, client = self.check()
        self.assertTrue(result['ok'] and result['start']['ok'])
        self.assertAlmostEqual(result['length'], 3.0)
        self.assertEqual((result['start']['max_distance'], result['start']['max_heading_deg']), (LINE_START_TOL_M, 20))
        self.assertEqual(client.lines, [])                             # ตรวจอย่างเดียว ไม่ส่งอะไร

    def test_start_far_and_heading_are_reported(self):
        far, _ = self.check(pose=(LINE_START_TOL_M + 0.1, 0.0, 0.0))
        self.assertEqual((far['ok'], far['start']['ok']), (False, False))
        self.assertAlmostEqual(far['start']['distance'], LINE_START_TOL_M + 0.1, places=2)
        turned, _ = self.check(pose=(0.0, 0.0, LINE_HEADING_TOL_RAD + 0.1))
        self.assertEqual((turned['ok'], turned['start']['ok']), (False, False))
        self.assertAlmostEqual(turned['start']['heading_deg'], math.degrees(LINE_HEADING_TOL_RAD + 0.1), places=1)

    def test_heading_wraps_around_pi(self):
        result, _ = self.check(start=(0.0, 0.0), goal=(-3.0, 0.0, math.pi), pose=(0.0, 0.0, -math.pi + 0.05))
        self.assertTrue(result['ok'])                                  # หัน ≈ 180° เท่ากับทิศเส้น (ข้ามรอยต่อ ±π)

    def test_blocked_line_makes_it_not_ok(self):
        result, _ = self.check(line_probe=blocked_line('unknown', 2.0))
        self.assertEqual((result['ok'], result['reason'], result['at']), (False, 'unknown', 2.0))

    def test_length_limits(self):
        goal, _, _, _ = make(max_straight=5.0)
        for end in ((LINE_MIN_M - 0.05, 0.0, 0.0), (5.5, 0.0, 0.0), (0.0, 0.0, 0.0)):
            with self.assertRaises(GoalError) as cm:
                goal.check_line((0.0, 0.0), end, (0, 0, 0), free_line)
            self.assertEqual(cm.exception.code, 'nav.err.line_length', end)
        goal.check_line((0.0, 0.0), (LINE_MIN_M, 0.0, 0.0), (0, 0, 0), free_line)
        goal.check_line((0.0, 0.0), (5.0, 0.0, 0.0), (0, 0, 0), free_line)

    def test_bad_numbers_and_missing_pose(self):
        goal, _, _, _ = make()
        for start, end in (((None, 0.0), (3.0, 0.0, 0.0)), ((0.0, 0.0), (3.0, 0.0, 'x')),
                           ((True, 0.0), (3.0, 0.0, 0.0)), ((0.0, 0.0), (float('nan'), 0.0, 0.0))):
            with self.assertRaises(GoalError) as cm:
                goal.check_line(start, end, (0, 0, 0), free_line)
            self.assertEqual(cm.exception.code, 'nav.err.goal_invalid', (start, end))
        with self.assertRaises(GoalError) as cm:
            goal.check_line((0.0, 0.0), (3.0, 0.0, 0.0), None, free_line)
        self.assertEqual(cm.exception.code, 'nav.err.straight_no_pose')


class SendLineTest(unittest.TestCase):

    def refused(self, code, goal=None, client=None, **kw):
        if goal is None:
            goal, client, _, _ = make()
        with self.assertRaises(GoalError) as cm:
            line(goal, **kw)
        self.assertEqual(cm.exception.code, code)
        self.assertEqual(client.lines, [], 'ผิดกติกาแล้วต้องไม่ส่งอะไรให้ตัวควบคุม')
        self.assertEqual(goal.status()['state'], 'idle')
        return cm.exception

    def test_sends_one_straight_path_and_reports_state(self):
        goal, client, _, events = make()
        status, check = line(goal, start=(0.0, 0.0), end=(3.0, 0.0, 1.0))
        self.assertEqual((status['state'], status['goal'], status['line']),
                         ('active', {'x': 3.0, 'y': 0.0, 'yaw': 1.0}, {'x0': 0.0, 'y0': 0.0}))
        self.assertTrue(check['ok'])
        self.assertEqual(len(client.lines), 1)
        pts = client.lines[0]
        self.assertEqual((pts[0][:2], pts[-1]), ((0.0, 0.0), (3.0, 0.0, 1.0)))
        self.assertTrue(all(p[1] == 0.0 for p in pts))                 # ตรงจริง ไม่มีส่ายข้าง
        gaps = [math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(pts, pts[1:])]
        self.assertLessEqual(max(gaps), LINE_STEP_M + 1e-9)             # เส้นถี่ (ไม่ใช่แค่ 2 จุดปลาย) ตัวควบคุมจึงเกาะเส้นได้
        self.assertGreater(len(pts), 50)
        self.assertEqual(client.sent, [], 'ต้องไม่ไปใช้ NavigateToPose (planner)')
        self.assertTrue(events and events[0].startswith('Nav line'))

    def test_confirmed_must_be_exactly_true(self):
        for bad in (None, False, 1, 'true', 'yes'):
            self.refused('nav.err.goal_confirm_required', confirmed=bad)

    def test_must_be_localized(self):
        self.refused('nav.err.goal_not_localized', localized=False)

    def test_blocked_line_reports_where(self):
        for reason, code in (('occupied', 'nav.err.straight_unknown'), ('unknown', 'nav.err.straight_unknown'),
                             ('tight', 'nav.err.straight_tight'), ('outside', 'nav.err.straight_outside'),
                             ('no_map', 'nav.err.no_map')):
            err = self.refused(code, probe=blocked_line(reason, 1.5))
            self.assertEqual(err.params['at'], 1.5, reason)

    def test_robot_must_be_at_the_start_and_face_the_line(self):
        err = self.refused('nav.err.line_start_far', pose=(1.0, 0.0, 0.0))
        self.assertAlmostEqual(err.params['found'], 1.0)
        self.assertEqual(err.params['max'], LINE_START_TOL_M)
        err = self.refused('nav.err.line_heading', pose=(0.0, 0.0, math.radians(60)))
        self.assertAlmostEqual(err.params['found'], 60.0)
        self.assertEqual(err.params['max'], 20)

    def test_blocked_line_is_reported_before_robot_position(self):
        self.refused('nav.err.straight_tight', pose=(2.0, 2.0, 2.0), probe=blocked_line())

    def test_one_job_at_a_time_across_goal_and_line(self):
        goal, client, _, _ = make()
        line(goal)
        with self.assertRaises(GoalError) as cm:                       # เส้นซ้อนเส้น
            line(goal)
        self.assertEqual(cm.exception.code, 'nav.err.goal_busy')
        with self.assertRaises(GoalError) as cm:                       # goal ปกติซ้อนเส้น
            goal.send(1.0, 2.0, 0.0, ok_probe, True, True)
        self.assertEqual(cm.exception.code, 'nav.err.goal_busy')
        self.assertEqual((len(client.lines), client.sent, goal.status()['state']), (1, [], 'active'))

    def test_server_not_ready_sends_nothing(self):
        client = FakeClient()
        client.line_is_ready = False
        goal, client, _, _ = make(client=client)
        self.refused('nav.err.goal_no_server', goal=goal, client=client)

    def test_rejected_and_unreachable_server(self):
        goal, client, _, _ = make(client=FakeClient(accept='no'))
        with self.assertRaises(GoalError) as cm:
            line(goal)
        self.assertEqual(cm.exception.code, 'nav.err.goal_rejected')
        self.assertEqual(goal.status()['state'], 'failed')
        failing = FakeClient()
        failing.fail_send = True
        goal, client, _, _ = make(client=failing)
        with self.assertRaises(GoalError) as cm:
            line(goal)
        self.assertEqual((cm.exception.code, goal.status()['state'], client.lines), ('nav.err.goal_no_server', 'failed', []))
        line_ok = FakeClient()                                           # ส่งไม่ได้แล้ว ต้องไม่มีงานค้างขวางงานถัดไป
        goal, _, _, _ = make(client=line_ok)
        line(goal)

    def test_cancel_uses_the_same_path_as_goals(self):
        goal, client, _, _ = make()
        line(goal)
        self.assertTrue(goal.cancel())
        self.assertEqual((client.cancels, goal.status()['state']), (1, 'canceling'))
        client.listener.on_result('canceled')
        self.assertEqual(goal.status()['state'], 'canceled')

    def test_feedback_and_result_follow_the_controller(self):
        goal, client, clock, _ = make()
        line(goal)
        client.listener.on_feedback({'distance_remaining': 2.0, 'eta': None, 'recoveries': 0, 'elapsed': None})
        self.assertEqual(goal.status()['feedback']['distance_remaining'], 2.0)
        client.listener.on_result('succeeded')
        status = goal.status()
        self.assertEqual((status['state'], status['line']), ('succeeded', {'x0': 0.0, 'y0': 0.0}))

    def test_next_normal_goal_clears_the_line_flag(self):
        goal, client, _, _ = make()
        line(goal)
        client.listener.on_result('succeeded')
        status = goal.send(1.0, 2.0, 0.0, ok_probe, True, True)
        self.assertIsNone(status['line'])
        self.assertEqual(len(client.sent), 1)

    def test_normal_goal_status_has_no_line(self):
        goal, _, _, _ = make()
        self.assertIsNone(goal.status()['line'])


if __name__ == '__main__':
    unittest.main()
