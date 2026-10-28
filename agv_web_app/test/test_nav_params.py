"""
ทดสอบ NavParams (ค่า Nav2 ที่ปรับได้ + ไฟล์ที่ส่งให้ launch) กับไฟล์ nav2_params_s2.yaml ตัวจริง — ไม่ต้องมี ROS

รัน:  cd ~/ros2_ws/src/agv_web_app && python3 -m unittest discover -s test -v
"""
import copy
import json
import math
import os
import shutil
import tempfile
import unittest
from unittest import mock

import yaml

from agv_web_app.nav_params import GROUPS, SCHEMA, NavParamError, NavParams, _get

SHIPPED = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'agv_s2_bringup', 'config',
                       'nav2_params_s2.yaml')


def shipped():
    with open(SHIPPED, encoding='utf-8') as f:
        return yaml.safe_load(f)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='navp_')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.warnings = []

    def make(self, defaults=SHIPPED):
        return NavParams(defaults, self.tmp, on_error=self.warnings.append)

    def overrides_file(self):
        return os.path.join(self.tmp, 'nav', 'nav_overrides.json')

    def write_overrides(self, text):
        os.makedirs(os.path.join(self.tmp, 'nav'), exist_ok=True)
        with open(self.overrides_file(), 'w') as f:
            f.write(text)

    def write_defaults(self, name, data):
        path = os.path.join(self.tmp, f'defaults_{name.replace(" ", "_")}.yaml')       # แต่ละกรณีไฟล์ของตัวเอง
        with open(path, 'w') as f:
            f.write(data if isinstance(data, str) else yaml.safe_dump(data))
        return path

    def code(self, call, *args):
        with self.assertRaises(NavParamError) as ctx:
            call(*args)
        return ctx.exception.code


class DefaultsTest(Base):

    def test_every_tunable_exists_in_the_shipped_file_within_its_range(self):
        data = shipped()
        for key, (_g, kind, _u, lo, hi, _s, paths) in SCHEMA.items():
            for path in paths:
                # ชื่อใน Nav2 yaml เปลี่ยน = เทสต์นี้ฟ้อง
                self.assertIsNotNone(_get(data, path), f'{key}: {path}')
            ref = _get(data, paths[0])
            if kind == 'bool':
                self.assertIsInstance(ref, bool, key)
            else:
                self.assertTrue(lo <= ref <= hi, f'{key}: {ref} not in [{lo}, {hi}]')

    def test_defaults_come_from_the_shipped_file(self):
        params = self.make()
        self.assertTrue(params.available)
        data = shipped()
        for key, spec in SCHEMA.items():
            self.assertEqual(params.default(key), _get(data, spec[6][0]), key)
        self.assertEqual(self.warnings, [])

    def test_schema_is_safe_by_construction(self):
        # เพดานความเร็วอยู่ในโค้ด (เท่ากับขับมือ)
        self.assertLessEqual(SCHEMA['desired_linear_vel'][4], 0.5)
        self.assertGreater(SCHEMA['desired_linear_vel'][3], 0.0)
        for key, (group, kind, _u, lo, hi, step, paths) in SCHEMA.items():
            self.assertIn(group, GROUPS, key)
            self.assertIn(kind, ('float', 'bool'), key)
            if kind == 'float':
                self.assertTrue(math.isfinite(lo) and math.isfinite(hi) and 0 <= lo < hi and step > 0, key)
            for path in paths:                                                           # ไม่เปิดให้แก้ของอันตราย
                forbidden = {'footprint', 'plugins', 'plugin', 'odom_topic', 'amcl', 'bt_navigator'}
                self.assertFalse(forbidden & set(path), key)

    def test_unreadable_or_incomplete_defaults_disable_everything(self):
        bad_cases = {
            'missing file': os.path.join(self.tmp, 'nope.yaml'),
            'not yaml': self.write_defaults('not yaml', 'a: [unclosed'),
            'not a mapping': self.write_defaults('not a mapping', '- 1\n- 2\n'),
            'none path': None,
        }
        broken = shipped()
        del broken['controller_server']['ros__parameters']['FollowPath']['lookahead_time']
        bad_cases['missing key'] = self.write_defaults('missing key', broken)
        broken = copy.deepcopy(shipped())
        del broken['local_costmap']['local_costmap']['ros__parameters']['inflation_layer']['inflation_radius']
        bad_cases['missing second path'] = self.write_defaults('missing second path', broken)
        broken = copy.deepcopy(shipped())
        broken['controller_server']['ros__parameters']['FollowPath']['desired_linear_vel'] = 5.0
        bad_cases['default out of range'] = self.write_defaults('default out of range', broken)
        broken = copy.deepcopy(shipped())
        broken['controller_server']['ros__parameters']['FollowPath']['use_collision_detection'] = 'yes'
        bad_cases['wrong type'] = self.write_defaults('wrong type', broken)
        for name, path in bad_cases.items():
            self.warnings.clear()
            params = self.make(path)
            self.assertFalse(params.available, name)
            self.assertEqual(len(self.warnings), 1, name)
            self.assertEqual(self.code(params.set_values, {'xy_goal_tolerance': 0.2}), 'nav.err.no_defaults', name)
            self.assertEqual((params.extra_args(), params.effective()), ([], {}), name)
            self.assertEqual(params.info(), {'available': False, 'groups': [], 'modified': 0, 'in_use': None,
                                             'restart_needed': False}, name)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, 'nav')))                  # ไม่เขียนอะไรลงดิสก์


