"""
routes — Route (ลำดับจุดหมาย) ต่อแผนที่ (ไม่พึ่ง ROS → ทดสอบด้วยของปลอมได้)

แนวคิด: 1 แผนที่มีได้หลาย Route ; Route = ลำดับจุด (ชื่อ, x, y, ทิศ, เวลาหยุดรอ) + จำนวนรอบ (loop)
  - ผูกกับ "ชื่อแผนที่ที่บันทึกไว้" เท่านั้น (เหมือนจุดเริ่มต้น) ; เก็บที่ <data_dir>/nav/routes.json
  - บันทึกได้เฉพาะจุดที่ "หุ่นไปได้จริง": ผ่านกติกาเดียวกับการส่ง goal (รู้ว่าว่าง + ห่างผนัง ≥ clearance)
    ตรวจที่เซิร์ฟเวอร์ ไม่ใช่แค่หน้าเว็บ ; ผิดข้อไหนไม่บันทึกอะไรเลย (all-or-nothing)
  - ไฟล์นี้ "แก้ไข/เก็บ" อย่างเดียว ไม่สั่งหุ่น (การวิ่งตาม Route เป็นอีกชั้นหนึ่ง)
"""
import json
import math
import os
import threading
import time

from agv_web_app.nav_goal import LINE_MIN_M

MAX_POINTS = 50
MAX_NAME = 40
MAX_WAIT_S = 600.0
MAX_LOOP = 99                    # 1 = รอบเดียว · 0 = วนไม่จบ · 2..99 = จำนวนรอบ
MAX_ROUTES = 50
MODES = ('navigate', 'straight')   # navigate = ไปทีละจุดผ่าน planner (เดิม) · straight = ทุกช่วงเป็นเส้นตรงจริง ข้าม planner

PROBE_ERRORS = {
    'no_map': 'route.err.no_map',
    'outside': 'route.err.point_outside',
    'unknown': 'route.err.point_unknown',
    'occupied': 'route.err.point_unknown',
    'tight': 'route.err.point_tight',
}


class RouteError(Exception):
    def __init__(self, code, **params):
        super().__init__(code)
        self.code = code
        self.params = params


def clean_name(value, code):
    """ชื่อ Route/จุด: ข้อความ 1..MAX_NAME ตัว ไม่มีอักขระควบคุม ; คืนชื่อที่ตัดช่องว่างหัวท้ายแล้ว"""
    if not isinstance(value, str):
        raise RouteError(code)
    name = ' '.join(value.split())
    if not name or len(name) > MAX_NAME or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise RouteError(code)
    return name


def _finite(value, code, **params):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise RouteError(code, **params)
    return float(value)


def normalize_yaw(yaw):
    yaw = math.fmod(yaw, 2 * math.pi)
    if yaw > math.pi:
        yaw -= 2 * math.pi
    elif yaw <= -math.pi:
        yaw += 2 * math.pi
    return yaw


def validate_route(name, loop, points, probe, clearance, mode='navigate'):
    """ตรวจ Route ทั้งชุด → คืน (ชื่อที่ล้างแล้ว, route dict) หรือโยน RouteError
    probe(x, y, radius) -> {'status', 'clearance'} (LiveMap.probe)
    mode = 'straight' : ต้องมีอย่างน้อย 2 จุด และทุกช่วงยาวพอให้เป็นเส้น (ตรวจแนวกับแผนที่ตอนสั่งรัน)"""
    name = clean_name(name, 'route.err.name')
    if mode not in MODES:
        raise RouteError('route.err.mode')
    if isinstance(loop, bool) or not isinstance(loop, int) or not 0 <= loop <= MAX_LOOP:
        raise RouteError('route.err.loop', max=MAX_LOOP)
    if not isinstance(points, list) or not points:
        raise RouteError('route.err.empty')
    if len(points) > MAX_POINTS:
        raise RouteError('route.err.too_many', max=MAX_POINTS)
    out = []
    for i, p in enumerate(points, start=1):
        if not isinstance(p, dict):
            raise RouteError('route.err.point_invalid', n=i)
        pname = clean_name(p.get('name'), 'route.err.point_name') if p.get('name') is not None else f'Point {i}'
        x = _finite(p.get('x'), 'route.err.point_invalid', n=i)
        y = _finite(p.get('y'), 'route.err.point_invalid', n=i)
        yaw = normalize_yaw(_finite(p.get('yaw'), 'route.err.point_invalid', n=i))
        wait = _finite(p.get('wait', 0), 'route.err.point_invalid', n=i)
        auto = p.get('auto', False)                       # ทิศหุ่นอัตโนมัติ : หน้าเว็บคำนวณ yaw เอง ที่นี่แค่จำธง
        if not isinstance(auto, bool):
            raise RouteError('route.err.point_invalid', n=i)
        stop = p.get('stop', True)                        # False = วิ่งผ่านไปเลยด้วยความเร็วปกติ (เฉพาะโหมด straight)
        if not isinstance(stop, bool):
            raise RouteError('route.err.point_invalid', n=i)
        if not stop and mode != 'straight':
            raise RouteError('route.err.pass_navigate', n=i)
        if i == 1 or i == len(points):                    # จุดแรก (หุ่นเริ่มที่นี่/ปิดวงมาถึงที่นี่) กับจุดสุดท้าย = หยุดเสมอ
            stop = True
        if not 0 <= wait <= MAX_WAIT_S:
            raise RouteError('route.err.wait', n=i, max=int(MAX_WAIT_S))
        result = probe(x, y, clearance)
        if result['status'] != 'ok':
            raise RouteError(PROBE_ERRORS.get(result['status'], 'route.err.point_unknown'), n=i,
                             clearance=clearance, found=result['clearance'])
        out.append({'name': pname, 'x': round(x, 4), 'y': round(y, 4), 'yaw': round(yaw, 5), 'wait': round(wait, 1),
                    'auto': auto, 'stop': stop})
    if mode == 'straight':
        _check_segments(out, loop)
    return name, {'loop': loop, 'mode': mode, 'points': out}


