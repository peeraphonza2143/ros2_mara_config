"""
stats — สถิติการทำงานสะสมของหุ่น (ระยะทาง / เวลาทำงาน / ไทม์ไลน์โหมด) เก็บลงไฟล์

ไม่พึ่ง ROS → ทดสอบได้ ; web_server_node เรียก update() ~2 Hz จาก thread ROS
ส่วน summary() ถูกเรียกจาก thread tornado → ทุกอย่างอยู่ใต้ lock

หลักการ:
  - ระยะทาง = Σ |v|·Δt จากความเร็ว odometry ที่ "สดอยู่" เท่านั้น (ข้อมูลขาด = ไม่นับ ไม่เดาต่อ)
    ความเร็วต่ำกว่าเกณฑ์ (noise ตอนหุ่นนิ่ง) ไม่นับ ; Δt เกิน MAX_GAP_S (โปรแกรมค้าง/ข้อมูลขาด)
    ไม่นับ ; นาฬิกาถอยหลัง (Δt < 0) ไม่นับ
  - เวลาทำงาน = เวลาที่ระบบอยู่ในโหมด (Mapping/Manual/Navigation) และสถานะ RUNNING
    (ไม่นับ Standby, ตอนกำลังเปิด/ปิด หรือตอน error)
  - "วัน" ตามเวลาท้องถิ่นของเครื่องหุ่น (ตัดวันใหม่เที่ยงคืน) ; เก็บย้อนหลัง DAYS วัน
  - รอบการทำงาน (rounds) = จำนวนรอบของ Route ที่หุ่นวิ่งครบทุกจุดแล้ว (ตัวรัน Route แจ้งผ่าน add_round) นับต่อวัน + สะสม
  - รีเซ็ต: ลบทั้งหมด (ยอดสะสม วันนี้ 7 วัน ไทม์ไลน์) ; ไฟล์เดิมสำรองเป็น .bak 1 ชุด ; ระบบที่กำลังทำงานอยู่
    ไม่ถูกกระทบ — ช่วงที่เปิดอยู่เริ่มนับใหม่จากตอนนี้
  - ไฟล์เสีย/อ่านไม่ได้ → เริ่มใหม่ (ย้ายไฟล์เสียไปเป็น .corrupt) ; เขียนไม่ได้ → ทำงานต่อใน memory
    เขียนไฟล์แบบ atomic (เขียนไฟล์ชั่วคราวแล้ว rename) กันไฟดับกลางคันได้ไฟล์ครึ่งเดียว
"""
import datetime
import json
import os
import threading
import time

DAYS = 7                        # จำนวนวันที่เก็บรายวัน (รวมวันนี้)
MAX_GAP_S = 2.0                 # ช่วงเวลาระหว่างตัวอย่างที่เกินนี้ = ข้อมูลขาด ไม่นับ
TIMELINE_WINDOW_S = 24 * 3600.0
MAX_SEGMENTS = 500              # กันไฟล์โตไม่จำกัดถ้าสลับโหมดถี่ผิดปกติ
SAVE_INTERVAL_S = 30.0
VERSION = 1


def local_day(ts):
    """วันที่ตามเวลาท้องถิ่น 'YYYY-MM-DD' (แยกเป็นฟังก์ชันให้เทสต์แทนที่ได้)"""
    return time.strftime('%Y-%m-%d', time.localtime(ts))


def _day_index(day):
    """เลขวันต่อเนื่อง (ไม่ขึ้นกับ time zone) — วันถัดไป = +1"""
    return datetime.date.fromisoformat(day).toordinal()


def active_mode(mode_snapshot):
    """โหมดที่ "ทำงานอยู่จริง" จาก ModeManager.snapshot() ; None = ไม่นับเป็นเวลาทำงาน
    (Standby, กำลังเปิด/ปิด, error, หรือโหมดที่ไม่รู้จัก)"""
    if mode_snapshot.get('state') != 'RUNNING':
        return None
    mode = mode_snapshot.get('mode')
    return mode if mode in ('MAPPING', 'MANUAL', 'NAVIGATION') else None


