"""
ทดสอบ NavLive (ปรับค่า Nav2 สด) ด้วย transport ปลอม — ไม่ต้องมี ROS
จำลอง: node ล่ม · Nav2 ปฏิเสธค่า · ค่าถูกปัดกลับหลังตั้ง · ย้อนค่าไม่สำเร็จ

รัน:  cd ~/ros2_ws/src/agv_web_app && python3 -m unittest discover -s test -v
"""
import os
import threading
import unittest

import yaml

from agv_web_app.nav_live import (NavLive, NavLiveError, TransportError, node_names, risky_keys, targets)
from agv_web_app.nav_params import SCHEMA, NavParamError, _get

SHIPPED = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'agv_s2_bringup', 'config',
                       'nav2_params_s2.yaml')


class FakeTransport:
    """แทน node ของ Nav2: เก็บค่าจริงไว้ใน state[(node, name)] และบันทึกทุกการเรียก"""

    def __init__(self):
        with open(SHIPPED, encoding='utf-8') as f:
            data = yaml.safe_load(f)
        self.state = {}
        for key, spec in SCHEMA.items():
            for node, name in targets(key):
                self.state[(node, name)] = _get(data, spec[6][0])
        self.down = set()            # node ที่ติดต่อไม่ได้
        self.reject = {}             # (node, name) → เหตุผลที่ Nav2 ปฏิเสธ
        self.coerce = {}             # (node, name) → ค่าที่ Nav2 "ปัดเป็น" แทนค่าที่ส่งไป
        self.missing = set()         # (node, name) ที่ node ไม่ได้ประกาศ
        self.fail_set_calls = set()  # หมายเลขการเรียก set (นับจาก 1) ที่ให้ล้มเหลว
        self.gets, self.sets = [], []

    def get(self, node, names):
        self.gets.append((node, list(names)))
        if node in self.down:
            raise TransportError(node, 'down')
        return {n: (None if (node, n) in self.missing else self.state[(node, n)]) for n in names}

    def set(self, node, values):
        self.sets.append((node, dict(values)))
        if node in self.down or len(self.sets) in self.fail_set_calls:
            raise TransportError(node, 'down')
        out = {}
        for name, value in values.items():
            if (node, name) in self.reject:
                out[name] = (False, self.reject[(node, name)])
            else:
                # ปัดค่าครั้งเดียว (ไม่ปัดตอนย้อนกลับ)
                self.state[(node, name)] = self.coerce.pop((node, name), value)
                out[name] = (True, '')
        return out

    def snapshot(self):
        return dict(self.state)


class Base(unittest.TestCase):
    def setUp(self):
        self.t = FakeTransport()
        self.events = []
        self.now = 100.0
        self.live = NavLive(self.t, on_event=self.events.append, clock=lambda: self.now)

    def code(self, call, *args, **kw):
        with self.assertRaises((NavLiveError, NavParamError)) as ctx:
            call(*args, **kw)
        return ctx.exception.code

    def err(self, call, *args, **kw):
        with self.assertRaises(NavLiveError) as ctx:
            call(*args, **kw)
        return ctx.exception


class TargetsTest(Base):

    def test_yaml_paths_become_ros_node_and_parameter_names(self):
        self.assertEqual(targets('desired_linear_vel'), [('/controller_server', 'FollowPath.desired_linear_vel')])
        self.assertEqual(targets('xy_goal_tolerance'),
                         [('/controller_server', 'general_goal_checker.xy_goal_tolerance')])
        self.assertEqual(targets('inflation_radius'),
                         [('/global_costmap/global_costmap', 'inflation_layer.inflation_radius'),
                          ('/local_costmap/local_costmap', 'inflation_layer.inflation_radius')])

    def test_node_names_and_shape_of_every_target(self):
        self.assertEqual(node_names(),
                         ['/controller_server', '/global_costmap/global_costmap', '/local_costmap/local_costmap'])
        for key in SCHEMA:
            for node, name in targets(key):
                self.assertTrue(node.startswith('/') and not node.endswith('/'), key)
                self.assertTrue(name and not name.startswith('.') and 'ros__parameters' not in name, key)


