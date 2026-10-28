"""
ทดสอบ SlamParams (ค่า SLAM ที่ปรับได้ + Default + ไฟล์ที่ส่งให้ launch) — ไม่ต้องมี ROS

รัน:  cd ~/ros2_ws/src/agv_web_app && python3 -m unittest discover -s test -v
"""
import hashlib
import json
import math
import os
import re
import shutil
import tempfile
import unittest
from unittest import mock

import yaml

from agv_web_app import slam_params
from agv_web_app.slam_params import GROUPS, SCHEMA, SlamParamError, SlamParams

PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FACTORY = os.path.join(os.path.dirname(PKG_ROOT), 'agv_s2_bringup', 'config', 'mapper_params_s2_factory.yaml')


def sha(path):
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='slam_')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.errors = []

    def make(self, defaults=FACTORY):
        return SlamParams(defaults, self.tmp, on_error=self.errors.append)

    def write_defaults(self, text):
        path = os.path.join(self.tmp, 'defaults.yaml')
        with open(path, 'w') as f:
            f.write(text)
        return path


@unittest.skipUnless(os.path.exists(FACTORY), 'ไม่พบไฟล์ mapper_params_s2_factory.yaml ของโปรเจกต์')
class DefaultsTest(Base):

    def test_defaults_come_from_the_project_file(self):
        sp = self.make()
        self.assertTrue(sp.available)
        raw = yaml.safe_load(open(FACTORY))['slam_toolbox']['ros__parameters']
        for key in SCHEMA:
            self.assertEqual(sp.default(key), raw[key], key)
        # ค่าที่จูนไว้ (ป้าย [factory]) ต้องเป็น Default ไม่ใช่ค่าของ slam_toolbox/รุ่นก่อน
        self.assertEqual(sp.default('map_update_interval'), 3.0)
        self.assertEqual(sp.default('minimum_travel_heading'), 0.15)
        self.assertEqual(sp.default('scan_buffer_maximum_scan_distance'), 20.0)
        self.assertEqual(sp.default('loop_search_maximum_distance'), 8.0)

    def test_every_default_is_inside_its_own_allowed_range(self):
        sp = self.make()
        self.assertEqual(self.errors, [])
        for key, (_g, _k, _u, lo, hi, _s) in SCHEMA.items():
            if lo is not None:
                self.assertTrue(lo <= sp.default(key) <= hi, key)

    def test_info_shape(self):
        info = self.make().info()
        self.assertTrue(info['available'])
        self.assertEqual([g['id'] for g in info['groups']], list(GROUPS))
        rows = [r for g in info['groups'] for r in g['params']]
        self.assertEqual(sorted(r['key'] for r in rows), sorted(SCHEMA))
        for r in rows:
            self.assertEqual(r['value'], r['default'], r['key'])
            self.assertFalse(r['modified'], r['key'])
            self.assertEqual(r['type'], SCHEMA[r['key']][1])
        self.assertEqual(info['modified'], 0)
        self.assertIsNone(info['in_use'])
        json.dumps(info)
        adv = {a['key']: a['value'] for a in info['advanced']}
        self.assertEqual(adv['scan_topic'], '/scan_filtered')            # ตัวอ่านอย่างเดียวแสดงครบ
        self.assertEqual(adv['odom_frame'], 'odom')
        self.assertTrue(set(adv).isdisjoint(SCHEMA))                      # ไม่ซ้ำกับตัวที่แก้ได้

    def test_there_are_twelve_safe_parameters_and_dangerous_ones_are_not_editable(self):
        self.assertEqual(len(SCHEMA), 12)
        for key in ('odom_frame', 'map_frame', 'base_frame', 'scan_topic', 'solver_plugin', 'mode',
                    'ceres_linear_solver', 'map_file_name', 'use_map_saver', 'stack_size_to_use'):
            self.assertNotIn(key, SCHEMA)


