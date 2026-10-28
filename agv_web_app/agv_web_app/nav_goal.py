"""
nav_goal — ส่งหุ่นไปจุดหมายบนแผนที่ผ่าน Nav2 (NavigateToPose) ที่ปลอดภัยไว้ก่อน (ไม่พึ่ง ROS → ทดสอบด้วย client ปลอมได้)

กติกาที่ "เซิร์ฟเวอร์เป็นคนบังคับ" (หน้าเว็บแค่ช่วยแสดง) — ผิดข้อไหน ไม่ส่งอะไรให้ Nav2 เลย:
  1. ต้องมี confirmed=True ทุกครั้ง (หน้าเว็บถามยืนยันก่อน)
  2. ตัวเลขต้องเป็นจำนวนจริงจำกัด และต้องมีแผนที่
  3. ต้อง localize แล้ว (AMCL เชื่อถือได้พอ) มิฉะนั้นไม่รู้ว่าหุ่นอยู่ตรงไหน
  4. จุดหมายต้องอยู่ในแผนที่ · เป็นช่องที่ "รู้ว่าว่าง" · ห่างช่องไม่ว่าง/ไม่รู้ อย่างน้อย clearance เมตร
  5. ทีละ goal เดียว (มี goal ค้างอยู่ = ปฏิเสธ ไม่ซ้อนคิว)
  6. ต้องมี action server ของ Nav2 พร้อมจริง
  - การยกเลิก = ยกเลิก goal ที่ Nav2 (ตัวหยุดจริงคือ Nav2 เอง ไม่ใช่เว็บ) ; ผู้เรียกส่งความเร็วศูนย์ซ้ำอีกชั้น
  - สถานะมาจาก feedback/result ของ Nav2 จริง ; "waiting" (ระยะไม่ลดลงนาน) เป็น "ข้อสันนิษฐาน" ไม่ใช่ข้อเท็จจริง
    (Nav2 ของงานนี้หยุดรอเมื่อมีสิ่งกีดขวาง ไม่หลบ) จึงมีธง waiting_assumed แยกไว้

client = วัตถุที่มี
    ready() -> bool                       action server พร้อมไหม
    send(x, y, yaw, listener)             ส่ง goal แบบไม่บล็อก ; แล้วเรียก listener.on_accepted(bool),
                                          listener.on_feedback({distance_remaining, eta, recoveries, elapsed}),
                                          listener.on_result('succeeded' | 'canceled' | 'failed')
    cancel()                              ขอยกเลิก goal ปัจจุบัน (ถ้ายังไม่ถูกรับ ให้ยกเลิกทันทีที่ถูกรับ) ไม่บล็อก
    line_ready() -> bool                  action server ที่ตามเส้นพร้อมไหม (FollowPath ของ controller_server)
    send_line(points, listener)           ส่งเส้นตรงที่สร้างเองให้ตัวควบคุมตามตรงๆ ไม่ผ่าน planner (points = [(x, y, yaw), ...])
                                          listener เหมือน send()

โหมด "วิ่งตามเส้นตรง" (send_line) : ข้าม planner ทั้งหมด (planner วางเส้นใหม่ซ้ำๆ จึงไม่ตรง) เส้นถูกสร้างที่นี่เป็นเส้นตรงจริง
ตัวควบคุม (RPP) ตามเส้นนั้นเส้นเดียว ; ใช้กติกา/สถานะ/การยกเลิก/ปุ่ม ■ ชุดเดียวกับ goal ปกติ (ทีละงานเดียว)
"""
import math
import threading
import time

from agv_web_app.nav_pose import LIMIT, normalize_yaw

LOCALIZED = ('good', 'fair')       # คุณภาพ localization ที่ยอมให้ส่ง goal
CLEARANCE_M = 0.40                 # ห่างช่องไม่ว่าง/ไม่รู้อย่างน้อยเท่านี้ (ครึ่งความกว้างหุ่น ~0.33 + เผื่อ)
ACCEPT_TIMEOUT_S = 5.0
STALL_S = 8.0                      # ระยะไม่ลดลงนานเท่านี้ = สันนิษฐานว่ากำลังรอ/ติด
STALL_PROGRESS_M = 0.05            # ลดลงอย่างน้อยเท่านี้ถึงนับว่ามีความคืบหน้า
STRAIGHT_MIN_M = 0.5               # Drive straight : ระยะต่ำสุด/สูงสุดต่อคำสั่ง (สูงสุดตั้งได้ที่ drive_straight_max)
STRAIGHT_MAX_M = 10.0
SOFT_CLEARANCE_M = 0.55            # ห่างน้อยกว่านี้ = อยู่ในเขต inflation → planner อาจดึงเส้นเข้ากลางทาง (ไม่ตรงเป๊ะ)

