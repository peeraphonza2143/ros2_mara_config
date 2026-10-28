"""
ทดสอบจุด "ผ่านไปเลย" (stop=False) ของ Route โหมด straight : เรขาคณิตของเส้นโค้งมน · ที่เก็บ Route · ตัวรันแบบท่อน ·
ตัวตรวจเส้นทางบนแผนที่ (LiveMap.probe_path) · NavGoal.send_leg — ไม่ต้องมี ROS

รัน:  cd ~/ros2_ws/src/agv_web_app && python3 -m unittest discover -s test -v
"""
import math
import unittest

from test_nav_goal import FakeClient, make, ok_probe
from test_route_straight import StraightRig, free_line

from agv_web_app.live_map import LiveMap
from agv_web_app.nav_goal import (CORNER_MAX_RAD, LINE_STEP_M, PASS_MIN_RADIUS_M, CornerError, GoalError,
                                  smooth_path)
from agv_web_app.route_run import RunError
from agv_web_app.routes import RouteError, RouteStore, validate_route


def P(name, x, y, yaw=0.0, wait=0, stop=True):
    return {'name': name, 'x': x, 'y': y, 'yaw': yaw, 'wait': wait, 'stop': stop}


def gaps(path):
    return [math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(path, path[1:])]


class SmoothPathTest(unittest.TestCase):

    def test_corner_is_rounded_with_the_requested_radius(self):
        path = smooth_path([(0, 0), (4, 0), (4, 4)], 1.0, radius=0.9)
        self.assertEqual((path[0][:2], path[-1]), ((0.0, 0.0), (4.0, 4.0, 1.0)))
        nearest = min(math.hypot(x - 4.0, y) for x, y, _ in path)
        self.assertAlmostEqual(nearest, 0.9 * (math.sqrt(2) - 1), delta=0.01)       # เฉียดจุดมุม R(√2−1)
        self.assertLessEqual(max(gaps(path)), LINE_STEP_M + 1e-9)
        headings = [h for *_, h in path[:-1]]
        self.assertAlmostEqual(min(headings), 0.0, places=6)
        self.assertAlmostEqual(max(headings), math.pi / 2, delta=0.02)               # เลี้ยวซ้าย 90° ทีละน้อย ไม่กระโดด
        self.assertTrue(all(b >= a - 1e-9 for a, b in zip(headings, headings[1:])))

    def test_straight_parts_are_exactly_straight(self):
        path = smooth_path([(0, 0), (4, 0), (4, 4)], 0.0, radius=0.9)
        before = [p for p in path if p[0] < 3.05]
        after = [p for p in path if p[1] > 0.95]
        self.assertTrue(all(p[1] == 0.0 for p in before))
        self.assertTrue(all(abs(p[0] - 4.0) < 1e-9 for p in after))

    def test_turn_direction_right_and_left(self):
        left = smooth_path([(0, 0), (4, 0), (4, 4)], 0.0)
        right = smooth_path([(0, 0), (4, 0), (4, -4)], 0.0)
        self.assertGreater(max(h for *_, h in left[:-1]), 1.0)
        self.assertLess(min(h for *_, h in right[:-1]), -1.0)
        self.assertTrue(all(y <= 1e-9 for _, y, _ in right))                         # โค้งอยู่ด้านใน ไม่ล้ำออกนอกมุม

    def test_radius_shrinks_on_short_legs_and_too_tight_is_refused(self):
        path = smooth_path([(0, 0), (1.0, 0), (1.0, 1.0)], 0.0, radius=0.9)          # ขาละ 1 m → รัศมีลดเหลือ 0.5
        nearest = min(math.hypot(x - 1.0, y) for x, y, _ in path)
        self.assertAlmostEqual(nearest, 0.5 * (math.sqrt(2) - 1), delta=0.01)
        with self.assertRaises(CornerError) as cm:
            smooth_path([(0, 0), (0.8, 0), (0.8, 0.8)], 0.0, radius=0.9)             # ขาละ 0.8 m → 0.4 < 0.45
        self.assertEqual((cm.exception.code, cm.exception.index, cm.exception.params['min']), ('nav.err.line_corner', 1, PASS_MIN_RADIUS_M))
        self.assertAlmostEqual(cm.exception.params['found'], 0.4)

    def test_u_turn_is_refused(self):
        with self.assertRaises(CornerError) as cm:
            smooth_path([(0, 0), (3, 0), (0, 0.2)], 0.0)
        self.assertEqual(cm.exception.params['found'], 0.0)
        self.assertGreater(math.radians(179), CORNER_MAX_RAD)

    def test_straight_through_points_stay_exact(self):
        path = smooth_path([(0, 0), (2, 0), (4, 0.05)], 0.0)                          # เลี้ยว ~1.4° : ถือว่าตรง ผ่านจุดเป๊ะ
        self.assertTrue(any(abs(p[0] - 2.0) < 1e-9 and abs(p[1]) < 1e-9 for p in path))
        flat = smooth_path([(0, 0), (2, 0), (4, 0)], 0.0)
        self.assertTrue(all(p[1] == 0.0 for p in flat))
        self.assertEqual(len(flat), 81)

    def test_two_corners_in_a_row(self):
        path = smooth_path([(0, 0), (3, 0), (3, 3), (0, 3)], 3.0)                    # S-U : ซ้ายสองครั้ง
        self.assertEqual(path[-1], (0.0, 3.0, 3.0))
        self.assertLessEqual(max(gaps(path)), LINE_STEP_M + 1e-9)
        self.assertAlmostEqual(max(h for *_, h in path[:-1]), math.pi, delta=0.05)

    def test_end_pose_uses_end_yaw_normalized(self):
        self.assertAlmostEqual(smooth_path([(0, 0), (2, 0)], 7.0)[-1][2], 7.0 - 2 * math.pi)


