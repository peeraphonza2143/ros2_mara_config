"""
drive_control — ตัวควบคุมการขับหุ่นด้วยมือจากหน้าเว็บ (จอยสติ๊ก/ปุ่มทิศทาง) ที่ปลอดภัยไว้ก่อน (ไม่พึ่ง ROS → ทดสอบได้)

เป็นตัวเดียวในเว็บแอปที่ "ตัดสินใจ" ว่าจะ publish ความเร็วอะไรที่ /cmd_vel ; ทุกชั้นป้องกันอยู่ที่นี่และใน API:
  1. เพดานความเร็วจริงอยู่ที่ฝั่งหุ่น (max_linear / max_angular จาก config) — หน้าเว็บส่งมาเกินก็ถูกตัด
  2. หน้าเว็บส่ง "สัดส่วน" (-1..1) ไม่ใช่ความเร็วตรงๆ ; ความเร็ว = สัดส่วน × ค่าสูงสุดที่ผู้ใช้ตั้ง (≤ เพดาน)
  3. จำกัดอัตราเร่ง/เบรก (slew rate): ไม่กระชาก
  4. deadman: ต้องมีคำสั่งใหม่ภายใน heartbeat_timeout ไม่งั้นลดความเร็วลงศูนย์เอง (WiFi หลุด/แท็บถูกซ่อน/โปรแกรมค้าง)
  5. stop() = ศูนย์ทันที ไม่มี ramp
  6. publish เฉพาะตอน "กำลังขับ" และช่วงสั้นๆ หลังหยุด (ส่งศูนย์ซ้ำ) แล้วหยุด publish — ไม่ส่งศูนย์ค้างตลอดเวลา
     (ไม่งั้นจะไปแย่ง /cmd_vel กับ Nav2 / teleop ตัวอื่น)

เรียก tick() เป็นจังหวะคงที่ (เช่น 20 Hz) ; คืน (v, w) ที่ควร publish หรือ None (ไม่ต้อง publish)
"""
import math
import threading
import time

DRIVE_MODES = ('MAPPING', 'MANUAL')           # โหมดที่ขับด้วยมือได้ (Navigation ห้าม: Nav2 สั่ง /cmd_vel อยู่)
RELEASE_HOLD_S = 0.5                           # หลังหยุด ส่งศูนย์ต่ออีกกี่วินาที


def drive_allowed(mode_snapshot):
    """ขับด้วยมือได้ไหมในสถานะโหมดนี้ (จาก ModeManager.snapshot())"""
    return (mode_snapshot.get('mode') in DRIVE_MODES and mode_snapshot.get('state') == 'RUNNING'
            and not mode_snapshot.get('external') and not mode_snapshot.get('leftover'))


def _finite(*values):
    return all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in values)


def _slew(current, target, accel, decel, dt):
    """ขยับ current เข้าหา target ไม่เกินอัตรา (accel = เร่งออกจากศูนย์/เพิ่มขนาด, decel = ลดขนาด/กลับทิศ)"""
    if current == target:
        return current
    slowing = current != 0 and (target * current <= 0 or abs(target) < abs(current))
    step = (decel if slowing else accel) * dt
    if abs(target - current) <= step:
        return target
    return current + math.copysign(step, target - current)