LINE_MIN_M = 0.3                   # วิ่งตามเส้นตรง : ความยาวเส้นต่ำสุด (สูงสุด = drive_straight_max เหมือน Drive straight)
LINE_STEP_M = 0.05                 # ระยะห่างระหว่างตำแหน่งบนเส้นที่ส่งให้ตัวควบคุม
LINE_START_TOL_M = 0.30            # หุ่นต้องอยู่ใกล้จุดเริ่มเส้นไม่เกินเท่านี้
LINE_HEADING_TOL_RAD = math.radians(20.0)   # และหันตรงแนวเส้นไม่เกินเท่านี้ (เกิน 45° ตัวควบคุมจะหมุนอยู่กับที่ก่อนออกตัว)

PASS_RADIUS_M = 0.9                # จุด "ผ่านไปเลย" (ไม่หยุด) ที่เป็นมุมเลี้ยว : เลี้ยวโค้งมนรัศมีนี้ (ลดลงถ้าช่วงสั้น)
PASS_MIN_RADIUS_M = 0.45           # แคบกว่านี้ = ผ่านโดยไม่ชะลอไม่ได้ (รถลากจูง) → ต้องเป็นจุดหยุด
CORNER_SKIP_RAD = math.radians(2.0)    # เลี้ยวน้อยกว่านี้ถือว่าอยู่แนวตรงเดียวกัน (ผ่านจุดเป๊ะ ไม่ต้องโค้ง)
CORNER_MAX_RAD = math.radians(150.0)   # หักกลับเกินนี้ = ผ่านโดยไม่หยุดไม่ได้

BUSY_STATES = ('sending', 'active', 'canceling')
STRAIGHT_BLOCKED = {'outside': 'nav.err.straight_outside', 'unknown': 'nav.err.straight_unknown',
                    'occupied': 'nav.err.straight_unknown', 'tight': 'nav.err.straight_tight',
                    'no_map': 'nav.err.no_map'}
PROBE_ERRORS = {'outside': 'nav.err.goal_outside', 'unknown': 'nav.err.goal_unknown',
                'occupied': 'nav.err.goal_unknown', 'tight': 'nav.err.goal_tight', 'no_map': 'nav.err.no_map'}


class GoalError(Exception):
    """code/params ใช้สร้างข้อความให้หน้าเว็บแปลภาษา"""

    def __init__(self, code, **params):
        super().__init__(code)
        self.code = code
        self.params = params


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GoalError('nav.err.goal_invalid')
    value = float(value)
    if not math.isfinite(value) or abs(value) > LIMIT:
        raise GoalError('nav.err.goal_invalid')
    return value


class CornerError(GoalError):
    """มุมที่เป็นจุด "ผ่านไปเลย" แคบเกินกว่าจะเลี้ยวโดยไม่ชะลอ ; index = ลำดับของจุดมุมใน waypoints (0 = จุดแรก)"""

    def __init__(self, index, found):
        super().__init__('nav.err.line_corner', found=round(found, 2), min=PASS_MIN_RADIUS_M)
        self.index = index