class PassRouteStoreTest(unittest.TestCase):

    def v(self, points, mode='straight', loop=1):
        return validate_route('R', loop, points, ok_probe, 0.4, mode)[1]['points']

    def pts(self, *stops):
        return [{'name': n, 'x': float(i * 2), 'y': 0.0, 'yaw': 0.0, 'stop': s} for i, (n, s) in enumerate(stops)]

    def test_stop_defaults_to_true_and_first_last_are_always_stops(self):
        out = self.v([{'x': 0, 'y': 0, 'yaw': 0}, {'x': 2, 'y': 0, 'yaw': 0}, {'x': 4, 'y': 0, 'yaw': 0}])
        self.assertEqual([p['stop'] for p in out], [True, True, True])
        out = self.v(self.pts(('A', False), ('B', False), ('C', False)))
        self.assertEqual([p['stop'] for p in out], [True, False, True])              # จุดแรก/สุดท้ายถูกบังคับเป็นจุดหยุด

    def test_pass_through_needs_straight_mode_and_a_real_bool(self):
        with self.assertRaises(RouteError) as cm:
            self.v(self.pts(('A', True), ('B', False), ('C', True)), mode='navigate')
        self.assertEqual((cm.exception.code, cm.exception.params['n']), ('route.err.pass_navigate', 2))
        for bad in ('no', 0, None):
            points = self.pts(('A', True), ('B', True), ('C', True))
            points[1]['stop'] = bad
            with self.assertRaises(RouteError) as cm:
                self.v(points)
            self.assertEqual(cm.exception.code, 'route.err.point_invalid', bad)
        self.v(self.pts(('A', True), ('B', True), ('C', True)), mode='navigate')      # stop:true ในโหมดเดิมไม่เป็นไร

    def test_stored_and_read_back_and_old_files_default_to_stop(self):
        import json
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            store = RouteStore(os.path.join(tmp, 'routes.json'), lambda n: True)
            store.save('lab', 'R', 1, self.pts(('A', True), ('B', False), ('C', True)), ok_probe, 0.4, mode='straight')
            self.assertEqual([p['stop'] for p in store.get('lab', 'R')['points']], [True, False, True])
            with open(store.path) as f:
                raw = json.load(f)
            for p in raw['lab']['R']['points']:
                p.pop('stop')                                                         # จำลองไฟล์เก่าที่ไม่มีฟิลด์นี้
            with open(store.path, 'w') as f:
                json.dump(raw, f)
            self.assertEqual([p['stop'] for p in store.get('lab', 'R')['points']], [True, True, True])