class ReadTest(Base):

    def test_reads_every_value_with_one_call_per_node(self):
        values, mixed = self.live.read()
        self.assertEqual(sorted(values), sorted(SCHEMA))
        self.assertEqual(mixed, [])
        # 1 ครั้ง/node (รวมชุดเดียว)
        self.assertEqual(sorted(node for node, _ in self.t.gets), node_names())
        speed = self.t.state[('/controller_server', 'FollowPath.desired_linear_vel')]
        self.assertEqual(values['desired_linear_vel'], speed)

    def test_results_are_cached_for_a_second_so_polling_does_not_flood_nav2(self):
        self.live.read()
        n = len(self.t.gets)
        self.live.read()
        self.assertEqual(len(self.t.gets), n)
        self.now += 1.01
        self.live.read()
        self.assertEqual(len(self.t.gets), 2 * n)

    def test_unreachable_node_and_undeclared_parameter(self):
        self.t.down.add('/local_costmap/local_costmap')
        self.assertEqual(self.err(self.live.read).params, {'node': '/local_costmap/local_costmap'})
        self.t.down.clear()
        self.now += 5
        # Nav2 ยัง configure ไม่เสร็จ
        self.t.missing.add(('/controller_server', 'FollowPath.lookahead_time'))
        e = self.err(self.live.read)
        self.assertEqual((e.code, e.params), ('nav.err.unreachable', {'node': '/controller_server'}))

    def test_mixed_when_the_two_costmaps_disagree_and_first_target_wins(self):
        self.t.state[('/local_costmap/local_costmap', 'inflation_layer.inflation_radius')] = 0.9
        values, mixed = self.live.read()
        self.assertEqual(mixed, ['inflation_radius'])
        first = self.t.state[('/global_costmap/global_costmap', 'inflation_layer.inflation_radius')]
        self.assertEqual(values['inflation_radius'], first)


class ApplyTest(Base):

    def test_no_change_means_no_write(self):
        live, _ = self.live.read()
        result = self.live.apply({'xy_goal_tolerance': live['xy_goal_tolerance']})
        self.assertEqual((result['applied'], self.t.sets), ({}, []))
        self.assertEqual(sorted(result['live']), sorted(SCHEMA))

    def test_float_noise_is_not_a_change(self):
        self.t.state[('/controller_server', 'general_goal_checker.xy_goal_tolerance')] = 0.30000000000000004
        self.assertEqual(self.live.apply({'xy_goal_tolerance': 0.3})['applied'], {})
        self.assertEqual(self.t.sets, [])

    def test_safe_change_is_applied_verified_and_reported(self):
        before = self.live.read()[0]['xy_goal_tolerance']
        result = self.live.apply({'xy_goal_tolerance': 0.2})
        self.assertEqual(result['applied'], {'xy_goal_tolerance': {'from': before, 'to': 0.2}})
        self.assertEqual(result['live']['xy_goal_tolerance'], 0.2)
        self.assertEqual(self.t.state[('/controller_server', 'general_goal_checker.xy_goal_tolerance')], 0.2)
        self.assertEqual(self.t.sets, [('/controller_server', {'general_goal_checker.xy_goal_tolerance': 0.2})])
        self.assertEqual(self.events, [f'Nav2 live: xy_goal_tolerance {before} → 0.2'])

    def test_a_value_shared_by_two_costmaps_is_written_to_both(self):
        self.live.apply({'inflation_radius': 0.7})
        self.assertEqual([n for n, _ in self.t.sets],
                         ['/global_costmap/global_costmap', '/local_costmap/local_costmap'])
        for node in ('/global_costmap/global_costmap', '/local_costmap/local_costmap'):
            self.assertEqual(self.t.state[(node, 'inflation_layer.inflation_radius')], 0.7)

    def test_a_value_that_differs_between_the_two_costmaps_is_unified(self):
        glob = ('/global_costmap/global_costmap', 'inflation_layer.inflation_radius')
        local = ('/local_costmap/local_costmap', 'inflation_layer.inflation_radius')
        self.t.state[glob], self.t.state[local] = 0.7, 0.9
        # ตรงกับตำแหน่งแรกอยู่แล้ว แต่ตำแหน่งที่สองไม่ตรง
        result = self.live.apply({'inflation_radius': 0.7})
        self.assertEqual((self.t.state[glob], self.t.state[local]), (0.7, 0.7))
        self.assertEqual(sorted(result['applied']), ['inflation_radius'])
        self.assertEqual(result['mixed'], [])

    def test_changes_to_the_same_node_are_batched_into_one_call(self):
        self.live.apply({'xy_goal_tolerance': 0.2, 'yaw_goal_tolerance': 0.2, 'lookahead_time': 1.0})
        self.assertEqual(len(self.t.sets), 1)
        self.assertEqual(self.t.sets[0][0], '/controller_server')
        self.assertEqual(len(self.t.sets[0][1]), 3)

    def test_only_changed_keys_are_written(self):
        live = self.live.read()[0]
        self.now += 5
        self.live.apply({'xy_goal_tolerance': 0.2, 'yaw_goal_tolerance': live['yaw_goal_tolerance']})
        self.assertEqual(list(self.t.sets[0][1]), ['general_goal_checker.xy_goal_tolerance'])

    def test_numbers_are_sent_as_float_even_if_given_as_int(self):
        self.live.apply({'lookahead_time': 1})                                                # 1.5 → 1 (ลดลง ไม่เสี่ยง)
        sent = self.t.sets[0][1]['FollowPath.lookahead_time']
        self.assertIs(type(sent), float)
        self.assertEqual(sent, 1.0)

    def test_bad_input_is_refused_before_anything_is_sent_or_even_read(self):
        for bad in (None, {}, [], 'x', {'unknown': 1}, {'footprint': '[[1,1]]'}, {'desired_linear_vel': 9},
                    {'desired_linear_vel': 'fast'}, {'use_collision_detection': 1},
                    {'xy_goal_tolerance': float('nan')}):
            self.assertIn(self.code(self.live.apply, bad, confirmed=True),
                          ('nav.err.empty', 'nav.err.unknown', 'nav.err.range', 'nav.err.type'), repr(bad))
        self.assertEqual((self.t.gets, self.t.sets), ([], []))

    def test_lookahead_order_is_checked_against_the_live_values(self):
        self.t.state[('/controller_server', 'FollowPath.max_lookahead_dist')] = 0.6
        self.assertEqual(self.code(self.live.apply, {'min_lookahead_dist': 0.7}), 'nav.err.lookahead_order')
        self.assertEqual(self.t.sets, [])
        self.live.apply({'min_lookahead_dist': 0.6})                                          # เท่ากันได้
        self.assertEqual(self.t.state[('/controller_server', 'FollowPath.min_lookahead_dist')], 0.6)


