"""
ทดสอบ Route (RouteStore + validate_route) — ใช้ของปลอมทั้งหมด ไม่ต้องมี ROS

รัน:  cd ~/ros2_ws/src/agv_web_app && python3 -m unittest discover -s test -v
"""
import json
import math
import os
import shutil
import tempfile
import unittest

from agv_web_app.routes import MAX_NAME, MAX_POINTS, RouteError, RouteStore, normalize_yaw, validate_route

VALID = lambda name: isinstance(name, str) and name.replace('_', '').replace('-', '').isalnum()    # noqa: E731
CLEAR = 0.40


def probe_ok(x, y, radius):
    return {'status': 'ok', 'clearance': None}


def probe_wall(x, y, radius):                       # ผนังที่ x ≥ 5 : ห่างผนัง < radius = tight ; x ≥ 5.2 = ไม่รู้
    if x >= 5.2:
        return {'status': 'unknown', 'clearance': 0.0}
    if x >= 5.0 - radius:
        return {'status': 'tight', 'clearance': 5.0 - x}
    if x < 0:
        return {'status': 'outside', 'clearance': None}
    return {'status': 'ok', 'clearance': None}


def pt(name='A', x=1.0, y=1.0, yaw=0.0, wait=0):
    return {'name': name, 'x': x, 'y': y, 'yaw': yaw, 'wait': wait}


def code_of(fn, *args, **kw):
    """เรียก fn ที่ต้องโยน RouteError แล้วคืนตัว exception (ไม่โยน = เทสต์ล้ม)"""
    try:
        fn(*args, **kw)
    except RouteError as e:
        return e
    raise AssertionError('RouteError not raised')


class ValidateTest(unittest.TestCase):

    def v(self, name='R', loop=1, points=None, probe=probe_ok):
        return validate_route(name, loop, [pt()] if points is None else points, probe, CLEAR)

    def test_valid_route_is_normalised(self):
        name, route = self.v('  My   route ', 3, [pt('  Dock ', 1.23456789, 2, 7.0, 5)])
        self.assertEqual(name, 'My route')
        p = route['points'][0]
        self.assertEqual((route['loop'], p['name'], p['x']), (3, 'Dock', 1.2346))
        self.assertTrue(-math.pi < p['yaw'] <= math.pi)
        self.assertAlmostEqual(p['yaw'], 7.0 - 2 * math.pi, places=4)

    def test_default_point_name(self):
        _, route = self.v(points=[{'x': 1, 'y': 1, 'yaw': 0}, {'x': 2, 'y': 1, 'yaw': 0}])
        self.assertEqual([p['name'] for p in route['points']], ['Point 1', 'Point 2'])

    def test_auto_heading_flag_defaults_to_false_and_must_be_boolean(self):
        _, route = self.v(points=[pt(), dict(pt(), auto=True)])
        self.assertEqual([p['auto'] for p in route['points']], [False, True])
        for bad in (1, 0, 'true', None, [True]):
            e = code_of(self.v, points=[pt(), dict(pt(), auto=bad)])
            self.assertEqual((e.code, e.params['n']), ('route.err.point_invalid', 2), repr(bad))

    def test_bad_route_names(self):
        for bad in ('', '   ', None, 5, 'x' * (MAX_NAME + 1), 'a\x00b', 'a\x7fb'):
            self.assertEqual(code_of(self.v, name=bad).code, 'route.err.name', repr(bad))

    def test_whitespace_including_newlines_is_collapsed(self):
        self.assertEqual(self.v(name='a\n\t b')[0], 'a b')

    def test_loop_must_be_int_in_range(self):
        for bad in (-1, 100, 1.5, '3', True, None):
            self.assertEqual(code_of(self.v, loop=bad).code, 'route.err.loop', repr(bad))
        for good in (0, 1, 2, 99):
            self.v(loop=good)

    def test_points_shape_and_count(self):
        self.assertEqual(code_of(self.v, points=[]).code, 'route.err.empty')
        self.assertEqual(code_of(self.v, points='x').code, 'route.err.empty')
        self.assertEqual(code_of(self.v, points=[pt()] * (MAX_POINTS + 1)).code, 'route.err.too_many')
        self.v(points=[pt()] * MAX_POINTS)
        self.assertEqual(code_of(self.v, points=['x']).code, 'route.err.point_invalid')

    def test_numbers_must_be_finite_real(self):
        for field in ('x', 'y', 'yaw', 'wait'):
            for bad in (None, 'a', float('nan'), float('inf'), True):
                p = pt()
                p[field] = bad
                e = code_of(self.v, points=[pt(), p])
                self.assertEqual((e.code, e.params['n']), ('route.err.point_invalid', 2), (field, bad))

    def test_wait_range(self):
        self.assertEqual(code_of(self.v, points=[pt(wait=-1)]).code, 'route.err.wait')
        self.assertEqual(code_of(self.v, points=[pt(wait=601)]).code, 'route.err.wait')
        self.v(points=[pt(wait=0), pt(wait=600)])

    def test_point_name_invalid(self):
        self.assertEqual(code_of(self.v, points=[pt(name='x' * 41)]).code, 'route.err.point_name')

    def test_map_rules_reported_with_point_number(self):
        e = code_of(self.v, points=[pt(x=1.0), pt(x=4.8)], probe=probe_wall)
        self.assertEqual((e.code, e.params['n']), ('route.err.point_tight', 2))
        self.assertEqual(code_of(self.v, points=[pt(x=6)], probe=probe_wall).code, 'route.err.point_unknown')
        self.assertEqual(code_of(self.v, points=[pt(x=-1)], probe=probe_wall).code, 'route.err.point_outside')
        no_map = lambda x, y, r: {'status': 'no_map', 'clearance': None}    # noqa: E731
        self.assertEqual(code_of(self.v, probe=no_map).code, 'route.err.no_map')

    def test_normalize_yaw(self):
        for raw in (0, 1, -1, math.pi, -math.pi, 3 * math.pi, -7.5, 100.0):
            y = normalize_yaw(raw)
            self.assertTrue(-math.pi < y <= math.pi, raw)
            self.assertAlmostEqual(math.cos(y), math.cos(raw), places=9)


class StoreTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='rt_')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, 'nav', 'routes.json')
        self.errors = []
        self.store = RouteStore(self.path, VALID, on_error=self.errors.append)

    def save(self, name='R', map_name='lab', loop=1, points=None, **kw):
        return self.store.save(map_name, name, loop, [pt()] if points is None else points, probe_ok, CLEAR, **kw)

    def test_roundtrip_per_map_and_names(self):
        self.assertEqual(self.store.routes('lab'), {})
        self.save('A', 'lab', 3, [pt('Dock', 1, 2, 0.5, 5)], now=10.0)
        self.save('A', 'hall')
        r = self.store.get('lab', 'A')
        first = r['points'][0]
        self.assertEqual((r['loop'], r['saved'], first['name'], first['wait']), (3, 10.0, 'Dock', 5.0))
        self.assertEqual(sorted(self.store.routes('hall')), ['A'])
        self.assertEqual(self.store.routes('other'), {})
        self.assertEqual(self.store.routes(None), {})

    def test_auto_flag_roundtrips_and_old_files_without_it_mean_manual(self):
        self.save('A', points=[dict(pt('a'), auto=True), pt('b')])
        self.assertEqual([p['auto'] for p in self.store.get('lab', 'A')['points']], [True, False])
        with open(self.path, encoding='utf-8') as f:
            data = json.load(f)
        for p in data['lab']['A']['points']:
            del p['auto']
        with open(self.path, 'w', encoding='utf-8') as f:
            json.dump(data, f)
        self.assertEqual([p['auto'] for p in self.store.get('lab', 'A')['points']], [False, False])

    def test_update_in_place_and_duplicate_name_refused(self):
        self.save('A')
        self.assertEqual(code_of(self.save, 'A').code, 'route.err.exists')           # สร้างใหม่ชื่อซ้ำ
        self.save('A', loop=2, previous='A')                                          # แก้ตัวเอง = ได้
        self.assertEqual(self.store.get('lab', 'A')['loop'], 2)

    def test_rename_is_atomic(self):
        self.save('A')
        self.save('B')
        self.assertEqual(code_of(self.save, 'B', previous='A').code, 'route.err.exists')
        self.assertEqual(sorted(self.store.routes('lab')), ['A', 'B'])
        self.save('C', previous='A')
        self.assertEqual(sorted(self.store.routes('lab')), ['B', 'C'])
        self.assertEqual(code_of(self.save, 'Z', previous='nope').code, 'route.err.missing')

    def test_invalid_route_changes_nothing(self):
        self.save('A')
        before = open(self.path, encoding='utf-8').read()
        self.assertEqual(code_of(self.save, 'A', points=[pt(x=float('nan'))], previous='A').code,
                         'route.err.point_invalid')
        self.assertEqual(open(self.path, encoding='utf-8').read(), before)

    def test_bad_map_name_refused(self):
        self.assertEqual(code_of(self.save, map_name='../x').code, 'route.err.nomap')
        self.assertEqual(code_of(self.save, map_name=None).code, 'route.err.nomap')
        self.assertFalse(os.path.exists(self.path))

    def test_delete(self):
        self.save('A')
        self.save('B')
        self.store.delete('lab', 'A')
        self.assertEqual(sorted(self.store.routes('lab')), ['B'])
        self.store.delete('lab', 'B')
        self.assertEqual(json.load(open(self.path, encoding='utf-8')), {})
        self.assertEqual(code_of(self.store.delete, 'lab', 'B').code, 'route.err.missing')

    def test_route_count_limit(self):
        for i in range(50):
            self.save(f'R{i}')
        self.assertEqual(code_of(self.save, 'extra').code, 'route.err.too_many_routes')
        self.save('R0', loop=2, previous='R0')                                         # แก้ของเดิมยังได้

    def test_thai_names_survive(self):
        self.save('ส่งของ', points=[pt('จุดรับ')])
        self.assertEqual(self.store.get('lab', 'ส่งของ')['points'][0]['name'], 'จุดรับ')

    def test_corrupt_files_are_tolerated(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, 'w') as f:
            f.write('{not json')
        self.assertEqual(self.store.routes('lab'), {})
        self.assertTrue(self.errors)
        self.save('A')                                                               # เขียนทับได้
        with open(self.path, 'w', encoding='utf-8') as f:
            json.dump({'lab': {'ok': {'loop': 1, 'points': [pt()]}, 'bad': {'points': 'x'},
                               'empty': {'points': []}, 'half': {'points': [{'name': 'a'}]}}, 'other': 5}, f)
        self.assertEqual(sorted(self.store.routes('lab')), ['ok'])
        self.assertEqual(self.store.routes('other'), {})

    def test_write_failure_leaves_no_tmp_and_raises(self):
        os.makedirs(os.path.dirname(self.path))
        os.makedirs(self.path)                 # ชื่อไฟล์ถูกแทนด้วยโฟลเดอร์ → replace ล้ม
        self.assertEqual(code_of(self.save, 'A').code, 'route.err.io')
        self.assertFalse(os.path.exists(self.path + '.tmp'))


if __name__ == '__main__':
    unittest.main()