class PassRunTest(unittest.TestCase):
    """A(0,0) →(ผ่าน) B(3,0) →(ผ่าน) C(3,3) → D(0,3) : จุดหยุดมีแค่ A กับ D"""

    ROUTE = {'loop': 1, 'mode': 'straight', 'points': [P('A', 0, 0, 0.0), P('B', 3, 0, 0.0, stop=False),
                                                       P('C', 3, 3, math.pi / 2, stop=False), P('D', 0, 3, 3.0)]}

    def test_one_continuous_path_through_the_pass_points(self):
        rig = StraightRig()
        rig.start(route=self.ROUTE)
        path = rig.lines()[0]
        self.assertEqual((path[0][:2], path[-1][:2], len(rig.lines())), ((0.0, 0.0), (0.0, 3.0), 1))
        self.assertEqual(rig.client.sent, [])
        self.assertAlmostEqual(path[-1][2], 3.0)
        self.assertGreater(min(math.hypot(x - 3.0, y) for x, y, _ in path), 0.3)    # ตัดมุม ไม่ผ่านตัวจุด B เป๊ะ
        self.assertAlmostEqual(max(h for *_, h in path[:-1]), math.pi, delta=0.05)  # เลี้ยวซ้ายสองมุมรวม 180°
        s = rig.status()
        self.assertEqual((s['state'], s['index'], s['current']['name']), ('running', 3, 'D'))

    def test_arriving_at_the_next_stop_finishes_without_stopping_at_pass_points(self):
        rig = StraightRig()
        rig.start(route=self.ROUTE)
        rig.arrive_at(0.0, 3.0, 3.0)
        s = rig.status()
        self.assertEqual((s['state'], len(rig.lines()), rig.rounds), ('succeeded', 1, 1))

    def test_pass_point_ignores_wait_but_stop_point_keeps_it(self):
        points = [P('A', 0, 0, 0.0), P('B', 3, 0, 0.0, wait=30, stop=False), P('C', 3, 3, math.pi, wait=7),
                  P('D', 0, 3, 3.0)]
        rig = StraightRig()
        rig.start(route={'loop': 1, 'mode': 'straight', 'points': points})
        self.assertEqual(rig.status()['index'], 2)                                    # เป้าหมายแรกคือ C (B ถูกข้าม)
        rig.arrive_at(3.0, 3.0, math.pi)
        self.assertEqual((rig.status()['phase'], len(rig.lines())), ('waiting', 1))   # หยุดรอที่ C ตาม wait=7
        rig.clock.now += 7.1
        rig.runner.tick()
        second = rig.lines()[1]
        self.assertEqual((second[0][:2], second[-1][:2]), ((3.0, 3.0), (0.0, 3.0)))

    def test_stop_in_the_middle_splits_the_legs(self):
        points = [P('A', 0, 0, 0.0), P('B', 3, 0, math.pi / 2), P('C', 3, 3, math.pi / 2, stop=False), P('D', 3, 6, 0.5)]
        rig = StraightRig()
        rig.start(route={'loop': 1, 'mode': 'straight', 'points': points})
        self.assertEqual(rig.status()['index'], 1)
        rig.arrive_at(3.0, 0.0, math.pi / 2)
        second = rig.lines()[1]                                                      # B → (ผ่าน C เป๊ะ เพราะอยู่แนวตรง) → D
        self.assertEqual((second[0][:2], second[-1][:2]), ((3.0, 0.0), (3.0, 6.0)))
        self.assertTrue(any(abs(x - 3.0) < 1e-9 and abs(y - 3.0) < 1e-9 for x, y, _ in second))
        rig.arrive_at(3.0, 6.0, 0.5)
        self.assertEqual((rig.status()['state'], len(rig.lines())), ('succeeded', 2))

    def test_corner_too_tight_for_a_pass_point_is_refused_before_moving(self):
        points = [P('A', 0, 0, 0.0), P('B', 0.8, 0, 0.0, stop=False), P('C', 0.8, 0.8, 0.0), P('D', 3.0, 0.8, 0.0)]
        rig = StraightRig()
        with self.assertRaises(RunError) as cm:
            rig.start(route={'loop': 1, 'mode': 'straight', 'points': points})
        self.assertEqual((cm.exception.code, cm.exception.params['n'], cm.exception.params['min']), ('route.run.straight_corner', 2, PASS_MIN_RADIUS_M))
        self.assertEqual((rig.lines(), rig.status()['state']), ([], 'idle'))

    def test_next_stop_must_face_the_next_leg_through_pass_points(self):
        points = [P('A', 0, 0, 0.0), P('B', 3, 0, 0.0), P('C', 3, 3, 0.0, stop=False), P('D', 6, 3, 0.0)]    # B หันไปทางเดิม แต่ท่อนถัดไปขึ้น
        rig = StraightRig()
        with self.assertRaises(RunError) as cm:
            rig.start(route={'loop': 1, 'mode': 'straight', 'points': points})
        self.assertEqual((cm.exception.code, cm.exception.params['n']), ('route.run.straight_yaw', 2))

    def test_second_round_closes_directly_back_to_the_first_point(self):
        points = [P('A', 0, 0, 0.0), P('B', 3, 0, 0.0, stop=False), P('C', 3, 3, -3 * math.pi / 4)]
        rig = StraightRig()
        rig.start(route={'loop': 2, 'mode': 'straight', 'points': points})
        rig.arrive_at(3.0, 3.0, -3 * math.pi / 4)                                    # ถึงจุดหยุด C ท่อนที่ 1 จบ (ผ่าน B)
        closing = rig.lines()[1]
        self.assertEqual((closing[0][:2], closing[-1][:2], rig.status()['index']), ((3.0, 3.0), (0.0, 0.0), 0))
        self.assertAlmostEqual(math.hypot(closing[0][0] - closing[-1][0], closing[0][1] - closing[-1][1]), 3 * math.sqrt(2), places=2)
        rig.arrive_at(0.0, 0.0, 0.0)
        self.assertEqual((rig.status()['index'], len(rig.lines())), (2, 3))          # รอบสองไปจุดหยุดถัดไปเลย (C) ผ่าน B
        self.assertEqual(rig.lines()[2][-1][:2], (3.0, 3.0))

    def test_resume_keeps_only_the_pass_points_still_ahead(self):
        rig = StraightRig()
        rig.start(route=self.ROUTE)
        rig.pose[:] = [1.5, 0.0, 0.0]                                                # ยังไม่ถึง B
        rig.runner.pause()
        rig.client.listener.on_result('canceled')
        rig.runner.resume()
        ahead = rig.lines()[1]
        self.assertEqual(ahead[0][:2], (1.5, 0.0))
        self.assertAlmostEqual(max(h for *_, h in ahead[:-1]), math.pi, delta=0.05)  # ยังเลี้ยวผ่านทั้ง B และ C
        rig.runner.pause()
        rig.client.listener.on_result('canceled')
        rig.pose[:] = [3.0, 1.5, math.pi / 2]                                        # ผ่าน B มาแล้ว อยู่กลางช่วง B→C
        rig.runner.resume()
        later = rig.lines()[2]
        self.assertEqual(later[0][:2], (3.0, 1.5))
        self.assertGreaterEqual(min(h for *_, h in later[:-1]), math.pi / 2 - 0.02)  # ไม่ย้อนกลับไปเลี้ยว B อีก
        self.assertEqual(later[-1][:2], (0.0, 3.0))


