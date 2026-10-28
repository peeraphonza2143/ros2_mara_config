"""
ทดสอบ LiveMap (แปลง OccupancyGrid → PNG) — ไม่ต้องมี ROS

รัน:  cd ~/ros2_ws/src/agv_web_app && python3 -m unittest discover -s test -v
"""
import io
import threading
import unittest
from unittest import mock

from PIL import Image

from agv_web_app import live_map
from agv_web_app.live_map import LiveMap, map_clear_needed


def decode(png):
    image = Image.open(io.BytesIO(png))
    return image.mode, image.size, list(image.getdata())


class MapClearRuleTest(unittest.TestCase):
    """เมื่อไรต้องลืมแผนที่ตอนเปลี่ยนโหมด — เคยลืมแผนที่ latched ของ Nav2 จนหน้า Navigation ไม่เห็นแผนที่"""

    @staticmethod
    def node_logic(live, old, new):
        """ทำซ้ำสิ่งที่ web_server_node ทำเมื่อโหมดเปลี่ยน"""
        if map_clear_needed(old, new):
            live.clear()

    def new_map(self, live):
        live.update(4, 4, 0.05, 0.0, 0.0, 0.0, [0] * 16)

    def test_truth_table_only_entering_navigation_keeps_the_map(self):
        for old in (None, 'STANDBY', 'MAPPING', 'MANUAL', 'NAVIGATION'):
            for new in ('STANDBY', 'MAPPING', 'MANUAL', 'NAVIGATION'):
                if old != new:
                    self.assertEqual(map_clear_needed(old, new), new != 'NAVIGATION', (old, new))

    def test_latched_map_that_arrives_before_the_mode_flips_to_navigation_survives(self):
        live = LiveMap()
        self.new_map(live)                                  # map_server ส่งครั้งเดียวตอนเปิด ก่อนโหมดจะเป็น NAVIGATION
        self.node_logic(live, 'STANDBY', 'NAVIGATION')      # แล้วโหมดค่อยเปลี่ยน
        self.assertTrue(live.meta()['available'])

    def test_leaving_navigation_forgets_the_map_so_the_next_run_cannot_show_a_stale_one(self):
        live = LiveMap()
        self.new_map(live)
        self.node_logic(live, 'NAVIGATION', 'STANDBY')
        self.assertFalse(live.meta()['available'])

    def test_a_slam_map_never_leaks_into_other_modes(self):
        live = LiveMap()
        self.new_map(live)
        self.node_logic(live, 'MAPPING', 'STANDBY')         # ออกจาก Mapping
        self.assertFalse(live.meta()['available'])
        self.new_map(live)
        self.node_logic(live, 'STANDBY', 'MAPPING')         # เข้า Mapping รอบใหม่ไม่เห็นแผนที่เก่า
        self.assertFalse(live.meta()['available'])


