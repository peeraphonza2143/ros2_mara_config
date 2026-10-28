"""
nav_params — ค่า Nav2 ที่ผู้ใช้ปรับได้ (ไม่พึ่ง ROS → ทดสอบได้) ใช้หลักเดียวกับ slam_params

หลักการ
  - "ค่า Default" = ค่าในไฟล์ของโปรเจกต์ (nav2_params_s2.yaml ที่จูนไว้แล้ว) ไม่ใช่ค่าตั้งต้นของ Nav2 เอง
  - ผู้ใช้แก้ได้เฉพาะชุดที่ปลอดภัย (SCHEMA) พร้อมช่วงต่ำสุด/สูงสุดที่กำหนดในโค้ด ไม่ใช่ตามที่ผู้ใช้พิมพ์ ;
    footprint / plugin / topic / BT / AMCL ไม่เปิดให้แก้ (แก้ผิดตัวเดียวหุ่นหยุดหรือเพี้ยนเงียบๆ)
  - ค่าที่แก้เก็บเป็น "ส่วนต่าง" จาก Default ที่ <data_dir>/nav/nav_overrides.json (คืนค่า Default = ลบส่วนต่าง)
  - ตอนเปิด Navigation สร้างไฟล์ YAML ใหม่ = ไฟล์ Default ทั้งไฟล์ + ส่วนต่าง แล้วส่ง nav2_params:=<ไฟล์> ให้ launch
    (ไม่แก้ไฟล์ต้นฉบับของโปรเจกต์) ; ไม่มีส่วนต่าง = ไม่ส่งอะไรเลย (ใช้ไฟล์เดิมของ launch)
  - ค่าที่บันทึกมีผล "ตอนเปิด Navigation ครั้งถัดไป" (การปรับสดขณะ Nav รันเป็นอีกขั้นหนึ่ง)
  - ชนิดต้องตรงกับที่ Nav2 ประกาศ: ค่า double ต้องเขียนเป็น 3.0 (ไม่ใช่ 3) → ใช้ float() เสมอ
  - หนึ่งค่าเขียนได้หลายตำแหน่งในไฟล์ (เช่น inflation ของทั้ง global และ local costmap) ;
    ค่า Default ที่แสดงอ้างอิงตำแหน่งแรก
"""
import copy
import json
import math
import os
import threading

import yaml

GROUPS = ('speed', 'path', 'obstacle', 'goal', 'costmap')

_FOLLOW = ('controller_server', 'ros__parameters', 'FollowPath')
_GOAL = ('controller_server', 'ros__parameters', 'general_goal_checker')
_G_INFLATION = ('global_costmap', 'global_costmap', 'ros__parameters', 'inflation_layer')
_L_INFLATION = ('local_costmap', 'local_costmap', 'ros__parameters', 'inflation_layer')

# key → (กลุ่ม, ชนิด, หน่วย, ต่ำสุด, สูงสุด, step ของช่องกรอก, [ตำแหน่งในไฟล์ yaml ...])
# ความเร็วสูงสุด 0.5 m/s = เพดานเดียวกับการขับด้วยมือ (manual_max_linear) — เป็นค่าในโค้ด ผู้ใช้เพิ่มเองไม่ได้
SCHEMA = {
    'desired_linear_vel': ('speed', 'float', 'm/s', 0.05, 0.5, 0.05, [_FOLLOW + ('desired_linear_vel',)]),
    'rotate_to_heading_angular_vel': ('speed', 'float', 'rad/s', 0.1, 1.0, 0.05,
                                      [_FOLLOW + ('rotate_to_heading_angular_vel',)]),
    'max_angular_accel': ('speed', 'float', 'rad/s²', 0.2, 3.2, 0.1, [_FOLLOW + ('max_angular_accel',)]),
    'lookahead_dist': ('path', 'float', 'm', 0.3, 2.0, 0.05, [_FOLLOW + ('lookahead_dist',)]),
    'min_lookahead_dist': ('path', 'float', 'm', 0.2, 1.5, 0.05, [_FOLLOW + ('min_lookahead_dist',)]),
    'max_lookahead_dist': ('path', 'float', 'm', 0.5, 3.0, 0.05, [_FOLLOW + ('max_lookahead_dist',)]),
    'lookahead_time': ('path', 'float', 's', 0.5, 3.0, 0.1, [_FOLLOW + ('lookahead_time',)]),
    'use_collision_detection': ('obstacle', 'bool', '', None, None, None, [_FOLLOW + ('use_collision_detection',)]),
    'max_allowed_time_to_collision_up_to_carrot': (
        'obstacle', 'float', 's', 0.5, 5.0, 0.1,
        [_FOLLOW + ('max_allowed_time_to_collision_up_to_carrot',)]),
    'xy_goal_tolerance': ('goal', 'float', 'm', 0.05, 0.5, 0.01, [_GOAL + ('xy_goal_tolerance',)]),
    'yaw_goal_tolerance': ('goal', 'float', 'rad', 0.03, 0.5, 0.01, [_GOAL + ('yaw_goal_tolerance',)]),
    'inflation_radius': ('costmap', 'float', 'm', 0.35, 1.5, 0.05,
                         [_G_INFLATION + ('inflation_radius',), _L_INFLATION + ('inflation_radius',)]),
    'cost_scaling_factor': ('costmap', 'float', '', 1.0, 10.0, 0.5,
                            [_G_INFLATION + ('cost_scaling_factor',), _L_INFLATION + ('cost_scaling_factor',)]),
}


