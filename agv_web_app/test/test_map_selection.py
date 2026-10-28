"""
ทดสอบ MapSelection (จำแผนที่ที่เลือกไว้ใช้กับ Navigation) — ไม่ต้องมี ROS

รัน:  cd ~/ros2_ws/src/agv_web_app && python3 -m unittest discover -s test -v
"""
import os
import shutil
import tempfile
import unittest
from unittest import mock

from agv_web_app.map_saver import MapSaver, MapSaverError
from agv_web_app.map_selection import MapSelection
from test_map_library import write_map


class SelBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='mapsel_')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.maps = os.path.join(self.tmp, 'maps')
        self.file = os.path.join(self.tmp, 'nav', 'selected_map.json')
        self.warnings = []
        self.saver = MapSaver(self.maps)
        write_map(self.maps, 'room1')
        write_map(self.maps, 'room2')

    def make(self, fallback=None):
        return MapSelection(self.file, self.saver, fallback=fallback, on_error=self.warnings.append)

    def code(self, call, *args):
        with self.assertRaises(MapSaverError) as ctx:
            call(*args)
        return ctx.exception.code


class SelectTest(SelBase):

    def test_nothing_selected_means_launch_default(self):
        sel = self.make()
        self.assertIsNone(sel.name())
        self.assertEqual(sel.choice(), {'source': 'default', 'name': None, 'file': None})
        self.assertEqual(sel.launch_args(), ([], sel.choice()))                  # ไม่ส่ง map_file:= เลย

    def test_select_is_remembered_and_becomes_the_launch_argument(self):
        sel = self.make()
        sel.select('room2')
        self.assertEqual(sel.name(), 'room2')
        yaml_path = os.path.join(self.maps, 'room2.yaml')
        args, choice = sel.launch_args()
        self.assertEqual(args, [f'map_file:={yaml_path}'])
        self.assertEqual(choice, {'source': 'selected', 'name': 'room2', 'file': yaml_path})
        self.assertEqual(os.listdir(os.path.dirname(self.file)), ['selected_map.json'])    # ไม่มีไฟล์ .tmp ค้าง

    def test_selection_survives_a_restart(self):
        self.make().select('room1')
        self.assertEqual(self.make().name(), 'room1')

    def test_path_with_spaces_stays_one_argument(self):
        spaced = os.path.join(self.tmp, 'my maps')
        write_map(spaced, 'room1')
        sel = MapSelection(self.file, MapSaver(spaced))
        sel.select('room1')
        args, _ = sel.launch_args()
        self.assertEqual(args, [f'map_file:={spaced}/room1.yaml'])

    def test_bad_names_are_refused_and_write_nothing(self):
        sel = self.make()
        for bad in ('', '..', '../room1', 'a/b', 'a b', 'room1.yaml', 'ก', 'a' * 49, 5, ['room1'], {'a': 1}):
            self.assertEqual(self.code(sel.select, bad), 'map.err.name', repr(bad))
        self.assertFalse(os.path.exists(os.path.dirname(self.file)))

    def test_missing_or_incomplete_map_is_refused_and_keeps_the_old_choice(self):
        sel = self.make()
        sel.select('room1')
        with self.assertRaises(MapSaverError) as ctx:
            sel.select('nope')
        self.assertEqual((ctx.exception.code, ctx.exception.params), ('map.err.not_found', {'name': 'nope'}))
        os.remove(os.path.join(self.maps, 'room2.pgm'))                           # เหลือแต่ .yaml/.json
        self.assertEqual(self.code(sel.select, 'room2'), 'map.err.not_found')
        self.assertEqual(sel.name(), 'room1')

    def test_clear_goes_back_to_default_and_is_idempotent(self):
        sel = self.make()
        sel.select('room1')
        sel.select(None)
        self.assertIsNone(sel.name())
        self.assertEqual(sel.choice()['source'], 'default')
        sel.select(None)                                                           # ยกเลิกซ้ำไม่ล้ม
        self.assertFalse(os.path.exists(self.file))

    def test_write_failure_keeps_the_old_choice_and_leaves_no_tmp(self):
        sel = self.make()
        sel.select('room1')
        with mock.patch('agv_web_app.map_selection.os.replace', side_effect=OSError('disk full')):
            self.assertEqual(self.code(sel.select, 'room2'), 'map.err.io')
        self.assertEqual(sel.name(), 'room1')
        self.assertEqual(os.listdir(os.path.dirname(self.file)), ['selected_map.json'])


