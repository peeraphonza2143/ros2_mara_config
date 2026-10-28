"""
ทดสอบ Route โหมด straight (ทุกช่วงเป็นเส้นตรงจริง ข้าม planner) ด้วย NavGoal จริง + ตัวควบคุมปลอม — ไม่ต้องมี ROS
เน้น: ตรวจ "ทุกช่วง" ก่อนปล่อยหุ่น · หุ่นต้องอยู่ที่จุดแรกและหันตามช่วงแรก · ทิศจุดกลางทางต้องตรงช่วงถัดไป ·
ช่วงปิดวงจรเมื่อหลายรอบ · พัก/ต่อ (ต่อจากตำแหน่งหุ่น) · ผิดกติกาแล้วไม่ส่งอะไรให้ตัวควบคุม

รัน:  cd ~/ros2_ws/src/agv_web_app && python3 -m unittest discover -s test -v
"""
import math
import unittest

from test_nav_goal import ok_probe
from test_route_run import Rig

from agv_web_app.nav_goal import GoalError
from agv_web_app.route_run import RunError
from agv_web_app.routes import RouteError, validate_route


def P(name, x, y, yaw=0.0, wait=0):
    return {'name': name, 'x': x, 'y': y, 'yaw': yaw, 'wait': wait}


def free_line(*args):
    return {'ok': True, 'at': None, 'reason': None, 'min_clearance': None, 'soft_risk': False}


# A(0,0) → B(2,0) → C(2,2) : B หันขึ้น (ทิศช่วง B→C) ; C หันตามใจ (ไม่มีช่วงถัดไปถ้ารอบเดียว)
L_ROUTE = {'loop': 1, 'mode': 'straight', 'points': [P('A', 0.0, 0.0, 0.0), P('B', 2.0, 0.0, math.pi / 2),
                                                     P('C', 2.0, 2.0, 1.0)]}


class StraightRig(Rig):
    def __init__(self, path_probe=free_line, **kw):
        self.pose = [0.0, 0.0, 0.0]
        self.path_probe_fn = path_probe
        super().__init__(pose=lambda: tuple(self.pose) if self.pose else None,
                         path_probe=lambda *a: self.path_probe_fn(*a), **kw)

    def lines(self):
        return self.client.lines

    def arrive_at(self, x, y, yaw):
        """ตัวควบคุมรายงานว่าถึงปลายเส้นแล้ว และหุ่นอยู่ที่ (x, y, yaw)"""
        self.pose[:] = [x, y, yaw]
        return self.arrive()


