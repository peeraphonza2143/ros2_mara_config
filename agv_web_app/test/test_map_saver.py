"""
ทดสอบ MapSaver (บันทึกแผนที่) ด้วยตัวแทน map_saver_cli ที่เป็น process จริง — ไม่ต้องมี ROS

รัน:  cd ~/ros2_ws/src/agv_web_app && python3 -m unittest discover -s test -v
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

import yaml

from agv_web_app.map_saver import MapSaver, MapSaverError

FAKE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fake_map_saver.py')


class SaverBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='mapsave_')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.dir = os.path.join(self.tmp, 'maps')
        self.errors = []

    def make(self, mode='ok', timeout=10.0):
        return MapSaver(self.dir, command=[sys.executable, FAKE, mode], timeout=timeout, on_error=self.errors.append)

    def files(self):
        return sorted(os.listdir(self.dir)) if os.path.isdir(self.dir) else []


class NameTest(SaverBase):

    def test_valid_names(self):
        for name in ('a', 'factory_ekf', 'Room-2', 'map01', '0abc', 'A' * 48, 'x_y-z'):
            self.assertTrue(MapSaver.valid_name(name), name)

    def test_invalid_names_never_reach_the_filesystem(self):
        saver = self.make()
        bad = ['', ' ', '.', '..', '../x', 'a/b', '/etc/passwd', 'a b', 'a.b', '-abc', '_abc', 'ก', 'map\n', 'a' * 49,
               'a\x00b', 'a;rm', 'a$(x)', '~', None, 5, ['a'], 'a..b', 'x.yaml']
        for name in bad:
            with self.assertRaises(MapSaverError, msg=repr(name)) as ctx:
                saver.save(name)
            self.assertEqual(ctx.exception.code, 'map.err.name', repr(name))
        self.assertEqual(self.files(), [])                     # ไม่มีแม้แต่โฟลเดอร์/ไฟล์ถูกสร้าง
        self.assertFalse(os.path.exists(os.path.join(self.tmp, 'x')))


class SaveTest(SaverBase):

    def test_success_creates_a_complete_map_and_nothing_else(self):
        info = self.make().save('room1', meta={'size_m': {'w': 5.0, 'h': 4.0}})
        self.assertEqual(self.files(), ['room1.json', 'room1.pgm', 'room1.yaml'])      # ไม่มีโฟลเดอร์ชั่วคราวค้าง
        self.assertEqual(info['name'], 'room1')
        self.assertEqual(info['yaml'], os.path.join(self.dir, 'room1.yaml'))
        self.assertGreater(info['bytes'], 0)
        data = yaml.safe_load(open(info['yaml']))
        # Nav2 อ่านได้ (ภาพอยู่ข้างไฟล์ yaml)
        self.assertEqual((data['image'], data['resolution']), ('room1.pgm', 0.05))
        meta = json.load(open(os.path.join(self.dir, 'room1.json')))
        self.assertEqual((meta['name'], meta['size_m']), ('room1', {'w': 5.0, 'h': 4.0}))
        self.assertAlmostEqual(meta['saved'], time.time(), delta=5)

    def test_creates_the_maps_folder_when_missing(self):
        self.assertFalse(os.path.exists(self.dir))
        self.make().save('a')
        self.assertTrue(os.path.isdir(self.dir))

    def test_existing_name_is_refused_without_overwrite_and_untouched(self):
        saver = self.make()
        saver.save('room1', meta={'v': 1})
        before = {f: open(os.path.join(self.dir, f), 'rb').read() for f in self.files()}
        with self.assertRaises(MapSaverError) as ctx:
            saver.save('room1')
        self.assertEqual((ctx.exception.code, ctx.exception.params), ('map.err.exists', {'name': 'room1'}))
        self.assertEqual({f: open(os.path.join(self.dir, f), 'rb').read() for f in self.files()}, before)

    def test_overwrite_replaces_all_three_files(self):
        saver = self.make()
        saver.save('room1', meta={'v': 1})
        saver.save('room1', overwrite=True, meta={'v': 2})
        self.assertEqual(json.load(open(self.path('room1.json')))['v'], 2)
        self.assertEqual(self.files(), ['room1.json', 'room1.pgm', 'room1.yaml'])

    def path(self, f):
        return os.path.join(self.dir, f)

    def test_failed_overwrite_keeps_the_previous_map_intact(self):
        self.make().save('room1', meta={'v': 1})
        before = {f: open(self.path(f), 'rb').read() for f in self.files()}
        for mode in ('fail', 'empty', 'bad_yaml', 'no_files', 'partial'):
            with self.assertRaises(MapSaverError, msg=mode):
                self.make(mode).save('room1', overwrite=True)
            self.assertEqual({f: open(self.path(f), 'rb').read() for f in self.files()}, before, mode)

    def test_failures_leave_no_files_behind(self):
        for mode, code in (('fail', 'map.err.save_failed'), ('empty', 'map.err.save_failed'),
                           ('bad_yaml', 'map.err.save_failed'), ('no_files', 'map.err.save_failed'),
                           ('partial', 'map.err.save_failed')):
            with self.assertRaises(MapSaverError, msg=mode) as ctx:
                self.make(mode).save('new1')
            self.assertEqual(ctx.exception.code, code, mode)
            self.assertEqual(self.files(), [], mode)

    def test_failure_detail_comes_from_the_tool_output(self):
        with self.assertRaises(MapSaverError) as ctx:
            self.make('fail').save('x')
        self.assertIn('Failed to spin map subscription', ctx.exception.params['detail'])
        self.assertTrue(self.errors)

    def test_timeout(self):
        t0 = time.time()
        with self.assertRaises(MapSaverError) as ctx:
            self.make('slow', timeout=0.5).save('x')
        self.assertEqual((ctx.exception.code, ctx.exception.params), ('map.err.timeout', {'seconds': 0}))
        self.assertLess(time.time() - t0, 5)
        self.assertEqual(self.files(), [])

    def test_missing_tool(self):
        saver = MapSaver(self.dir, command=['/no/such/tool'])
        with self.assertRaises(MapSaverError) as ctx:
            saver.save('x')
        self.assertEqual(ctx.exception.code, 'map.err.no_saver')
        self.assertEqual(self.files(), [])

    def test_second_save_while_busy_is_refused(self):
        saver = MapSaver(self.dir, command=[sys.executable, FAKE, 'slow'], timeout=3.0)
        results = []
        t = threading.Thread(target=lambda: results.append(self.try_save(saver, 'a')))
        t.start()
        time.sleep(0.5)
        with self.assertRaises(MapSaverError) as ctx:
            saver.save('b')
        self.assertEqual(ctx.exception.code, 'map.err.busy')
        t.join()
        self.assertEqual(results, ['map.err.timeout'])
        self.make().save('c')                                    # ว่างแล้วบันทึกได้ตามปกติ

    @staticmethod
    def try_save(saver, name):
        try:
            saver.save(name)
        except MapSaverError as e:
            return e.code
        return 'ok'

    def test_busy_flag_is_released_after_failure(self):
        saver = self.make('fail')
        for _ in range(3):
            with self.assertRaises(MapSaverError) as ctx:
                saver.save('x')
            self.assertEqual(ctx.exception.code, 'map.err.save_failed')


class RetryTest(SaverBase):

    def test_flaky_tool_succeeds_on_the_second_attempt(self):
        info = MapSaver(self.dir, command=[sys.executable, FAKE, 'flaky_once'], timeout=10.0,
                        on_error=self.errors.append).save('a')
        self.assertEqual(info['name'], 'a')
        self.assertEqual(open(os.path.join(self.tmp, 'attempts.txt')).read(), '2')
        self.assertEqual(len(self.errors), 1)                       # รายงานความล้มเหลวครั้งแรกไว้ใน log
        self.assertEqual(self.files(), ['a.json', 'a.pgm', 'a.yaml'])

    def test_persistent_failure_is_tried_exactly_twice_then_reported(self):
        saver = MapSaver(self.dir, command=[sys.executable, FAKE, 'fail'], on_error=self.errors.append)
        with self.assertRaises(MapSaverError) as ctx:
            saver.save('a')
        self.assertEqual(ctx.exception.code, 'map.err.save_failed')
        self.assertEqual(len(self.errors), 2)
        self.assertEqual(self.files(), [])

    def test_timeout_is_not_retried(self):
        t0 = time.time()
        with self.assertRaises(MapSaverError):
            self.make('slow', timeout=0.5).save('x')
        self.assertLess(time.time() - t0, 1.5)

    def test_default_command_gets_a_longer_wait_for_the_map(self):
        from agv_web_app import map_saver
        saver = MapSaver(self.dir)
        self.assertEqual(saver.command, list(map_saver.DEFAULT_COMMAND))
        self.assertIn('save_map_timeout:=8.0', saver.extra_args)
        self.assertEqual(saver.extra_args[0], '--ros-args')           # ต้องอยู่ท้ายคำสั่ง (ข้อกำหนดของ map_saver_cli)

    def test_custom_command_gets_no_extra_args_unless_given(self):
        self.assertEqual(MapSaver(self.dir, command=['x']).extra_args, [])
        self.assertEqual(MapSaver(self.dir, command=['x'], extra_args=['-y']).extra_args, ['-y'])

    def test_extra_args_follow_the_output_prefix(self):
        MapSaver(self.dir, command=[sys.executable, FAKE, 'ok_args'], extra_args=['--ros-args', '-p', 'a:=1']).save('m')
        args = open(os.path.join(self.tmp, 'args.txt')).read()
        self.assertRegex(args, r'^ok_args -f \S+/m --ros-args -p a:=1$')


class ListTest(SaverBase):

    def test_list_newest_first_with_meta(self):
        saver = self.make()
        saver.save('old', meta={'k': 1})
        time.sleep(0.05)
        saver.save('new')
        names = [m['name'] for m in saver.list()]
        self.assertEqual(names, ['new', 'old'])
        self.assertEqual(saver.list()[1]['meta']['k'], 1)
        self.assertGreater(saver.list()[0]['bytes'], 0)

    def test_list_ignores_incomplete_or_foreign_files(self):
        saver = self.make()
        saver.save('ok1')
        open(self.path('half.yaml'), 'w').write('x')              # มี yaml แต่ไม่มี pgm
        open(self.path('bad name.yaml'), 'w').write('x')
        open(self.path('readme.txt'), 'w').write('x')
        self.assertEqual([m['name'] for m in saver.list()], ['ok1'])

    def path(self, f):
        return os.path.join(self.dir, f)

    def test_list_without_folder_is_empty(self):
        self.assertEqual(self.make().list(), [])

    def test_exists(self):
        saver = self.make()
        self.assertFalse(saver.exists('a'))
        saver.save('a')
        self.assertTrue(saver.exists('a'))
        self.assertFalse(saver.exists('../a'))


if __name__ == '__main__':
    unittest.main()
