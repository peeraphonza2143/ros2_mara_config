"""
ทดสอบคลังแผนที่ของ MapSaver: rename / delete / thumbnail (ไฟล์แผนที่จริงรูปแบบเดียวกับ map_saver_cli — ไม่ต้องมี ROS)

รัน:  cd ~/ros2_ws/src/agv_web_app && python3 -m unittest discover -s test -v
"""
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import yaml
from PIL import Image

from agv_web_app.map_saver import MapSaver, MapSaverError


def write_map(maps_dir, name, size=(600, 300), meta=True, mtime=None):
    """สร้างแผนที่หน้าตาเหมือนที่ map_saver_cli + MapSaver.save() สร้าง (trinary: 0 ชน / 254 ว่าง / 205 ไม่รู้)"""
    os.makedirs(maps_dir, exist_ok=True)
    img = Image.new('L', size, 205)
    img.paste(254, (0, 0, size[0] // 2, size[1]))
    # ผนังกว้าง 1 พิกเซลที่ x=300 (ย่อ 600→240 แบบ NEAREST จะข้ามพิกเซลนี้ไป)
    img.paste(0, (size[0] // 2, 0, size[0] // 2 + 1, size[1]))
    img.save(os.path.join(maps_dir, name + '.pgm'))
    with open(os.path.join(maps_dir, name + '.yaml'), 'w') as f:
        f.write(f'image: {name}.pgm\nmode: trinary\nresolution: 0.05\norigin: [-3.04, -5.98, 0]\nnegate: 0\n'
                'occupied_thresh: 0.65\nfree_thresh: 0.25')
    if meta:
        with open(os.path.join(maps_dir, name + '.json'), 'w') as f:
            json.dump({'name': name, 'saved': 1793104325.3, 'resolution': 0.05, 'size_m': {'w': 30.0, 'h': 15.0},
                       'slam': {'resolution': 0.05}}, f)
    if mtime is not None:
        os.utime(os.path.join(maps_dir, name + '.yaml'), (mtime, mtime))


class LibBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='maplib_')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.dir = os.path.join(self.tmp, 'maps')
        self.errors = []
        self.saver = MapSaver(self.dir, on_error=self.errors.append)

    def files(self):
        return sorted(os.listdir(self.dir)) if os.path.isdir(self.dir) else []

    def read(self, name):
        with open(os.path.join(self.dir, name), 'rb') as f:
            return f.read()

    def code(self, call, *args):
        with self.assertRaises(MapSaverError) as ctx:
            call(*args)
        return ctx.exception.code


class RenameTest(LibBase):

    def test_rename_moves_the_whole_set_and_fixes_the_image_field(self):
        write_map(self.dir, 'old1', mtime=1700000000)
        pgm = self.read('old1.pgm')
        self.assertEqual(self.saver.rename('old1', 'new1'), {'name': 'new1'})
        self.assertEqual(self.files(), ['new1.json', 'new1.pgm', 'new1.yaml'])            # ไม่มีของเดิม/ชั่วคราวค้าง
        data = yaml.safe_load(self.read('new1.yaml'))
        self.assertEqual(data['image'], 'new1.pgm')                                       # map_server ต้องหาภาพเจอ
        self.assertTrue(os.path.exists(os.path.join(self.dir, data['image'])))
        self.assertEqual((data['resolution'], data['origin'], data['mode']), (0.05, [-3.04, -5.98, 0], 'trinary'))
        self.assertEqual(self.read('new1.pgm'), pgm)

    def test_rename_updates_name_in_json_and_keeps_everything_else(self):
        write_map(self.dir, 'old1')
        self.saver.rename('old1', 'new1')
        info = json.loads(self.read('new1.json'))
        self.assertEqual(info['name'], 'new1')
        self.assertEqual((info['saved'], info['size_m'], info['slam']),
                         (1793104325.3, {'w': 30.0, 'h': 15.0}, {'resolution': 0.05}))

    def test_rename_keeps_the_saved_date_so_the_library_order_does_not_change(self):
        write_map(self.dir, 'a', mtime=1700000100)
        write_map(self.dir, 'b', mtime=1700000200)
        self.assertEqual([m['name'] for m in self.saver.list()], ['b', 'a'])
        self.saver.rename('a', 'zzz')
        listed = self.saver.list()
        self.assertEqual([m['name'] for m in listed], ['b', 'zzz'])
        self.assertAlmostEqual(listed[1]['saved'], 1700000100, delta=1)

    def test_rename_works_when_json_is_missing_or_corrupt(self):
        write_map(self.dir, 'nojson', meta=False)
        self.saver.rename('nojson', 'fixed1')
        self.assertEqual(json.loads(self.read('fixed1.json')), {'name': 'fixed1'})
        write_map(self.dir, 'bad')
        with open(os.path.join(self.dir, 'bad.json'), 'w') as f:
            f.write('{not json')
        self.saver.rename('bad', 'fixed2')
        self.assertEqual(json.loads(self.read('fixed2.json')), {'name': 'fixed2'})

    def test_rename_refuses_bad_names_and_never_touches_the_filesystem(self):
        write_map(self.dir, 'room1')
        before = {f: self.read(f) for f in self.files()}
        bad = ['', ' ', '.', '..', '../x', 'a/b', '/etc/x', 'a b', 'a.b', '-a', '_a', 'ก', 'a\n', 'a' * 49, 'a\x00b',
               'x.yaml', None, 5, ['a']]
        for name in bad:
            self.assertEqual(self.code(self.saver.rename, 'room1', name), 'map.err.name', repr(name))
            self.assertEqual(self.code(self.saver.rename, name, 'room2'), 'map.err.name', repr(name))
        self.assertEqual({f: self.read(f) for f in self.files()}, before)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, 'x')))

    def test_rename_to_same_name_missing_source_and_clash(self):
        write_map(self.dir, 'a')
        write_map(self.dir, 'b')
        before = {f: self.read(f) for f in self.files()}
        self.assertEqual(self.code(self.saver.rename, 'a', 'a'), 'map.err.same_name')
        self.assertEqual(self.code(self.saver.rename, 'nope', 'c'), 'map.err.not_found')
        with self.assertRaises(MapSaverError) as ctx:
            self.saver.rename('a', 'b')
        self.assertEqual((ctx.exception.code, ctx.exception.params), ('map.err.exists', {'name': 'b'}))
        self.assertEqual({f: self.read(f) for f in self.files()}, before)             # ของเดิมทั้งคู่ไม่ถูกแตะ

    def test_rename_refuses_an_incomplete_source(self):
        write_map(self.dir, 'half')
        os.remove(os.path.join(self.dir, 'half.pgm'))                                  # มีแต่ .yaml/.json
        self.assertEqual(self.code(self.saver.rename, 'half', 'other'), 'map.err.not_found')

    def test_failure_midway_leaves_the_original_complete_and_no_litter(self):
        write_map(self.dir, 'keep')
        before = {f: self.read(f) for f in self.files()}
        real = os.replace

        def flaky(src, dst, *a, **k):
            if dst.endswith('new1.yaml'):
                raise OSError('disk full')
            return real(src, dst, *a, **k)

        with mock.patch('agv_web_app.map_saver.os.replace', side_effect=flaky):
            self.assertEqual(self.code(self.saver.rename, 'keep', 'new1'), 'map.err.io')
        self.assertEqual({f: self.read(f) for f in self.files()}, before)      # ของใหม่ที่วางไปแล้วถูกเก็บกวาด
        self.assertEqual([m['name'] for m in self.saver.list()], ['keep'])

    def test_failure_to_remove_old_files_still_gives_a_complete_new_map(self):
        write_map(self.dir, 'old1')
        real = os.remove

        def flaky(path, *a, **k):
            if path.endswith('old1.pgm'):
                raise OSError('busy')
            return real(path, *a, **k)

        with mock.patch('agv_web_app.map_saver.os.remove', side_effect=flaky):
            self.assertEqual(self.saver.rename('old1', 'new1'), {'name': 'new1'})
        self.assertIn('new1.yaml', self.files())
        self.assertTrue(any('old1.pgm' in e for e in self.errors))                    # แจ้งผ่าน on_error ไม่ล้มทั้งงาน

    def test_busy_while_another_operation_is_running(self):
        write_map(self.dir, 'a')
        with self.saver._busy:
            self.assertEqual(self.code(self.saver.rename, 'a', 'b'), 'map.err.busy')
            self.assertEqual(self.code(self.saver.delete, 'a'), 'map.err.busy')
        self.assertEqual(self.files(), ['a.json', 'a.pgm', 'a.yaml'])
        self.saver.rename('a', 'b')                                            # ปล่อยแล้วต้องใช้ได้ (ไม่ล็อกค้าง)


