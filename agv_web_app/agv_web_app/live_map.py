"""
live_map — เก็บแผนที่ล่าสุดจาก /map (OccupancyGrid) แล้วแปลงเป็นภาพ PNG ให้หน้าเว็บวาด

ไม่พึ่ง ROS (รับค่าเป็นตัวเลข/ลิสต์ธรรมดา) → ทดสอบได้ ; web_server_node เรียก update() จาก thread ROS
ส่วน meta()/png() ถูกเรียกจาก thread tornado → ใต้ lock

กติกาของภาพ (ตามมาตรฐาน map_saver):
  - ช่อง -1 (ไม่รู้) = เทา 205 ; ว่าง (0) = ขาว ; มีสิ่งกีดขวาง (100) = ดำ ; ค่ากลางไล่โทนเทา
  - แถวแรกของ OccupancyGrid คือ "ล่างสุด" (y น้อยสุด) → พลิกแนวตั้งให้ภาพตั้งตรงแบบแผนที่ทั่วไป
  - ภาพมี 1 พิกเซลต่อ 1 ช่อง ; หน้าเว็บเป็นคนขยาย (ไม่ทำ smoothing เพื่อไม่ให้ขอบเบลอ)
"""
import hashlib
import io
import math
import threading
import time

import numpy as np
from PIL import Image

UNKNOWN_GRAY = 205
FREE_MAX = 25                   # ช่องที่ค่าไม่เกินนี้ถือว่า "ว่าง" (เท่า free_thresh 0.25 ของ map_server)
MAX_CELLS = 25_000_000          # ใหญ่กว่านี้ถือว่าผิดปกติ (ที่ 0.05 m = 5,000 × 5,000 ช่อง = 250 × 250 m)


def map_clear_needed(old_mode, new_mode):
    """เปลี่ยนโหมดแล้วต้องลืมแผนที่ที่เก็บไว้หรือไม่
    - เข้า Navigation "ห้ามลืม": map_server ส่งแผนที่แบบ latched ครั้งเดียวตอนเปิด ซึ่งมาถึงก่อนที่โหมดจะเปลี่ยนเป็น
      NAVIGATION (ModeManager รอ node พร้อมก่อน) ถ้าลืมตอนนั้นแผนที่จะหายถาวร เพราะไม่มีใครส่งใหม่ให้
    - ออกจากโหมดใดๆ / เข้า Mapping ต้องลืม (กันแผนที่เก่าหรือแผนที่ของโหมดอื่นค้างอยู่)"""
    return new_mode != 'NAVIGATION'