class DriveController:

    def __init__(self, max_linear=0.5, max_angular=1.0, default_linear=0.25, default_angular=0.6,
                 linear_accel=0.6, angular_accel=1.5, linear_decel=1.5, angular_decel=3.0,
                 heartbeat_timeout=0.3, release_hold=RELEASE_HOLD_S, clock=time.monotonic):
        self.max_linear, self.max_angular = float(max_linear), float(max_angular)
        self.default_linear = min(float(default_linear), self.max_linear)
        self.default_angular = min(float(default_angular), self.max_angular)
        self._accel = (float(linear_accel), float(angular_accel))
        self._decel = (float(linear_decel), float(angular_decel))
        self.heartbeat_timeout = float(heartbeat_timeout)
        self.release_hold = float(release_hold)
        self._clock = clock
        self._lock = threading.Lock()
        self._target = (0.0, 0.0)
        self._current = (0.0, 0.0)
        self._last_cmd = None            # เวลาของคำสั่งล่าสุด
        self._last_tick = None
        self._hold_until = 0.0           # ส่งศูนย์ต่อจนถึงเวลานี้ (หลังหยุด)
        self.reason = 'idle'             # idle | driving | released | timeout | stop | <เหตุผลที่ผู้เรียกระบุ>

    # ── รับคำสั่ง ────────────────────────────────────────────────────────
    def command(self, lin, ang, vmax=None, wmax=None, now=None):
        """lin/ang = สัดส่วน -1..1 (หน้า/ซ้าย เป็นบวก) ; vmax/wmax = ค่าสูงสุดที่ผู้ใช้ตั้ง (ตัดที่เพดาน)
        คืน True ถ้ารับ ; ค่าผิดรูป (NaN/inf/ไม่ใช่ตัวเลข) → False และ "หยุด" ทันที (ไม่ปล่อยคำสั่งเก่าค้าง)"""
        now = self._clock() if now is None else now
        extras = [v for v in (vmax, wmax) if v is not None]
        if not _finite(lin, ang) or (extras and not _finite(*extras)):
            self.stop('invalid', now)
            return False
        vmax = self.default_linear if vmax is None else max(0.0, min(float(vmax), self.max_linear))
        wmax = self.default_angular if wmax is None else max(0.0, min(float(wmax), self.max_angular))
        lin = max(-1.0, min(1.0, float(lin)))
        ang = max(-1.0, min(1.0, float(ang)))
        with self._lock:
            self._target = (lin * vmax, ang * wmax)
            self._last_cmd = now
            if self._target == (0.0, 0.0):
                self.reason = 'released'
            else:
                self.reason = 'driving'
                self._hold_until = 0.0
        return True

    def stop(self, reason='stop', now=None):
        """ศูนย์ทันที (ไม่ ramp) แล้วส่งศูนย์ต่อสักพักให้ตัวขับได้รับแน่ๆ"""
        now = self._clock() if now is None else now
        with self._lock:
            self._target = (0.0, 0.0)
            self._current = (0.0, 0.0)
            self._last_cmd = None
            self._hold_until = now + self.release_hold
            self.reason = reason

    # ── จังหวะคงที่ ──────────────────────────────────────────────────────
    def tick(self, now=None):
        now = self._clock() if now is None else now
        with self._lock:
            dt = 0.05 if self._last_tick is None else max(0.0, min(now - self._last_tick, 0.2))
            self._last_tick = now
            moving = self._target != (0.0, 0.0) or self._current != (0.0, 0.0)
            if moving and self._last_cmd is not None and now - self._last_cmd > self.heartbeat_timeout:
                self._target = (0.0, 0.0)                     # deadman: คำสั่งใหม่ไม่มา → ลดลงศูนย์
                self.reason = 'timeout'
            if not moving and now >= self._hold_until:
                return None                                    # ไม่ได้ขับ ไม่ต้อง publish อะไรเลย
            was_moving = self._current != (0.0, 0.0)
            self._current = (
                _slew(self._current[0], self._target[0], self._accel[0], self._decel[0], dt),
                _slew(self._current[1], self._target[1], self._accel[1], self._decel[1], dt))
            if was_moving and self._current == (0.0, 0.0) and self._target == (0.0, 0.0):
                self._hold_until = now + self.release_hold     # เพิ่งหยุดสนิท → ส่งศูนย์ต่ออีกสักพัก
            v, w = self._current
            return (v + 0.0, w + 0.0)                          # (+0.0 กัน -0.0)

    # ── สถานะ ────────────────────────────────────────────────────────────
    def state(self):
        with self._lock:
            return {'driving': self._current != (0.0, 0.0) or self._target != (0.0, 0.0),
                    'v': round(self._current[0], 3), 'w': round(self._current[1], 3), 'reason': self.reason}

    @property
    def active(self):
        with self._lock:
            return self._current != (0.0, 0.0) or self._target != (0.0, 0.0)

    def limits(self):
        return {'max_linear': self.max_linear, 'max_angular': self.max_angular,
                'default_linear': self.default_linear, 'default_angular': self.default_angular,
                'heartbeat_timeout': self.heartbeat_timeout}