class PriorityTest(SelBase):

    def test_config_fallback_when_nothing_selected(self):
        cfg = os.path.join(self.tmp, 'cfg', 'fixed.yaml')
        sel = self.make(fallback=cfg)
        self.assertEqual(sel.choice(), {'source': 'config', 'name': None, 'file': cfg})
        # ไม่ตรวจว่ามีไฟล์ (พฤติกรรมเดิมของ nav_map_file)
        self.assertEqual(sel.launch_args()[0], [f'map_file:={cfg}'])

    def test_selected_beats_config_and_exactly_one_map_file_argument(self):
        sel = self.make(fallback='/x/fixed.yaml')
        sel.select('room1')
        args, choice = sel.launch_args()
        self.assertEqual(choice['source'], 'selected')
        self.assertEqual(len(args), 1)                                             # ห้ามมี map_file:= ซ้ำสองตัว
        self.assertEqual(args[0], f'map_file:={os.path.join(self.maps, "room1.yaml")}')

    def test_clearing_returns_to_config(self):
        sel = self.make(fallback='/x/fixed.yaml')
        sel.select('room1')
        sel.select(None)
        self.assertEqual(sel.choice()['source'], 'config')

    def test_selected_map_deleted_by_hand_falls_back_with_one_warning(self):
        sel = self.make(fallback='/x/fixed.yaml')
        sel.select('room1')
        os.remove(os.path.join(self.maps, 'room1.yaml'))
        args, choice = sel.launch_args()
        self.assertEqual((args, choice['source']), (['map_file:=/x/fixed.yaml'], 'config'))
        self.assertEqual(len(self.warnings), 1)
        self.assertIn('room1', self.warnings[0])
        sel.choice()
        sel.info()                                           # อ่านสถานะซ้ำๆ (หน้าเว็บ poll) ต้องไม่ท่วม log
        self.assertEqual(len(self.warnings), 1)

    def test_selected_map_deleted_by_hand_without_config_uses_default(self):
        sel = self.make()
        sel.select('room1')
        shutil.rmtree(self.maps)
        self.assertEqual(sel.launch_args()[0], [])


class CorruptFileTest(SelBase):

    def test_unreadable_selection_files_mean_no_selection(self):
        sel = self.make()
        os.makedirs(os.path.dirname(self.file))
        for text in ('', '{bad', '[]', 'null', '"room1"', '{}', '{"name": null}', '{"name": 5}', '{"name": "../room1"}',
                     '{"name": "a b"}'):
            with open(self.file, 'w') as f:
                f.write(text)
            self.assertIsNone(sel.name(), text)
            self.assertEqual(sel.choice()['source'], 'default', text)
            self.assertEqual(sel.launch_args()[0], [], text)


class InfoTest(SelBase):

    def test_info_shape_when_nothing_selected(self):
        self.assertEqual(self.make().info(), {'name': None, 'available': False,
                                              'choice': {'source': 'default', 'name': None, 'file': None},
                                              'in_use': None, 'restart_needed': False})

    def test_available_turns_false_when_the_map_disappears(self):
        sel = self.make()
        sel.select('room1')
        self.assertTrue(sel.info()['available'])
        os.remove(os.path.join(self.maps, 'room1.pgm'))
        info = sel.info()
        self.assertEqual((info['name'], info['available'], info['choice']['source']), ('room1', False, 'default'))

    def test_restart_needed_only_when_nav_runs_with_a_different_map(self):
        sel = self.make()
        sel.select('room1')
        running_room1 = sel.choice()
        self.assertFalse(sel.info(in_use=running_room1)['restart_needed'])        # ตรงกัน
        self.assertFalse(sel.info(in_use=None)['restart_needed'])                 # Nav ไม่ได้รัน
        sel.select('room2')
        info = sel.info(in_use=running_room1)
        self.assertTrue(info['restart_needed'])                                    # เลือกใหม่ระหว่างที่ Nav รันอยู่
        self.assertEqual(info['in_use'], running_room1)
        sel.select(None)
        # ยกเลิกการเลือก = ครั้งหน้ากลับไป default → ไม่ตรงกับที่รันอยู่
        self.assertTrue(sel.info(in_use=running_room1)['restart_needed'])


if __name__ == '__main__':
    unittest.main()
