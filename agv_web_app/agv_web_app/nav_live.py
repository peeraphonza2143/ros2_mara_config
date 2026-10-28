"""
nav_live — ปรับค่า Nav2 "สด" ขณะ Navigation ทำงานอยู่ (ไม่พึ่ง ROS → ทดสอบด้วย transport ปลอมได้)

หลักการ
  - ใช้ชุดค่าและช่วงเดียวกับ nav_params (SCHEMA) ; ตำแหน่งในไฟล์ yaml → (ชื่อ node, ชื่อพารามิเตอร์) ของ ROS
      ('controller_server', 'ros__parameters', 'FollowPath', 'desired_linear_vel')
        → /controller_server  FollowPath.desired_linear_vel
      ('global_costmap', 'global_costmap', 'ros__parameters', 'inflation_layer', 'inflation_radius')
        → /global_costmap/global_costmap  inflation_layer.inflation_radius
  - all-or-nothing: อ่านค่าสดก่อน → เขียนทีละ node → อ่านกลับยืนยัน ; พลาดตรงไหนย้อนค่าที่เขียนไปแล้วกลับทั้งหมด
  - ค่าที่เพิ่มความเสี่ยง (เร็วขึ้น / ปิดตรวจชน / ตรวจชนช้าลง / เว้นระยะน้อยลง) ต้องมี confirmed=True
    — เซิร์ฟเวอร์เป็นคนบังคับ ไม่ใช่แค่หน้าเว็บ
  - ค่าที่ปรับสดมีผลจน Nav2 ปิด (ไม่บันทึก) ; การบันทึกไว้ใช้ครั้งหน้าเป็นหน้าที่ของ nav_params
  - transport = วัตถุที่มี
      get(node, names) -> {name: ค่า}                 โยน TransportError ถ้าติดต่อ node ไม่ได้/ไม่มีพารามิเตอร์
      set(node, {name: ค่า}) -> {name: (สำเร็จ, เหตุผล)}  โยน TransportError ถ้าติดต่อ node ไม่ได้
"""
import math
import threading
import time

from agv_web_app.nav_params import SCHEMA, NavParamError, _coerce

CACHE_S = 1.0          # อ่านค่าสดซ้ำภายในช่วงนี้ใช้ผลเดิม (หน้าเว็บเรียกบ่อยได้ ไม่ท่วม Nav2)

# ค่าที่ "เพิ่มความเสี่ยง" เมื่อเปลี่ยนไปทางนี้ → ต้องมี confirmed=True
#   'up' = เพิ่มค่าแล้วเสี่ยง · 'down' = ลดค่าแล้วเสี่ยง · 'off' = เปลี่ยนจากเปิดเป็นปิดแล้วเสี่ยง
RISK = {
    'desired_linear_vel': 'up',                              # เร็วขึ้น
    'rotate_to_heading_angular_vel': 'up',                   # หมุนเร็วขึ้น
    'max_angular_accel': 'up',                               # เร่งเลี้ยวแรงขึ้น
    'use_collision_detection': 'off',                        # ปิดตรวจชน
    'max_allowed_time_to_collision_up_to_carrot': 'down',    # ตรวจชนล่วงหน้าสั้นลง
    'inflation_radius': 'down',                              # เว้นระยะจากสิ่งกีดขวางน้อยลง
}


class TransportError(Exception):
    """ติดต่อ node ของ Nav2 ไม่ได้ (ไม่รัน / ยังไม่พร้อม / ไม่มีพารามิเตอร์ / หมดเวลา)"""

    def __init__(self, node, detail=''):
        super().__init__(f'{node}: {detail}')
        self.node = node
        self.detail = detail


class NavLiveError(Exception):
    """code/params ใช้สร้างข้อความให้หน้าเว็บแปลภาษา"""

    def __init__(self, code, **params):
        super().__init__(code)
        self.code = code
        self.params = params


def targets(key):
    """[(ชื่อ node แบบ /a/b, ชื่อพารามิเตอร์แบบ a.b)] ของค่านี้ — แยกจากตำแหน่งในไฟล์ yaml"""
    out = []
    for path in SCHEMA[key][6]:
        i = path.index('ros__parameters')
        out.append(('/' + '/'.join(path[:i]), '.'.join(path[i + 1:])))
    return out


def node_names():
    """ชื่อ node ของ Nav2 ที่ต้องคุยด้วยทั้งหมด"""
    return sorted({node for key in SCHEMA for node, _ in targets(key)})


def _same(a, b):
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12)


def risky_keys(live, changes):
    """key ที่การเปลี่ยนจากค่าสดไปค่าใหม่ "เพิ่มความเสี่ยง" """
    out = []
    for key, new in changes.items():
        rule, old = RISK.get(key), live[key]
        if (rule == 'up' and new > old) or (rule == 'down' and new < old) or (rule == 'off' and old and not new):
            out.append(key)
    return out