class NavParamError(ValueError):
    """ค่าที่ส่งมาไม่ถูกต้อง: code/params ใช้สร้างข้อความให้หน้าเว็บแปลภาษา"""

    def __init__(self, code, **params):
        super().__init__(code)
        self.code = code
        self.params = params


def _coerce(key, value):
    """แปลงค่าที่รับมา (จาก JSON) ให้เป็นชนิดที่ถูกต้อง หรือโยน NavParamError"""
    _group, kind, _unit, lo, hi, _step, _paths = SCHEMA[key]
    if kind == 'bool':
        if not isinstance(value, bool):
            raise NavParamError('nav.err.type', key=key)
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise NavParamError('nav.err.type', key=key)
    if isinstance(value, float) and not math.isfinite(value):
        raise NavParamError('nav.err.type', key=key)
    value = float(value)
    if not lo <= value <= hi:
        raise NavParamError('nav.err.range', key=key, min=lo, max=hi)
    return value


def _get(data, path):
    for part in path:
        data = data[part]
    return data


def _set(data, path, value):
    for part in path[:-1]:
        data = data[part]
    data[path[-1]] = value


class NavParams:

    def __init__(self, defaults_path, data_dir, on_error=None):
        self._defaults_path = os.path.expanduser(defaults_path) if defaults_path else None
        self._dir = os.path.join(os.path.expanduser(data_dir), 'nav')
        self._overrides_path = os.path.join(self._dir, 'nav_overrides.json')
        self.generated_path = os.path.join(self._dir, 'nav2_params_user.yaml')
        self._on_error = on_error or (lambda text: None)
        self._lock = threading.Lock()
        self._data, self.available = {}, False
        self._load_defaults()
        self._overrides = self._load_overrides() if self.available else {}

    # ── Default (จากไฟล์ของโปรเจกต์) ────────────────────────────────────
    def _load_defaults(self):
        try:
            with open(self._defaults_path, encoding='utf-8') as f:
                data = yaml.safe_load(f)
            if not isinstance(data, dict):
                raise ValueError('รูปแบบไฟล์ไม่ถูกต้อง')
        except (OSError, TypeError, ValueError, yaml.YAMLError) as e:
            self._on_error(f'อ่านไฟล์ค่า Default ของ Nav2 ไม่ได้ ({e}) — ปิดการปรับค่า Nav2')
            return
        for key, spec in SCHEMA.items():       # ทุกตำแหน่งต้องมีในไฟล์ และค่าของตำแหน่งแรกต้องถูกชนิด/ช่วง
            try:
                for path in spec[6]:
                    _get(data, path)
                _coerce(key, _get(data, spec[6][0]))
            except (KeyError, TypeError):
                self._on_error(f'ไม่พบ {key} ในไฟล์ค่า Default ของ Nav2 — ปิดการปรับค่า Nav2')
                return
            except NavParamError as e:
                self._on_error(f'ค่า Default ของ {key} ในไฟล์ไม่ถูกต้อง ({e.code}) — ปิดการปรับค่า Nav2')
                return
        self._data = data
        self.available = True

    def default(self, key):
        return _coerce(key, _get(self._data, SCHEMA[key][6][0]))

    # ── ส่วนต่างที่ผู้ใช้ตั้ง ────────────────────────────────────────────
    def _load_overrides(self):
        try:
            with open(self._overrides_path, encoding='utf-8') as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                raise ValueError('รูปแบบไม่ใช่ object')
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            self._on_error(f'อ่านค่า Nav2 ที่ตั้งไว้ไม่ได้ ({e}) — ใช้ค่า Default')
            return {}
        out = {}
        for key, value in raw.items():
            try:
                if key not in SCHEMA:
                    raise NavParamError('nav.err.unknown', key=key)
                coerced = _coerce(key, value)
            except NavParamError as e:
                self._on_error(f'ทิ้งค่า Nav2 ที่ตั้งไว้ของ {key} ({e.code})')
                continue
            if coerced != self.default(key):
                out[key] = coerced
        if not self._order_ok({k: out.get(k, self.default(k)) for k in SCHEMA}):
            self._on_error('ค่า lookahead ที่ตั้งไว้ขัดกันเอง — ใช้ค่า Default')
            return {}
        return out

    def _save_overrides(self):
        self._write_atomic(self._overrides_path, lambda f: json.dump(self._overrides, f))

    def _write_atomic(self, path, writer):
        """เขียนไฟล์ชั่วคราวก่อนแล้วค่อย replace (ไม่มีไฟล์ครึ่งๆ กลางๆ) ; ล้มเหลวเก็บกวาดไฟล์ชั่วคราวแล้วโยน OSError"""
        tmp = path + '.tmp'
        try:
            os.makedirs(self._dir, exist_ok=True)
            with open(tmp, 'w', encoding='utf-8') as f:
                writer(f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise

    @staticmethod
    def _order_ok(current):
        return current['min_lookahead_dist'] <= current['max_lookahead_dist']

    def value(self, key):
        return self._overrides.get(key, self.default(key))

    def effective(self):
        """ค่าของพารามิเตอร์ที่แก้ได้ทั้งหมด (Default + ส่วนต่าง)"""
        with self._lock:
            return {key: self.value(key) for key in SCHEMA} if self.available else {}

    # ── แก้ค่า ──────────────────────────────────────────────────────────
    def set_values(self, values):
        """ตั้งค่า (เฉพาะ key ที่ส่งมา) ; ค่าเท่า Default = ลบส่วนต่างของ key นั้น
        ผิดตรงไหนโยน NavParamError ก่อนเปลี่ยนอะไรเลย (all-or-nothing)
        บันทึกไฟล์ไม่ได้โยน OSError และไม่เปลี่ยนค่า"""
        if not self.available:
            raise NavParamError('nav.err.no_defaults')
        if not isinstance(values, dict) or not values:
            raise NavParamError('nav.err.empty')
        clean = {}
        for key, value in values.items():
            if key not in SCHEMA:
                raise NavParamError('nav.err.unknown', key=str(key))
            clean[key] = _coerce(key, value)
        with self._lock:
            merged = dict(self._overrides)
            for key, value in clean.items():
                if value == self.default(key):
                    merged.pop(key, None)
                else:
                    merged[key] = value
            if not self._order_ok({key: merged.get(key, self.default(key)) for key in SCHEMA}):
                raise NavParamError('nav.err.lookahead_order')
            before, self._overrides = self._overrides, merged
            try:
                self._save_overrides()
            except OSError:
                self._overrides = before
                raise

    # ── ส่งให้ launch ───────────────────────────────────────────────────
    def extra_args(self):
        """อาร์กิวเมนต์เพิ่มของคำสั่งเปิด Navigation: [] = ใช้ไฟล์เดิมของ launch ; มีส่วนต่าง = ไฟล์ใหม่ทั้งไฟล์
        เขียนไฟล์ไม่ได้โยน OSError (ดีกว่าเปิด Navigation ด้วยค่าที่ผู้ใช้ไม่ได้ตั้งโดยไม่บอก)"""
        with self._lock:
            if not self.available or not self._overrides:
                return []
            data = copy.deepcopy(self._data)
            for key, value in self._overrides.items():
                for path in SCHEMA[key][6]:
                    _set(data, path, value)
            text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
            self._write_atomic(self.generated_path, lambda f: f.write(
                '# Generated by the AGV web app (project defaults + user changes).\n'
                '# Editing this file has no effect.\n' + text))
            return [f'nav2_params:={self.generated_path}']

    # ── ส่งให้หน้าเว็บ ──────────────────────────────────────────────────
    def info(self, in_use=None):
        """in_use = ค่าที่ Navigation เปิดไว้ตอนนี้ (None = Nav ไม่ได้รัน) ;
        restart_needed = Nav รันอยู่แต่ค่าที่ตั้งไว้ตอนนี้ไม่ตรงกับที่ใช้อยู่"""
        with self._lock:
            if not self.available:
                return {'available': False, 'groups': [], 'modified': 0, 'in_use': None, 'restart_needed': False}
            groups = []
            for gid in GROUPS:
                rows = []
                for key, (group, kind, unit, lo, hi, step, _paths) in SCHEMA.items():
                    if group != gid:
                        continue
                    default = self.default(key)
                    value = self.value(key)
                    rows.append({'key': key, 'type': kind, 'unit': unit, 'min': lo, 'max': hi, 'step': step,
                                 'default': default, 'value': value, 'modified': value != default})
                groups.append({'id': gid, 'params': rows})
            current = {key: self.value(key) for key in SCHEMA}
            return {'available': True, 'groups': groups, 'modified': len(self._overrides), 'in_use': in_use,
                    'restart_needed': in_use is not None and in_use != current}
