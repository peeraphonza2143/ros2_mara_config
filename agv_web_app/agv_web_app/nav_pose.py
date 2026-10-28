"""
nav_pose — วางตำแหน่งเริ่มต้นของหุ่น (initial pose) ให้ AMCL ผ่านหน้าเว็บ (ไม่พึ่ง ROS → ทดสอบด้วย publisher ปลอมได้)

หลักการ
  - ไม่ทำให้หุ่นเคลื่อนที่: แค่บอก AMCL ว่า "ตอนนี้หุ่นอยู่ตรงนี้บนแผนที่" (เหมือน 2D Pose Estimate ใน RViz)
  - ตรวจก่อนส่ง: ตัวเลขต้องเป็นจำนวนจริงจำกัด · ตำแหน่งต้องอยู่ในกรอบของแผนที่ที่โหลดอยู่ · ต้องมีแผนที่
  - ต้องมี AMCL ฟังอยู่จริง (รอได้ WAIT_LISTENER_S) มิฉะนั้นไม่ส่ง — กันกรณีส่งไปแล้วไม่มีใครรับแต่หน้าเว็บบอกว่าสำเร็จ
  - ส่งซ้ำ REPEATS ครั้งห่างกัน INTERVAL_S (บทเรียนเก่า: ส่งครั้งเดียวบางครั้ง AMCL ไม่ได้รับ)
  - ทีละคำขอ ; ล็อกเหตุการณ์ทุกครั้ง
  - publisher = วัตถุที่มี listeners() -> จำนวนผู้ฟัง และ publish(x, y, yaw, covariance)
"""
import math
import threading
import time

# ความแปรปรวนของ x, y, yaw — ค่าเดียวกับที่ RViz ใช้ตอนกด 2D Pose Estimate (yaw ≈ (15°)²)
COVARIANCE = (0.25, 0.25, 0.0685)
REPEATS = 3
INTERVAL_S = 0.4
WAIT_LISTENER_S = 2.0
REFRESH_TIMES = 2          # หลังส่งตำแหน่ง สั่ง AMCL อัปเดตด้วยสแกนปัจจุบัน (ไม่ต้องรอให้หุ่นขยับ) กี่ครั้ง
REFRESH_GAP_S = 0.5
POLL_S = 0.1
LIMIT = 1.0e6              # กันเลขใหญ่ผิดปกติ (ไม่ใช่ขอบเขตแผนที่ — ขอบเขตแผนที่ตรวจแยก)


class PoseError(Exception):
    """code/params ใช้สร้างข้อความให้หน้าเว็บแปลภาษา"""

    def __init__(self, code, **params):
        super().__init__(code)
        self.code = code
        self.params = params


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PoseError('nav.err.pose_invalid')
    value = float(value)
    if not math.isfinite(value) or abs(value) > LIMIT:
        raise PoseError('nav.err.pose_invalid')
    return value


def normalize_yaw(yaw):
    """ทิศเป็นเรเดียนในช่วง (-π, π]"""
    yaw = math.fmod(yaw, 2 * math.pi)
    if yaw > math.pi:
        yaw -= 2 * math.pi
    elif yaw <= -math.pi:
        yaw += 2 * math.pi
    return yaw


def inside_map(x, y, meta):
    """จุด (x, y) อยู่ในสี่เหลี่ยมของแผนที่หรือไม่ (รวมขอบ ; รองรับแผนที่ที่ origin หมุน)"""
    origin, size = meta['origin'], meta['size_m']
    dx, dy = x - origin['x'], y - origin['y']
    c, s = math.cos(-origin.get('yaw', 0.0)), math.sin(-origin.get('yaw', 0.0))
    u, v = dx * c - dy * s, dx * s + dy * c
    return 0.0 <= u <= size['w'] and 0.0 <= v <= size['h']


def validate(x, y, yaw, meta):
    """คืน (x, y, yaw ที่ปรับช่วงแล้ว) หรือโยน PoseError"""
    x, y, yaw = _number(x), _number(y), _number(yaw)
    if not meta or not meta.get('available'):
        raise PoseError('nav.err.no_map')
    if not inside_map(x, y, meta):
        raise PoseError('nav.err.pose_outside')
    return x, y, normalize_yaw(yaw)


class InitialPose:

    def __init__(self, publisher, on_event=None, sleep=time.sleep, clock=time.monotonic, refresher=None):
        """refresher() -> bool : สั่ง AMCL อัปเดตทันที (/request_nomotion_update) ; None = ไม่ใช้"""
        self._publisher = publisher
        self._refresher = refresher
        self._on_event = on_event or (lambda text: None)
        self._sleep = sleep
        self._clock = clock
        self._lock = threading.Lock()

    def set(self, x, y, yaw, meta):
        """ตรวจ → รอผู้ฟัง → ส่งซ้ำ ; คืน {'x','y','yaw','yaw_deg'} ; ผิดพลาดโยน PoseError (ไม่ส่งอะไรเลย)"""
        x, y, yaw = validate(x, y, yaw, meta)
        if not self._lock.acquire(blocking=False):
            raise PoseError('nav.err.busy')
        try:
            self._wait_for_listener()
            for i in range(REPEATS):
                if i:
                    self._sleep(INTERVAL_S)
                self._publisher.publish(x, y, yaw, COVARIANCE)
            self._on_event(f'Initial pose: x={x:.2f} y={y:.2f} yaw={math.degrees(yaw):.1f}°')
            self._refresh_after_pose()
            return {'x': x, 'y': y, 'yaw': yaw, 'yaw_deg': math.degrees(yaw)}
        finally:
            self._lock.release()

    def _refresh_after_pose(self):
        """AMCL อัปเดตเมื่อหุ่นขยับเท่านั้น → σ จึงค้างที่ค่าตั้งต้นของจุดที่วาง ; สั่งอัปเดตเองเพื่อให้รู้ตัวทันที
        (ล้มเหลวไม่ทำให้การวางตำแหน่งล้ม — แค่บันทึก)"""
        if self._refresher is None:
            return
        for _ in range(REFRESH_TIMES):
            self._sleep(REFRESH_GAP_S)
            if not self._refresher():
                self._on_event('AMCL nomotion update unavailable')
                return

    def refresh(self):
        """ปุ่ม Refresh localization : สั่ง AMCL อัปเดตด้วยสแกนปัจจุบัน ; ไม่ได้ผลโยน
         PoseError('nav.err.refresh_failed')"""
        if self._refresher is None or not self._lock.acquire(blocking=False):
            raise PoseError('nav.err.refresh_failed')
        try:
            ok = self._refresher()
        finally:
            self._lock.release()
        if not ok:
            raise PoseError('nav.err.refresh_failed')
        self._on_event('AMCL nomotion update requested')

    def _wait_for_listener(self):
        deadline = self._clock() + WAIT_LISTENER_S
        while self._publisher.listeners() < 1:
            if self._clock() >= deadline:
                raise PoseError('nav.err.no_listener')
            self._sleep(POLL_S)
