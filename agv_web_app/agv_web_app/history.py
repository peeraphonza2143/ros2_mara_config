"""
history — เก็บประวัติความเร็วล่าสุดใน memory (ring buffer) ให้กราฟบนหน้า Dashboard

เก็บไว้ที่ backend (ไม่ใช่ในเบราว์เซอร์) เพื่อให้เปิดหน้าใหม่ รีเฟรช หรือใช้หลายเครื่อง
แล้วเห็นกราฟเหมือนกัน
ไม่พึ่ง ROS → ทดสอบได้ ; ข้อมูลหายเมื่อปิดแอป (ประวัติระยะยาวรายวันจะเก็บลงไฟล์ในขั้น D2)
"""
import threading
import time
from collections import deque

SAMPLE_HZ = 2.0                   # ความถี่เก็บตัวอย่าง
MAX_WINDOW_S = 240.0
DEFAULT_WINDOW_S = 120.0


class SpeedHistory:

    def __init__(self, maxlen=int(MAX_WINDOW_S * SAMPLE_HZ)):
        # (เวลา epoch, v, w) ; v/w = None → ไม่มีข้อมูลช่วงนั้น
        self._buf = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def add(self, ts, v, w):
        with self._lock:
            self._buf.append((ts, v, w))

    def series(self, window_s=DEFAULT_WINDOW_S, now=None):
        """ตัวอย่างในช่วง window_s วินาทีล่าสุด

        t = เวลาเทียบกับ "ตอนนี้" (วินาที, ≤ 0) ; v = m/s ; w = rad/s
        None = ไม่มีข้อมูล (ให้เส้นกราฟขาด)
        """
        window_s = max(1.0, min(float(window_s), MAX_WINDOW_S))
        now = time.time() if now is None else now
        with self._lock:
            rows = [r for r in self._buf if now - r[0] <= window_s]
        return {
            'window': window_s,
            'now': now,
            't': [round(r[0] - now, 2) for r in rows],
            'v': [None if r[1] is None else round(r[1], 3) for r in rows],
            'w': [None if r[2] is None else round(r[2], 3) for r in rows],
        }
