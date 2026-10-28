"""
saved_map — อ่านแผนที่ที่ "บันทึกไว้" (.pgm + .yaml) เป็นตารางความว่าง ไว้ตรวจจุดของ Route โดยไม่ต้องรัน Navigation

แปลงตามกติกาเดียวกับ nav2_map_server (โหมด trinary — โหมดเดียวที่ map_saver ของแอปสร้าง)
จึงได้ตารางเดียวกับที่ Nav2 เห็นตอนโหลดไฟล์นี้ :
  p = (255 - พิกเซล) / 255          (negate: 0)   หรือ   p = พิกเซล / 255   (negate: 1)
  p > occupied_thresh → 100 (ไม่ว่าง) ; p < free_thresh → 0 (ว่าง) ; ที่เหลือ → -1 (ไม่รู้)
แถวแรกของภาพคือ y มากสุด → พลิกแนวตั้งให้แถวแรกของตารางเป็น y น้อยสุด (แบบเดียวกับ OccupancyGrid)

ข้อยกเว้นที่เข้มกว่า map_server (ตั้งใจ) : พิกเซลเทา 205 ± 9 ที่ map_saver เขียนแทน "ไม่รู้" นับเป็น "ไม่รู้" เสมอ
ถ้าตามสูตรข้างบน 205 → p = 0.196 < free_thresh 0.25 จะกลายเป็น "ว่าง" และพื้นที่ที่ยังไม่เคยสำรวจ
(ซึ่งปกติเป็นส่วนใหญ่ของภาพ) จะถูกนับว่าวางจุดได้ — Route ต้องไม่ชี้เข้าไปในพื้นที่ที่หุ่นไม่เคยเห็น ;
ตรวจจากแผนที่จริงของแอป (มีแค่ 0 / 205 / 254) แล้ว
ผิดทางที่ปลอดภัยเท่านั้น (ปฏิเสธเพิ่ม ไม่ยอมรับเพิ่ม)

ตารางถูกใส่ใน LiveMap (ตัวเดียวกับแผนที่สด) จึงใช้ probe()/meta()/png() ชุดเดียวกัน — กติกาตรวจจุดจึงเหมือนกันทุกที่
ไม่พึ่ง ROS → ทดสอบได้ ; เก็บแคชไว้ไม่กี่แผนที่ (ไฟล์เปลี่ยน = โหลดใหม่)
"""
import math
import os
import threading

import numpy as np
import yaml
from PIL import Image

from agv_web_app.live_map import MAX_CELLS, LiveMap

CACHE_MAX = 3
UNKNOWN_GRAY = 205              # ค่าพิกเซลที่ map_saver ใช้แทน "ไม่รู้"
UNKNOWN_BAND = 9                # ยอมให้คลาดเคลื่อนเท่านี้ (ภาพที่ถูกบีบอัด/ปรับแต่งเล็กน้อย)


class SavedMapError(Exception):
    pass


def read_grid(yaml_path, pgm_path):
    """อ่านไฟล์แผนที่ → (width, height, resolution, origin_x, origin_y, origin_yaw, data) ; ผิดพลาดโยน SavedMapError"""
    try:
        with open(yaml_path, encoding='utf-8') as f:
            meta = yaml.safe_load(f)
        if not isinstance(meta, dict):
            raise ValueError('ไฟล์ .yaml ไม่ใช่ตารางค่า')
        resolution = float(meta['resolution'])
        origin = [float(v) for v in meta['origin']]
        finite = all(math.isfinite(v) for v in origin) and math.isfinite(resolution)
        if len(origin) < 3 or not finite or resolution <= 0:
            raise ValueError('resolution/origin ไม่ถูกต้อง')
        negate = int(meta.get('negate', 0))
        occupied = float(meta.get('occupied_thresh', 0.65))
        free = float(meta.get('free_thresh', 0.25))
        mode = str(meta.get('mode', 'trinary'))
        if mode != 'trinary':
            raise ValueError(f'ไม่รองรับ mode: {mode}')
        with Image.open(pgm_path) as src:
            if src.width * src.height > MAX_CELLS:
                raise ValueError('แผนที่ใหญ่เกินไป')
            pixels = np.asarray(src.convert('L'), dtype=np.float64)
    except (OSError, KeyError, TypeError, ValueError, yaml.YAMLError) as e:
        raise SavedMapError(str(e)[:200])
    p = pixels / 255.0 if negate else (255.0 - pixels) / 255.0
    grid = np.full(p.shape, -1, dtype=np.int16)
    grid[p < free] = 0
    grid[p > occupied] = 100                  # ลำดับนี้: ถ้าเกณฑ์ซ้อนกัน "ไม่ว่าง" ชนะ (ปลอดภัยกว่า)
    if not negate:
        grid[np.abs(pixels - UNKNOWN_GRAY) <= UNKNOWN_BAND] = -1      # "ไม่รู้" ของ map_saver (ดูหัวไฟล์)
    height, width = grid.shape
    return width, height, resolution, origin[0], origin[1], origin[2], grid[::-1]


class SavedMaps:
    """แคชแผนที่ที่บันทึกไว้ : get(name) → (LiveMap, generation) หรือ None (ไม่มี/อ่านไม่ได้) ;
    generation เพิ่มทุกครั้งที่โหลดใหม่ (หน้าเว็บใช้เทียบว่าต้องโหลดภาพใหม่หรือไม่ — ต่างแผนที่ต้องไม่ซ้ำกัน)"""

    def __init__(self, saver, on_error=None):
        self._saver = saver
        self._on_error = on_error or (lambda text: None)
        self._lock = threading.Lock()
        self._cache = {}                 # ชื่อ → (ลายนิ้วมือไฟล์, LiveMap, generation)
        self._generation = 0

    def _stamp(self, name):
        """ลายนิ้วมือของไฟล์ (เวลาแก้ไข + ขนาด) ; None = ไฟล์ไม่ครบ"""
        try:
            stats = [os.stat(self._saver.path(name, ext)) for ext in ('.yaml', '.pgm')]
        except OSError:
            return None
        return tuple((st.st_mtime_ns, st.st_size) for st in stats)

    def get(self, name):
        if not self._saver.valid_name(name):
            return None
        stamp = self._stamp(name)
        if stamp is None:
            with self._lock:
                self._cache.pop(name, None)
            return None
        with self._lock:
            hit = self._cache.get(name)
            if hit and hit[0] == stamp:
                return hit[1], hit[2]
        try:
            grid = read_grid(self._saver.path(name, '.yaml'), self._saver.path(name, '.pgm'))
        except SavedMapError as e:
            self._on_error(f'อ่านแผนที่ที่บันทึกไว้ไม่ได้ ({name}): {e}')
            return None
        live = LiveMap(on_error=self._on_error)
        if not live.update(*grid, stamp=stamp[0][0] / 1e9):          # เวลาในข้อมูลแผนที่ = เวลาที่ไฟล์ถูกบันทึก
            return None
        with self._lock:
            self._generation += 1
            self._cache[name] = (stamp, live, self._generation)
            while len(self._cache) > CACHE_MAX:
                self._cache.pop(next(iter(self._cache)))
            return live, self._generation