class RiskTest(Base):

    def test_risk_rules(self):
        live = {key: SCHEMA[key][3] for key in SCHEMA if SCHEMA[key][1] == 'float'}
        live['use_collision_detection'] = True
        cases = [
            ('desired_linear_vel', 0.2, 0.3, True), ('desired_linear_vel', 0.2, 0.1, False),
            ('rotate_to_heading_angular_vel', 0.5, 0.6, True), ('rotate_to_heading_angular_vel', 0.5, 0.4, False),
            ('max_angular_accel', 0.8, 1.0, True), ('max_angular_accel', 0.8, 0.5, False),
            ('max_allowed_time_to_collision_up_to_carrot', 3.0, 2.0, True),
            ('max_allowed_time_to_collision_up_to_carrot', 3.0, 4.0, False),
            ('inflation_radius', 0.55, 0.4, True), ('inflation_radius', 0.55, 0.7, False),
            ('xy_goal_tolerance', 0.15, 0.3, False), ('lookahead_dist', 0.8, 1.5, False),
            ('cost_scaling_factor', 3.0, 1.0, False),
        ]
        for key, old, new, risky in cases:
            self.assertEqual(risky_keys(dict(live, **{key: old}), {key: new}) == [key], risky, f'{key} {old}→{new}')
        self.assertEqual(risky_keys(dict(live, use_collision_detection=True), {'use_collision_detection': False}),
                         ['use_collision_detection'])
        self.assertEqual(risky_keys(dict(live, use_collision_detection=False), {'use_collision_detection': True}), [])

    def test_risky_change_needs_confirmation_and_nothing_is_written_without_it(self):
        e = self.err(self.live.apply, {'desired_linear_vel': 0.3, 'xy_goal_tolerance': 0.2})
        self.assertEqual((e.code, e.params), ('nav.err.confirm_required', {'keys': 'desired_linear_vel'}))
        self.assertEqual(self.t.sets, [])                                      # แม้ค่าที่ไม่เสี่ยงก็ไม่ถูกเขียน
        for not_true in (False, None, 1, 'yes', 'true'):
            self.assertEqual(self.code(self.live.apply, {'desired_linear_vel': 0.3}, confirmed=not_true),
                             'nav.err.confirm_required', repr(not_true))
        self.assertEqual(self.t.sets, [])
        result = self.live.apply({'desired_linear_vel': 0.3, 'xy_goal_tolerance': 0.2}, confirmed=True)
        self.assertEqual(sorted(result['applied']), ['desired_linear_vel', 'xy_goal_tolerance'])

    def test_all_risky_keys_are_listed(self):
        e = self.err(self.live.apply,
                     {'desired_linear_vel': 0.4, 'use_collision_detection': False, 'inflation_radius': 0.4})
        self.assertEqual(e.params['keys'], 'desired_linear_vel, inflation_radius, use_collision_detection')

    def test_confirmation_is_judged_against_the_live_value_not_the_saved_one(self):
        self.t.state[('/controller_server', 'FollowPath.desired_linear_vel')] = 0.4          # ตอนนี้ Nav2 วิ่ง 0.4
        # ลดจากค่าสด = ไม่เสี่ยง ไม่ต้องยืนยัน
        self.live.apply({'desired_linear_vel': 0.3})
        self.assertEqual(self.t.state[('/controller_server', 'FollowPath.desired_linear_vel')], 0.3)