class NavLive:

    def __init__(self, transport, on_event=None, clock=time.monotonic):
        self._transport = transport
        self._on_event = on_event or (lambda text: None)
        self._clock = clock
        self._lock = threading.Lock()              # apply ทีละคำขอ
        self._cache_lock = threading.Lock()
        self._cache = None                         # (เวลา, values, mixed)
        # รุ่นของแคช: เพิ่มทุกครั้งที่ล้าง → ผลที่เริ่มอ่านก่อนการล้างจะไม่ถูกเก็บ
        self._gen = 0

    # ── อ่านค่าสด ───────────────────────────────────────────────────────
    def _read_targets(self):
        """{(node, name): ค่า} ของทุกตำแหน่ง ; ติดต่อไม่ได้โยน NavLiveError('nav.err.unreachable')"""
        wanted = {}
        for key in SCHEMA:
            for node, name in targets(key):
                wanted.setdefault(node, set()).add(name)
        out = {}
        for node in sorted(wanted):
            try:
                values = self._transport.get(node, sorted(wanted[node]))
            except TransportError as e:
                raise NavLiveError('nav.err.unreachable', node=e.node)
            for name in wanted[node]:
                if name not in values or values[name] is None:
                    raise NavLiveError('nav.err.unreachable', node=node)
                out[(node, name)] = values[name]
        return out

    @staticmethod
    def _fold(read):
        """รวมเป็น {key: ค่า} (ใช้ค่าของตำแหน่งแรก) + key ที่แต่ละตำแหน่งไม่ตรงกัน"""
        values, mixed = {}, []
        for key in SCHEMA:
            found = [read[t] for t in targets(key)]
            values[key] = found[0]
            if not all(_same(found[0], v) for v in found[1:]):
                mixed.append(key)
        return values, mixed

    def read(self):
        """(values, mixed) = ค่าที่ Nav2 ใช้อยู่จริงตอนนี้ ; ใช้ผลเก่าถ้าอายุไม่เกิน CACHE_S"""
        with self._cache_lock:
            if self._cache and self._clock() - self._cache[0] < CACHE_S:
                return dict(self._cache[1]), list(self._cache[2])
            gen = self._gen
        values, mixed = self._fold(self._read_targets())
        with self._cache_lock:
            # ระหว่างอ่านมี apply มาล้างแคชไปแล้ว = ผลนี้อาจเป็นค่าเก่า ไม่เก็บ
            if gen == self._gen:
                self._cache = (self._clock(), values, mixed)
        return dict(values), list(mixed)

    def _forget(self):
        with self._cache_lock:
            self._cache = None
            self._gen += 1

    # ── ใช้ค่าใหม่สด ────────────────────────────────────────────────────
    def apply(self, values, confirmed=False):
        """คืน {'applied': {key: {'from', 'to'}}, 'live': {key: ค่า}} ; ผิดพลาดโยน NavParamError / NavLiveError
        (ถ้าโยน = ค่าของ Nav2 กลับเป็นเหมือนก่อนเรียกทุกตัว)"""
        if not isinstance(values, dict) or not values:
            raise NavParamError('nav.err.empty')
        clean = {}
        for key, value in values.items():
            if key not in SCHEMA:
                raise NavParamError('nav.err.unknown', key=str(key))
            clean[key] = _coerce(key, value)
        if not self._lock.acquire(blocking=False):
            raise NavLiveError('nav.err.busy')
        try:
            before = self._read_targets()
            live, mixed = self._fold(before)
            changes = {key: value for key, value in clean.items()
                       if any(not _same(before[t], value) for t in targets(key))}
            if not changes:
                return {'applied': {}, 'live': live, 'mixed': mixed}
            merged = dict(live, **changes)
            if merged['min_lookahead_dist'] > merged['max_lookahead_dist']:
                raise NavParamError('nav.err.lookahead_order')
            risky = risky_keys(live, changes)
            if risky and confirmed is not True:
                raise NavLiveError('nav.err.confirm_required', keys=', '.join(sorted(risky)))
            self._write(changes, before)
            after, mixed = self._fold(self._read_after(changes, before))
            for key, value in changes.items():
                self._on_event(f'Nav2 live: {key} {live[key]} → {value}')
            self._forget()
            return {'applied': {key: {'from': live[key], 'to': value} for key, value in changes.items()},
                    'live': after, 'mixed': mixed}
        finally:
            self._lock.release()

    def _write(self, changes, before):
        """เขียนทีละ node ; พลาดตรงไหนย้อนที่เขียนแล้วกลับแล้วโยน"""
        by_node = {}
        owner = {}
        for key, value in changes.items():
            for node, name in targets(key):
                by_node.setdefault(node, {})[name] = value
                owner[(node, name)] = key
        done = []                                   # [(node, name)] ที่เขียนสำเร็จแล้ว (ไว้ย้อนกลับ)
        failure = None
        for node in sorted(by_node):
            try:
                results = self._transport.set(node, by_node[node])
            except TransportError as e:
                failure = NavLiveError('nav.err.unreachable', node=e.node)
                break
            for name in by_node[node]:
                ok, reason = results.get(name, (False, 'no result'))
                if ok:
                    done.append((node, name))
                elif failure is None:
                    failure = NavLiveError('nav.err.rejected', key=owner[(node, name)], reason=str(reason)[:160])
            if failure:
                break
        if failure:
            self._rollback(done, before, failure)
            raise failure

    def _read_after(self, changes, before):
        """อ่านกลับหลังเขียน — ไม่ตรงกับที่ตั้งใจ = ย้อนกลับทั้งหมดแล้วโยน"""
        written = [t for key in changes for t in targets(key)]
        try:
            after = self._read_targets()
        except NavLiveError as e:
            self._rollback(written, before, e)
            raise
        for key, value in changes.items():
            if any(not _same(after[t], value) for t in targets(key)):
                err = NavLiveError('nav.err.verify', key=key)
                self._rollback(written, before, err)
                raise err
        return after

    def _rollback(self, written, before, cause):
        """ย้อนค่ากลับเท่าที่ทำได้ ; ย้อนไม่สำเร็จ → โยน rollback_failed แทน (สถานะ Nav2 อาจไม่ตรงกับที่ผู้ใช้คิด)"""
        failed = []
        for node, name in reversed(written):
            try:
                result = self._transport.set(node, {name: before[(node, name)]}).get(name, (False, ''))
                if not result[0]:
                    failed.append(name)
            except TransportError:
                failed.append(name)
        self._forget()
        if failed:
            raise NavLiveError('nav.err.rollback_failed', keys=', '.join(sorted(set(failed)))) from cause