class DeleteTest(LibBase):

    def test_delete_moves_the_set_to_trash_and_hides_it_from_the_list(self):
        write_map(self.dir, 'gone')
        write_map(self.dir, 'stay')
        result = self.saver.delete('gone')
        self.assertEqual(result['name'], 'gone')
        self.assertEqual([m['name'] for m in self.saver.list()], ['stay'])
        self.assertFalse(self.saver.exists('gone'))
        # ไฟล์ครบชุดในถังขยะ = กู้คืนด้วยมือได้
        self.assertEqual(sorted(os.listdir(result['trash'])), ['gone.json', 'gone.pgm', 'gone.yaml'])
        self.assertTrue(result['trash'].startswith(os.path.join(self.dir, '.trash')))
        self.assertEqual(sorted(f for f in self.files() if f.startswith('gone')), [])

    def test_delete_then_same_name_can_be_saved_again(self):
        write_map(self.dir, 'again')
        self.saver.delete('again')
        write_map(self.dir, 'again')
        self.saver.delete('again')                                                     # ถังขยะต้องรับชื่อซ้ำซ้อนได้
        self.assertEqual(len(os.listdir(os.path.join(self.dir, '.trash'))), 2)

    def test_delete_refuses_bad_and_missing_names(self):
        write_map(self.dir, 'room1')
        before = self.files()
        for name in ('', '..', '../room1', 'a/b', 'room1.yaml', None, 5):
            self.assertEqual(self.code(self.saver.delete, name), 'map.err.name', repr(name))
        with self.assertRaises(MapSaverError) as ctx:
            self.saver.delete('nope')
        self.assertEqual((ctx.exception.code, ctx.exception.params), ('map.err.not_found', {'name': 'nope'}))
        self.assertEqual(self.files(), before)
        self.assertFalse(os.path.exists(os.path.join(self.dir, '.trash')))

    def test_delete_failure_is_reported(self):
        write_map(self.dir, 'a')
        with mock.patch('agv_web_app.map_saver.os.replace', side_effect=OSError('read-only')):
            self.assertEqual(self.code(self.saver.delete, 'a'), 'map.err.io')
        self.assertTrue(self.saver.exists('a'))


