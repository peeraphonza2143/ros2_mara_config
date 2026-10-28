"""
start_pose — จุดเริ่มต้นประจำแผนที่ + วางตำแหน่งให้อัตโนมัติเมื่อ Navigation เริ่ม (ไม่พึ่ง ROS → ทดสอบด้วยของปลอมได้)

แนวคิด: หุ่นจอดที่จุดเดิมทุกครั้ง → บันทึก "จุดเริ่มต้น" ไว้ต่อแผนที่ครั้งเดียว
แล้วทุกครั้งที่ Navigation เริ่มทำงานนิ่ง
เว็บส่งจุดนั้นให้ AMCL เอง (เหมือนกด Set robot position ให้) — ผู้ใช้ไม่ต้องวางตำแหน่งเองทุกรอบ
  - ผูกกับ "ชื่อแผนที่ที่บันทึกไว้" เท่านั้น (แผนที่ default/จาก config ไม่มีชื่อ = ไม่มีจุดเริ่มต้น)
  - ไม่เดา: ถ้าจอดผิดที่ AMCL ก็ผิดตาม → หน้าเว็บแสดงชัดว่า "ใช้จุดเริ่มต้นอัตโนมัติ" และวางใหม่/ล้างได้
  - เริ่มทำครั้งเดียวต่อรอบการรันของ Navigation (เทียบจาก since ของโหมด) ; AMCL/แผนที่ยังไม่พร้อมก็ลองซ้ำจนครบเวลา
"""
import json
import os
import threading
import time

from agv_web_app.nav_pose import PoseError, validate

RETRY_S = 1.0
DEADLINE_S = 45.0


class StartPoseError(Exception):
    def __init__(self, code, **params):
        super().__init__(code)
        self.code = code
        self.params = params


class StartPoses:

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
            self._on_error(f'อ่านจุดเริ่มต้นไม่ได้ ({self.path}): {e}')
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data):
        tmp = self.path + '.tmp'
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f)
            os.replace(tmp, self.path)
        except OSError as e:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise StartPoseError('nav.err.startpose_io', detail=str(e)[:160])

    def get(self, name):
        """{'x','y','yaw','saved'} หรือ None (ชื่อไม่ถูกต้อง/ไม่มี/ข้อมูลเสีย)"""
        if not self._valid_name(name):
            return None
        item = self._load().get(name)
        try:
            return {'x': float(item['x']), 'y': float(item['y']), 'yaw': float(item['yaw']),
                    'saved': float(item.get('saved', 0))}
        except (TypeError, KeyError, ValueError):
            return None

    def set(self, name, x, y, yaw, meta, now=None):
        """บันทึกจุดเริ่มต้นของแผนที่ ; ตรวจเหมือนการวางตำแหน่ง (ตัวเลข/อยู่ในกรอบแผนที่) ; คืนค่าที่บันทึก"""
        if not self._valid_name(name):
            raise StartPoseError('nav.err.startpose_nomap')
        try:
            x, y, yaw = validate(x, y, yaw, meta)
        except PoseError as e:
            raise StartPoseError(e.code, **e.params)
        with self._lock:
            data = self._load()
            data[name] = {'x': x, 'y': y, 'yaw': yaw, 'saved': time.time() if now is None else now}
            self._save(data)
        return self.get(name)

    def clear(self, name):
        with self._lock:
            data = self._load()
            if data.pop(name, None) is not None:
                self._save(data)


def with_deg(pose):
    import math
    return None if pose is None else dict(pose, yaw_deg=round(math.degrees(pose['yaw']), 1))


class AutoStart:
    """ตัวทำงานอัตโนมัติ: เรียก tick(mode) ~1 Hz ; เจอ Navigation เริ่มรอบใหม่ → ส่งจุดเริ่มต้นให้ AMCL (thread แยก)"""

    def __init__(self, poses, initial_pose, get_meta, map_name, on_event=None, sleep=time.sleep,
                 clock=time.monotonic, deadline=DEADLINE_S):
        self._poses, self._initial = poses, initial_pose
        self._get_meta, self._map_name = get_meta, map_name
        self._on_event = on_event or (lambda text: None)
        self._sleep, self._clock, self._deadline = sleep, clock, deadline
        self._lock = threading.Lock()
        self._run = None
        self._state, self._error, self._name = 'idle', None, None
        self.thread = None

    @staticmethod
    def _running(mode):
        return mode['mode'] == 'NAVIGATION' and mode['state'] == 'RUNNING' and not mode.get('target')

    def tick(self, mode):
        if not self._running(mode):
            with self._lock:
                if self._run is not None or self._state != 'idle':
                    self._run, self._state, self._error, self._name = None, 'idle', None, None
            return
        key = mode.get('since')
        with self._lock:
            if key == self._run:
                return
            self._run = key
            name = self._map_name()
            pose = self._poses.get(name) if name else None
            self._name = name
            if pose is None:
                self._state, self._error = 'none', None
                return
            self._state, self._error = 'pending', None
        self.thread = threading.Thread(target=self._apply, args=(key, name, pose), daemon=True)
        self.thread.start()

    def _current(self, key):
        with self._lock:
            return self._run == key

    def _apply(self, key, name, pose):
        end = self._clock() + self._deadline
        error = 'nav.err.no_map'
        while self._current(key):
            try:
                self._initial.set(pose['x'], pose['y'], pose['yaw'], self._get_meta())
            except PoseError as e:
                error = e.code
                if e.code not in ('nav.err.no_map', 'nav.err.no_listener', 'nav.err.busy') or self._clock() >= end:
                    break
                self._sleep(RETRY_S)
                continue
            with self._lock:
                if self._run == key:
                    self._state, self._error = 'applied', None
            self._on_event(f'Start pose applied automatically for map {name}')
            return
        with self._lock:
            if self._run == key:
                self._state, self._error = 'failed', error
        self._on_event(f'Start pose NOT applied for map {name}: {error}')

    def note_manual(self, name):
        """ผู้ใช้กด Use start pose เอง → บันทึกสถานะว่าใช้แล้ว"""
        with self._lock:
            self._state, self._error, self._name = 'applied', None, name

    def status(self):
        with self._lock:
            return {'state': self._state, 'error': self._error, 'name': self._name}