class ProbePathTest(unittest.TestCase):

    def setUp(self):
        self.map = LiveMap()
        grid = [0] * (400 * 100)
        for row in range(100):
            grid[row * 400 + 300] = 100                                               # ผนัง x = 15.0
        for col in range(400):
            grid[col] = 100
            grid[99 * 400 + col] = 100
        self.map.update(400, 100, 0.05, 0.0, 0.0, 0.0, grid)

    def test_matches_probe_line_on_a_straight_path(self):
        path = smooth_path([(1.0, 2.5), (10.0, 2.5)], 0.0)
        a = self.map.probe_path(path, 0.4, 0.55)
        b = self.map.probe_line(1.0, 2.5, 10.0, 2.5, 0.4, 0.55)
        self.assertEqual((a['ok'], a['soft_risk']), (b['ok'], b['soft_risk']))
        blocked = self.map.probe_path(smooth_path([(10.0, 2.5), (18.0, 2.5)], 0.0), 0.4, 0.55)
        self.assertEqual((blocked['ok'], blocked['reason']), (False, 'tight'))
        self.assertAlmostEqual(blocked['at'], 15.0 - 0.4 - 10.0, delta=0.2)

    def test_curved_path_is_checked_along_the_arc(self):
        # สิ่งกีดขวางเล็กๆ ที่ (9.75, 2.75) อยู่ด้านในมุม (10, 2.5) : ห่างจากขาตรงทั้งสองข้าง ~0.69 m แต่ขวางส่วนโค้ง
        grid = [0] * (400 * 100)
        for col in range(400):
            grid[col] = 100
            grid[99 * 400 + col] = 100
        grid[55 * 400 + 195] = 100
        self.map.update(400, 100, 0.05, 0.0, 0.0, 0.0, grid)
        self.assertTrue(self.map.probe_line(1.0, 2.5, 9.1, 2.5, 0.4, 0.55)['ok'])
        self.assertTrue(self.map.probe_line(10.0, 3.4, 10.0, 4.5, 0.4, 0.55)['ok'])
        curved = smooth_path([(1.0, 2.5), (10.0, 2.5), (10.0, 4.5)], 0.0, radius=0.9)
        result = self.map.probe_path(curved, 0.4, 0.55)
        self.assertEqual((result['ok'], result['reason']), (False, 'tight'))         # เจอเพราะตรวจตลอดส่วนโค้งด้วย
        self.assertGreater(result['at'], 8.0)
        self.assertEqual(self.map.probe_path([(1.0, 2.5, 0.0)], 0.4)['ok'], True)
        self.assertEqual(LiveMap().probe_path([(1.0, 2.5, 0.0)], 0.4)['reason'], 'no_map')


