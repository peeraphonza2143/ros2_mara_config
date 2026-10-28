"""
ทดสอบ InitialPose (วางตำแหน่งเริ่มต้นให้ AMCL) ด้วย publisher ปลอม — ไม่ต้องมี ROS
เน้น: ไม่ส่งอะไรออกไปเลยเมื่อค่าผิด/ไม่มีผู้ฟัง · ตำแหน่งต้องอยู่ในกรอบแผนที่ · ส่งซ้ำตามที่กำหนด

รัน:  cd ~/ros2_ws/src/agv_web_app && python3 -m unittest discover -s test -v
"""
import math
import threading
import unittest

from agv_web_app.nav_pose import (COVARIANCE, INTERVAL_S, POLL_S, REPEATS, WAIT_LISTENER_S, InitialPose, PoseError,
                                  inside_map, normalize_yaw, validate)

META = {'available': True, 'origin': {'x': -3.0, 'y': -2.0, 'yaw': 0.0}, 'size_m': {'w': 10.0, 'h': 8.0}}


class FakePublisher:
    def __init__(self, listeners=1):
        self.count = listeners
        self.sent = []
        self.polls = 0

    def listeners(self):
        self.polls += 1
        return self.count

    def publish(self, x, y, yaw, covariance):
        self.sent.append((x, y, yaw, covariance))


class Clock:
    """นาฬิกาปลอม: sleep เดินเวลา ไม่รอจริง"""

    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def __call__(self):
        return self.now


class Base(unittest.TestCase):
    def setUp(self):
        self.pub = FakePublisher()
        self.clock = Clock()
        self.events = []
        self.pose = InitialPose(self.pub, on_event=self.events.append, sleep=self.clock.sleep, clock=self.clock)

    def code(self, *args, **kw):
        with self.assertRaises(PoseError) as ctx:
            self.pose.set(*args, **kw)
        return ctx.exception.code


class ValidateTest(unittest.TestCase):

    def test_normalize_yaw_wraps_into_minus_pi_to_pi(self):
        for raw, want in ((0.0, 0.0), (math.pi, math.pi), (-math.pi, math.pi), (3 * math.pi, math.pi),
                          (2 * math.pi, 0.0), (7.0, 7.0 - 2 * math.pi), (-7.0, -7.0 + 2 * math.pi), (0.5, 0.5)):
            self.assertAlmostEqual(normalize_yaw(raw), want, msg=str(raw))
        for raw in (-100.0, -3.2, 3.2, 100.0, 1e5):
            self.assertTrue(-math.pi < normalize_yaw(raw) <= math.pi, raw)

    def test_inside_map_includes_the_edges_and_rejects_just_outside(self):
        for x, y in ((-3.0, -2.0), (7.0, 6.0), (-3.0, 6.0), (7.0, -2.0), (2.0, 2.0)):
            self.assertTrue(inside_map(x, y, META), (x, y))
        for x, y in ((-3.001, 0.0), (7.001, 0.0), (0.0, -2.001), (0.0, 6.001), (100.0, 100.0), (-100.0, 0.0)):
            self.assertFalse(inside_map(x, y, META), (x, y))

    def test_rotated_map_origin(self):
        rotated = {'available': True, 'origin': {'x': 0.0, 'y': 0.0, 'yaw': math.pi / 2},
                   'size_m': {'w': 4.0, 'h': 2.0}}
        self.assertTrue(inside_map(-1.0, 3.0, rotated))                    # หมุน 90°: แกน u ของแผนที่ชี้ไปทาง +y โลก
        self.assertFalse(inside_map(3.0, 1.0, rotated))
        self.assertFalse(inside_map(1.0, -1.0, rotated))

    def test_numbers_must_be_real_finite_numbers(self):
        bad = [None, 'a', '1', [1], {}, True, False, float('nan'), float('inf'), -float('inf'), 1e7, -1e7]
        for value in bad:
            for args in ((value, 0.0, 0.0), (0.0, value, 0.0), (0.0, 0.0, value)):
                with self.assertRaises(PoseError) as ctx:
                    validate(*args, META)
                self.assertEqual(ctx.exception.code, 'nav.err.pose_invalid', repr(args))

    def test_missing_map_and_outside_map(self):
        for meta in (None, {}, {'available': False}):
            with self.assertRaises(PoseError) as ctx:
                validate(0.0, 0.0, 0.0, meta)
            self.assertEqual(ctx.exception.code, 'nav.err.no_map', repr(meta))
        with self.assertRaises(PoseError) as ctx:
            validate(50.0, 0.0, 0.0, META)
        self.assertEqual(ctx.exception.code, 'nav.err.pose_outside')

    def test_valid_values_are_returned_as_floats_with_wrapped_yaw(self):
        x, y, yaw = validate(1, 2, 7, META)
        self.assertEqual((x, y), (1.0, 2.0))
        self.assertIs(type(x), float)
        self.assertAlmostEqual(yaw, 7 - 2 * math.pi)