def smooth_path(waypoints, end_yaw, radius=PASS_RADIUS_M, step=LINE_STEP_M, min_radius=PASS_MIN_RADIUS_M):
    """เส้นทางต่อเนื่องผ่านจุดทั้งหมด [(x, y), ...] : ช่วงตรงเป็นเส้นตรงจริง ส่วน "จุดตรงกลาง" ที่เป็นมุมเลี้ยวถูกแทนด้วยส่วนโค้ง
    รัศมี radius (ลดลงให้พอดีครึ่งหนึ่งของช่วงที่ติดกัน) ; คืน [(x, y, yaw), ...] ถี่ ~step เมตร ทิศทุกจุดตามเส้นทาง
    จุดสุดท้ายอยู่ที่จุดสุดท้ายเป๊ะและหันตาม end_yaw ; โยน CornerError ถ้ามุมไหนแคบกว่า min_radius หรือหักกลับเกิน 150°"""
    pts = [(float(x), float(y)) for x, y in waypoints]
    n = len(pts)
    lens = [math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1]) for i in range(n - 1)]
    dirs = [math.atan2(pts[i + 1][1] - pts[i][1], pts[i + 1][0] - pts[i][0]) for i in range(n - 1)]
    corners = {}
    for i in range(1, n - 1):
        turn = normalize_yaw(dirs[i] - dirs[i - 1])
        theta = abs(turn)
        if theta < CORNER_SKIP_RAD:
            continue
        if theta > CORNER_MAX_RAD:
            raise CornerError(i, 0.0)
        tangent = min(radius * math.tan(theta / 2), min(lens[i - 1], lens[i]) / 2)
        eff = tangent / math.tan(theta / 2)
        if eff < min_radius - 1e-9:
            raise CornerError(i, eff)
        corners[i] = (tangent, eff, 1.0 if turn > 0 else -1.0, theta)
    out = []
    for j in range(n - 1):
        start = corners[j][0] if j in corners else (step if j > 0 else 0.0)   # ไม่มีโค้ง : จุด P_j ถูกส่งไปแล้วเป็นปลายช่วงก่อนหน้า (ไม่ส่งซ้ำ)
        end = lens[j] - corners[j + 1][0] if (j + 1) in corners else lens[j]
        d = dirs[j]
        s = start
        while s < end - 1e-9:
            out.append((pts[j][0] + s * math.cos(d), pts[j][1] + s * math.sin(d), d))
            s += step
        if (j + 1) not in corners:
            out.append((pts[j + 1][0], pts[j + 1][1], d))
        else:
            tangent, eff, sign, theta = corners[j + 1]
            tx, ty = pts[j + 1][0] - tangent * math.cos(d), pts[j + 1][1] - tangent * math.sin(d)
            cx, cy = tx + sign * eff * -math.sin(d), ty + sign * eff * math.cos(d)
            count = max(1, math.ceil(eff * theta / step))
            for k in range(count):
                h = d + sign * theta * k / count
                out.append((cx - sign * eff * -math.sin(h), cy - sign * eff * math.cos(h), h))
    x, y, _ = out[-1]
    out[-1] = (pts[-1][0], pts[-1][1], normalize_yaw(float(end_yaw)))
    return out


def line_points(x0, y0, x1, y1, end_yaw, step=LINE_STEP_M):
    """เส้นตรงจริงจาก (x0, y0) ถึง (x1, y1) : [(x, y, yaw), ...] ทุก step เมตร ; ทิศทุกจุด = ทิศของเส้น
    ยกเว้นจุดสุดท้าย = end_yaw (ตัวควบคุมใช้ทิศจุดสุดท้ายหมุนเข้าหาทิศปลายทาง)"""
    length = math.hypot(x1 - x0, y1 - y0)
    direction = math.atan2(y1 - y0, x1 - x0)
    count = max(1, math.ceil(length / step))
    points = []
    for i in range(count + 1):
        t = i / count
        points.append((x0 + (x1 - x0) * t, y0 + (y1 - y0) * t, direction))
    points[-1] = (x1, y1, end_yaw)
    return points


class _Listener:
    """ตัวรับ callback ของ goal หนึ่งๆ — ทิ้งข้อความของ goal เก่าที่มาช้า (เทียบ seq)"""

    def __init__(self, owner, seq):
        self._owner, self._seq = owner, seq

    def on_accepted(self, ok):
        self._owner._accepted(self._seq, ok)

    def on_feedback(self, feedback):
        self._owner._feedback(self._seq, feedback)

    def on_result(self, code):
        self._owner._result(self._seq, code)