class SendLegTest(unittest.TestCase):

    def test_sends_one_smoothed_path_and_obeys_the_same_rules(self):
        goal, client, _, _ = make()
        wps = [(0.0, 0.0), (4.0, 0.0), (4.0, 4.0)]
        status, check = goal.send_leg(wps, 1.0, (0.0, 0.0, 0.0), free_line, True, True)
        self.assertEqual((status['state'], status['line']), ('active', {'x0': 0.0, 'y0': 0.0}))
        self.assertEqual((client.lines[0][-1], client.sent), ((4.0, 4.0, 1.0), []))
        self.assertTrue(check['ok'] and check['start']['ok'])
        for kw, code in (({'confirmed': False}, 'nav.err.goal_confirm_required'), ({'localized': False}, 'nav.err.goal_not_localized'),
                         ({'pose': (1.0, 0.0, 0.0)}, 'nav.err.line_start_far'), ({'pose': (0.0, 0.0, 1.5)}, 'nav.err.line_heading')):
            g, c, _, _ = make()
            args = dict(pose=(0.0, 0.0, 0.0), localized=True, confirmed=True)
            args.update(kw)
            with self.assertRaises(GoalError) as cm:
                g.send_leg(wps, 1.0, args['pose'], free_line, args['localized'], args['confirmed'])
            self.assertEqual(cm.exception.code, code)
            self.assertEqual(c.lines, [])

    def test_corner_error_is_a_goal_error_and_nothing_is_sent(self):
        goal, client, _, _ = make()
        with self.assertRaises(GoalError) as cm:
            goal.send_leg([(0.0, 0.0), (0.8, 0.0), (0.8, 0.8)], 0.0, (0.0, 0.0, 0.0), free_line, True, True)
        self.assertEqual(cm.exception.code, 'nav.err.line_corner')
        self.assertEqual((client.lines, goal.status()['state']), ([], 'idle'))
        self.assertIsInstance(FakeClient(), FakeClient)


if __name__ == '__main__':
    unittest.main()