@unittest.skipUnless(os.path.exists(FACTORY), 'ไม่พบไฟล์ mapper_params_s2_factory.yaml ของโปรเจกต์')
class EditTest(Base):

    def test_set_value_marks_modified_and_persists(self):
        sp = self.make()
        sp.set_values({'resolution': 0.1, 'map_update_interval': 4})
        info = sp.info()
        rows = {r['key']: r for g in info['groups'] for r in g['params']}
        self.assertEqual((rows['resolution']['value'], rows['resolution']['modified']), (0.1, True))
        self.assertEqual(rows['resolution']['default'], 0.05)             # Default ยังแสดงอยู่
        self.assertEqual(rows['map_update_interval']['value'], 4.0)
        self.assertIsInstance(rows['map_update_interval']['value'], float)  # int ที่ส่งมาถูกแปลงเป็น double
        self.assertEqual(info['modified'], 2)
        again = self.make()                                              # เปิดแอปใหม่ ค่ายังอยู่
        self.assertEqual(again.value('resolution'), 0.1)
        self.assertEqual(again.info()['modified'], 2)

    def test_setting_a_value_equal_to_default_removes_the_override(self):
        sp = self.make()
        sp.set_values({'resolution': 0.1})
        sp.set_values({'resolution': 0.05})
        self.assertEqual(sp.info()['modified'], 0)
        self.assertEqual(sp.extra_args(), [])
        self.assertEqual(json.load(open(sp._overrides_path)), {})

    def test_only_given_keys_change(self):
        sp = self.make()
        sp.set_values({'resolution': 0.1})
        sp.set_values({'minimum_travel_distance': 0.3})
        self.assertEqual(sp.value('resolution'), 0.1)
        self.assertEqual(sp.value('minimum_travel_distance'), 0.3)

    def test_bool_and_int_parameters(self):
        sp = self.make()
        sp.set_values({'do_loop_closing': False, 'throttle_scans': 3.0})
        self.assertIs(sp.value('do_loop_closing'), False)
        self.assertEqual(sp.value('throttle_scans'), 3)
        self.assertIsInstance(sp.value('throttle_scans'), int)

    def test_invalid_values_are_rejected_with_codes(self):
        sp = self.make()
        cases = [
            ({'resolution': 'abc'}, 'slam.err.type'),
            ({'resolution': True}, 'slam.err.type'),                 # bool ไม่ใช่ตัวเลข
            ({'resolution': None}, 'slam.err.type'),
            ({'resolution': [0.1]}, 'slam.err.type'),
            ({'resolution': float('nan')}, 'slam.err.type'),
            ({'resolution': float('inf')}, 'slam.err.type'),
            ({'do_loop_closing': 1}, 'slam.err.type'),               # bool ต้องเป็น true/false จริง
            ({'do_loop_closing': 'true'}, 'slam.err.type'),
            ({'throttle_scans': 2.5}, 'slam.err.type'),
            ({'resolution': 0.0}, 'slam.err.range'),
            ({'resolution': 0.21}, 'slam.err.range'),
            ({'resolution': -0.05}, 'slam.err.range'),
            ({'throttle_scans': 0}, 'slam.err.range'),
            ({'throttle_scans': 11}, 'slam.err.range'),
            ({'max_laser_range': 31}, 'slam.err.range'),
            ({'odom_frame': 'odom2'}, 'slam.err.unknown'),           # ตัวอ่านอย่างเดียวห้ามแก้
            ({'scan_topic': '/x'}, 'slam.err.unknown'),
            ({'no_such_key': 1}, 'slam.err.unknown'),
            ({}, 'slam.err.empty'),
            (None, 'slam.err.empty'),
            ([('resolution', 0.1)], 'slam.err.empty'),
        ]
        for values, code in cases:
            with self.assertRaises(SlamParamError, msg=str(values)) as ctx:
                sp.set_values(values)
            self.assertEqual(ctx.exception.code, code, str(values))
        self.assertEqual(sp.info()['modified'], 0)
        self.assertFalse(os.path.exists(sp._overrides_path))

    def test_range_error_carries_the_limits_for_the_message(self):
        with self.assertRaises(SlamParamError) as ctx:
            self.make().set_values({'resolution': 0.5})
        self.assertEqual(ctx.exception.params, {'key': 'resolution', 'min': 0.02, 'max': 0.2})

    def test_boundaries_are_inclusive(self):
        sp = self.make()
        sp.set_values({'resolution': 0.02, 'max_laser_range': 30.0, 'throttle_scans': 10})
        self.assertEqual((sp.value('resolution'), sp.value('max_laser_range'), sp.value('throttle_scans')),
                         (0.02, 30.0, 10))

    def test_all_or_nothing(self):
        sp = self.make()
        with self.assertRaises(SlamParamError):
            sp.set_values({'resolution': 0.1, 'map_update_interval': 99})
        self.assertEqual(sp.value('resolution'), 0.05)                   # ตัวแรกที่ถูกต้องก็ไม่ถูกบันทึก
        self.assertEqual(sp.info()['modified'], 0)

    def test_fine_loop_threshold_must_not_be_below_coarse(self):
        sp = self.make()
        with self.assertRaises(SlamParamError) as ctx:
            sp.set_values({'loop_match_minimum_response_coarse': 0.8})   # Default fine = 0.45 < 0.8
        self.assertEqual(ctx.exception.code, 'slam.err.loop_order')
        sp.set_values({'loop_match_minimum_response_coarse': 0.8, 'loop_match_minimum_response_fine': 0.9})
        self.assertEqual(sp.value('loop_match_minimum_response_fine'), 0.9)

    def test_save_failure_keeps_old_values_and_raises(self):
        sp = self.make()
        sp.set_values({'resolution': 0.1})
        with mock.patch('agv_web_app.slam_params.os.replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                sp.set_values({'resolution': 0.15})
        self.assertEqual(sp.value('resolution'), 0.1)

    def test_effective_lists_all_editable_values(self):
        sp = self.make()
        sp.set_values({'resolution': 0.1})
        eff = sp.effective()
        self.assertEqual(sorted(eff), sorted(SCHEMA))
        self.assertEqual(eff['resolution'], 0.1)
        self.assertEqual(eff['map_update_interval'], 3.0)


@unittest.skipUnless(os.path.exists(FACTORY), 'ไม่พบไฟล์ mapper_params_s2_factory.yaml ของโปรเจกต์')
class GeneratedFileTest(Base):

    def test_no_overrides_means_no_extra_launch_argument(self):
        self.assertEqual(self.make().extra_args(), [])

    def test_generated_file_is_defaults_plus_overrides_only(self):
        before = sha(FACTORY)
        sp = self.make()
        sp.set_values({'resolution': 0.1, 'map_update_interval': 4, 'do_loop_closing': False, 'throttle_scans': 2})
        args = sp.extra_args()
        self.assertEqual(args, [f'slam_params_file:={sp.generated_path}'])
        got = yaml.safe_load(open(sp.generated_path))
        orig = yaml.safe_load(open(FACTORY))
        self.assertEqual(list(got), ['slam_toolbox'])
        new, old = got['slam_toolbox']['ros__parameters'], orig['slam_toolbox']['ros__parameters']
        self.assertEqual(set(new), set(old))                              # ไม่เพิ่ม/หายไปสักตัว (รวม frame/solver)
        diff = {k for k in old if new[k] != old[k]}
        self.assertEqual(diff, {'resolution', 'map_update_interval', 'do_loop_closing', 'throttle_scans'})
        self.assertEqual(new['resolution'], 0.1)
        self.assertEqual(new['map_update_interval'], 4.0)
        self.assertIs(new['do_loop_closing'], False)
        self.assertEqual(new['throttle_scans'], 2)
        self.assertEqual(sha(FACTORY), before)                            # ไฟล์ต้นฉบับของโปรเจกต์ไม่ถูกแก้

    def test_types_match_what_ros_declares(self):
        """ROS ปฏิเสธ int ที่ส่งให้พารามิเตอร์ double → ค่า double ต้องเป็น float เสมอ (เช่น 4.0 ไม่ใช่ 4)"""
        sp = self.make()
        sp.set_values({'map_update_interval': 4, 'max_laser_range': 20, 'throttle_scans': 2})
        sp.extra_args()
        new = yaml.safe_load(open(sp.generated_path))['slam_toolbox']['ros__parameters']
        old = yaml.safe_load(open(FACTORY))['slam_toolbox']['ros__parameters']
        for key in SCHEMA:
            self.assertIs(type(new[key]), type(old[key]), key)
        text = open(sp.generated_path).read()
        self.assertRegex(text, r'(?m)^    map_update_interval: 4\.0$')
        self.assertRegex(text, r'(?m)^    throttle_scans: 2$')

    def test_generated_file_is_rewritten_on_each_call(self):
        sp = self.make()
        sp.set_values({'resolution': 0.1})
        sp.extra_args()
        sp.set_values({'resolution': 0.12})
        sp.extra_args()
        self.assertEqual(yaml.safe_load(open(sp.generated_path))['slam_toolbox']['ros__parameters']['resolution'], 0.12)

    def test_write_failure_raises_instead_of_silently_using_defaults(self):
        sp = self.make()
        sp.set_values({'resolution': 0.1})
        with mock.patch('agv_web_app.slam_params.os.replace', side_effect=OSError('read-only')):
            with self.assertRaises(OSError):
                sp.extra_args()


@unittest.skipUnless(os.path.exists(FACTORY), 'ไม่พบไฟล์ mapper_params_s2_factory.yaml ของโปรเจกต์')
class StoredOverridesTest(Base):

    def store(self, text):
        os.makedirs(os.path.join(self.tmp, 'slam'), exist_ok=True)
        with open(os.path.join(self.tmp, 'slam', 'overrides.json'), 'w') as f:
            f.write(text)

    def test_corrupt_file_falls_back_to_defaults(self):
        for bad in ('{ nope', '[]', '"x"', ''):
            self.errors.clear()
            self.store(bad)
            sp = self.make()
            self.assertEqual(sp.info()['modified'], 0, bad)
            self.assertEqual(len(self.errors), 1, bad)

    def test_invalid_entries_are_dropped_but_good_ones_kept(self):
        self.store(json.dumps({'resolution': 0.1, 'map_update_interval': 99, 'odom_frame': 'x',
                               'throttle_scans': 'two', 'minimum_travel_distance': 0.2}))
        sp = self.make()
        self.assertEqual(sp.value('resolution'), 0.1)
        self.assertEqual(sp.info()['modified'], 1)                       # 0.2 เท่า Default ก็ไม่ถือว่าแก้
        self.assertEqual(len(self.errors), 3)

    def test_missing_overrides_file_is_normal(self):
        self.assertEqual(self.make().info()['modified'], 0)
        self.assertEqual(self.errors, [])


class BrokenDefaultsTest(Base):

    def check_disabled(self, sp):
        self.assertFalse(sp.available)
        self.assertEqual(sp.info(), {'available': False, 'groups': [], 'advanced': [], 'modified': 0, 'in_use': None})
        self.assertEqual(sp.extra_args(), [])
        self.assertEqual(sp.effective(), {})
        with self.assertRaises(SlamParamError) as ctx:
            sp.set_values({'resolution': 0.1})
        self.assertEqual(ctx.exception.code, 'slam.err.no_defaults')
        self.assertGreaterEqual(len(self.errors), 1)

    def test_missing_file(self):
        self.check_disabled(self.make(os.path.join(self.tmp, 'nope.yaml')))

    def test_no_path(self):
        self.check_disabled(self.make(None))

    def test_not_yaml_or_wrong_shape(self):
        for text in ('::: [', '- a\n- b\n', 'slam_toolbox: 3\n', 'slam_toolbox:\n  other: 1\n', ''):
            self.errors.clear()
            self.check_disabled(self.make(self.write_defaults(text)))

    def test_default_missing_a_required_key(self):
        text = yaml.safe_dump({'slam_toolbox': {'ros__parameters': {'resolution': 0.05}}})
        self.check_disabled(self.make(self.write_defaults(text)))

    def test_default_outside_allowed_range(self):
        raw = yaml.safe_load(open(FACTORY))
        raw['slam_toolbox']['ros__parameters']['resolution'] = 5.0
        self.check_disabled(self.make(self.write_defaults(yaml.safe_dump(raw))))


class I18nCoverageTest(unittest.TestCase):
    """ทุกพารามิเตอร์/กลุ่มต้องมีชื่อ+คำอธิบายทั้ง EN และ TH ใน web/i18n.js"""

    def test_names_and_descriptions_exist_in_both_languages(self):
        with open(os.path.join(PKG_ROOT, 'web', 'i18n.js'), encoding='utf-8') as f:
            text = f.read()
        wanted = [f'slam.p.{k}.name' for k in SCHEMA] + [f'slam.p.{k}.desc' for k in SCHEMA] + \
                 [f'slam.group.{g}' for g in GROUPS]
        for key in wanted:
            self.assertEqual(len(re.findall(r'"%s":' % re.escape(key), text)), 2, key)

    def test_error_codes_are_translated(self):
        with open(os.path.join(PKG_ROOT, 'web', 'i18n.js'), encoding='utf-8') as f:
            text = f.read()
        for code in ('slam.err.type', 'slam.err.range', 'slam.err.unknown', 'slam.err.empty',
                     'slam.err.no_defaults', 'slam.err.loop_order', 'slam.err.save'):
            self.assertEqual(len(re.findall(r'"%s":' % re.escape(code), text)), 2, code)

    def test_range_helper_is_finite(self):
        for _key, (_g, kind, _u, lo, hi, step) in SCHEMA.items():
            if kind != 'bool':
                self.assertTrue(math.isfinite(lo) and math.isfinite(hi) and lo < hi and step > 0)
        self.assertTrue(slam_params._coerce('resolution', 0.05) == 0.05)


if __name__ == '__main__':
    unittest.main()