class StraightStartTest(unittest.TestCase):

    def refused(self, exc, code, rig=None, route=L_ROUTE, **kw):
        rig = rig or StraightRig()
        with self.assertRaises(exc) as cm:
            rig.start(route=route, **kw)
        self.assertEqual(cm.exception.code, code)
        self.assertEqual((rig.client.lines, rig.client.sent), ([], []), 'ผิดกติกาแล้วต้องไม่ส่งอะไรให้ตัวควบคุม')
        self.assertEqual(rig.status()['state'], 'idle')
        self.assertFalse(rig.runner.active())
        return cm.exception

    def test_starts_with_the_first_segment_as_a_straight_line(self):
        rig = StraightRig()
        rig.start(route=L_ROUTE)
        pts = rig.lines()[0]
        self.assertEqual(pts[0][:2], (0.0, 0.0))
        self.assertEqual(pts[-1][:2], (2.0, 0.0))
        self.assertAlmostEqual(pts[-1][2], math.pi / 2, places=4)                      # จุดสุดท้ายหันตามทิศที่ตั้งไว้ (ปัดที่ทศนิยม 5)
        self.assertTrue(all(p[1] == 0.0 for p in pts))
        self.assertEqual(rig.client.sent, [], 'ต้องไม่ผ่าน planner')
        s = rig.status()
        self.assertEqual((s['state'], s['mode'], s['index'], s['total'], s['current']['name']), ('running', 'straight', 1, 3, 'B'))

    def test_robot_must_be_at_the_first_point_and_face_the_first_segment(self):
        rig = StraightRig()
        rig.pose[:] = [1.0, 0.0, 0.0]
        self.refused(GoalError, 'nav.err.line_start_far', rig=rig)
        rig = StraightRig()
        rig.pose[:] = [0.0, 0.0, math.radians(60)]
        self.refused(GoalError, 'nav.err.line_heading', rig=rig)
        rig = StraightRig()
        rig.pose = None
        self.refused(GoalError, 'nav.err.straight_no_pose', rig=rig)

    def test_every_segment_is_checked_before_the_robot_moves(self):
        def second_blocked(points, *a):
            return {'ok': False, 'at': 1.2, 'reason': 'tight', 'min_clearance': 0.1, 'soft_risk': True} if points[-1][:2] == (2.0, 2.0) else free_line()
        err = self.refused(RunError, 'route.run.straight_blocked', rig=StraightRig(path_probe=second_blocked))
        self.assertEqual((err.params['n'], err.params['at'], err.params['clearance']), (3, 1.2, 0.4))      # n = ลำดับจุดที่เส้นทางไปถึง (จุด C)

    def test_intermediate_point_must_face_the_next_segment(self):
        bad = {'loop': 1, 'mode': 'straight', 'points': [P('A', 0, 0), P('B', 2, 0, 0.0), P('C', 2, 2, 1.0)]}   # B หันไปทางเดิม แต่ช่วงถัดไปขึ้น
        err = self.refused(RunError, 'route.run.straight_yaw', route=bad)
        self.assertEqual((err.params['n'], err.params['found'], err.params['max']), (2, 90.0, 20))
        near = {'loop': 1, 'mode': 'straight', 'points': [P('A', 0, 0), P('B', 2, 0, math.pi / 2 - math.radians(15)), P('C', 2, 2)]}
        StraightRig().start(route=near)                                       # เบี่ยง 15° ยังอยู่ในเกณฑ์

    def test_segment_length_limits(self):
        far = {'loop': 1, 'mode': 'straight', 'points': [P('A', 0, 0), P('B', 12.0, 0, 0.0)]}
        err = self.refused(RunError, 'route.run.straight_length', route=far)
        self.assertEqual((err.params['n'], err.params['max']), (1, 10.0))

    def test_multi_round_checks_the_closing_segment_too(self):
        rig = StraightRig()
        closed = {'loop': 2, 'mode': 'straight', 'points': [P('A', 0, 0, 0.0), P('B', 2, 0, math.pi / 2), P('C', 2, 2, 1.0)]}
        err = self.refused(RunError, 'route.run.straight_yaw', rig=rig, route=closed)          # C ต้องหันกลับไปหา A
        self.assertEqual(err.params['n'], 3)
        good = {'loop': 2, 'mode': 'straight', 'points': [P('A', 0, 0, 0.0), P('B', 2, 0, math.pi / 2), P('C', 2, 2, -3 * math.pi / 4)]}
        rig.start(route=good)
        self.assertEqual(rig.status()['state'], 'running')
        short = {'loop': 2, 'mode': 'straight', 'points': [P('A', 0, 0, 0.0), P('B', 2, 0, math.pi / 2), P('C', 0.1, 0.1, -3 * math.pi / 4)]}
        self.refused(RouteError, 'route.err.segment_short', route=short)                          # ช่วงปิดวงสั้นเกิน: ถูกจับตั้งแต่ตรวจ Route

    def test_unavailable_without_pose_or_line_probe(self):
        from agv_web_app.route_run import RouteRunner
        rig = StraightRig()
        rig.runner = RouteRunner(rig.goal, ok_probe, lambda: True, lambda: True, rig._stop, threaded=False, clock=rig.clock)
        self.refused(RunError, 'route.run.straight_unavailable', rig=rig)

    def test_navigate_mode_is_unchanged(self):
        rig = StraightRig()
        rig.start(route={'loop': 1, 'points': [P('A', 1.0, 1.0), P('B', 2.0, 1.0)]})        # ไม่มี mode = navigate
        self.assertEqual((rig.client.lines, len(rig.client.sent), rig.status()['mode'], rig.status()['index']), ([], 1, 'navigate', 0))


