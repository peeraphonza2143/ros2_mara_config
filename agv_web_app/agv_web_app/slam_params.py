"""
slam_params — ค่า SLAM Toolbox ที่ผู้ใช้ปรับได้ก่อนเริ่ม Mapping (ไม่พึ่ง ROS → ทดสอบได้)

หลักการ
  - "ค่า Default" = ค่าในไฟล์ของโปรเจกต์ (mapper_params_s2_factory.yaml ที่จูนไว้แล้ว)
    ไม่ใช่ค่าตั้งต้นของ slam_toolbox เอง
  - ผู้ใช้แก้ได้เฉพาะชุดที่ปลอดภัย (SCHEMA) ; ตัวอื่นแสดงอ่านอย่างเดียว
    (frame / topic / solver ฯลฯ — แก้ผิดตัวเดียว SLAM ไม่ขึ้น)
  - ค่าที่แก้เก็บเป็น "ส่วนต่าง" จาก Default ที่ <data_dir>/slam/overrides.json (คืนค่าเดิม = ลบส่วนต่าง)
  - ตอนเริ่ม Mapping สร้างไฟล์ YAML ใหม่ = ไฟล์ Default + ส่วนต่าง แล้วส่งให้ launch ด้วย slam_params_file:=<ไฟล์>
    (ไม่แก้ไฟล์ต้นฉบับของโปรเจกต์) ; ไม่มีส่วนต่าง = ไม่ส่งอะไรเลย ใช้ไฟล์เดิมของ launch ตามปกติ
  - ชนิดต้องตรงกับที่ ROS ประกาศ: ค่า double ต้องเขียนเป็น 3.0 (ไม่ใช่ 3) ไม่งั้น slam_toolbox ปฏิเสธ
"""
import json
import math
import os
import threading

import yaml

GROUPS = ('map', 'update', 'motion', 'match', 'loop')

# key → (กลุ่ม, ชนิด, หน่วย, ต่ำสุด, สูงสุด, step ของช่องกรอก)
SCHEMA = {
    'resolution': ('map', 'float', 'm', 0.02, 0.2, 0.01),
    'max_laser_range': ('map', 'float', 'm', 5.0, 30.0, 0.5),
    'map_update_interval': ('update', 'float', 's', 0.5, 10.0, 0.5),
    'minimum_time_interval': ('update', 'float', 's', 0.05, 2.0, 0.05),
    'throttle_scans': ('update', 'int', '', 1, 10, 1),
    'minimum_travel_distance': ('motion', 'float', 'm', 0.02, 1.0, 0.01),
    'minimum_travel_heading': ('motion', 'float', 'rad', 0.02, 1.0, 0.01),
    'scan_buffer_maximum_scan_distance': ('match', 'float', 'm', 3.0, 30.0, 0.5),
    'do_loop_closing': ('loop', 'bool', '', None, None, None),
    'loop_search_maximum_distance': ('loop', 'float', 'm', 1.0, 20.0, 0.5),
    'loop_match_minimum_response_coarse': ('loop', 'float', '', 0.1, 0.9, 0.01),
    'loop_match_minimum_response_fine': ('loop', 'float', '', 0.1, 0.95, 0.01),
}


class SlamParamError(ValueError):
    """ค่าที่ส่งมาไม่ถูกต้อง: code/params ใช้สร้างข้อความให้หน้าเว็บแปลภาษา"""

    def __init__(self, code, **params):
        super().__init__(code)
        self.code = code
        self.params = params


def _coerce(key, value):
    """แปลงค่าที่รับมา (จาก JSON) ให้เป็นชนิดที่ถูกต้อง หรือโยน SlamParamError"""
    _group, kind, _unit, lo, hi, _step = SCHEMA[key]
    if kind == 'bool':
        if not isinstance(value, bool):
            raise SlamParamError('slam.err.type', key=key)
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SlamParamError('slam.err.type', key=key)
    if isinstance(value, float) and not math.isfinite(value):
        raise SlamParamError('slam.err.type', key=key)
    if kind == 'int':
        if isinstance(value, float) and not value.is_integer():
            raise SlamParamError('slam.err.type', key=key)
        value = int(value)
    else:
        value = float(value)
    if not lo <= value <= hi:
        raise SlamParamError('slam.err.range', key=key, min=lo, max=hi)
    return value