class LiveMap:

    def __init__(self, on_error=None):
        self._lock = threading.Lock()
        self._on_error = on_error or (lambda text: None)
        self._png = None
        self._meta = None
        # อาเรย์ int8 (แถวแรก = y น้อยสุด) ; แทนที่ทั้งก้อนเสมอ ไม่แก้ในที่ → อ่านนอก lock ได้
        self._grid = None
        self._version = 0

    def update(self, width, height, resolution, origin_x, origin_y, origin_yaw, data, stamp=None):
        """data = ลิสต์/อาเรย์ความยาว width*height (ค่า -1..100 เรียงแถวจาก y น้อยไปมาก) ; คืน True ถ้าสำเร็จ"""
        try:
            width, height = int(width), int(height)
            # ROS ส่งเป็น float32 (0.05 → 0.05000000074505806) จึงปัดให้แสดงผลสวย
            resolution = round(float(resolution), 6)
            if width <= 0 or height <= 0 or not resolution > 0:
                raise ValueError('ขนาด/ความละเอียดแผนที่ไม่ถูกต้อง')
            if width * height > MAX_CELLS:
                raise ValueError(f'แผนที่ใหญ่เกินไป ({width}x{height})')
            grid = np.asarray(data, dtype=np.int16)
            if grid.size != width * height:
                raise ValueError(f'ข้อมูลแผนที่ขนาดไม่ตรง ({grid.size} ≠ {width}x{height})')
            grid = grid.reshape(height, width)
            known = grid >= 0
            gray = np.full(grid.shape, UNKNOWN_GRAY, dtype=np.uint8)
            # 0 → 254 (ขาวนวล เหมือน map_saver) ... 100 → 0 (ดำ) ; ค่าเกิน 100 ถูกจำกัดไว้ที่ 100
            gray[known] = (254 - np.clip(grid[known], 0, 100) * 254 / 100).round().astype(np.uint8)
            image = Image.fromarray(gray[::-1], mode='L')          # พลิกแนวตั้ง
            buf = io.BytesIO()
            image.save(buf, format='PNG', optimize=False, compress_level=3)
            png = buf.getvalue()
            known_cells = int(known.sum())
        except (ValueError, TypeError, OverflowError) as e:
            self._on_error(f'แปลงแผนที่ไม่ได้: {e}')
            return False
        with self._lock:
            self._version += 1
            self._png = png
            self._grid = np.clip(grid, -1, 100).astype(np.int8)
            self._meta = {
                'version': self._version,
                'width': width, 'height': height,
                'resolution': resolution,
                'origin': {'x': round(float(origin_x), 6), 'y': round(float(origin_y), 6),
                           'yaw': round(float(origin_yaw), 6)},
                'size_m': {'w': round(width * resolution, 2), 'h': round(height * resolution, 2)},
                'known_cells': known_cells,
                'hash': hashlib.sha1(png).hexdigest()[:12],        # ใช้เทียบว่าแผนที่ปัจจุบันเคยบันทึกแล้วหรือยัง
                'stamp': time.time() if stamp is None else float(stamp),
            }
        return True

    def clear(self):
        """ลืมแผนที่เดิม (เช่นตอนออกจากโหมด Mapping) — หน้าเว็บจะไม่เห็นแผนที่เก่าค้าง"""
        with self._lock:
            self._png = None
            self._meta = None
            self._grid = None
            self._version += 1

    def probe(self, x, y, radius):
        """ตรวจจุด (x, y) บนแผนที่ที่โหลดอยู่ว่า "ปลอดภัยพอให้เป็นจุดหมาย" ไหม ; คืน {'status', 'clearance'}
        status: 'no_map' · 'outside' (นอกกรอบ) · 'unknown' (ช่องนั้นยังไม่รู้) · 'occupied' (ช่องนั้นไม่ว่าง) ·
                'tight' (ช่องว่างแต่มีช่องไม่ว่าง/ไม่รู้ ใกล้กว่า radius) · 'ok'
        clearance = ระยะ (m) ถึงช่องไม่ว่าง/ไม่รู้ที่ใกล้ที่สุดในรัศมี radius (None ถ้าไม่มี)
        ขอบแผนที่นับเป็น "ไม่รู้"""
        with self._lock:
            grid, meta = self._grid, self._meta
        if grid is None or meta is None:
            return {'status': 'no_map', 'clearance': None}
        res, origin = meta['resolution'], meta['origin']
        dx, dy = x - origin['x'], y - origin['y']
        c, s = math.cos(-origin['yaw']), math.sin(-origin['yaw'])
        col, row = math.floor((dx * c - dy * s) / res), math.floor((dx * s + dy * c) / res)
        height, width = grid.shape
        if not (0 <= col < width and 0 <= row < height):
            return {'status': 'outside', 'clearance': None}
        value = int(grid[row, col])
        if value < 0:
            return {'status': 'unknown', 'clearance': 0.0}
        if value > FREE_MAX:
            return {'status': 'occupied', 'clearance': 0.0}
        r = max(1, math.ceil(radius / res))
        # หน้าต่างรอบจุด (นอกแผนที่ = -1 "ไม่รู้") สร้างเฉพาะหน้าต่างเล็ก ไม่ pad ทั้งแผนที่
        # (probe_line เรียกซ้ำเป็นร้อยครั้ง)
        window = np.full((2 * r + 1, 2 * r + 1), -1, dtype=grid.dtype)
        r0, r1, c0, c1 = max(row - r, 0), min(row + r + 1, height), max(col - r, 0), min(col + r + 1, width)
        window[r0 - (row - r):r1 - (row - r), c0 - (col - r):c1 - (col - r)] = grid[r0:r1, c0:c1]
        offsets = (np.arange(2 * r + 1) - r) * res
        dist = np.hypot(offsets[:, None], offsets[None, :])
        blocked = (window < 0) | (window > FREE_MAX)
        near = float(dist[blocked].min()) if blocked.any() else None
        if near is not None and near < radius:
            return {'status': 'tight', 'clearance': round(near, 3)}
        return {'status': 'ok', 'clearance': None if near is None else round(near, 3)}

    def probe_line(self, x0, y0, x1, y1, radius, soft=None, step=None):
        """ตรวจ "ตลอดแนวเส้นตรง" จาก (x0, y0) ถึง (x1, y1) ทุกๆ step เมตร (ค่าตั้งต้น = ความละเอียดแผนที่) :
        ทุกจุดต้อง probe(radius) ผ่าน (รู้ว่าว่าง + ห่างอย่างน้อย radius) ; คืน dict
          ok            : ผ่านตลอดแนวหรือไม่
          at            : ระยะ (m) จากจุดเริ่มที่ติดเป็นที่แรก (None ถ้าผ่าน) ; reason = status ของ probe ตรงนั้น
          min_clearance : ระยะห่างต่ำสุดตลอดแนว (m) เท่าที่วัดได้ภายในรัศมี soft (None = ไม่มีอะไรใกล้กว่า soft เลย)
          soft_risk     : มีช่วงที่ห่างน้อยกว่า soft (เช่น inflation) → planner อาจดึงเส้นเข้ากลางทางไม่ตรงเป๊ะ"""
        meta = self.meta()
        if not meta.get('available'):
            return {'ok': False, 'at': 0.0, 'reason': 'no_map', 'min_clearance': None, 'soft_risk': False}
        soft = max(radius, soft or radius)
        step = step or meta['resolution']
        length = math.hypot(x1 - x0, y1 - y0)
        count = max(1, math.ceil(length / step))
        lowest = None
        for i in range(count + 1):
            d = min(i * step, length)
            t = 0.0 if length == 0 else d / length
            result = self.probe(x0 + (x1 - x0) * t, y0 + (y1 - y0) * t, soft)
            if result['status'] in ('no_map', 'outside', 'unknown', 'occupied'):
                return {'ok': False, 'at': round(d, 2), 'reason': result['status'], 'min_clearance': lowest,
                        'soft_risk': True}
            near = result['clearance']
            if near is not None:
                if near < radius:
                    return {'ok': False, 'at': round(d, 2), 'reason': 'tight', 'min_clearance': near,
                            'soft_risk': True}
                lowest = near if lowest is None else min(lowest, near)
        return {'ok': True, 'at': None, 'reason': None, 'min_clearance': lowest,
                'soft_risk': lowest is not None and lowest < soft}

    def probe_path(self, points, radius, soft=None):
        """ตรวจ "ตลอดเส้นทาง" ที่เป็นลำดับจุด [(x, y, ...), ...] (เส้นตรง/เส้นโค้งมนที่สร้างไว้ ห่างกันถี่ ~ความละเอียดแผนที่) :
        ผลลัพธ์รูปแบบเดียวกับ probe_line (ok / at = ระยะสะสมจากจุดเริ่ม / reason / min_clearance / soft_risk)"""
        meta = self.meta()
        if not meta.get('available'):
            return {'ok': False, 'at': 0.0, 'reason': 'no_map', 'min_clearance': None, 'soft_risk': False}
        soft = max(radius, soft or radius)
        lowest, travelled, previous = None, 0.0, None
        for point in points:
            x, y = point[0], point[1]
            if previous is not None:
                travelled += math.hypot(x - previous[0], y - previous[1])
            previous = (x, y)
            result = self.probe(x, y, soft)
            if result['status'] in ('no_map', 'outside', 'unknown', 'occupied'):
                return {'ok': False, 'at': round(travelled, 2), 'reason': result['status'], 'min_clearance': lowest,
                        'soft_risk': True}
            near = result['clearance']
            if near is not None:
                if near < radius:
                    return {'ok': False, 'at': round(travelled, 2), 'reason': 'tight', 'min_clearance': near,
                            'soft_risk': True}
                lowest = near if lowest is None else min(lowest, near)
        return {'ok': True, 'at': None, 'reason': None, 'min_clearance': lowest,
                'soft_risk': lowest is not None and lowest < soft}

    def meta(self):
        """ข้อมูลอธิบายแผนที่ (JSON) ; available=False ถ้ายังไม่เคยได้รับ"""
        with self._lock:
            if self._meta is None:
                return {'available': False, 'version': self._version}
            out = dict(self._meta)
            out['available'] = True
            return out

    def png(self):
        with self._lock:
            return self._png