class FailureTest(Base):

    def test_rejection_rolls_back_everything_already_written(self):
        before = self.t.snapshot()
        self.t.reject[('/local_costmap/local_costmap', 'inflation_layer.inflation_radius')] = 'out of bounds'
        # ลำดับเขียน: controller → global → local (ตัวสุดท้ายถูกปฏิเสธ → สองตัวแรกต้องถูกย้อน)
        e = self.err(self.live.apply, {'xy_goal_tolerance': 0.2, 'inflation_radius': 0.7})
        self.assertEqual((e.code, e.params),
                         ('nav.err.rejected', {'key': 'inflation_radius', 'reason': 'out of bounds'}))
        self.assertEqual(self.t.snapshot(), before)
        # ไม่ล็อกว่า "เปลี่ยนแล้ว" ทั้งที่ย้อนกลับ
        self.assertEqual(self.events, [])

    def test_rollback_restores_in_reverse_order(self):
        self.t.reject[('/local_costmap/local_costmap', 'inflation_layer.cost_scaling_factor')] = 'no'
        self.err(self.live.apply, {'xy_goal_tolerance': 0.2, 'cost_scaling_factor': 4.0})
        undo = [(node, list(values)[0]) for node, values in self.t.sets[3:]]
        self.assertEqual(undo, [('/global_costmap/global_costmap', 'inflation_layer.cost_scaling_factor'),
                                ('/controller_server', 'general_goal_checker.xy_goal_tolerance')])

    def test_one_rejected_value_in_a_batch_rolls_back_its_accepted_siblings(self):
        before = self.t.snapshot()
        self.t.reject[('/controller_server', 'FollowPath.lookahead_time')] = 'bad'
        e = self.err(self.live.apply, {'xy_goal_tolerance': 0.2, 'lookahead_time': 1.0, 'yaw_goal_tolerance': 0.2})
        self.assertEqual(e.params['key'], 'lookahead_time')
        self.assertEqual(self.t.snapshot(), before)

    def test_node_going_down_mid_apply_rolls_back_and_names_the_node(self):
        before = self.t.snapshot()
        original = self.t.set

        def dying(node, values):
            if node == '/local_costmap/local_costmap':
                self.t.down.add(node)
            return original(node, values)

        self.t.set = dying
        e = self.err(self.live.apply, {'inflation_radius': 0.7})
        self.assertEqual((e.code, e.params), ('nav.err.unreachable', {'node': '/local_costmap/local_costmap'}))
        self.t.down.clear()
        self.assertEqual(self.t.snapshot(), before)

    def test_value_changed_by_nav2_after_setting_is_detected_and_rolled_back(self):
        before = self.t.snapshot()
        self.t.coerce[('/controller_server', 'general_goal_checker.xy_goal_tolerance')] = 0.25   # Nav2 ปัดค่า
        e = self.err(self.live.apply, {'xy_goal_tolerance': 0.2})
        self.assertEqual((e.code, e.params), ('nav.err.verify', {'key': 'xy_goal_tolerance'}))
        self.t.coerce.clear()
        self.assertEqual(self.t.snapshot(), before)

    def test_rollback_that_cannot_complete_is_reported_loudly(self):
        self.t.reject[('/local_costmap/local_costmap', 'inflation_layer.inflation_radius')] = 'no'
        self.t.fail_set_calls = {4}                                   # การเรียกที่ 4 = ตอนย้อนค่าตัวแรก
        e = self.err(self.live.apply, {'xy_goal_tolerance': 0.2, 'inflation_radius': 0.7})
        self.assertEqual(e.code, 'nav.err.rollback_failed')
        self.assertIsInstance(e.__cause__, NavLiveError)
        self.assertIn('inflation_radius', e.params['keys'])

    def test_unreachable_before_any_write(self):
        self.t.down.add('/controller_server')
        self.assertEqual(self.err(self.live.apply, {'xy_goal_tolerance': 0.2}).code, 'nav.err.unreachable')
        self.assertEqual(self.t.sets, [])

    def test_lock_is_released_after_every_failure(self):
        self.t.reject[('/controller_server', 'general_goal_checker.xy_goal_tolerance')] = 'no'
        self.err(self.live.apply, {'xy_goal_tolerance': 0.2})
        self.t.reject.clear()
        self.assertEqual(self.live.apply({'xy_goal_tolerance': 0.2})['live']['xy_goal_tolerance'], 0.2)

    def test_only_one_apply_at_a_time(self):
        self.live._lock.acquire()
        try:
            self.assertEqual(self.code(self.live.apply, {'xy_goal_tolerance': 0.2}), 'nav.err.busy')
        finally:
            self.live._lock.release()
        self.assertEqual(self.t.sets, [])

    def test_concurrent_applies_never_interleave(self):
        entered, release = threading.Event(), threading.Event()
        original = self.t.set

        def slow(node, values):
            entered.set()
            release.wait(5)
            return original(node, values)

        self.t.set = slow
        first = threading.Thread(target=lambda: self.live.apply({'xy_goal_tolerance': 0.2}))
        first.start()
        entered.wait(5)
        try:
            self.assertEqual(self.code(self.live.apply, {'yaw_goal_tolerance': 0.2}), 'nav.err.busy')
        finally:
            release.set()
            first.join(5)
        self.assertEqual(self.t.state[('/controller_server', 'general_goal_checker.xy_goal_tolerance')], 0.2)