def _check_segments(points, loop):
    """Route โหมด straight : ≥ 2 จุด · ทุกช่วงยาวอย่างน้อย LINE_MIN_M (ช่วงปิดวงจากจุดท้ายกลับจุดแรกด้วย ถ้ามีหลายรอบ)"""
    if len(points) < 2:
        raise RouteError('route.err.straight_points', min=2)
    pairs = list(zip(points, points[1:]))
    if loop != 1:
        pairs.append((points[-1], points[0]))
    for k, (a, b) in enumerate(pairs, start=1):
        if math.hypot(b['x'] - a['x'], b['y'] - a['y']) < LINE_MIN_M:
            raise RouteError('route.err.segment_short', n=k, min=LINE_MIN_M)


class RouteStore:

    def __init__(self, path, valid_name, on_error=None):
        self.path = os.path.expanduser(path)
        self._valid_name = valid_name
        self._on_error = on_error or (lambda text: None)
        self._lock = threading.Lock()

    def _load(self):
        try:
            with open(self.path, encoding='utf-8') as f:
                data = json.load(f)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            self._on_error(f'อ่าน Route ไม่ได้ ({self.path}): {e}')
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data):
        tmp = self.path + '.tmp'
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError as e:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise RouteError('route.err.io', detail=str(e)[:160])

    @staticmethod
    def _tidy(item):
        """route ที่อ่านจากไฟล์: คืนเฉพาะรูปแบบที่ถูกต้อง (ไฟล์ถูกแก้มือ/เสียบางส่วน ไม่ทำให้ทั้งหน้าพัง)"""
        try:
            pts = [{'name': str(p['name']), 'x': float(p['x']), 'y': float(p['y']), 'yaw': float(p['yaw']),
                    'wait': float(p.get('wait', 0)), 'auto': p.get('auto', False) is True,   # ไฟล์เก่าไม่มีธง = ตั้งเอง
                    'stop': p.get('stop', True) is not False}                                # ไฟล์เก่าไม่มีธง = หยุดทุกจุด
                   for p in item['points']]
            loop = int(item.get('loop', 1))
        except (TypeError, KeyError, ValueError, AttributeError):
            return None
        mode = item.get('mode') if item.get('mode') in MODES else 'navigate'        # ไฟล์เก่าไม่มี mode = โหมดเดิม
        return {'loop': loop, 'mode': mode, 'points': pts, 'saved': float(item.get('saved', 0) or 0)} if pts else None

    def routes(self, map_name):
        """{ชื่อ route: {'loop','points','saved'}} ของแผนที่นี้ (ว่างถ้าไม่มีชื่อแผนที่)"""
        if not self._valid_name(map_name):
            return {}
        raw = self._load().get(map_name)
        if not isinstance(raw, dict):
            return {}
        tidy = {k: self._tidy(v) for k, v in raw.items()}
        return {k: v for k, v in tidy.items() if v is not None}

    def get(self, map_name, name):
        return self.routes(map_name).get(name)

    def save(self, map_name, name, loop, points, probe, clearance, previous=None, now=None, mode='navigate'):
        """บันทึก/แก้ Route ; previous = ชื่อเดิม (เปลี่ยนชื่อ = ลบชื่อเดิมในครั้งเดียวกัน) ; คืน (ชื่อ, route)"""
        if not self._valid_name(map_name):
            raise RouteError('route.err.nomap')
        name, route = validate_route(name, loop, points, probe, clearance, mode)
        with self._lock:
            data = self._load()
            mine = data.get(map_name) if isinstance(data.get(map_name), dict) else {}
            if previous is not None and previous != name and previous not in mine:
                raise RouteError('route.err.missing')
            if name != previous and name in mine:
                raise RouteError('route.err.exists')
            if name not in mine and len(mine) >= MAX_ROUTES:
                raise RouteError('route.err.too_many_routes', max=MAX_ROUTES)
            route['saved'] = time.time() if now is None else now
            if previous is not None and previous != name:
                mine.pop(previous, None)
            mine[name] = route
            data[map_name] = mine
            self._save(data)
        return name, route

    def delete(self, map_name, name):
        if not self._valid_name(map_name):
            raise RouteError('route.err.nomap')
        with self._lock:
            data = self._load()
            mine = data.get(map_name)
            if not isinstance(mine, dict) or name not in mine:
                raise RouteError('route.err.missing')
            del mine[name]
            if not mine:
                del data[map_name]
            self._save(data)
