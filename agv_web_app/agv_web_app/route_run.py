"""
route_run — ตัวรัน Route : พาหุ่นไปทีละจุดตามลำดับ (หยุดรอที่จุด · วนซ้ำ · พัก/ต่อ · ยกเลิก)
(ไม่พึ่ง ROS → ทดสอบด้วย NavGoal ปลอมได้)

หลักคิด: ตัวรันไม่ได้คุยกับ Nav2 เอง — "ส่ง goal ผ่าน NavGoal ตัวเดิมทุกครั้ง" ทุกจุดจึงผ่านด่านเดียวกับการสั่งทีละจุด
(confirmed · localize แล้ว · จุดอยู่บนช่องว่างและห่างผนัง · ทีละ goal · action server พร้อม)
และสถานะมาจาก Nav2 จริง

กติกาที่ "เซิร์ฟเวอร์เป็นคนบังคับ" :
  1. เริ่มได้เมื่อ confirmed เป็น True เป๊ะ · Navigation รันนิ่ง · localize แล้ว · ไม่มี Route/goal อื่นค้าง
     (วนไม่จบ ต้องยืนยันเพิ่มอีกชั้น forever_confirmed)
  2. ตรวจ "ทุกจุด" ของ Route กับแผนที่ก่อนปล่อยหุ่นออก (ไม่ใช่ปล่อยไปแล้วไปล้มกลางทาง)
  3. ล้มที่จุดไหน = หยุดทั้ง Route ไม่ข้ามไปจุดถัดไปเอง
     (aborted / ถูกปฏิเสธ / ไม่ localize / Nav ดับ / action server หาย / goal ถูกยกเลิกจากที่อื่น)
     ข้อยกเว้นเดียวที่ "รอ" ก่อนล้ม : ตำแหน่งไม่น่าเชื่อถือ (σ ของ AMCL เกินเกณฑ์) — ค่า σ แกว่งได้เองตอนหุ่นจอด
     จึงเข้าช่วง locating : ไม่ส่ง goal เลย สั่ง AMCL อัปเดตด้วยสแกนปัจจุบัน (ไม่ต้องให้ผู้ใช้กด Refresh)
     แล้วรอให้กลับมาเป็น good/fair ได้ไม่เกิน loc_grace วินาที ไม่ฟื้นค่อยล้ม
     (loc_grace = 0 = เข้มเหมือนเดิม : ล้ม/ปฏิเสธทันที)
  4. เพดานเวลา : ต่อจุด (point_timeout — Nav2 ของงานนี้หยุดรอสิ่งกีดขวางไม่หลบ จึงอาจรอนานมาก)
     และรวมทั้งงาน (max_run)
  5. ยกเลิก (stop) = ยกเลิก goal ที่ Nav2 + ส่งความเร็วศูนย์ + ไม่ส่งจุดถัดไปอีก (ปุ่ม ■ / Cancel trip เรียกอันนี้ด้วย)
  6. พัก (pause) : Nav2 ไม่มี "พัก" จริง → ยกเลิก goal ปัจจุบัน + ศูนย์ แล้วจำจุดไว้
     ต่อ (resume) = ตรวจ localize ซ้ำแล้ว "ส่ง goal ไปจุดเดิมใหม่" (หุ่นอาจถูกเข็นระหว่างพัก จึงไม่ใช้ goal เดิม)
     ระหว่างพักเวลาหยุดรอที่จุดหยุดนับ และไม่นับเข้าเพดานรวม
  7. รอบ (round) นับเมื่อหุ่นถึงจุดสุดท้ายของ Route (แล้วรอครบเวลา) ; รอบถัดไปเริ่มจากจุดแรกอีกครั้ง

โหมด straight (Route.mode) : ทุกช่วงเป็นเส้นตรงจริงจากจุดก่อนหน้าไปจุดนี้ ส่งผ่าน NavGoal.send_line (ข้าม planner)
  - เริ่มได้เมื่อหุ่นอยู่ที่จุดแรกและหันตามช่วงแรก (ห่าง ≤ LINE_START_TOL_M, เบี่ยง ≤ 20°) จึงเริ่มวิ่งที่จุดที่ 2
  - ก่อนปล่อยหุ่น ตรวจ "ทุกช่วง" ว่าว่างและห่างผนัง + ความยาวพอ + ทิศของจุดกลางทางต้องตรงกับช่วงถัดไป (≤ 20°)
    (ที่มุม หุ่นหยุดแล้วหมุนเข้าทิศของจุด = ทิศช่วงถัดไป จึงออกตัวตรงโดยไม่ต้องหมุนอีก)
  - หลายรอบ : ช่วงปิดวงจากจุดสุดท้ายกลับจุดแรกก็เป็นเส้นตรงและถูกตรวจด้วย
  - จุด "ผ่านไปเลย" (stop=False) : ไม่หยุด/ไม่หมุน/ไม่รอ ; จุดหยุดสองจุดที่คั่นด้วยจุดผ่านถูกรวมเป็น "ท่อน" เดียว ส่งให้ตัวควบคุมครั้งเดียว
    ตรงจุดผ่านที่เป็นมุมเลี้ยว เส้นเลี้ยวโค้งมน (รัศมี pass_radius ลดลงให้พอดีช่วง) จึงไม่ผ่านตัวจุดเป๊ะ ; แคบเกิน = ปฏิเสธ
  - ต่อจากพัก : ส่งเส้นตรงใหม่จาก "ตำแหน่งหุ่นตอนนี้" ไปจุดเดิม (หุ่นอยู่กลางช่วง ไม่ใช่ที่จุดเริ่มช่วง)

เครื่องสถานะ : idle → running ⇄ paused → succeeded | canceled | failed
(สามค่าท้ายค้างไว้ให้หน้าเว็บเห็นผลจนเริ่มรอบใหม่)
"""
import math
import threading
import time