class SetValuesTest(Base):

    def test_values_persist_across_restart(self):
        params = self.make()
        params.set_values({'desired_linear_vel': 0.3, 'use_collision_detection': False})
        again = self.make()
        self.assertEqual((again.value('desired_linear_vel'), again.value('use_collision_detection')), (0.3, False))
        saved = json.load(open(self.overrides_file()))
        self.assertEqual(saved, {'desired_linear_vel': 0.3, 'use_collision_detection': False})
        # ไม่มี .tmp ค้าง
        self.assertEqual(os.listdir(os.path.dirname(self.overrides_file())), ['nav_overrides.json'])

    def test_value_equal_to_default_removes_the_override(self):
        params = self.make()
        default = params.default('xy_goal_tolerance')
        params.set_values({'xy_goal_tolerance': 0.3})
        self.assertEqual(params.info()['modified'], 1)
        params.set_values({'xy_goal_tolerance': default})
        self.assertEqual((params.info()['modified'], params.value('xy_goal_tolerance')), (0, default))
        self.assertEqual(json.load(open(self.overrides_file())), {})
        self.assertEqual(params.extra_args(), [])

    def test_only_sent_keys_change(self):
        params = self.make()
        params.set_values({'xy_goal_tolerance': 0.3})
        params.set_values({'yaw_goal_tolerance': 0.2})
        self.assertEqual((params.value('xy_goal_tolerance'), params.value('yaw_goal_tolerance')), (0.3, 0.2))

    def test_numbers_are_coerced_to_float_and_bool_must_be_bool(self):
        params = self.make()
        params.set_values({'max_allowed_time_to_collision_up_to_carrot': 2})
        # Nav2 ประกาศเป็น double
        self.assertIsInstance(params.value('max_allowed_time_to_collision_up_to_carrot'), float)
        for key, bad in (('desired_linear_vel', True), ('desired_linear_vel', '0.3'), ('desired_linear_vel', None),
                         ('desired_linear_vel', [0.3]), ('desired_linear_vel', float('nan')),
                         ('desired_linear_vel', float('inf')), ('use_collision_detection', 1),
                         ('use_collision_detection', 'true'), ('use_collision_detection', None)):
            self.assertEqual(self.code(params.set_values, {key: bad}), 'nav.err.type', f'{key}={bad!r}')

    def test_range_edges_are_inclusive(self):
        params = self.make()
        for key, (_g, kind, _u, lo, hi, _s, _p) in SCHEMA.items():
            if kind != 'float' or key in ('min_lookahead_dist', 'max_lookahead_dist'):
                continue
            params.set_values({key: lo})
            params.set_values({key: hi})
            for outside in (lo - 0.001, hi + 0.001):
                with self.assertRaises(NavParamError) as ctx:
                    params.set_values({key: outside})
                self.assertEqual((ctx.exception.code, ctx.exception.params),
                                 ('nav.err.range', {'key': key, 'min': lo, 'max': hi}))

    def test_speed_cannot_exceed_the_ceiling_in_code(self):
        params = self.make()
        params.set_values({'desired_linear_vel': 0.5})
        for fast in (0.51, 1.0, 100):
            self.assertEqual(self.code(params.set_values, {'desired_linear_vel': fast}), 'nav.err.range')
        self.assertEqual(params.value('desired_linear_vel'), 0.5)

    def test_bad_input_changes_nothing_and_writes_nothing(self):
        params = self.make()
        for bad in (None, {}, [], 'x', 5, {'unknown_key': 1}, {'footprint': '[[1,1]]'}, {'plugins': []}):
            self.assertIn(self.code(params.set_values, bad), ('nav.err.empty', 'nav.err.unknown'), repr(bad))
        self.assertFalse(os.path.exists(self.overrides_file()))

    def test_all_or_nothing(self):
        params = self.make()
        before = params.effective()
        self.assertEqual(self.code(params.set_values, {'desired_linear_vel': 0.3, 'xy_goal_tolerance': 9}),
                         'nav.err.range')
        self.assertEqual(self.code(params.set_values, {'desired_linear_vel': 0.3, 'nope': 1}), 'nav.err.unknown')
        self.assertEqual(params.effective(), before)
        self.assertFalse(os.path.exists(self.overrides_file()))

    def test_lookahead_min_must_not_exceed_max(self):
        params = self.make()
        high_max = params.default('max_lookahead_dist')
        self.assertEqual(self.code(params.set_values, {'min_lookahead_dist': min(1.5, high_max + 0.2)}),
                         'nav.err.lookahead_order')
        # เปลี่ยนคู่กันในคำสั่งเดียวได้
        params.set_values({'min_lookahead_dist': 1.0, 'max_lookahead_dist': 1.5})
        self.assertEqual(self.code(params.set_values, {'max_lookahead_dist': 0.9}), 'nav.err.lookahead_order')
        params.set_values({'min_lookahead_dist': 1.5})                                          # เท่ากันได้
        self.assertEqual((params.value('min_lookahead_dist'), params.value('max_lookahead_dist')), (1.5, 1.5))

    def test_save_failure_keeps_the_old_values_and_leaves_no_tmp(self):
        params = self.make()
        params.set_values({'xy_goal_tolerance': 0.3})
        with mock.patch('agv_web_app.nav_params.os.replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                params.set_values({'xy_goal_tolerance': 0.4})
        self.assertEqual(params.value('xy_goal_tolerance'), 0.3)
        self.assertEqual(os.listdir(os.path.dirname(self.overrides_file())), ['nav_overrides.json'])


class OverridesFileTest(Base):

    def test_unreadable_files_mean_defaults_with_a_warning(self):
        for text in ('', '{bad', '[]', 'null', '"x"', '5'):
            self.warnings.clear()
            self.write_overrides(text)
            params = self.make()
            self.assertEqual(params.info()['modified'], 0, text)
            self.assertEqual(len(self.warnings), 1, text)

    def test_bad_entries_are_dropped_individually(self):
        self.write_overrides(json.dumps({'desired_linear_vel': 0.3, 'unknown': 1, 'xy_goal_tolerance': 99,
                                         'use_collision_detection': 'no', 'yaw_goal_tolerance': 'x'}))
        params = self.make()
        self.assertEqual(params.info()['modified'], 1)
        self.assertEqual(params.value('desired_linear_vel'), 0.3)
        self.assertEqual(len(self.warnings), 4)

    def test_values_equal_to_default_are_not_kept(self):
        default = self.make().default('xy_goal_tolerance')
        self.write_overrides(json.dumps({'xy_goal_tolerance': default}))
        self.assertEqual(self.make().info()['modified'], 0)

    def test_contradicting_lookahead_in_the_file_is_dropped_as_a_whole(self):
        self.write_overrides(json.dumps({'min_lookahead_dist': 1.5, 'max_lookahead_dist': 0.5,
                                         'desired_linear_vel': 0.3}))
        params = self.make()
        self.assertEqual(params.info()['modified'], 0)
        self.assertEqual(len(self.warnings), 1)


class LaunchArgsTest(Base):

    def flatten(self, data, prefix=()):
        out = {}
        for k, v in data.items():
            if isinstance(v, dict):
                out.update(self.flatten(v, prefix + (k,)))
            else:
                out[prefix + (k,)] = v
        return out

    @staticmethod
    def generated_speed(params):
        with open(params.generated_path, encoding='utf-8') as f:
            return yaml.safe_load(f)['controller_server']['ros__parameters']['FollowPath']['desired_linear_vel']

    def test_no_overrides_means_no_argument_and_no_file(self):
        params = self.make()
        self.assertEqual(params.extra_args(), [])
        self.assertFalse(os.path.exists(params.generated_path))

    def test_generated_file_is_the_whole_default_file_plus_the_changes_only(self):
        params = self.make()
        params.set_values({'desired_linear_vel': 0.3, 'use_collision_detection': False, 'inflation_radius': 0.7,
                           'xy_goal_tolerance': 0.2})
        args = params.extra_args()
        self.assertEqual(args, [f'nav2_params:={params.generated_path}'])
        with open(params.generated_path, encoding='utf-8') as f:
            text = f.read()
        self.assertTrue(text.startswith('# Generated by the AGV web app'))
        generated = yaml.safe_load(text)
        expected = self.flatten(shipped())
        expected[('controller_server', 'ros__parameters', 'FollowPath', 'desired_linear_vel')] = 0.3
        expected[('controller_server', 'ros__parameters', 'FollowPath', 'use_collision_detection')] = False
        expected[('controller_server', 'ros__parameters', 'general_goal_checker', 'xy_goal_tolerance')] = 0.2
        expected[('global_costmap', 'global_costmap', 'ros__parameters', 'inflation_layer', 'inflation_radius')] = 0.7
        expected[('local_costmap', 'local_costmap', 'ros__parameters', 'inflation_layer', 'inflation_radius')] = 0.7
        # ที่เหลือทั้งไฟล์เหมือนเดิมทุกตัว
        self.assertEqual(self.flatten(generated), expected)
        self.assertEqual(list(generated), list(shipped()))                               # node ครบ ลำดับเดิม

    def test_types_match_what_nav2_declares(self):
        params = self.make()
        params.set_values({'desired_linear_vel': 0.3, 'max_allowed_time_to_collision_up_to_carrot': 2,
                           'use_collision_detection': False})
        with open(params.extra_args()[0].split(':=', 1)[1], encoding='utf-8') as f:
            follow = yaml.safe_load(f)['controller_server']['ros__parameters']['FollowPath']
        self.assertIs(type(follow['desired_linear_vel']), float)
        # 2 ต้องกลายเป็น 2.0 ไม่ใช่ int
        self.assertIs(type(follow['max_allowed_time_to_collision_up_to_carrot']), float)
        self.assertIs(follow['use_collision_detection'], False)

    def test_generating_the_file_never_alters_the_defaults(self):
        params = self.make()
        before = {key: params.default(key) for key in SCHEMA}
        params.set_values({'desired_linear_vel': 0.3, 'inflation_radius': 0.7})
        params.extra_args()
        # Default ยังเป็นค่าในไฟล์โปรเจกต์
        self.assertEqual({key: params.default(key) for key in SCHEMA}, before)
        rows = {r['key']: r for g in params.info()['groups'] for r in g['params']}
        self.assertEqual((rows['desired_linear_vel']['default'], rows['desired_linear_vel']['modified']),
                         (before['desired_linear_vel'], True))
        self.assertEqual(self.make().default('desired_linear_vel'), before['desired_linear_vel'])

    def test_unchanged_nodes_and_unrelated_values_are_untouched(self):
        params = self.make()
        params.set_values({'yaw_goal_tolerance': 0.2})
        with open(params.extra_args()[0].split(':=', 1)[1], encoding='utf-8') as f:
            generated = yaml.safe_load(f)
        for node in ('amcl', 'bt_navigator', 'planner_server', 'behavior_server', 'map_server'):
            self.assertEqual(generated[node], shipped()[node], node)

    def test_regenerated_on_each_call_and_back_to_nothing_when_overrides_are_cleared(self):
        params = self.make()
        params.set_values({'desired_linear_vel': 0.3})
        first = params.extra_args()
        params.set_values({'desired_linear_vel': 0.4})
        self.assertEqual(self.generated_speed(params), 0.3)
        self.assertEqual(params.extra_args(), first)
        self.assertEqual(self.generated_speed(params), 0.4)
        params.set_values({'desired_linear_vel': params.default('desired_linear_vel')})
        # ไม่มีส่วนต่าง = ไม่ส่งไฟล์เก่าที่ค้างอยู่
        self.assertEqual(params.extra_args(), [])
        self.assertNotIn('.tmp', ''.join(os.listdir(os.path.dirname(params.generated_path))))

    def test_write_failure_raises_and_leaves_no_partial_file(self):
        params = self.make()
        params.set_values({'desired_linear_vel': 0.3})
        with mock.patch('agv_web_app.nav_params.os.replace', side_effect=OSError('read-only')):
            with self.assertRaises(OSError):
                params.extra_args()
        self.assertFalse(any(name.endswith('.tmp') for name in os.listdir(os.path.dirname(params.generated_path))))


class InfoTest(Base):

    def test_shape_and_modified_flags(self):
        params = self.make()
        params.set_values({'desired_linear_vel': 0.3, 'use_collision_detection': False})
        info = params.info()
        self.assertEqual([g['id'] for g in info['groups']], list(GROUPS))
        rows = {r['key']: r for g in info['groups'] for r in g['params']}
        self.assertEqual(sorted(rows), sorted(SCHEMA))                                    # ทุกตัวอยู่ครบ ไม่ซ้ำ
        self.assertEqual(sum(len(g['params']) for g in info['groups']), len(SCHEMA))
        self.assertTrue(rows['desired_linear_vel']['modified'] and rows['use_collision_detection']['modified'])
        self.assertEqual(rows['desired_linear_vel']['value'], 0.3)
        self.assertEqual(rows['desired_linear_vel']['default'], params.default('desired_linear_vel'))
        self.assertFalse(rows['xy_goal_tolerance']['modified'])
        self.assertEqual((info['modified'], info['available']), (2, True))
        for row in rows.values():
            self.assertEqual(set(row), {'key', 'type', 'unit', 'min', 'max', 'step', 'default', 'value', 'modified'})
        json.dumps(info)                                                                  # ส่งเป็น JSON ได้

    def test_restart_needed_only_when_nav_runs_with_different_values(self):
        params = self.make()
        started = params.effective()
        self.assertFalse(params.info(in_use=started)['restart_needed'])                   # รันด้วยค่าเดียวกับที่ตั้งไว้
        self.assertFalse(params.info(in_use=None)['restart_needed'])                      # Nav ไม่ได้รัน
        params.set_values({'desired_linear_vel': 0.3})
        info = params.info(in_use=started)
        self.assertTrue(info['restart_needed'])
        self.assertEqual(info['in_use'], started)
        params.set_values({'desired_linear_vel': params.default('desired_linear_vel')})
        # เปลี่ยนกลับ = ไม่ต้อง restart
        self.assertFalse(params.info(in_use=started)['restart_needed'])


if __name__ == '__main__':
    unittest.main()