class LiveMapTest(unittest.TestCase):

    def setUp(self):
        self.errors = []
        self.map = LiveMap(on_error=self.errors.append)

    def put(self, width=3, height=2, data=None, **kw):
        data = [0, 100, -1, 50, 0, 100] if data is None else data
        args = dict(resolution=0.05, origin_x=-1.0, origin_y=-2.0, origin_yaw=0.0)
        args.update(kw)
        return self.map.update(width, height, data=data, **args)

    def test_empty_before_first_map(self):
        self.assertEqual(self.map.meta(), {'available': False, 'version': 0})
        self.assertIsNone(self.map.png())

    def test_pixels_follow_map_saver_convention_and_image_is_flipped(self):
        self.assertTrue(self.put())
        mode, size, pixels = decode(self.map.png())
        self.assertEqual((mode, size), ('L', (3, 2)))
        # แถวแรกของ grid = ล่างสุด → อยู่แถวล่างของภาพ ; แถวที่สอง (50, 0, 100) อยู่บน
        self.assertEqual(pixels[0:3], [127, 254, 0])        # แถวบนของภาพ = grid แถวที่ 2
        self.assertEqual(pixels[3:6], [254, 0, 205])        # แถวล่างของภาพ = grid แถวแรก
        self.assertEqual(self.errors, [])

    def test_meta_has_geometry_and_counts(self):
        self.put(origin_x=-3.59, origin_y=-5.23, origin_yaw=0.5, stamp=1234.5)
        m = self.map.meta()
        self.assertTrue(m['available'])
        self.assertEqual((m['width'], m['height'], m['resolution']), (3, 2, 0.05))
        self.assertEqual(m['origin'], {'x': -3.59, 'y': -5.23, 'yaw': 0.5})
        self.assertEqual(m['size_m'], {'w': 0.15, 'h': 0.1})
        self.assertEqual(m['known_cells'], 5)               # มี -1 อยู่ 1 ช่อง
        self.assertEqual(m['stamp'], 1234.5)
        self.assertEqual(m['version'], 1)

    def test_float32_noise_from_ros_is_rounded_for_display(self):
        import numpy as np
        self.put(resolution=float(np.float32(0.05)), origin_x=float(np.float32(-3.59)),
                 origin_y=float(np.float32(-5.23)), origin_yaw=float(np.float32(0.1)))
        m = self.map.meta()
        self.assertEqual(m['resolution'], 0.05)
        self.assertEqual(m['origin'], {'x': -3.59, 'y': -5.23, 'yaw': 0.1})
        self.assertEqual(m['size_m'], {'w': 0.15, 'h': 0.1})

    def test_version_increases_on_every_update_and_clear(self):
        self.put()
        self.put()
        self.assertEqual(self.map.meta()['version'], 2)
        self.map.clear()
        self.assertEqual(self.map.meta(), {'available': False, 'version': 3})
        self.assertIsNone(self.map.png())

    def test_out_of_range_values_are_clamped_or_unknown(self):
        self.put(data=[120, 0, -5, 100, 1, 99])
        _, _, pixels = decode(self.map.png())
        top, bottom = pixels[0:3], pixels[3:6]
        self.assertEqual(top, [0, 251, 3])                  # 100 → 0 ; 1 → 251 ; 99 → 3
        self.assertEqual(bottom, [0, 254, 205])             # 120 ถูกจำกัดที่ 100 → 0 ; -5 = ไม่รู้

    def test_bad_input_is_rejected_and_old_map_is_kept(self):
        self.put()
        good = self.map.png()
        bad_cases = [
            dict(width=0, height=2, data=[]),
            dict(width=3, height=-1, data=[]),
            dict(width=3, height=2, data=[0] * 5),                        # ขนาดไม่ตรง
            dict(width=3, height=2, data=[0] * 6, resolution=0.0),
            dict(width=3, height=2, data=[0] * 6, resolution=-0.05),
            dict(width=3, height=2, data=[0] * 6, resolution=float('nan')),
            dict(width=3, height=2, data=['a'] * 6),
            dict(width=3, height=2, data=None),
        ]
        for case in bad_cases:
            kw = dict(resolution=0.05, origin_x=0, origin_y=0, origin_yaw=0)
            kw.update(case)
            self.assertFalse(self.map.update(**kw), case)
        self.assertEqual(len(self.errors), len(bad_cases))
        self.assertEqual(self.map.png(), good)                # แผนที่เดิมไม่เสีย
        self.assertEqual(self.map.meta()['version'], 1)

    def test_oversized_map_is_rejected(self):
        with mock.patch.object(live_map, 'MAX_CELLS', 5):
            self.assertFalse(self.put())
        self.assertIn('ใหญ่เกินไป', self.errors[0])

    def test_realistic_size_roundtrip(self):
        w, h = 239, 238                                      # ขนาดแผนที่ factory_ekf
        data = [(-1 if (i % w) < 10 else (100 if i % 7 == 0 else 0)) for i in range(w * h)]
        self.assertTrue(self.map.update(w, h, 0.05, -3.59, -5.23, 0.0, data))
        _, size, pixels = decode(self.map.png())
        self.assertEqual(size, (w, h))
        self.assertEqual(self.map.meta()['size_m'], {'w': 11.95, 'h': 11.9})
        self.assertEqual(pixels.count(205), 10 * h)

    def test_concurrent_update_and_read(self):
        stop = threading.Event()
        errors = []

        def reader():
            while not stop.is_set():
                try:
                    meta = self.map.meta()
                    png = self.map.png()
                    if meta['available'] and png is None and meta['version'] % 2:
                        pass
                except Exception as e:      # noqa: BLE001
                    errors.append(e)
        t = threading.Thread(target=reader)
        t.start()
        for _ in range(200):
            self.put()
        stop.set()
        t.join()
        self.assertEqual(errors, [])


if __name__ == '__main__':
    unittest.main()
