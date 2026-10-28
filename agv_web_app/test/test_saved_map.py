"""
ทดสอบการอ่านแผนที่ที่บันทึกไว้ (saved_map) — แปลงพิกเซลเป็นตารางความว่างตามกติกา nav2_map_server + แคช

รัน:  cd ~/ros2_ws/src/agv_web_app && python3 -m unittest discover -s test -v
"""
import os
import shutil
import tempfile
import unittest

import numpy as np
from PIL import Image

from agv_web_app.map_saver import MapSaver
from agv_web_app.saved_map import SavedMapError, SavedMaps, read_grid

YAML = ('image: {name}.pgm\nresolution: 0.05\norigin: [-3.0, -2.0, 0.0]\nnegate: {negate}\n'
        'occupied_thresh: 0.65\nfree_thresh: 0.25\n')


def write_map(maps_dir, name, pixels, negate=0, yaml_text=None):
    os.makedirs(maps_dir, exist_ok=True)
    Image.fromarray(np.asarray(pixels, dtype=np.uint8), mode='L').save(os.path.join(maps_dir, name + '.pgm'))
    with open(os.path.join(maps_dir, name + '.yaml'), 'w') as f:
        f.write(yaml_text if yaml_text is not None else YAML.format(name=name, negate=negate))


class ReadGridTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='sm_')
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def grid(self, pixels, **kw):
        write_map(self.tmp, 'm', pixels, **kw)
        w, h, res, ox, oy, oyaw, data = read_grid(os.path.join(self.tmp, 'm.yaml'), os.path.join(self.tmp, 'm.pgm'))
        return w, h, res, (ox, oy, oyaw), data

    def test_trinary_thresholds_like_nav2_map_server(self):
        w, h, res, origin, data = self.grid([[254, 0, 100, 180, 205]])
        self.assertEqual((w, h, res, origin), (5, 1, 0.05, (-3.0, -2.0, 0.0)))
        # p = (255-x)/255 : 254→0.004 ว่าง · 0→1.0 ไม่ว่าง · 100→0.608 ไม่รู้ (0.25..0.65) · 180→0.294 ไม่รู้ ·
        # 205→0.196 (ต่ำกว่า free 0.25 → "ว่าง" ตามสูตร map_server) แต่แอปตั้งใจนับ 205 เป็น "ไม่รู้"
        self.assertEqual(data[0].tolist(), [0, 100, -1, -1, -1])

    def test_unknown_gray_of_map_saver_is_unknown_not_free(self):
        # แผนที่จริงของแอปมีแค่ 3 ค่า: 0 (ผนัง) · 205 (ไม่รู้ — ส่วนใหญ่ของภาพ) · 254 (ว่าง)
        _, _, _, _, data = self.grid([[0, 205, 254], [196, 214, 215]])
        self.assertEqual(data[::-1].tolist(), [[100, -1, 0], [-1, -1, 0]])             # แถวบนของภาพ = แถวท้ายของตาราง

    def test_unknown_gray_rule_does_not_apply_when_negated(self):
        _, _, _, _, data = self.grid([[205]], negate=1)
        self.assertEqual(data[0].tolist(), [100])

    def test_negate_flips_meaning(self):
        _, _, _, _, data = self.grid([[254, 0]], negate=1)
        self.assertEqual(data[0].tolist(), [100, 0])

    def test_boundary_values_are_strict_like_map_server(self):
        # map_server ใช้ p < free_thresh และ p > occupied_thresh (ไม่รวมค่าเท่ากับเกณฑ์)
        yaml_text = ('image: m.pgm\nresolution: 0.05\norigin: [0, 0, 0]\nnegate: 1\n'
                     'occupied_thresh: 0.5\nfree_thresh: 0.5\n')
        _, _, _, _, data = self.grid([[127, 128]], yaml_text=yaml_text)       # negate: p = x/255 → 0.498 / 0.502
        self.assertEqual(data[0].tolist(), [0, 100])

    def test_first_row_is_lowest_y(self):
        # ภาพแถวบน = y มาก → ตารางต้องพลิก (แถวแรกของตาราง = แถวล่างของภาพ)
        _, h, _, _, data = self.grid([[0, 0], [254, 254], [254, 254]])
        self.assertEqual(h, 3)
        self.assertEqual(data[:, 0].tolist(), [0, 0, 100])

    def test_errors_are_reported_not_raised_as_random_exceptions(self):
        write_map(self.tmp, 'bad', [[0]], yaml_text='not: a map')
        with self.assertRaises(SavedMapError):
            read_grid(os.path.join(self.tmp, 'bad.yaml'), os.path.join(self.tmp, 'bad.pgm'))
        write_map(self.tmp, 'bad2', [[0]], yaml_text='resolution: 0\norigin: [0,0,0]\n')
        with self.assertRaises(SavedMapError):
            read_grid(os.path.join(self.tmp, 'bad2.yaml'), os.path.join(self.tmp, 'bad2.pgm'))
        write_map(self.tmp, 'scale', [[0]], yaml_text='resolution: 0.05\norigin: [0,0,0]\nmode: scale\n')
        with self.assertRaises(SavedMapError):
            read_grid(os.path.join(self.tmp, 'scale.yaml'), os.path.join(self.tmp, 'scale.pgm'))
        with self.assertRaises(SavedMapError):
            read_grid(os.path.join(self.tmp, 'nope.yaml'), os.path.join(self.tmp, 'nope.pgm'))
        with open(os.path.join(self.tmp, 'junk.pgm'), 'w') as f:
            f.write('not an image')
        with open(os.path.join(self.tmp, 'junk.yaml'), 'w') as f:
            f.write(YAML.format(name='junk', negate=0))
        with self.assertRaises(SavedMapError):
            read_grid(os.path.join(self.tmp, 'junk.yaml'), os.path.join(self.tmp, 'junk.pgm'))


class SavedMapsTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='sm_')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.errors = []
        self.saver = MapSaver(self.tmp)
        self.maps = SavedMaps(self.saver, on_error=self.errors.append)

    def pixels(self):
        px = np.full((160, 200), 254)                  # 10 × 8 m ว่างทั้งหมด
        px[:, 190] = 0                                 # ผนังที่ x = -3 + 190 × 0.05 = 6.5
        return px

    def test_probe_matches_rules_and_meta_is_like_live_map(self):
        write_map(self.tmp, 'lab', self.pixels())
        live, gen = self.maps.get('lab')
        meta = live.meta()
        self.assertEqual((meta['available'], meta['width'], meta['height'], meta['resolution']), (True, 200, 160, 0.05))
        self.assertEqual((meta['origin']['x'], meta['origin']['y']), (-3.0, -2.0))
        self.assertEqual(live.probe(1.0, 1.0, 0.4)['status'], 'ok')
        self.assertEqual(live.probe(6.3, 1.0, 0.4)['status'], 'tight')
        self.assertEqual(live.probe(6.5, 1.0, 0.4)['status'], 'occupied')          # บนผนัง
        self.assertEqual(live.probe(50.0, 1.0, 0.4)['status'], 'outside')
        self.assertGreater(gen, 0)
        self.assertTrue(live.png().startswith(b'\x89PNG'))

    def test_cached_until_files_change_and_generations_are_unique_across_maps(self):
        write_map(self.tmp, 'a', self.pixels())
        write_map(self.tmp, 'b', self.pixels())
        la, ga = self.maps.get('a')
        lb, gb = self.maps.get('b')
        self.assertNotEqual(ga, gb)
        self.assertIs(self.maps.get('a')[0], la)
        self.assertEqual(self.maps.get('a')[1], ga)
        px = self.pixels()
        px[:, 100] = 0
        write_map(self.tmp, 'a', px)
        os.utime(os.path.join(self.tmp, 'a.pgm'), (1, 1))                           # เวลาแก้ไขต่างแน่นอน
        la2, ga2 = self.maps.get('a')
        self.assertIsNot(la2, la)
        self.assertGreater(ga2, gb)
        # ผนังใหม่ที่ x = -3 + 100 × 0.05 = 2.0
        self.assertEqual(la2.probe(2.0, 1.0, 0.4)['status'], 'occupied')

    def test_missing_invalid_or_corrupt_gives_none(self):
        self.assertIsNone(self.maps.get('nope'))
        self.assertIsNone(self.maps.get(None))
        self.assertIsNone(self.maps.get('../etc'))
        write_map(self.tmp, 'bad', [[0]], yaml_text='nothing')
        self.assertIsNone(self.maps.get('bad'))
        self.assertTrue(self.errors)

    def test_deleted_map_is_forgotten(self):
        write_map(self.tmp, 'lab', self.pixels())
        self.assertIsNotNone(self.maps.get('lab'))
        os.remove(os.path.join(self.tmp, 'lab.pgm'))
        self.assertIsNone(self.maps.get('lab'))

    def test_cache_is_bounded(self):
        for i in range(6):
            write_map(self.tmp, f'm{i}', [[254]])
            self.assertIsNotNone(self.maps.get(f'm{i}'))
        self.assertLessEqual(len(self.maps._cache), 3)


if __name__ == '__main__':
    unittest.main()