class StatsStore:

    def __init__(self, path, noise_speed=0.02, day_of=local_day, on_error=None, on_event=None):
        self.path = os.path.expanduser(path) if path else None
        self.noise_speed = float(noise_speed)
        self._day_of = day_of
        self._on_error = on_error or (lambda text: None)
        self._on_event = on_event or (lambda text: None)
        self._lock = threading.Lock()
        self._last_ts = None            # เวลาของตัวอย่างล่าสุด (None = ห่วงโซ่ขาด)
        self._last_v = None             # ความเร็ว (ผ่านตัวกรอง noise) ของตัวอย่างล่าสุด
        self._last_mode = None
        self._last_save = 0.0
        self._dirty = False
        self.save_ok = True
        self.load_note = None           # None | 'corrupt' (ไฟล์เสียถูกย้ายไป .corrupt)
        self.total = {'distance_m': 0.0, 'active_s': 0.0, 'since': None, 'rounds': 0}
        self.days = {}                  # 'YYYY-MM-DD' → {'distance_m', 'active_s', 'rounds'}
        self.segments = []              # [{'start', 'end', 'mode'}] ; end=None = กำลังดำเนินอยู่
        self._load()

    # ── ไฟล์ ────────────────────────────────────────────────────────────
    def _load(self):
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding='utf-8') as f:
                data = json.load(f)
            total = data['total']
            days = {str(k): {'distance_m': float(v['distance_m']), 'active_s': float(v['active_s']),
                             'rounds': max(0, int(v.get('rounds', 0)))}          # ไฟล์รุ่นเก่าไม่มี rounds → 0
                    for k, v in data['days'].items()}
            segments = []
            for s in data['segments']:
                seg = {'start': float(s['start']),
                       'end': None if s['end'] is None else float(s['end']),
                       'mode': str(s['mode'])}
                if seg['end'] is None:
                    seg['seen'] = float(s.get('seen', seg['start']))
                segments.append(seg)
            for k in days:
                _day_index(k)           # รูปแบบวันที่ผิด → ValueError
            self.total = {'distance_m': float(total['distance_m']),
                          'active_s': float(total['active_s']),
                          'since': None if total.get('since') is None else float(total['since']),
                          'rounds': max(0, int(total.get('rounds', 0)))}
            self.days, self.segments = days, segments
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            self._on_error(f'อ่านไฟล์สถิติไม่ได้ ({e}) — เริ่มนับใหม่')
            self.load_note = 'corrupt'
            try:
                os.replace(self.path, self.path + '.corrupt')
            except OSError:
                pass
            self.total = {'distance_m': 0.0, 'active_s': 0.0, 'since': None, 'rounds': 0}
            self.days, self.segments = {}, []
            return
        # โปรแกรมปิดไม่สะอาด: ช่วงที่ค้าง "กำลังดำเนินอยู่" ไม่รู้เวลาจบจริง → ปิดที่เวลาที่เห็นล่าสุด
        for s in self.segments:
            if s['end'] is None:
                s['end'] = s.pop('seen')

    def _write_file(self, path):
        """เขียนสถานะปัจจุบันลงไฟล์แบบ atomic (ต้องถือ lock อยู่) ; ล้มเหลวโยน OSError"""
        payload = {'version': VERSION, 'total': self.total, 'days': self.days,
                   'segments': self.segments}
        tmp = path + '.tmp'
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    def save(self, now=None, force=False):
        """เขียนลงไฟล์ (ถ้ามีการเปลี่ยนแปลงและถึงเวลา) ; คืน True ถ้าเขียนสำเร็จ"""
        now = time.time() if now is None else now
        with self._lock:
            if not self.path or not (self._dirty or force):
                return False
            if not force and now - self._last_save < SAVE_INTERVAL_S:
                return False
            try:
                self._write_file(self.path)
            except OSError as e:
                if self.save_ok:
                    self._on_error(f'เขียนไฟล์สถิติไม่ได้ ({e}) — นับต่อใน memory')
                self.save_ok = False
                self._last_save = now       # ไม่ลองถี่เกินไป
                return False
            self.save_ok = True
            self._dirty = False
            self._last_save = now
            return True

    def reset(self, now=None, who=''):
        """ลบสถิติทั้งหมดแล้วเริ่มนับใหม่ ; คืน True ถ้าบันทึกไฟล์ใหม่สำเร็จ
        (ล้มเหลวก็ยังรีเซ็ตใน memory — ผู้ใช้สั่งแล้ว — และ summary()['persist'] จะบอกว่าบันทึกไม่ได้)"""
        now = time.time() if now is None else now
        with self._lock:
            if self.path and (self.total['distance_m'] or self.total['active_s'] or self.total['rounds'] or self.days):
                try:
                    # สำรอง "สถานะปัจจุบันใน memory" (ไม่ใช่ไฟล์เดิม ซึ่งอาจเก่าถึง 30 วินาที)
                    self._write_file(self.path + '.bak')
                except OSError as e:
                    self._on_error(f'สำรองไฟล์สถิติก่อนรีเซ็ตไม่ได้ ({e}) — รีเซ็ตต่อ')
            self.total = {'distance_m': 0.0, 'active_s': 0.0, 'since': now, 'rounds': 0}
            self.days = {}
            self.segments = []
            self.load_note = None
            self._dirty = True
            # ตัดห่วงโซ่ตัวอย่าง: ช่วงเวลาที่คร่อมจังหวะรีเซ็ตไม่นับ (ครึ่งหนึ่งเป็นของก่อนรีเซ็ต)
            # ตัวอย่างถัดไปเริ่มนับใหม่ และ _track_segment เปิดช่วงโหมดใหม่ให้เองถ้ายังมีโหมดทำงานอยู่
            self._last_ts = None
            self._last_v = None
            self._last_mode = None
        self._on_event(f'สถิติถูกรีเซ็ตโดย {who or "ไม่ทราบ"}')
        return self.save(now, force=True)

    # ── อัปเดตจาก thread ROS ──────────────────────────────────────────────
    def update(self, now, v, mode):
        """now = epoch ; v = ความเร็วเดินหน้า m/s (None = ข้อมูลขาด) ;
        mode = ชื่อโหมดที่ RUNNING อยู่ หรือ None (Standby/กำลังเปลี่ยน/error)"""
        with self._lock:
            dt = None if self._last_ts is None else now - self._last_ts
            prev_v, prev_mode = self._last_v, self._last_mode
            self._last_ts = now
            self._last_v = None if v is None else (0.0 if abs(v) < self.noise_speed else abs(v))
            self._last_mode = mode
            if self.total['since'] is None:
                self.total['since'] = now
            day = self._day(now)
            counted = dt is not None and 0 < dt <= MAX_GAP_S
            # ช่วง [ตัวอย่างก่อนหน้า, ตอนนี้]: ระยะทางใช้ค่าเฉลี่ยของความเร็วปลายทั้งสอง (trapezoid)
            # เวลาทำงานนับตามโหมดของตัวอย่างก่อนหน้า (ช่วงนั้นอยู่ในโหมดนั้นจริง)
            if counted and prev_v is not None and self._last_v is not None:
                d = (prev_v + self._last_v) / 2.0 * dt
                if d > 0:
                    day['distance_m'] += d
                    self.total['distance_m'] += d
                    self._dirty = True
            if counted and prev_mode is not None:
                day['active_s'] += dt
                self.total['active_s'] += dt
                self._dirty = True
            self._track_segment(now, mode, counted)
            self._prune(now)

    def _day(self, now):
        return self.days.setdefault(self._day_of(now), {'distance_m': 0.0, 'active_s': 0.0, 'rounds': 0})

    def add_round(self, now=None):
        """นับ 1 รอบของ Route ที่วิ่งครบทุกจุดแล้ว (เรียกจากตัวรัน Route) ; คืนจำนวนรอบวันนี้"""
        now = time.time() if now is None else now
        with self._lock:
            day = self._day(now)
            day['rounds'] += 1
            self.total['rounds'] += 1
            self._dirty = True
            return day['rounds']

    def _track_segment(self, now, mode, counted):
        last = self.segments[-1] if self.segments else None
        open_seg = last if last is not None and last['end'] is None else None
        if open_seg is not None and (mode != open_seg['mode'] or not counted):
            # โหมดเปลี่ยน หรือข้อมูลขาดนานเกิน → ปิดช่วง (ที่เวลาล่าสุดที่เห็นจริง)
            open_seg['end'] = open_seg['seen'] if not counted else now
            open_seg.pop('seen', None)
            open_seg = None
            self._dirty = True
        if open_seg is None and mode is not None:
            self.segments.append({'start': now, 'end': None, 'mode': mode, 'seen': now})
            self._dirty = True
        elif open_seg is not None:
            open_seg['seen'] = now

    def _prune(self, now):
        keep = _day_index(self._day_of(now)) - (DAYS - 1)
        for k in [k for k in self.days if _day_index(k) < keep]:
            del self.days[k]
            self._dirty = True
        cutoff = now - DAYS * 86400
        self.segments = [s for s in self.segments
                         if s['end'] is None or s['end'] >= cutoff][-MAX_SEGMENTS:]

    # ── อ่านจาก thread tornado ────────────────────────────────────────────
    def summary(self, now=None):
        now = time.time() if now is None else now
        with self._lock:
            today = self._day_of(now)
            today_idx = _day_index(today)
            days = []
            for back in range(DAYS - 1, -1, -1):
                key = datetime.date.fromordinal(today_idx - back).isoformat()
                rec = self.days.get(key, {'distance_m': 0.0, 'active_s': 0.0, 'rounds': 0})
                days.append({'date': key, 'distance_m': round(rec['distance_m'], 2),
                             'active_s': round(rec['active_s'], 1), 'rounds': rec.get('rounds', 0)})
            start = now - TIMELINE_WINDOW_S
            timeline = []
            for s in self.segments:
                end = now if s['end'] is None else s['end']
                if end <= start:
                    continue
                timeline.append({'start': max(s['start'], start), 'end': end,
                                 'mode': s['mode'], 'open': s['end'] is None})
            cur = self.days.get(today, {'distance_m': 0.0, 'active_s': 0.0, 'rounds': 0})
            return {
                'now': now,
                'today': {'date': today, 'distance_m': round(cur['distance_m'], 2),
                          'active_s': round(cur['active_s'], 1), 'rounds': cur.get('rounds', 0)},
                'total': {'distance_m': round(self.total['distance_m'], 2),
                          'active_s': round(self.total['active_s'], 1),
                          'since': self.total['since'], 'rounds': self.total['rounds']},
                'days': days,
                'timeline': timeline,
                'window': TIMELINE_WINDOW_S,
                'rounds': cur.get('rounds', 0),         # รอบของ Route ที่ครบวันนี้ (สะสมอยู่ใน total.rounds)
                'persist': {'ok': self.save_ok, 'note': self.load_note},
            }