class ThumbnailTest(LibBase):

    def png(self, name):
        data = self.saver.thumbnail(name)
        self.assertTrue(data.startswith(b'\x89PNG\r\n\x1a\n'))
        return Image.open(io.BytesIO(data))

    def test_large_map_is_shrunk_keeping_aspect_ratio(self):
        write_map(self.dir, 'big', size=(600, 300))
        img = self.png('big')
        self.assertEqual(img.size, (240, 120))
        self.assertEqual(img.mode, 'L')

    def test_small_map_is_not_enlarged(self):
        write_map(self.dir, 'tiny', size=(40, 20))
        self.assertEqual(self.png('tiny').size, (40, 20))

    def test_free_and_unknown_areas_stay_distinguishable(self):
        write_map(self.dir, 'big', size=(600, 300))
        img = self.png('big')
        self.assertGreater(img.getpixel((10, 60)), 240)                                # ซีกซ้าย = ว่าง (ขาว)
        self.assertLess(abs(img.getpixel((230, 60)) - 205), 10)                        # ซีกขวา = ไม่รู้ (เทา)

    def test_thin_walls_survive_downscaling(self):
        write_map(self.dir, 'big', size=(600, 300))                                    # ผนังกว้าง 1 พิกเซล
        row = [self.png('big').getpixel((x, 60)) for x in range(240)]
        self.assertLess(min(row), 200)                         # ถ้าย่อแบบข้ามพิกเซล เส้นจะหายเป็นขาว/เทา

    def test_bad_missing_and_corrupt(self):
        self.assertEqual(self.code(self.saver.thumbnail, '../x'), 'map.err.name')
        self.assertEqual(self.code(self.saver.thumbnail, 'nope'), 'map.err.not_found')
        write_map(self.dir, 'broken')
        with open(os.path.join(self.dir, 'broken.pgm'), 'wb') as f:
            f.write(b'not an image')
        self.assertEqual(self.code(self.saver.thumbnail, 'broken'), 'map.err.io')


if __name__ == '__main__':
    unittest.main()
