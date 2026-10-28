"""
ทดสอบ LaserOverlay (LaserScan → จุดในเฟรม map) — ไม่ต้องมี ROS

รัน:  cd ~/ros2_ws/src/agv_web_app && python3 -m unittest discover -s test -v
"""
import json
import math
import threading
import unittest

from agv_web_app.laser_overlay import LaserOverlay

INF = float('inf')
NAN = float('nan')


class LaserOverlayTest(unittest.TestCase):

    def setUp(self):
        self.errors = []
        self.ov = LaserOverlay(max_points=360, on_error=self.errors.append)

    def put(self, ranges, pose=(0.0, 0.0, 0.0), a0=0.0, da=math.pi / 2, lo=0.1, hi=10.0):
        return self.ov.update(ranges, a0, da, lo, hi, pose, now=100.0)

    def pts(self):
        return self.ov.latest(now=100.0)['points']

    def test_nothing_before_first_scan(self):
        self.assertEqual(self.ov.latest(), {'available': False, 'version': 0})

    def test_geometry_with_identity_pose(self):
        self.assertTrue(self.put([1.0, 1.0, 1.0, 1.0]))                  # 0°, 90°, 180°, 270°
        self.assertEqual(self.pts(), [[1.0, 0.0], [0.0, 1.0], [-1.0, 0.0], [-0.0, -1.0]])

    def test_pose_translation_and_rotation(self):
        self.put([2.0, 2.0], pose=(1.0, 3.0, math.pi / 2), da=math.pi / 2)  # หุ่นหันขึ้น 90°
        got = self.pts()
        self.assertAlmostEqual(got[0][0], 1.0, places=2)                  # มุม 0 ของเลเซอร์ = ทิศ +y ของ map
        self.assertAlmostEqual(got[0][1], 5.0, places=2)
        self.assertAlmostEqual(got[1][0], -1.0, places=2)                 # มุม 90° ของเลเซอร์ = ทิศ -x ของ map
        self.assertAlmostEqual(got[1][1], 3.0, places=2)

    def test_invalid_returns_are_dropped(self):
        self.put([1.0, INF, NAN, 0.05, 11.0, -1.0, 2.0], da=0.1)
        self.assertEqual(len(self.pts()), 2)                              # เหลือแค่ 1.0 และ 2.0
        meta = self.ov.latest(now=100.0)
        self.assertEqual(meta['count'], 2)

    def test_range_limits_are_inclusive(self):
        self.put([0.1, 10.0, 0.099, 10.001], lo=0.1, hi=10.0, da=0.1)
        self.assertEqual(len(self.pts()), 2)

    def test_empty_scan_is_valid_and_gives_no_points(self):
        self.assertTrue(self.put([]))
        self.assertEqual(self.ov.latest(now=100.0)['count'], 0)
        self.assertTrue(self.ov.latest(now=100.0)['available'])

    def test_downsampling_limits_points_and_keeps_angular_coverage(self):
        ov = LaserOverlay(max_points=100)
        n = 3200                                                         # ขนาดสแกนของ S2
        ov.update([2.0] * n, -math.pi, 2 * math.pi / n, 0.1, 30.0, (0.0, 0.0, 0.0), now=1.0)
        pts = ov.latest(now=1.0)['points']
        self.assertEqual(len(pts), 100)
        angles = sorted(math.atan2(y, x) for x, y in pts)
        self.assertLess(angles[0], -math.pi + 0.1)                       # ครอบคลุมรอบตัว
        self.assertGreater(angles[-1], math.pi - 0.1)
        self.assertLess(max(b - a for a, b in zip(angles, angles[1:])), 0.2)   # ไม่มีช่องว่างใหญ่

    def test_small_scans_are_not_downsampled(self):
        self.put([1.0] * 50, da=0.1)
        self.assertEqual(len(self.pts()), 50)

    def test_values_are_rounded_to_centimetres_and_json_safe(self):
        self.put([1.23456], pose=(0.0, 0.0, 0.0))
        self.assertEqual(self.pts(), [[1.23, 0.0]])
        json.dumps(self.ov.latest(now=100.0), allow_nan=False)

    def test_bad_input_is_rejected_and_old_scan_kept(self):
        self.put([1.0, 1.0])
        before = self.ov.latest(now=100.0)
        bad = [dict(ranges=[[1.0]], pose=(0, 0, 0)), dict(ranges=['a'], pose=(0, 0, 0)),
               dict(ranges=[1.0], pose=(NAN, 0, 0)), dict(ranges=[1.0], pose=(0, 0)),
               dict(ranges=None, pose=(0, 0, 0)), dict(ranges=[1.0], pose=(0, 0, INF)),
               dict(ranges=[1.0], pose=('x', 0, 0))]
        for case in bad:
            self.assertFalse(self.ov.update(case['ranges'], 0.0, 0.1, 0.1, 10.0, case['pose']), case)
        self.assertEqual(len(self.errors), len(bad))
        self.assertEqual(self.ov.latest(now=100.0), before)

    def test_bad_scan_parameters(self):
        for args in ((NAN, 0.1, 0.1, 10.0), (0.0, INF, 0.1, 10.0), (0.0, 0.1, NAN, 10.0), (0.0, 0.1, 0.1, NAN)):
            self.assertFalse(self.ov.update([1.0], *args, (0, 0, 0)), args)

    def test_age_and_version(self):
        self.put([1.0])
        self.assertEqual(self.ov.latest(now=100.0)['age'], 0.0)
        self.assertEqual(self.ov.latest(now=101.5)['age'], 1.5)
        self.assertEqual(self.ov.latest(now=99.0)['age'], 0.0)           # นาฬิกาถอยไม่ให้ติดลบ
        v = self.ov.latest()['version']
        self.put([1.0])
        self.assertEqual(self.ov.latest()['version'], v + 1)
        self.ov.clear()
        self.assertEqual(self.ov.latest()['available'], False)
        self.assertEqual(self.ov.latest()['version'], v + 2)

    def test_origin_is_the_laser_position(self):
        self.put([1.0], pose=(1.2345, -2.0, 0.3))
        self.assertEqual(self.ov.latest(now=100.0)['origin'], [1.234, -2.0])

    def test_concurrent_update_and_read(self):
        stop, errors = threading.Event(), []

        def reader():
            while not stop.is_set():
                try:
                    m = self.ov.latest()
                    if m['available']:
                        assert m['count'] == len(m['points'])
                except Exception as e:      # noqa: BLE001
                    errors.append(e)
        t = threading.Thread(target=reader)
        t.start()
        for i in range(300):
            self.put([1.0 + (i % 3)] * 20, da=0.1)
        stop.set()
        t.join()
        self.assertEqual(errors, [])


if __name__ == '__main__':
    unittest.main()