class SetTest(Base):

    def test_publishes_the_pose_three_times_with_the_default_covariance(self):
        result = self.pose.set(1.0, 2.0, 0.5, META)
        self.assertEqual(self.pub.sent, [(1.0, 2.0, 0.5, COVARIANCE)] * REPEATS)
        self.assertEqual(REPEATS, 3)
        self.assertEqual(COVARIANCE, (0.25, 0.25, 0.0685))
        self.assertEqual(self.clock.sleeps, [INTERVAL_S] * (REPEATS - 1))              # ห่างกัน ไม่ส่งรวดเดียว
        self.assertEqual((result['x'], result['y'], result['yaw']), (1.0, 2.0, 0.5))
        self.assertAlmostEqual(result['yaw_deg'], math.degrees(0.5))

    def test_yaw_is_wrapped_before_sending(self):
        self.pose.set(0.0, 0.0, 3 * math.pi + 0.1, META)
        self.assertAlmostEqual(self.pub.sent[0][2], -math.pi + 0.1)

    def test_bad_input_sends_nothing_and_does_not_even_look_for_listeners(self):
        for args in ((float('nan'), 0, 0), ('x', 0, 0), (999, 0, 0), (True, 0, 0)):
            with self.assertRaises(PoseError):
                self.pose.set(*args, META)
        with self.assertRaises(PoseError):
            self.pose.set(0, 0, 0, None)
        self.assertEqual((self.pub.sent, self.pub.polls), ([], 0))

    def test_waits_for_a_listener_and_sends_once_one_appears(self):
        self.pub.count = 0
        original = self.pub.listeners

        def appears_after_three_polls():
            original()                                         # นับจำนวนครั้งที่ถูกเช็ค (self.pub.polls)
            if self.pub.polls >= 3:
                self.pub.count = 1
            return self.pub.count

        self.pub.listeners = appears_after_three_polls
        self.pose.set(0.0, 0.0, 0.0, META)
        self.assertEqual(len(self.pub.sent), REPEATS)
        self.assertEqual(self.clock.sleeps[:2], [POLL_S, POLL_S])

    def test_no_listener_means_nothing_is_sent_after_the_wait(self):
        self.pub.count = 0
        self.assertEqual(self.code(0.0, 0.0, 0.0, META), 'nav.err.no_listener')
        self.assertEqual(self.pub.sent, [])
        self.assertGreaterEqual(self.clock.now, WAIT_LISTENER_S)                       # รอครบเวลาก่อนยอมแพ้
        self.assertLess(self.clock.now, WAIT_LISTENER_S + 1)

    def test_only_one_at_a_time_and_the_lock_is_released_after_failure(self):
        self.pose._lock.acquire()
        try:
            self.assertEqual(self.code(0.0, 0.0, 0.0, META), 'nav.err.busy')
        finally:
            self.pose._lock.release()
        self.assertEqual(self.pub.sent, [])
        self.pub.count = 0
        self.assertEqual(self.code(0.0, 0.0, 0.0, META), 'nav.err.no_listener')
        self.pub.count = 1
        self.pose.set(0.0, 0.0, 0.0, META)                                              # ล้มเหลวแล้วต้องไม่ล็อกค้าง
        self.assertEqual(len(self.pub.sent), REPEATS)

    def test_concurrent_requests_are_serialised(self):
        entered, release = threading.Event(), threading.Event()
        original = self.pub.publish

        def slow(*args):
            entered.set()
            release.wait(5)
            original(*args)

        self.pub.publish = slow
        first = threading.Thread(target=lambda: self.pose.set(1.0, 1.0, 0.0, META))
        first.start()
        entered.wait(5)
        try:
            self.assertEqual(self.code(2.0, 2.0, 0.0, META), 'nav.err.busy')
        finally:
            release.set()
            first.join(5)
        self.assertEqual({(x, y) for x, y, _, _ in self.pub.sent}, {(1.0, 1.0)})

    def test_every_successful_set_is_logged(self):
        self.pose.set(1.5, -1.0, math.pi / 2, META)
        self.assertEqual(self.events, ['Initial pose: x=1.50 y=-1.00 yaw=90.0°'])
        with self.assertRaises(PoseError):
            self.pose.set(999, 0, 0, META)
        self.assertEqual(len(self.events), 1)                                           # ล้มเหลวไม่ล็อกว่าตั้งแล้ว


if __name__ == '__main__':
    unittest.main()