class StraightRunTest(unittest.TestCase):

    def test_drives_every_segment_from_the_previous_point_then_succeeds(self):
        rig = StraightRig()
        rig.start(route=L_ROUTE)
        self.assertTrue(rig.arrive_at(2.0, 0.0, math.pi / 2))                 # ถึง B หันขึ้น
        self.assertEqual(rig.status()['index'], 2)
        second = rig.lines()[1]
        self.assertEqual((second[0][:2], second[-1][:2]), ((2.0, 0.0), (2.0, 2.0)))
        self.assertTrue(all(abs(p[0] - 2.0) < 1e-9 for p in second))
        self.assertFalse(rig.arrive_at(2.0, 2.0, 1.0))                        # ถึง C = จบ
        s = rig.status()
        self.assertEqual((s['state'], rig.rounds, len(rig.client.sent)), ('succeeded', 1, 0))

    def test_wait_at_a_point_before_the_next_segment(self):
        rig = StraightRig()
        route = {'loop': 1, 'mode': 'straight', 'points': [P('A', 0, 0, 0.0), P('B', 2, 0, math.pi / 2, wait=5), P('C', 2, 2)]}
        rig.start(route=route)
        rig.arrive_at(2.0, 0.0, math.pi / 2)
        self.assertEqual((rig.status()['phase'], len(rig.lines())), ('waiting', 1))
        rig.clock.now += 5.1
        rig.runner.tick()
        self.assertEqual((rig.status()['phase'], len(rig.lines())), ('going', 2))

    def test_second_round_drives_the_closing_segment_back_to_the_first_point(self):
        rig = StraightRig()
        route = {'loop': 2, 'mode': 'straight', 'points': [P('A', 0, 0, 0.0), P('B', 2, 0, math.pi / 2), P('C', 2, 2, -3 * math.pi / 4)]}
        rig.start(route=route)
        rig.arrive_at(2.0, 0.0, math.pi / 2)
        rig.arrive_at(2.0, 2.0, -3 * math.pi / 4)
        closing = rig.lines()[2]
        self.assertEqual((closing[0][:2], closing[-1][:2], rig.status()['round'], rig.status()['index']), ((2.0, 2.0), (0.0, 0.0), 2, 0))
        rig.arrive_at(0.0, 0.0, 0.0)                                           # ถึง A หันไปทาง B
        self.assertEqual(rig.lines()[3][-1][:2], (2.0, 0.0))                   # รอบสองเดินช่วง A→B ต่อ (index 1)
        rig.arrive_at(2.0, 0.0, math.pi / 2)
        rig.arrive_at(2.0, 2.0, -3 * math.pi / 4)
        self.assertEqual((rig.status()['state'], rig.rounds), ('succeeded', 2))

    def test_stops_the_whole_route_when_the_next_segment_cannot_start(self):
        rig = StraightRig()
        rig.start(route=L_ROUTE)
        rig.arrive_at(2.0, 0.0, math.radians(10))                              # ถึง B แต่หันผิด (ไม่ได้หมุนเข้าทิศช่วงถัดไป)
        s = rig.status()
        self.assertEqual((s['state'], s['reason']['code']), ('failed', 'nav.err.line_heading'))
        self.assertEqual(len(rig.lines()), 1, 'ล้มแล้วต้องไม่ส่งช่วงถัดไป')
        self.assertGreaterEqual(rig.stops, 1)

    def test_robot_far_from_the_previous_point_stops_the_route(self):
        rig = StraightRig()
        rig.start(route=L_ROUTE)
        rig.arrive_at(3.2, 0.0, math.pi / 2)                                   # ไปไกลเกินจุด B
        self.assertEqual((rig.status()['state'], rig.status()['reason']['code']), ('failed', 'nav.err.line_start_far'))

    def test_segment_failure_from_the_controller_stops_the_route(self):
        rig = StraightRig()
        rig.start(route=L_ROUTE)
        rig.client.listener.on_result('failed')
        rig.runner.tick()
        self.assertEqual(rig.status()['state'], 'failed')
        self.assertEqual(len(rig.lines()), 1)

    def test_pause_then_resume_continues_in_a_straight_line_from_where_the_robot_is(self):
        rig = StraightRig()
        rig.start(route=L_ROUTE)
        rig.pose[:] = [1.1, 0.02, 0.0]                                         # กลางช่วง A→B
        rig.runner.pause()
        rig.client.listener.on_result('canceled')
        self.assertTrue(rig.status()['pause_settled'])
        rig.runner.resume()
        resumed = rig.lines()[1]
        self.assertEqual((resumed[0][:2], resumed[-1][:2]), ((1.1, 0.02), (2.0, 0.0)))
        self.assertAlmostEqual(resumed[-1][2], math.pi / 2, places=4)
        self.assertEqual(rig.status()['state'], 'running')

    def test_stop_cancels_the_line(self):
        rig = StraightRig()
        rig.start(route=L_ROUTE)
        self.assertTrue(rig.runner.stop())
        self.assertEqual((rig.client.cancels >= 1, rig.status()['state']), (True, 'canceled'))


class StraightRouteStoreTest(unittest.TestCase):

    def v(self, points, loop=1, mode='straight'):
        return validate_route('R', loop, points, ok_probe, 0.4, mode)

    def test_mode_is_stored_and_defaults_to_navigate(self):
        _, route = self.v([{'name': 'A', 'x': 0, 'y': 0, 'yaw': 0}, {'name': 'B', 'x': 2, 'y': 0, 'yaw': 0}])
        self.assertEqual(route['mode'], 'straight')
        self.assertEqual(validate_route('R', 1, [{'x': 0, 'y': 0, 'yaw': 0}], ok_probe, 0.4)[1]['mode'], 'navigate')

    def test_straight_rules_are_refused_at_save_time(self):
        for kw, code in (({'points': [{'x': 0, 'y': 0, 'yaw': 0}]}, 'route.err.straight_points'),
                         ({'points': [{'x': 0, 'y': 0, 'yaw': 0}, {'x': 0.1, 'y': 0, 'yaw': 0}]}, 'route.err.segment_short'),
                         ({'points': [{'x': 0, 'y': 0, 'yaw': 0}, {'x': 2, 'y': 0, 'yaw': 0}], 'mode': 'curvy'}, 'route.err.mode'),
                         ({'points': [{'x': 0, 'y': 0, 'yaw': 0}, {'x': 2, 'y': 0, 'yaw': 0}, {'x': 0.1, 'y': 0.1, 'yaw': 0}], 'loop': 2},
                          'route.err.segment_short')):
            with self.assertRaises(RouteError) as cm:
                self.v(**kw)
            self.assertEqual(cm.exception.code, code, kw)
        self.v([{'x': 0, 'y': 0, 'yaw': 0}, {'x': 2, 'y': 0, 'yaw': 0}, {'x': 0.1, 'y': 0.1, 'yaw': 0}], loop=1)   # ช่วงปิดวงไม่นับถ้ารอบเดียว


if __name__ == '__main__':
    unittest.main()