from agv_web_app.msg import msg
from agv_web_app.nav_goal import BUSY_STATES, LINE_HEADING_TOL_RAD, PASS_RADIUS_M, CornerError, GoalError
from agv_web_app.routes import normalize_yaw, validate_route

ACTIVE = ('running', 'paused')
FINAL = ('succeeded', 'canceled', 'failed')
POINT_TIMEOUT_S = 600.0            # ต่อหนึ่งจุด (รวมเวลาที่ Nav2 หยุดรอสิ่งกีดขวาง)
MAX_RUN_S = 7200.0                 # รวมทั้งงาน (เฉพาะเวลาที่ไม่ได้พัก) — กันวนไม่จบจนลืม
POLL_S = 0.2
LOC_GRACE_S = 15.0                 # รอให้ตำแหน่งกลับมาน่าเชื่อถือได้นานสุดเท่านี้ ก่อนหยุด Route (0 = ไม่รอ)
LOC_REFRESH_EVERY_S = 3.0          # ระหว่างรอ สั่ง AMCL อัปเดตซ้ำทุกเท่านี้


class RunError(Exception):
    """ปฏิเสธการสั่ง (ไม่มีอะไรถูกส่งให้ Nav2) ; code/params ใช้สร้างข้อความให้หน้าเว็บแปลภาษา"""

    def __init__(self, code, **params):
        super().__init__(code)
        self.code = code
        self.params = params