class CacheTest(Base):

    def test_a_read_that_overlaps_an_apply_never_leaves_stale_values_in_the_cache(self):
        entered, release = threading.Event(), threading.Event()
        original = self.t.get
        calls = []

        def slow_first_read(node, names):
            calls.append(node)
            result = original(node, names)                     # อ่านค่า "เก่า" ไว้ก่อน
            if len(calls) == 1:
                entered.set()
                release.wait(5)                                # แล้วค้างไว้ให้ apply ทำงานจนจบ
            return result

        self.t.get = slow_first_read
        reader = threading.Thread(target=self.live.read)
        reader.start()
        entered.wait(5)
        self.t.get = original
        self.live.apply({'xy_goal_tolerance': 0.2})            # apply เสร็จทั้งหมดระหว่างที่ผู้อ่านยังค้าง
        release.set()
        reader.join(5)
        self.assertEqual(self.live.read()[0]['xy_goal_tolerance'], 0.2)    # ต้องไม่ใช่ค่าเก่าที่ผู้อ่านเพิ่งเก็บ

    def test_apply_invalidates_the_cache(self):
        self.live.read()
        self.live.apply({'xy_goal_tolerance': 0.2})
        self.assertEqual(self.live.read()[0]['xy_goal_tolerance'], 0.2)                       # ไม่ใช่ค่าเก่าจากแคช

    def test_failed_apply_also_invalidates_the_cache(self):
        self.live.read()
        self.t.reject[('/controller_server', 'general_goal_checker.xy_goal_tolerance')] = 'no'
        self.err(self.live.apply, {'xy_goal_tolerance': 0.2})
        gets = len(self.t.gets)
        self.live.read()
        self.assertGreater(len(self.t.gets), gets)


if __name__ == '__main__':
    unittest.main()