class SlamParams:

    def __init__(self, defaults_path, data_dir, on_error=None):
        self._defaults_path = os.path.expanduser(defaults_path) if defaults_path else None
        self._dir = os.path.join(os.path.expanduser(data_dir), 'slam')
        self._overrides_path = os.path.join(self._dir, 'overrides.json')
        self.generated_path = os.path.join(self._dir, 'mapper_params_user.yaml')
        self._on_error = on_error or (lambda text: None)
        self._lock = threading.Lock()
        self._node_key, self._all, self.available = 'slam_toolbox', {}, False
        self._load_defaults()
        self._overrides = self._load_overrides() if self.available else {}

    # ── Default (จากไฟล์ของโปรเจกต์) ────────────────────────────────────
    def _load_defaults(self):
        try:
            with open(self._defaults_path, encoding='utf-8') as f:
                data = yaml.safe_load(f)
            self._node_key = next(iter(data))
            params = dict(data[self._node_key]['ros__parameters'])
        except (OSError, TypeError, StopIteration, KeyError, AttributeError, yaml.YAMLError) as e:
            self._on_error(f'อ่านไฟล์ค่า Default ของ SLAM ไม่ได้ ({e}) — ปิดการปรับค่า SLAM')
            return
        self._all = params
        for key in SCHEMA:                      # ค่า Default ของตัวที่แก้ได้ต้องมีและถูกชนิด/ช่วง
            try:
                if key not in params:
                    raise SlamParamError('slam.err.unknown', key=key)
                _coerce(key, params[key])
            except SlamParamError as e:
                self._on_error(f'ค่า Default ของ {key} ในไฟล์ไม่ถูกต้อง ({e.code}) — ปิดการปรับค่า SLAM')
                self._all = {}
                return
        self.available = True

    def default(self, key):
        return _coerce(key, self._all[key])

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
            self._on_error(f'อ่านค่า SLAM ที่ตั้งไว้ไม่ได้ ({e}) — ใช้ค่า Default')
            return {}
        out = {}
        for key, value in raw.items():
            try:
                if key not in SCHEMA:
                    raise SlamParamError('slam.err.unknown', key=key)
                coerced = _coerce(key, value)
            except SlamParamError as e:
                self._on_error(f'ทิ้งค่า SLAM ที่ตั้งไว้ของ {key} ({e.code})')
                continue
            if coerced != self.default(key):
                out[key] = coerced
        return out

    def _save_overrides(self):
        tmp = self._overrides_path + '.tmp'
        os.makedirs(self._dir, exist_ok=True)
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(self._overrides, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._overrides_path)

    def value(self, key):
        return self._overrides.get(key, self.default(key))

    def effective(self):
        """ค่าของพารามิเตอร์ที่แก้ได้ทั้งหมด (Default + ส่วนต่าง)"""
        with self._lock:
            return {key: self.value(key) for key in SCHEMA} if self.available else {}

    # ── แก้ค่า ──────────────────────────────────────────────────────────
    def set_values(self, values):
        """ตั้งค่า (เฉพาะ key ที่ส่งมา) ; ค่าเท่า Default = ลบส่วนต่างของ key นั้น
        ผิดตรงไหนโยน SlamParamError ก่อนเปลี่ยนอะไรเลย (all-or-nothing)
        บันทึกไฟล์ไม่ได้โยน OSError และไม่เปลี่ยนค่า"""
        if not self.available:
            raise SlamParamError('slam.err.no_defaults')
        if not isinstance(values, dict) or not values:
            raise SlamParamError('slam.err.empty')
        clean = {}
        for key, value in values.items():
            if key not in SCHEMA:
                raise SlamParamError('slam.err.unknown', key=str(key))
            clean[key] = _coerce(key, value)
        with self._lock:
            merged = dict(self._overrides)
            for key, value in clean.items():
                if value == self.default(key):
                    merged.pop(key, None)
                else:
                    merged[key] = value
            current = {key: merged.get(key, self.default(key)) for key in SCHEMA}
            if current['loop_match_minimum_response_fine'] < current['loop_match_minimum_response_coarse']:
                raise SlamParamError('slam.err.loop_order')
            before, self._overrides = self._overrides, merged
            try:
                self._save_overrides()
            except OSError:
                self._overrides = before
                raise

    # ── ส่งให้ launch ───────────────────────────────────────────────────
    def extra_args(self):
        """อาร์กิวเมนต์เพิ่มของคำสั่งเปิด Mapping: [] = ใช้ไฟล์เดิมของ launch ; มีส่วนต่าง = ไฟล์ใหม่
        เขียนไฟล์ไม่ได้โยน OSError (ดีกว่าเปิด Mapping ด้วยค่าที่ผู้ใช้ไม่ได้ตั้งโดยไม่บอก)"""
        with self._lock:
            if not self.available or not self._overrides:
                return []
            params = dict(self._all)
            params.update(self._overrides)
            text = yaml.safe_dump({self._node_key: {'ros__parameters': params}},
                                  sort_keys=False, allow_unicode=True)
            tmp = self.generated_path + '.tmp'
            os.makedirs(self._dir, exist_ok=True)
            with open(tmp, 'w', encoding='utf-8') as f:
                f.write('# Generated by the AGV web app (project defaults + user changes).\n'
                        '# Editing this file has no effect.\n' + text)
            os.replace(tmp, self.generated_path)
            return [f'slam_params_file:={self.generated_path}']

    # ── ส่งให้หน้าเว็บ ──────────────────────────────────────────────────
    def info(self, in_use=None):
        with self._lock:
            if not self.available:
                return {'available': False, 'groups': [], 'advanced': [], 'modified': 0, 'in_use': None}
            groups = []
            for gid in GROUPS:
                rows = []
                for key, (group, kind, unit, lo, hi, step) in SCHEMA.items():
                    if group != gid:
                        continue
                    default = self.default(key)
                    value = self.value(key)
                    rows.append({'key': key, 'type': kind, 'unit': unit, 'min': lo, 'max': hi, 'step': step,
                                 'default': default, 'value': value, 'modified': value != default})
                groups.append({'id': gid, 'params': rows})
            advanced = [{'key': k, 'value': v} for k, v in self._all.items()
                        if k not in SCHEMA and isinstance(v, (str, int, float, bool))]
            return {'available': True, 'groups': groups, 'advanced': advanced,
                    'modified': len(self._overrides), 'in_use': in_use}
