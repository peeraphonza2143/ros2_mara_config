"""
laser_overlay — แปลง LaserScan เป็นจุดในเฟรม map ให้หน้าเว็บวาดทับแผนที่ (เหมือนจอแสดง LaserScan ของ rviz2)

ไม่พึ่ง ROS (รับตัวเลข/ลิสต์) → ทดสอบได้ ; web_server_node เรียก update() จาก thread ROS (จำกัด ~5 Hz)
ส่วน latest() ถูกเรียกจาก thread tornado → ใต้ lock

  - ตัดจุดที่ไม่ถูกต้อง (inf / nan / นอกช่วง range_min..range_max)
  - ลดจำนวนจุดให้ไม่เกิน max_points โดยเลือกกระจายเท่าๆ กันตามมุม
    (S2 ให้ ~3000 จุดต่อรอบ ส่งทั้งหมดผ่าน WiFi เปลืองเปล่า)
  - พิกัดคือตำแหน่งของ "จุดที่เลเซอร์กระทบ" ในเฟรม map (เมตร ปัด 2 ตำแหน่ง)
    pose = (x, y, yaw) ของเฟรมเลเซอร์ในเฟรม map
"""
import math
import threading
import time

import numpy as np


class LaserOverlay:

    def __init__(self, max_points=360, on_error=None):
        self.max_points = int(max_points)
        self._on_error = on_error or (lambda text: None)
        self._lock = threading.Lock()
        self._points = None
        self._stamp = None          # เวลา (epoch) ที่ได้รับสแกน
        self._origin = None         # ตำแหน่งเลเซอร์ใน map (x, y)
        self._version = 0

    def update(self, ranges, angle_min, angle_increment, range_min, range_max, pose, now=None):
        """คืน True ถ้าได้จุดอย่างน้อย 0 จุดและแปลงสำเร็จ (สแกนว่างก็ถือว่าสำเร็จ — แปลว่ามองไม่เห็นอะไร)"""
        try:
            r = np.asarray(ranges, dtype=np.float64)
            if r.ndim != 1:
                raise ValueError('ranges ต้องเป็นอาร์เรย์ 1 มิติ')
            tx, ty, yaw = (float(v) for v in pose)
            a0, da = float(angle_min), float(angle_increment)
            lo, hi = float(range_min), float(range_max)
            if not all(math.isfinite(v) for v in (tx, ty, yaw, a0, da, lo, hi)):
                raise ValueError('ค่า pose/มุม/ช่วงต้องเป็นตัวเลขจำกัด')
            n = r.size
            if n and self.max_points > 0 and n > self.max_points:
                idx = np.linspace(0, n - 1, self.max_points).astype(np.int64)    # กระจายเท่าๆ กันตามมุม
            else:
                idx = np.arange(n)
            rr = r[idx]
            ang = a0 + idx * da
            ok = np.isfinite(rr) & (rr >= lo) & (rr <= hi)
            rr, ang = rr[ok], ang[ok] + yaw
            xs = tx + rr * np.cos(ang)
            ys = ty + rr * np.sin(ang)
            points = np.stack([np.round(xs, 2), np.round(ys, 2)], axis=1).tolist()
        except (ValueError, TypeError, OverflowError) as e:
            self._on_error(f'แปลงสแกนเลเซอร์ไม่ได้: {e}')
            return False
        with self._lock:
            self._version += 1
            self._points = points
            self._origin = [round(tx, 3), round(ty, 3)]
            self._stamp = time.time() if now is None else now
        return True

    def clear(self):
        with self._lock:
            self._points = None
            self._stamp = None
            self._version += 1

    def latest(self, now=None):
        now = time.time() if now is None else now
        with self._lock:
            if self._points is None:
                return {'available': False, 'version': self._version}
            return {'available': True, 'version': self._version, 'age': round(max(0.0, now - self._stamp), 2),
                    'origin': self._origin, 'count': len(self._points), 'points': self._points}