class RouteRunner:

    def __init__(self, goal, probe, localized, nav_up, stop_motion, on_round=None, on_event=None,
                 clock=time.monotonic, sleep=time.sleep, point_timeout=POINT_TIMEOUT_S, max_run=MAX_RUN_S,
                 poll=POLL_S, threaded=True, refresh=None, loc_grace=LOC_GRACE_S, refresh_every=LOC_REFRESH_EVERY_S,
                 pose=None, path_probe=None, pass_radius=PASS_RADIUS_M):
        """goal = NavGoal ; probe(x, y, r) = ตัวตรวจจุดบนแผนที่ที่ Nav2 ใช้ (LiveMap.probe) ;
        localized() / nav_up() -> bool ; stop_motion() = ส่งความเร็วศูนย์ ; on_round() = แจ้งว่าครบ 1 รอบ ;
        refresh() = สั่ง AMCL อัปเดตด้วยสแกนปัจจุบัน (ล้มเหลวได้ ไม่เป็นไร) ; loc_grace = วินาทีที่ยอมรอตำแหน่งฟื้น ;
        pose() -> (x, y, yaw เรเดียน) หรือ None และ path_probe(points, radius, soft) (LiveMap.probe_path) ;
        pass_radius = รัศมีเลี้ยวโค้งมนที่จุดผ่าน ; ใช้เฉพาะ Route โหมด straight (ไม่ให้มา = รัน Route โหมด straight ไม่ได้)"""
        self._goal, self._probe = goal, probe
        self._localized, self._nav_up, self._stop_motion = localized, nav_up, stop_motion
        self._on_round = on_round or (lambda: None)
        self._on_event = on_event or (lambda text: None)
        self._clock, self._sleep = clock, sleep
        self._point_timeout, self._max_run, self._poll = float(point_timeout), float(max_run), float(poll)
        self._threaded = threaded       # False = ไม่เปิด thread (เทสต์เรียก tick() เอง)
        self._pose, self._path_probe, self._pass_radius = pose, path_probe, float(pass_radius)
        self._refresh = refresh or (lambda: None)
        self._loc_grace, self._refresh_every = max(0.0, float(loc_grace)), float(refresh_every)
        self._lock = threading.Lock()
        self._run_id = 0
        self._reset_fields('idle')
        self.thread = None

    def _reset_fields(self, state):
        self._state = state
        self._name = self._map = None
        self._points, self._loop, self._mode = [], 1, 'navigate'
        self._stops = []                                         # โหมด straight : ลำดับจุดหยุด (index ใน points)
        self._round, self._index = 1, 0
        # going = กำลังไป/ส่ง goal · waiting = หยุดรอที่จุด · locating = รอตำแหน่งฟื้นก่อนออกเดิน
        self._phase = 'going'
        self._wait_until = self._wait_left = None
        self._loc_until = self._loc_next = self._loc_left = None     # ช่วง locating (รอตำแหน่งฟื้น)
        self._goal_seq = None
        self._point_started = None
        self._started = self._deadline = None
        self._paused_at = None
        self._reason = None

    # ── คำสั่ง ──────────────────────────────────────────────────────────
    def start(self, name, map_name, route, check_probe, clearance, confirmed, forever_confirmed=False):
        """เริ่มรัน Route ; ปฏิเสธ (RunError / RouteError / GoalError) = ไม่มีอะไรถูกส่งให้ Nav2
        route = {'loop', 'points'} ; check_probe = ตัวตรวจจุดกับ "ไฟล์แผนที่ที่บันทึกไว้" (ตรวจทุกจุดก่อนปล่อยหุ่น)"""
        if confirmed is not True:
            raise RunError('route.run.confirm_required')
        with self._lock:
            if self._state in ACTIVE:
                raise RunError('route.run.busy')
        if not self._nav_up():
            raise RunError('route.run.no_nav')
        if self._goal.status()['state'] in BUSY_STATES:
            raise RunError('route.run.goal_busy')
        localized = bool(self._localized())
        if not localized and self._loc_grace <= 0:
            raise RunError('route.run.not_localized')
        if route.get('loop') == 0 and forever_confirmed is not True:
            raise RunError('route.run.forever_confirm_required')
        _, clean = validate_route(name, route.get('loop'), route.get('points'), check_probe, clearance,
                                  route.get('mode', 'navigate'))
        if clean['mode'] == 'straight':
            self._check_straight(clean)
        now = self._clock()
        with self._lock:
            if self._state in ACTIVE:                        # ผู้เรียกสองคนพร้อมกัน : คนแรกชนะ
                raise RunError('route.run.busy')
            self._run_id += 1
            run_id = self._run_id
            self._reset_fields('running')
            self._name, self._map = name, map_name
            self._points, self._loop, self._mode = clean['points'], clean['loop'], clean['mode']
            self._stops = self._stop_indices(clean['points']) if self._mode == 'straight' else []
            self._index = self._stops[1] if self._mode == 'straight' else 0   # straight : หุ่นอยู่ที่จุดแรกแล้ว เริ่มไปจุดหยุดถัดไป
            self._started, self._deadline = now, now + self._max_run
        if not localized:
            self._begin_locating(run_id)                     # ยังไม่ส่ง goal : สั่ง AMCL อัปเดต แล้วรอก่อน
        else:
            try:
                self._send_current(run_id)
            except (GoalError, RunError):
                with self._lock:
                    if self._run_id == run_id:
                        self._reset_fields('idle')           # ส่งจุดแรกไม่ได้ = ไม่เคยเริ่ม ไม่ค้างสถานะ failed
                raise
        self._on_event(f'Route run: start “{name}” ({len(clean["points"])} points, loop={clean["loop"]}, '
                       f'mode={clean["mode"]})')
        if self._threaded:
            self.thread = threading.Thread(target=self._loop_thread, args=(run_id,), daemon=True)
            self.thread.start()

    def stop(self):
        """ยกเลิกทั้ง Route ; คืน True ถ้ามี Route ทำงานอยู่ (เรียกซ้ำ/ตอนไม่มี Route ได้ ไม่เป็นไร)"""
        with self._lock:
            if self._state not in ACTIVE:
                return False
            self._state = 'canceled'
            self._reason = None
        self._halt()
        self._on_event('Route run: canceled')
        return True

    def pause(self):
        """พัก : ยกเลิก goal ปัจจุบัน + ศูนย์ แล้วจำจุดไว้ (ระหว่างหยุดรอที่จุด = หยุดนับเวลารอ ไม่มี goal ให้ยกเลิก)"""
        with self._lock:
            if self._state != 'running':
                raise RunError('route.run.not_running')
            now = self._clock()
            self._state = 'paused'
            self._paused_at = now
            phase = self._phase
            if phase == 'waiting':
                self._wait_left = max(0.0, self._wait_until - now)
            elif phase == 'locating':
                self._loc_left = max(0.0, self._loc_until - now)
        if phase == 'going':
            self._halt()
        self._on_event('Route run: paused')

    def resume(self):
        """ต่อ : ตรวจ Nav/localize ซ้ำ แล้วส่ง goal ไปจุดเดิมใหม่ (หรือนับเวลารอต่อ) — ไม่ผ่าน = ยังพักอยู่"""
        with self._lock:
            if self._state != 'paused':
                raise RunError('route.run.not_paused')
            run_id, phase = self._run_id, self._phase
        if not self._nav_up():
            raise RunError('route.run.no_nav')
        if phase != 'locating' and not self._localized():
            raise RunError('route.run.not_localized')
        if phase == 'going':
            if self._goal.status()['state'] in BUSY_STATES:
                raise RunError('route.run.pausing')          # goal เก่ายังไม่หยุดสนิท
            self._send_current(run_id, from_robot=True)
        now = self._clock()
        with self._lock:
            if self._run_id != run_id or self._state != 'paused':
                return
            if self._phase == 'waiting':
                self._wait_until = now + (self._wait_left or 0.0)
            elif self._phase == 'locating':
                self._loc_until = now + (self._loc_left or self._loc_grace)
                self._loc_next = now
            self._deadline += now - self._paused_at          # เวลาที่พักไม่นับเข้าเพดานรวม
            self._paused_at = None
            self._state = 'running'
        self._on_event('Route run: resumed')

    def active(self):
        with self._lock:
            return self._state in ACTIVE

    # ── ภายใน ───────────────────────────────────────────────────────────
    def _halt(self):
        self._goal.cancel()
        try:
            self._stop_motion()
        except Exception:                                    # noqa: BLE001 — การหยุดต้องไม่ล้มเพราะตัวส่งศูนย์
            pass

    def _end(self, run_id, state, reason=None):
        with self._lock:
            if self._run_id != run_id or self._state not in ACTIVE:
                return False
            self._state, self._reason = state, reason
        self._on_event(f'Route run: {state}' + (f' ({reason["code"]})' if reason else ''))
        return True

    def _fail(self, run_id, reason):
        if self._end(run_id, 'failed', reason):
            self._halt()

    @staticmethod
    def _stop_indices(points):
        """จุดหยุดของ Route โหมด straight : จุดแรก + จุดที่ stop=True + จุดสุดท้าย (จุดอื่น = ผ่านไปเลย)"""
        last = len(points) - 1
        return [0] + [i for i in range(1, last) if points[i].get('stop', True)] + [last]

    def _next_index(self, index):
        """จุดเป้าหมายถัดไปต่อจาก index (straight ข้ามจุดผ่าน) ; len(points) = จบรอบ"""
        if self._mode != 'straight':
            return index + 1
        for s in self._stops:
            if s > index:
                return s
        return len(self._points)

    @staticmethod
    def _leg_waypoints(points, stops, target):
        """จุดทั้งหมดของท่อนที่ไป target : ปิดวง (target = 0) = จากจุดท้ายตรงมาจุดแรก ; อื่นๆ = จากจุดหยุดก่อนหน้า ผ่านจุดผ่าน ถึง target"""
        if target == 0:
            return [points[-1], points[0]]
        previous = max(s for s in stops if s < target)
        return points[previous:target + 1]

    def _check_straight(self, clean):
        """Route โหมด straight : ตรวจ "ทุกท่อน" ก่อนปล่อยหุ่น (ไม่ใช่ปล่อยไปแล้วไปล้มกลางทาง) — ผิดข้อไหนไม่ส่งอะไร"""
        if self._pose is None or self._path_probe is None:
            raise RunError('route.run.straight_unavailable')
        pts = clean['points']
        repeat = clean['loop'] != 1
        stops = self._stop_indices(pts)
        limits = self._goal.limits()
        pairs = list(zip(pts, pts[1:])) + ([(pts[-1], pts[0])] if repeat else [])
        for k, (a, b) in enumerate(pairs, start=1):          # ทุกช่วงระหว่างจุดติดกัน (รวมจุดผ่าน) ต้องยาวพอและไม่ยาวเกิน
            length = math.hypot(b['x'] - a['x'], b['y'] - a['y'])
            if not limits['line_min'] <= length <= limits['straight_max']:
                raise RunError('route.run.straight_length', n=k, min=limits['line_min'], max=limits['straight_max'])
        targets = stops[1:] + ([0] if repeat else [])
        legs = []
        for t in targets:
            wps = self._leg_waypoints(pts, stops, t)
            try:
                leg = self._goal.plan_leg([(p['x'], p['y']) for p in wps], pts[t]['yaw'], self._path_probe, self._pass_radius)
            except CornerError as e:
                raise RunError('route.run.straight_corner', n=pts.index(wps[e.index]) + 1, found=e.params['found'],
                               min=e.params['min'])
            if not leg['ok']:
                raise RunError('route.run.straight_blocked', n=t + 1, at=leg['at'], clearance=limits['clearance'])
            legs.append((wps, t))
        for k, (wps, t) in enumerate(legs):                   # ทิศของจุดหยุดที่ไปถึง ต้องตรงกับท่อนถัดไป (ไม่ต้องหมุนซ้ำตอนออกตัว)
            following = legs[k + 1] if k + 1 < len(legs) else (legs[0] if repeat else None)
            if following is None:
                continue
            nxt = following[0]
            direction = math.atan2(nxt[1]['y'] - nxt[0]['y'], nxt[1]['x'] - nxt[0]['x'])
            off = abs(normalize_yaw(direction - pts[t]['yaw']))
            if off > LINE_HEADING_TOL_RAD:
                raise RunError('route.run.straight_yaw', n=t + 1, found=round(math.degrees(off), 1),
                               max=round(math.degrees(LINE_HEADING_TOL_RAD)))

    def _send_current(self, run_id, from_robot=False):
        with self._lock:
            if self._run_id != run_id or self._state not in ACTIVE:
                return
            point = dict(self._points[self._index])
            straight = self._mode == 'straight'
            points = [dict(p) for p in self._points] if straight else None
            stops, target = list(self._stops), self._index
        if straight:
            pose = self._pose()
            wps = [(p['x'], p['y']) for p in self._leg_waypoints(points, stops, target)]
            if from_robot and pose is not None:                # ต่อจากพัก : จากตำแหน่งหุ่น ผ่านเฉพาะจุดผ่านที่ยังไม่ถึง
                wps = [(pose[0], pose[1])] + wps[self._passed_segment(wps, pose) + 1:]
            status, _ = self._goal.send_leg(wps, point['yaw'], pose, self._path_probe, bool(self._localized()), True,
                                            self._pass_radius)
        else:
            status = self._goal.send(point['x'], point['y'], point['yaw'], self._probe, bool(self._localized()), True)
        with self._lock:
            if self._run_id != run_id or self._state not in ACTIVE:
                stale = True                                  # ระหว่างรอ Nav2 รับ ผู้ใช้สั่งยกเลิกไปแล้ว
            else:
                stale = False
                self._phase = 'going'
                self._goal_seq = status['seq']
                self._point_started = self._clock()
        if stale:
            self._halt()

    @staticmethod
    def _passed_segment(waypoints, pose):
        """ช่วง (index ของจุดเริ่มช่วง) ที่ตำแหน่งหุ่นอยู่ใกล้ที่สุด : จุดตรงกลางที่ index ≤ ค่านี้ถือว่าผ่านมาแล้ว"""
        best, best_d = 0, float('inf')
        for k in range(len(waypoints) - 1):
            (x0, y0), (x1, y1) = waypoints[k], waypoints[k + 1]
            dx, dy = x1 - x0, y1 - y0
            length2 = dx * dx + dy * dy
            t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((pose[0] - x0) * dx + (pose[1] - y0) * dy) / length2))
            d = math.hypot(pose[0] - (x0 + t * dx), pose[1] - (y0 + t * dy))
            if d < best_d:
                best, best_d = k, d
        return best

    def _loop_thread(self, run_id):
        while self.tick(run_id):
            self._sleep(self._poll)

    def tick(self, run_id=None):
        """ก้าวหนึ่งรอบของเครื่องสถานะ (thread ภายในเรียกทุก ~0.2 วินาที) ; คืน False เมื่อ Route จบแล้ว"""
        with self._lock:
            if run_id is None:
                run_id = self._run_id
            if self._run_id != run_id or self._state not in ACTIVE:
                return False
            state, phase, deadline = self._state, self._phase, self._deadline
            index, seq, point_started, wait_until = self._index, self._goal_seq, self._point_started, self._wait_until
        now = self._clock()
        if not self._nav_up():                                # Nav ดับ/action server หาย : ล้มทันที (พักอยู่ก็ล้ม)
            self._fail(run_id, msg('route.run.nav_lost'))
            return False
        if state == 'paused':
            return True
        if now >= deadline:
            self._fail(run_id, msg('route.run.max_time'))
            return False
        if phase == 'waiting':
            if now >= wait_until:
                return self._advance(run_id)
            return True
        if phase == 'locating':
            return self._locating(run_id, now)
        g = self._goal.status(nav_up=True)
        if g['seq'] != seq:                                   # มีใครส่ง goal อื่นแทรก (ไม่ควรเกิด) → ไม่ปล่อยให้สับสน
            self._fail(run_id, msg('route.run.goal_conflict'))
            return False
        if g['state'] in ('sending', 'active'):
            if now - point_started >= self._point_timeout:
                self._fail(run_id, msg('route.run.point_timeout', n=index + 1, seconds=int(self._point_timeout)))
                return False
            return True
        if g['state'] == 'canceling':
            return True
        if g['state'] == 'succeeded':
            return self._arrived(run_id, now)
        if g['state'] == 'canceled':                          # ถูกยกเลิกจากที่อื่นโดยไม่ผ่านตัวรัน
            if self._end(run_id, 'canceled', msg('route.run.goal_canceled')):
                self._halt()
            return False
        reason = g['reason']
        self._fail(run_id, msg(reason) if reason else msg('nav.goal.aborted'))
        return False

    def _begin_locating(self, run_id):
        now = self._clock()
        with self._lock:
            if self._run_id != run_id or self._state not in ACTIVE:
                return
            self._phase = 'locating'
            self._loc_until = now + self._loc_grace
            self._loc_next = now + self._refresh_every
        self._on_event('Route run: waiting for localization')
        self._try_refresh()

    def _try_refresh(self):
        try:
            self._refresh()
        except Exception:                                    # noqa: BLE001 — สั่งอัปเดตไม่สำเร็จก็แค่รอต่อ
            pass

    def _locating(self, run_id, now):
        """รอตำแหน่งฟื้น : ฟื้นแล้ว → ส่ง goal ไปจุดปัจจุบัน ; หมดเวลา → หยุด Route ; ระหว่างรอสั่ง AMCL อัปเดตซ้ำ"""
        if self._localized():
            with self._lock:
                if self._run_id != run_id or self._state not in ACTIVE:
                    return False
                self._phase = 'going'
            try:
                self._send_current(run_id)
            except GoalError as e:
                self._fail(run_id, msg(e.code, **e.params))
                return False
            return True
        with self._lock:
            until, nxt = self._loc_until, self._loc_next
        if now >= until:
            self._fail(run_id, msg('route.run.not_localized'))
            return False
        if now >= nxt:
            with self._lock:
                self._loc_next = now + self._refresh_every
            self._try_refresh()
        return True

    def _arrived(self, run_id, now):
        with self._lock:
            if self._run_id != run_id or self._state not in ACTIVE:
                return False
            wait = self._points[self._index]['wait']
            if wait > 0:
                self._phase = 'waiting'
                self._wait_until = now + wait
                return True
        return self._advance(run_id)

    def _advance(self, run_id):
        """ถึงจุดและรอครบแล้ว → จุดถัดไป / ครบรอบ / จบ"""
        finished_round = False
        with self._lock:
            if self._run_id != run_id or self._state not in ACTIVE:
                return False
            self._index = self._next_index(self._index)
            if self._index >= len(self._points):
                finished_round = True
        if finished_round:
            self._on_round()
            with self._lock:
                if self._run_id != run_id or self._state not in ACTIVE:
                    return False
                if self._loop != 0 and self._round >= self._loop:
                    self._state, self._reason = 'succeeded', None
                    done = True
                else:
                    self._round += 1
                    self._index = 0
                    done = False
            if done:
                self._on_event('Route run: succeeded')
                return False
        if not self._localized():                             # ก่อนออกเดินทุกจุด : ตำแหน่งต้องเชื่อถือได้
            if self._loc_grace > 0:
                self._begin_locating(run_id)                  # ไม่ล้มทันที : สั่ง AMCL อัปเดตแล้วรอให้ฟื้นก่อน
                return True
            self._fail(run_id, msg('route.run.not_localized'))
            return False
        try:
            self._send_current(run_id)
        except GoalError as e:
            self._fail(run_id, msg(e.code, **e.params))
            return False
        return True

    # ── สถานะ (JSON) ────────────────────────────────────────────────────
    def status(self):
        now = self._clock()
        with self._lock:
            state = self._state
            points = self._points
            shown = state != 'idle' and points and self._index < len(points)
            current = dict(points[self._index]) if shown else None
            waiting = self._phase == 'waiting'
            if state == 'paused' and waiting:
                wait_left = self._wait_left
            elif state == 'running' and waiting and self._wait_until is not None:
                wait_left = max(0.0, self._wait_until - now)
            else:
                wait_left = None
            if state == 'running' and self._phase == 'locating' and self._loc_until is not None:
                loc_left = max(0.0, self._loc_until - now)
            elif state == 'paused' and self._phase == 'locating':
                loc_left = self._loc_left
            else:
                loc_left = None
            active = state in ACTIVE
            clock_at = self._paused_at if state == 'paused' else now
            elapsed = None if self._started is None else round(clock_at - self._started, 1)
            out = {'state': state, 'run': self._run_id, 'route': self._name, 'map': self._map, 'loop': self._loop,
                   'mode': self._mode,
                   'round': self._round if state != 'idle' else 0, 'index': self._index if state != 'idle' else 0,
                   'total': len(points), 'current': current, 'phase': self._phase if active else None,
                   'wait_left': None if wait_left is None else round(wait_left, 1), 'elapsed': elapsed,
                   'loc_left': None if loc_left is None else round(loc_left, 1), 'loc_grace': self._loc_grace,
                   'reason': self._reason, 'max_run': self._max_run, 'point_timeout': self._point_timeout}
        out['pause_settled'] = state != 'paused' or self._goal.status()['state'] not in BUSY_STATES
        return out