class NavGoal:

    def __init__(self, client, on_event=None, clock=time.monotonic, clearance=CLEARANCE_M,
                 accept_timeout=ACCEPT_TIMEOUT_S, max_straight=STRAIGHT_MAX_M):
        self._client = client
        self._max_straight = float(max_straight)
        self._on_event = on_event or (lambda text: None)
        self._clock = clock
        self._clearance = float(clearance)
        self._accept_timeout = float(accept_timeout)
        self._lock = threading.Lock()
        self._accepted_event = threading.Event()
        self._seq = 0
        self._state = 'idle'
        self._goal = None
        self._line = None            # {'x0','y0'} เมื่อกำลังวิ่งตามเส้นตรง (None = goal ปกติ)
        self._reason = None          # รหัสข้อความ (nav.err.* / nav.goal.*) เมื่อ failed
        self._feedback_data = None
        self._started = None
        self._best = None            # ระยะเหลือที่ดีที่สุดที่เคยเห็น
        self._progress_at = None

    # ── สั่งไป ──────────────────────────────────────────────────────────
    def send(self, x, y, yaw, probe, localized, confirmed):
        """ตรวจทุกข้อ → ส่ง → รอให้ Nav2 รับ/ปฏิเสธ ; สำเร็จคืนสถานะ ; ผิดพลาดโยน GoalError (ไม่มี goal ค้าง)
        probe(x, y, radius) -> {'status', 'clearance'} (LiveMap.probe)"""
        if confirmed is not True:
            raise GoalError('nav.err.goal_confirm_required')
        x, y, yaw = _number(x), _number(y), normalize_yaw(_number(yaw))
        if not localized:
            raise GoalError('nav.err.goal_not_localized')
        result = probe(x, y, self._clearance)
        if result['status'] != 'ok':
            raise GoalError(PROBE_ERRORS[result['status']], clearance=self._clearance, found=result['clearance'])
        goal = {'x': x, 'y': y, 'yaw': yaw}
        return self._launch(goal, None, self._client.ready, lambda seq: self._client.send(x, y, yaw, _Listener(self, seq)),
                            f'Nav goal: x={x:.2f} y={y:.2f} yaw={math.degrees(yaw):.1f}°')

    def _launch(self, goal, line, ready, start, event):
        """ท่อนร่วมของ goal ปกติ/เส้นตรง : server พร้อม → ทีละงานเดียว → ส่ง → รอ Nav2 รับ/ปฏิเสธ
        ready() -> bool ; start(seq) = ส่งจริง (โยน exception ได้) ; ผิดพลาดโยน GoalError (ไม่มีงานค้าง)"""
        if not ready():
            raise GoalError('nav.err.goal_no_server')
        with self._lock:
            if self._state in BUSY_STATES:
                raise GoalError('nav.err.goal_busy')
            self._seq += 1
            seq = self._seq
            self._state, self._goal, self._line, self._reason = 'sending', dict(goal), line, None
            self._feedback_data, self._started = None, self._clock()
            self._best = self._progress_at = None
            self._accepted_event.clear()
        try:
            start(seq)
        except Exception:                                      # noqa: BLE001 — ส่งไม่ได้ = ไม่มี goal ค้าง
            self._finish(seq, 'failed', 'nav.err.goal_no_server')
            raise GoalError('nav.err.goal_no_server')
        if not self._accepted_event.wait(self._accept_timeout):
            self._client.cancel()
            self._finish(seq, 'failed', 'nav.err.goal_timeout')
            raise GoalError('nav.err.goal_timeout')
        with self._lock:
            rejected = self._seq == seq and self._state == 'failed'
        if rejected:
            raise GoalError('nav.err.goal_rejected')
        self._on_event(event)
        return self.status()

    def limits(self):
        return {'straight_min': STRAIGHT_MIN_M, 'straight_max': self._max_straight, 'clearance': self._clearance,
                'line_min': LINE_MIN_M}

    def check_straight(self, distance, pose, line_probe):
        """ตรวจล่วงหน้า (ไม่ส่งอะไร) ว่าเดินตรงไปข้างหน้า distance เมตรจาก pose = (x, y, yaw เรเดียน) ได้ไหม :
        จุดหมาย = pose + distance ตามทิศ pose ; ทุกช่องตลอดแนวต้องรู้ว่าว่างและห่างอย่างน้อย clearance
        คืน {'distance','goal':{x,y,yaw},'ok','at','reason','min_clearance','soft_risk'} ; ค่าผิดโยน GoalError
        line_probe(x0, y0, x1, y1, radius, soft) -> dict (LiveMap.probe_line)"""
        distance = _number(distance)
        if not STRAIGHT_MIN_M <= distance <= self._max_straight:
            raise GoalError('nav.err.straight_distance', min=STRAIGHT_MIN_M, max=self._max_straight)
        if pose is None:
            raise GoalError('nav.err.straight_no_pose')
        x, y, yaw = (_number(v) for v in pose)
        goal = {'x': x + distance * math.cos(yaw), 'y': y + distance * math.sin(yaw), 'yaw': normalize_yaw(yaw)}
        line = line_probe(x, y, goal['x'], goal['y'], self._clearance, SOFT_CLEARANCE_M)
        return dict(line, distance=distance, goal=goal)

    def send_straight(self, distance, pose, line_probe, probe, localized, confirmed):
        """เดินตรงไปข้างหน้า : กติกาเดียวกับ send() ทุกข้อ + ทั้งแนวเส้นตรงต้องปลอดภัย ; คืน (สถานะ, ผลตรวจแนว)"""
        if confirmed is not True:
            raise GoalError('nav.err.goal_confirm_required')
        if not localized:
            raise GoalError('nav.err.goal_not_localized')
        check = self.check_straight(distance, pose, line_probe)
        if not check['ok']:
            raise GoalError(STRAIGHT_BLOCKED[check['reason']], at=check['at'], clearance=self._clearance)
        goal = check['goal']
        return self.send(goal['x'], goal['y'], goal['yaw'], probe, localized, confirmed), check

    def check_segment(self, start, goal, line_probe):
        """ตรวจเฉพาะ "ตัวเส้น" start = (x0, y0) → goal = (x1, y1, yaw) โดยไม่ดูตำแหน่งหุ่น : ความยาวอยู่ในช่วง และทุกช่องตลอดแนว
        ว่างและห่างอย่างน้อย clearance ; คืน dict ของ probe_line + length, direction, goal ; ค่าผิดโยน GoalError
        (ใช้ตรวจทุกช่วงของ Route โหมด straight ก่อนปล่อยหุ่น)"""
        x0, y0 = _number(start[0]), _number(start[1])
        x1, y1, yaw = _number(goal[0]), _number(goal[1]), normalize_yaw(_number(goal[2]))
        length = math.hypot(x1 - x0, y1 - y0)
        if not LINE_MIN_M <= length <= self._max_straight:
            raise GoalError('nav.err.line_length', min=LINE_MIN_M, max=self._max_straight)
        line = line_probe(x0, y0, x1, y1, self._clearance, SOFT_CLEARANCE_M)
        return dict(line, length=round(length, 3), direction=math.atan2(y1 - y0, x1 - x0),
                    goal={'x': x1, 'y': y1, 'yaw': yaw})

    def check_line(self, start, goal, pose, line_probe):
        """ตรวจล่วงหน้า (ไม่ส่งอะไร) ว่าวิ่งตรงจาก start = (x0, y0) ไป goal = (x1, y1, yaw) ได้ไหม :
        ความยาวอยู่ในช่วง · ทุกช่องตลอดแนวต้องรู้ว่าว่างและห่างอย่างน้อย clearance (LiveMap.probe_line) ·
        หุ่น (pose = x, y, yaw) ต้องอยู่ใกล้จุดเริ่มและหันตรงแนวเส้น (ไม่งั้นตัวควบคุมจะหมุน/ปาดโค้งหาเส้นก่อน)
        คืน dict ของ probe_line + length, direction, start{distance, heading_deg, ok} ;
        ok = ปลอดภัยทั้งแนว "และ" เริ่มได้ ; ค่าผิดโยน GoalError"""
        x0, y0 = float(_number(start[0])), float(_number(start[1]))
        segment = self.check_segment(start, goal, line_probe)
        if pose is None:
            raise GoalError('nav.err.straight_no_pose')
        px, py, pyaw = (_number(v) for v in pose)
        distance = math.hypot(px - x0, py - y0)
        heading = abs(normalize_yaw(segment['direction'] - pyaw))
        start_ok = distance <= LINE_START_TOL_M and heading <= LINE_HEADING_TOL_RAD
        return dict(segment, ok=bool(segment['ok'] and start_ok),
                    start={'distance': round(distance, 3), 'heading_deg': round(math.degrees(heading), 1),
                           'ok': start_ok, 'max_distance': LINE_START_TOL_M,
                           'max_heading_deg': round(math.degrees(LINE_HEADING_TOL_RAD))})

    def plan_leg(self, waypoints, end_yaw, path_probe, radius=PASS_RADIUS_M):
        """เส้นทางต่อเนื่องหนึ่งท่อน (จุดหยุดถึงจุดหยุดถัดไป โดยผ่านจุด "ไม่หยุด" ตรงกลาง) โดยไม่ดูตำแหน่งหุ่น :
        สร้างเส้นตรง+โค้งมน (smooth_path) แล้วตรวจทั้งเส้นว่าว่างและห่างผนัง ≥ clearance ;
        คืน dict ของ path_probe + points, length, direction (ทิศช่วงแรก) ; มุมแคบเกิน = CornerError"""
        for point in waypoints:
            _number(point[0]), _number(point[1])
        _number(end_yaw)
        points = smooth_path([(p[0], p[1]) for p in waypoints], end_yaw, radius)
        result = path_probe(points, self._clearance, SOFT_CLEARANCE_M)
        length = sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(points, points[1:]))
        direction = math.atan2(waypoints[1][1] - waypoints[0][1], waypoints[1][0] - waypoints[0][0])
        return dict(result, points=points, length=round(length, 3), direction=direction)

    def check_leg(self, waypoints, end_yaw, pose, path_probe, radius=PASS_RADIUS_M):
        """plan_leg + หุ่นต้องอยู่ใกล้จุดแรกและหันตรงทิศช่วงแรก (เหมือน check_line) ; ok = ปลอดภัยทั้งเส้น และเริ่มได้"""
        leg = self.plan_leg(waypoints, end_yaw, path_probe, radius)
        if pose is None:
            raise GoalError('nav.err.straight_no_pose')
        px, py, pyaw = (_number(v) for v in pose)
        distance = math.hypot(px - waypoints[0][0], py - waypoints[0][1])
        heading = abs(normalize_yaw(leg['direction'] - pyaw))
        start_ok = distance <= LINE_START_TOL_M and heading <= LINE_HEADING_TOL_RAD
        return dict(leg, ok=bool(leg['ok'] and start_ok),
                    start={'distance': round(distance, 3), 'heading_deg': round(math.degrees(heading), 1),
                           'ok': start_ok, 'max_distance': LINE_START_TOL_M,
                           'max_heading_deg': round(math.degrees(LINE_HEADING_TOL_RAD))})

    def send_leg(self, waypoints, end_yaw, pose, path_probe, localized, confirmed, radius=PASS_RADIUS_M):
        """วิ่งตามเส้นทางต่อเนื่องหนึ่งท่อน (ข้าม planner) : กติกาเดียวกับ send_line ทุกข้อ แต่เส้นอาจมีส่วนโค้งมนที่จุดผ่าน ;
        คืน (สถานะ, ผลตรวจ)"""
        if confirmed is not True:
            raise GoalError('nav.err.goal_confirm_required')
        if not localized:
            raise GoalError('nav.err.goal_not_localized')
        check = self.check_leg(waypoints, end_yaw, pose, path_probe, radius)
        if not check['ok']:
            if check['reason'] is not None:
                raise GoalError(STRAIGHT_BLOCKED[check['reason']], at=check['at'], clearance=self._clearance)
            if check['start']['distance'] > LINE_START_TOL_M:
                raise GoalError('nav.err.line_start_far', found=check['start']['distance'], max=LINE_START_TOL_M)
            raise GoalError('nav.err.line_heading', found=check['start']['heading_deg'],
                            max=round(math.degrees(LINE_HEADING_TOL_RAD)))
        x0, y0 = float(waypoints[0][0]), float(waypoints[0][1])
        points = check['points']
        end = {'x': points[-1][0], 'y': points[-1][1], 'yaw': points[-1][2]}
        status = self._launch(end, {'x0': x0, 'y0': y0}, self._client.line_ready,
                              lambda seq: self._client.send_line(points, _Listener(self, seq)),
                              f'Nav path: ({x0:.2f}, {y0:.2f}) -> ({end["x"]:.2f}, {end["y"]:.2f}) '
                              f'via {len(waypoints) - 2} pass-through point(s), yaw={math.degrees(end["yaw"]):.1f}°')
        return status, check

    def send_line(self, start, goal, pose, line_probe, localized, confirmed):
        """วิ่งตรงตามเส้น start → goal โดยไม่ผ่าน planner : confirmed / localized / ปลอดภัยทั้งแนว / หุ่นอยู่ที่จุดเริ่มและ
        หันตรงแนว / ทีละงานเดียว / server พร้อม ; ผิดข้อไหนไม่ส่งอะไร ; คืน (สถานะ, ผลตรวจ)"""
        if confirmed is not True:
            raise GoalError('nav.err.goal_confirm_required')
        if not localized:
            raise GoalError('nav.err.goal_not_localized')
        check = self.check_line(start, goal, pose, line_probe)
        if not check['ok']:
            if check['reason'] is not None:                    # แนวเส้นไม่ปลอดภัย (รายงานก่อนเรื่องตำแหน่งหุ่น)
                raise GoalError(STRAIGHT_BLOCKED[check['reason']], at=check['at'], clearance=self._clearance)
            if check['start']['distance'] > LINE_START_TOL_M:
                raise GoalError('nav.err.line_start_far', found=check['start']['distance'], max=LINE_START_TOL_M)
            raise GoalError('nav.err.line_heading', found=check['start']['heading_deg'],
                            max=round(math.degrees(LINE_HEADING_TOL_RAD)))
        x0, y0 = float(start[0]), float(start[1])
        end = check['goal']
        points = line_points(x0, y0, end['x'], end['y'], end['yaw'])
        status = self._launch(end, {'x0': x0, 'y0': y0}, self._client.line_ready,
                              lambda seq: self._client.send_line(points, _Listener(self, seq)),
                              f'Nav line: ({x0:.2f}, {y0:.2f}) -> ({end["x"]:.2f}, {end["y"]:.2f}) '
                              f'yaw={math.degrees(end["yaw"]):.1f}°')
        return status, check

    def cancel(self):
        """ขอยกเลิก goal ที่ค้างอยู่ ; คืน True ถ้ามีอะไรให้ยกเลิก (เรียกซ้ำ/ตอนไม่มี goal ได้ ไม่เป็นไร)"""
        with self._lock:
            if self._state not in BUSY_STATES:
                return False
            self._state = 'canceling'
        self._client.cancel()
        self._on_event('Nav goal: cancel requested')
        return True

    # ── ข้อมูลกลับจาก Nav2 ──────────────────────────────────────────────
    def _accepted(self, seq, ok):
        with self._lock:
            if seq != self._seq or self._state not in ('sending', 'canceling'):
                return
            if ok:
                if self._state == 'sending':
                    self._state = 'active'
                    self._progress_at = self._clock()
            else:
                self._state, self._reason = 'failed', 'nav.err.goal_rejected'
        self._accepted_event.set()

    def _feedback(self, seq, feedback):
        with self._lock:
            if seq != self._seq or self._state not in ('active', 'canceling'):
                return
            distance = feedback.get('distance_remaining')
            now = self._clock()
            if isinstance(distance, (int, float)) and math.isfinite(distance):
                if self._best is None or distance < self._best - STALL_PROGRESS_M:
                    self._best, self._progress_at = distance, now
            self._feedback_data = {k: feedback.get(k) for k in ('distance_remaining', 'eta', 'recoveries', 'elapsed')}

    def _result(self, seq, code):
        if code not in ('succeeded', 'canceled', 'failed'):
            code = 'failed'
        self._finish(seq, code, None if code != 'failed' else 'nav.goal.aborted')
        self._on_event(f'Nav goal: {code}')

    def _finish(self, seq, state, reason):
        with self._lock:
            if seq != self._seq or self._state not in BUSY_STATES:
                return
            self._state, self._reason = state, reason
        self._accepted_event.set()

    # ── สถานะ ───────────────────────────────────────────────────────────
    def server_ready(self):
        """action server ของ Nav2 ยังอยู่ใน graph ไหม (หายไป = goal ที่ค้างอยู่ไม่มีวันจบ)"""
        return bool(self._client.ready())

    def status(self, nav_up=True):
        """สถานะปัจจุบัน (dict สำหรับ JSON) ; nav_up=False = Nav2 ไม่ได้รันแล้ว → goal ที่ค้างถือว่าหาย"""
        with self._lock:
            if not nav_up and self._state in BUSY_STATES:
                self._state, self._reason = 'failed', 'nav.goal.lost'
            now = self._clock()
            busy = self._state in BUSY_STATES
            waiting = (self._state == 'active' and self._progress_at is not None
                       and now - self._progress_at >= STALL_S)
            return {'state': self._state, 'seq': self._seq, 'goal': dict(self._goal) if self._goal else None,
                    'line': dict(self._line) if self._line else None,
                    'reason': self._reason, 'feedback': dict(self._feedback_data) if self._feedback_data else None,
                    'elapsed': None if self._started is None or not busy else round(now - self._started, 1),
                    'waiting_assumed': waiting}
