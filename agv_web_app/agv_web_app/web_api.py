"""
web_api — handler ของ tornado (ไม่พึ่ง ROS → ทดสอบด้วย tornado.testing ได้)

ctx ที่ handler ใช้ (web_server_node ทำหน้าที่นี้): ต้องมี
  ctx.snapshot()  → dict สถานะทั้งหมด     ctx.auth  → AuthManager     ctx.modes → ModeManager
  ctx.history     → SpeedHistory (ประวัติความเร็วให้กราฟ)
  ctx.stats       → StatsStore (สถิติระยะทาง/เวลาทำงาน)
  ctx.live_map    → LiveMap (แผนที่สดจาก /map เป็น PNG)
  ctx.drive       → DriveController (ขับด้วยมือ)  ctx.overlay → LaserOverlay  ctx.saver → MapSaver
  ctx.map_saved   → {'hash','name','time'} แผนที่ล่าสุดที่บันทึกแล้ว
  ctx.selection   → MapSelection (แผนที่ที่เลือกไว้ใช้กับ Navigation) ; ctx.nav_map_in_use = แผนที่ที่ Nav เปิดอยู่
  ctx.navp        → NavParams (ค่า Nav2 ที่ปรับได้)
                    ctx.nav_params_in_use = ค่าที่ Nav2 ใช้อยู่ตอนนี้ (จำตอนเปิดโหมด + อัปเดตหลังปรับสด)
  ctx.navlive     → NavLive (ปรับค่า Nav2 สดผ่านพารามิเตอร์ของ ROS)
  ctx.goal        → NavGoal (ส่งหุ่นไปจุดหมาย) ; ctx.stop_motion() = ส่งความเร็วศูนย์ซ้ำ
  ctx.routes      → RouteStore (Route ต่อแผนที่ : ลำดับจุดหมาย)
  ctx.saved_maps  → SavedMaps (ไฟล์แผนที่ที่บันทึกไว้ ใช้ตรวจจุดของ Route)
  ctx.runner      → RouteRunner (รัน Route ทีละจุด) ; ctx.runner.active() = Route กำลังวิ่ง/พักอยู่
  ctx.startposes  → StartPoses (จุดเริ่มต้นต่อแผนที่) ; ctx.autostart → AutoStart (วางจุดนั้นให้เองตอน Nav เริ่ม)
  ctx.initial_pose → InitialPose (วางตำแหน่งเริ่มต้นให้ AMCL) ; ctx.live_map.meta() = กรอบแผนที่ที่โหลดอยู่
  ctx.slam        → SlamParams (ค่า SLAM ที่ปรับได้) ; ctx.slam_in_use = ค่าที่ใช้ตอนเปิด Mapping ล่าสุด

ความปลอดภัยของคำสั่ง (POST):
  - ต้องมีเฮดเดอร์ X-Requested-With: agv-web (เบราว์เซอร์ข้ามเว็บส่งแบบ form ธรรมดาไม่ได้)
  - ถ้ามี Origin ต้องตรงกับ Host (กันเว็บอื่นสั่งผ่านเบราว์เซอร์ของผู้ดูแล)
  - คุกกี้ session เป็น HttpOnly + SameSite=Strict
  - การเปลี่ยนโหมดต้องมี session ผู้ดูแล ; ดูสถานะ (GET) เปิดให้ทุกคน
"""
import asyncio
import json
import math
import time
from urllib.parse import urlparse

import tornado.web
import tornado.websocket

from agv_web_app.drive_control import drive_allowed
from agv_web_app.history import DEFAULT_WINDOW_S
from agv_web_app.map_saver import MapSaverError
from agv_web_app.mode_manager import MODES, NAVIGATION
from agv_web_app.msg import msg
from agv_web_app.nav_goal import LOCALIZED, GoalError
from agv_web_app.nav_live import NavLiveError
from agv_web_app.nav_params import NavParamError
from agv_web_app.nav_pose import PoseError
from agv_web_app.slam_params import SlamParamError
from agv_web_app.route_run import RunError
from agv_web_app.routes import MAX_LOOP, MAX_NAME, MAX_POINTS, MAX_WAIT_S, RouteError
from agv_web_app.start_pose import StartPoseError, with_deg
from agv_web_app.web_assets import AssemblyError, assemble

SESSION_COOKIE = 'agv_session'
CSRF_HEADER = 'X-Requested-With'
CSRF_VALUE = 'agv-web'


class ApiError(tornado.web.HTTPError):
    """ข้อผิดพลาดของ API: ส่งกลับเป็น {"ok": false, "error": msg(code, ...)} ให้หน้าเว็บแปลภาษา"""

    def __init__(self, status, code, **params):
        super().__init__(status, code)          # log_message = code (ไว้ดูใน log ของ server)
        self.msg = msg(code, **params)


HTML_TYPE = 'text/html; charset=UTF-8'
JS_TYPE = 'application/javascript; charset=UTF-8'


class NoCacheStaticHandler(tornado.web.StaticFileHandler):
    """กันเบราว์เซอร์จำ JS/CSS เก่าหลัง build ใหม่"""

    def set_extra_headers(self, path):
        self.set_header('Cache-Control', 'no-cache')


class AssembledHandler(tornado.web.RequestHandler):
    """index.html / app.js ที่ประกอบจากชิ้นส่วนต้นทาง (ดู web_assets.py) — เบราว์เซอร์ได้ไฟล์เดียวเหมือนเดิม
    ชิ้นส่วนขาด/ผิดกติกา = 500 พร้อมเหตุผล (ไม่ส่งหน้าที่ขาดครึ่งๆ กลางๆ)"""

    def initialize(self, web_dir, name, content_type):
        self.web_dir, self.name, self.content_type = web_dir, name, content_type

    def set_default_headers(self):
        self.set_header('Cache-Control', 'no-cache')

    def get(self):
        try:
            body = assemble(self.web_dir, self.name)
        except AssemblyError as e:
            self.set_status(500)
            self.set_header('Content-Type', 'text/plain; charset=UTF-8')
            self.finish(f'web assets error: {e}')
            return
        self.set_header('Content-Type', self.content_type)
        self.finish(body)


class ApiHandler(tornado.web.RequestHandler):

    def initialize(self, ctx):
        self.ctx = ctx

    def set_default_headers(self):
        self.set_header('Cache-Control', 'no-store')

    def write_error(self, status_code, **kwargs):
        exc = kwargs.get('exc_info', (None, None, None))[1]
        error = exc.msg if isinstance(exc, ApiError) else msg('api.http_error', status=status_code)
        self.finish({'ok': False, 'error': error})

    def reply(self, status, **payload):
        self.set_status(status)
        self.finish(payload)

    def require_same_site_request(self):
        if self.request.headers.get(CSRF_HEADER) != CSRF_VALUE:
            raise ApiError(403, 'api.csrf_header')
        origin = self.request.headers.get('Origin')
        if origin and urlparse(origin).netloc.lower() != self.request.host.lower():
            raise ApiError(403, 'api.csrf_origin')

    def json_body(self):
        try:
            data = json.loads(self.request.body or b'{}')
        except ValueError:
            raise ApiError(400, 'api.bad_json')
        if not isinstance(data, dict):
            raise ApiError(400, 'api.bad_json')
        return data

    def is_admin(self, touch=True):
        return self.ctx.auth.validate(self.get_cookie(SESSION_COOKIE), touch=touch)


class StatusHandler(ApiHandler):
    def get(self):
        self.finish(self.ctx.snapshot())


class HistoryHandler(ApiHandler):
    def get(self):
        try:
            window = float(self.get_argument('window', str(DEFAULT_WINDOW_S)))
        except ValueError:
            raise ApiError(400, 'api.bad_window')
        self.finish(self.ctx.history.series(window))


class MapMetaHandler(ApiHandler):
    def get(self):
        self.finish(self.ctx.live_map.meta())


class MapImageHandler(ApiHandler):
    def get(self):
        png = self.ctx.live_map.png()
        if png is None:
            raise ApiError(404, 'api.no_map')
        self.set_header('Content-Type', 'image/png')
        self.finish(png)


class ScanHandler(ApiHandler):
    def get(self):
        self.finish(self.ctx.overlay.latest())


class DriveInfoHandler(ApiHandler):
    def get(self):
        mode = self.ctx.modes.snapshot()
        self.finish({'limits': self.ctx.drive.limits(), 'allowed': drive_allowed(mode),
                     'in_use': DriveSocket.owner is not None})


class DriveStopHandler(ApiHandler):
    """ปุ่ม ■ บนแถบบน: หยุดเคลื่อนที่ทันที (ศูนย์ ไม่ ramp) ไม่ยกเลิกโหมด
    ใช้ได้ตอนขับด้วยมือได้ (Mapping/Manual) และตอน Navigation เปิดอยู่ (ยกเลิก goal + ส่งศูนย์)"""

    async def post(self):
        self.require_same_site_request()
        if not self.ctx.auth.enabled:
            raise ApiError(403, 'auth.disabled')
        if not self.is_admin():
            raise ApiError(401, 'auth.required')
        mode = self.ctx.modes.snapshot()
        if nav_active(mode):
            await halt_navigation(self.ctx)
        elif drive_allowed(mode):
            self.ctx.drive.stop('stop_button')
        else:
            raise ApiError(409, 'drive.err.not_allowed')
        self.finish({'ok': True})


async def halt_navigation(ctx):
    """ยกเลิก goal ที่ Nav2 + ส่งความเร็วศูนย์ซ้ำ (สองชั้น) — ไม่ว่ามี goal ค้างหรือไม่ก็ทำ
    ถ้ามี Route กำลังวิ่ง/พักอยู่ ยกเลิกทั้ง Route ด้วย (ไม่ใช่แค่ goal ปัจจุบัน — ไม่งั้นตัวรันจะส่งจุดถัดไปต่อ)"""
    loop = asyncio.get_running_loop()
    if await loop.run_in_executor(None, ctx.runner.stop):                   # ตัวรันยกเลิก goal + ส่งศูนย์เองแล้ว
        return
    ctx.goal.cancel()
    await loop.run_in_executor(None, ctx.stop_motion)


class MapSavedHandler(ApiHandler):
    def get(self):
        maps = [{'name': m['name'], 'saved': m['saved'], 'bytes': m['bytes'],
                 'size_m': (m['meta'] or {}).get('size_m')} for m in self.ctx.saver.list()[:50]]
        self.finish({'maps': maps, 'last': dict(self.ctx.map_saved)})


class MapSaveHandler(ApiHandler):
    """บันทึกแผนที่ที่กำลังสร้าง (Admin) : {name, overwrite?} — ใช้เวลาได้ถึง ~25 วินาที จึงทำใน thread แยก"""

    async def post(self):
        self.require_same_site_request()
        if not self.ctx.auth.enabled:
            raise ApiError(403, 'auth.disabled')
        if not self.is_admin():
            raise ApiError(401, 'auth.required')
        data = self.json_body()
        name, overwrite = data.get('name'), data.get('overwrite') is True
        if not self.ctx.saver.valid_name(name):
            raise ApiError(400, 'map.err.name')
        mode = self.ctx.modes.snapshot()
        if not (mode['mode'] == 'MAPPING' and mode['state'] == 'RUNNING'):
            raise ApiError(409, 'map.err.not_mapping')
        meta = self.ctx.live_map.meta()
        if not meta.get('available'):
            raise ApiError(409, 'map.err.no_map')
        info = {k: meta[k] for k in ('resolution', 'origin', 'size_m', 'width', 'height', 'known_cells')}
        info['slam'] = getattr(self.ctx, 'slam_in_use', None)
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(
                None, lambda: self.ctx.saver.save(name, overwrite=overwrite, meta=info))
        except MapSaverError as e:
            status = {'map.err.exists': 409, 'map.err.busy': 409, 'map.err.name': 400}.get(e.code, 500)
            raise ApiError(status, e.code, **e.params)
        self.ctx.map_saved.update({'hash': meta['hash'], 'name': name, 'time': result['saved']})
        self.finish({'ok': True, 'name': name, 'bytes': result['bytes'], 'saved': result['saved'],
                     'path': result['yaml']})


MAP_ERROR_STATUS = {'map.err.exists': 409, 'map.err.busy': 409, 'map.err.name': 400, 'map.err.same_name': 400,
                    'map.err.not_found': 404}


def nav_active(mode):
    """Navigation กำลังรัน หรือกำลังเปิด/ปิดอยู่ (map_server อาจอ่านไฟล์แผนที่อยู่)"""
    return NAVIGATION in (mode['mode'], mode.get('target'))


def nav_map_in_use(ctx, mode):
    """แผนที่ที่ Navigation เปิดไว้ตอนนี้ (None ถ้า Nav ไม่ได้รัน)"""
    return getattr(ctx, 'nav_map_in_use', None) if nav_active(mode) else None


def nav_params_in_use(ctx, mode):
    """ค่า Nav2 ที่ Navigation เปิดไว้ตอนนี้ (None ถ้า Nav ไม่ได้รัน)"""
    return getattr(ctx, 'nav_params_in_use', None) if nav_active(mode) else None


class NavParamsHandler(ApiHandler):
    """ค่า Nav2 ที่ปรับได้ : GET = ทุกคนดูได้ ; POST {values} = Admin (บันทึกไว้ใช้ตอนเปิด Navigation ครั้งถัดไป)"""

    def get(self):
        self.finish(self.ctx.navp.info(in_use=nav_params_in_use(self.ctx, self.ctx.modes.snapshot())))

    def post(self):
        self.require_same_site_request()
        if not self.ctx.auth.enabled:
            raise ApiError(403, 'auth.disabled')
        if not self.is_admin():
            raise ApiError(401, 'auth.required')
        try:
            self.ctx.navp.set_values(self.json_body().get('values'))
        except NavParamError as e:
            raise ApiError(400, e.code, **e.params)
        except OSError:
            raise ApiError(500, 'nav.err.save')
        self.finish({'ok': True, 'params': self.ctx.navp.info(
            in_use=nav_params_in_use(self.ctx, self.ctx.modes.snapshot()))})


LIVE_ERROR_STATUS = {'nav.err.not_running': 409, 'nav.err.confirm_required': 409, 'nav.err.busy': 409,
                     'nav.err.rejected': 409, 'nav.err.unreachable': 503}
# ที่เหลือ (verify / rollback_failed) = 500


def nav_running(mode):
    """Navigation ทำงานนิ่งอยู่ (เปิดเสร็จแล้ว ไม่ได้กำลังเปลี่ยนโหมด) — ปรับค่าสดได้เฉพาะตอนนี้"""
    return mode['mode'] == NAVIGATION and mode['state'] == 'RUNNING' and not mode.get('target')


class NavParamsLiveHandler(ApiHandler):
    """ค่า Nav2 ที่ใช้อยู่จริงตอนนี้ (อ่านจาก node ที่กำลังทำงาน) ; ทุกคนดูได้ ; Nav ไม่รัน = running false"""

    async def get(self):
        if not nav_running(self.ctx.modes.snapshot()):
            self.finish({'running': False, 'values': None, 'mixed': []})
            return
        try:
            values, mixed = await asyncio.get_running_loop().run_in_executor(None, self.ctx.navlive.read)
        except NavLiveError as e:
            raise ApiError(LIVE_ERROR_STATUS.get(e.code, 500), e.code, **e.params)
        self.finish({'running': True, 'values': values, 'mixed': mixed})


class NavParamsApplyHandler(ApiHandler):
    """ปรับค่า Nav2 สด (Admin) : {values, confirmed?} — เฉพาะตอน Navigation ทำงานนิ่งอยู่
    ค่าที่เพิ่มความเสี่ยงต้องส่ง confirmed:true ; ไม่บันทึกไว้ใช้ครั้งหน้า (ใช้ POST /api/nav/params)
    ผิดพลาดตรงไหน Nav2 กลับเป็นค่าเดิมทุกตัว (all-or-nothing)"""

    async def post(self):
        self.require_same_site_request()
        if not self.ctx.auth.enabled:
            raise ApiError(403, 'auth.disabled')
        if not self.is_admin():
            raise ApiError(401, 'auth.required')
        data = self.json_body()
        if not nav_running(self.ctx.modes.snapshot()):
            raise ApiError(409, 'nav.err.not_running')
        try:
            result = await asyncio.get_running_loop().run_in_executor(
                None, lambda: self.ctx.navlive.apply(data.get('values'), data.get('confirmed') is True))
        except NavParamError as e:
            raise ApiError(400, e.code, **e.params)
        except NavLiveError as e:
            raise ApiError(LIVE_ERROR_STATUS.get(e.code, 500), e.code, **e.params)
        self.ctx.nav_params_in_use = dict(result['live'])           # "ค่าที่ใช้อยู่" ตามจริงหลังปรับ
        self.finish({'ok': True, 'applied': result['applied'], 'live': result['live'], 'mixed': result['mixed'],
                     'params': self.ctx.navp.info(in_use=self.ctx.nav_params_in_use)})


POSE_ERROR_STATUS = {'nav.err.refresh_failed': 503, 'nav.err.startpose_nomap': 409, 'nav.err.startpose_none': 404,
                     'nav.err.startpose_io': 500, 'nav.err.pose_invalid': 400, 'nav.err.pose_outside': 400,
                     'nav.err.no_map': 409,
                     'nav.err.no_listener': 409, 'nav.err.busy': 409}


class NavInitialPoseHandler(ApiHandler):
    """วางตำแหน่งเริ่มต้นของหุ่นบนแผนที่ (Admin) : {x, y, yaw(เรเดียน)} — เฉพาะตอน Navigation ทำงานนิ่ง
    ไม่ทำให้หุ่นเคลื่อนที่ (แค่บอก AMCL ว่าหุ่นอยู่ตรงไหน) ; ตำแหน่งต้องอยู่ในกรอบของแผนที่ที่โหลดอยู่"""

    async def post(self):
        self.require_same_site_request()
        if not self.ctx.auth.enabled:
            raise ApiError(403, 'auth.disabled')
        if not self.is_admin():
            raise ApiError(401, 'auth.required')
        data = self.json_body()
        if not nav_running(self.ctx.modes.snapshot()):
            raise ApiError(409, 'nav.err.not_running')
        if self.ctx.runner.active():
            raise ApiError(409, 'route.run.active')
        try:
            pose = await asyncio.get_running_loop().run_in_executor(
                None, lambda: self.ctx.initial_pose.set(data.get('x'), data.get('y'), data.get('yaw'),
                                                        self.ctx.live_map.meta()))
        except PoseError as e:
            raise ApiError(POSE_ERROR_STATUS.get(e.code, 500), e.code, **e.params)
        self.finish({'ok': True, 'pose': pose})


def startpose_map(ctx, mode):
    """แผนที่ที่จุดเริ่มต้นผูกอยู่: ที่ Nav เปิดอยู่ ไม่งั้นที่เลือกไว้ (None = แผนที่ไม่มีชื่อ เช่น default)"""
    if nav_active(mode):
        return (nav_map_in_use(ctx, mode) or {}).get('name')
    return ctx.selection.choice()['name']


class NavStartPoseHandler(ApiHandler):
    """จุดเริ่มต้นประจำแผนที่ : GET = ดู (ทุกคน) ; POST {action: save|apply|clear} = Admin
    save = บันทึกตำแหน่งที่หุ่นรู้ตอนนี้ (ต้อง localize แล้ว) · apply = ส่งจุดนั้นให้ AMCL ตอนนี้ · clear = ลืมจุดนี้
    ตอน Navigation เริ่ม เว็บส่งจุดนี้ให้ AMCL เอง (AutoStart)"""

    def _info(self, mode):
        name = startpose_map(self.ctx, mode)
        return {'map': name, 'pose': with_deg(self.ctx.startposes.get(name)) if name else None,
                'auto': self.ctx.autostart.status()}

    def get(self):
        self.finish(self._info(self.ctx.modes.snapshot()))

    async def post(self):
        self.require_same_site_request()
        if not self.ctx.auth.enabled:
            raise ApiError(403, 'auth.disabled')
        if not self.is_admin():
            raise ApiError(401, 'auth.required')
        action = self.json_body().get('action')
        mode = self.ctx.modes.snapshot()
        name = startpose_map(self.ctx, mode)
        if action not in ('save', 'apply', 'clear'):
            raise ApiError(400, 'nav.err.pose_invalid')
        if name is None:
            raise ApiError(409, 'nav.err.startpose_nomap')
        try:
            if action == 'clear':
                self.ctx.startposes.clear(name)
            elif not nav_running(mode):
                raise ApiError(409, 'nav.err.not_running')
            elif action == 'save':
                status = self.ctx.snapshot()
                pose = status.get('pose')
                if (status.get('localization') or {}).get('quality') not in LOCALIZED or pose is None:
                    raise ApiError(409, 'nav.err.goal_not_localized')
                self.ctx.startposes.set(name, pose['x'], pose['y'], math.radians(pose['yaw_deg']),
                                        self.ctx.live_map.meta())
            else:
                if self.ctx.runner.active():                       # วางตำแหน่งให้ AMCL ตอน Route วิ่งอยู่ = อันตราย
                    raise ApiError(409, 'route.run.active')
                saved = self.ctx.startposes.get(name)
                if saved is None:
                    raise ApiError(404, 'nav.err.startpose_none')
                await asyncio.get_running_loop().run_in_executor(
                    None, lambda: self.ctx.initial_pose.set(saved['x'], saved['y'], saved['yaw'],
                                                            self.ctx.live_map.meta()))
                self.ctx.autostart.note_manual(name)
        except StartPoseError as e:
            raise ApiError(POSE_ERROR_STATUS.get(e.code, 500), e.code, **e.params)
        except PoseError as e:
            raise ApiError(POSE_ERROR_STATUS.get(e.code, 500), e.code, **e.params)
        self.finish(dict(self._info(mode), ok=True))


ROUTE_ERROR_STATUS = {'route.err.name': 400, 'route.err.loop': 400, 'route.err.empty': 400, 'route.err.too_many': 400,
                      'route.err.point_invalid': 400, 'route.err.point_name': 400, 'route.err.wait': 400,
                      'route.err.point_outside': 400, 'route.err.point_unknown': 400, 'route.err.point_tight': 400,
                      'route.err.no_map': 409, 'route.err.nomap': 409, 'route.err.exists': 409,
                      'route.err.missing': 404, 'route.err.too_many_routes': 409, 'route.err.io': 500,
                      'route.err.mode': 400, 'route.err.straight_points': 400, 'route.err.segment_short': 400,
                      'route.err.pass_navigate': 400}


def route_map(ctx, mode):
    """(ชื่อแผนที่ที่ Route ผูกอยู่, (LiveMap, generation) ของไฟล์ที่บันทึกไว้ หรือ None ถ้าไม่มี/อ่านไม่ได้)
    ตรวจจุดกับ "ไฟล์แผนที่ที่บันทึกไว้" เสมอ ไม่ว่า Navigation จะรันอยู่หรือไม่ (Nav2 โหลดไฟล์เดียวกัน → ผลเหมือนกัน)"""
    name = startpose_map(ctx, mode)
    return name, (ctx.saved_maps.get(name) if name else None)


def routes_info(ctx, mode):
    name, loaded = route_map(ctx, mode)
    return {'map': name, 'routes': ctx.routes.routes(name) if name else {}, 'running': nav_running(mode),
            'map_ready': loaded is not None,
            'limits': {'max_points': MAX_POINTS, 'max_name': MAX_NAME, 'max_wait': MAX_WAIT_S,
                       'max_loop': MAX_LOOP, 'clearance': ctx.goal.limits()['clearance']}}


class RoutesHandler(ApiHandler):
    """Route (ลำดับจุดหมาย) ต่อแผนที่ : GET = ดู (ทุกคน) ; POST {name, previous?, loop, points[]} = บันทึก/แก้ (Admin)
    ใช้ได้โดยไม่ต้องรัน Navigation : ตรวจทุกจุดกับไฟล์แผนที่ที่บันทึกไว้ (ว่างแน่ชัด + ห่างผนัง ≥ clearance)
    ผิดข้อไหนไม่บันทึกอะไรเลย · ไม่สั่งหุ่นเคลื่อนที่ (แก้ไขอย่างเดียว)"""

    def get(self):
        self.finish(routes_info(self.ctx, self.ctx.modes.snapshot()))

    async def post(self):
        self.require_same_site_request()
        if not self.ctx.auth.enabled:
            raise ApiError(403, 'auth.disabled')
        if not self.is_admin():
            raise ApiError(401, 'auth.required')
        data = self.json_body()
        mode = self.ctx.modes.snapshot()
        name, loaded = route_map(self.ctx, mode)
        if name is None:
            raise ApiError(409, 'route.err.nomap')
        if loaded is None:
            raise ApiError(409, 'route.err.no_map')
        try:
            self.ctx.routes.save(name, data.get('name'), data.get('loop'), data.get('points'),
                                 loaded[0].probe, self.ctx.goal.limits()['clearance'],
                                 previous=data.get('previous'), mode=data.get('mode', 'navigate'))
        except RouteError as e:
            raise ApiError(ROUTE_ERROR_STATUS.get(e.code, 500), e.code, **e.params)
        self.finish(dict(routes_info(self.ctx, mode), ok=True))


class RouteMapHandler(ApiHandler):
    """แผนที่ที่บันทึกไว้ของ Route (meta รูปแบบเดียวกับ /api/map/live) — หน้า Waypoint วาดจากนี้เมื่อ Navigation ไม่รัน
    version เพิ่มทุกครั้งที่โหลดไฟล์ใหม่ และไม่ซ้ำข้ามแผนที่"""

    def get(self):
        name, loaded = route_map(self.ctx, self.ctx.modes.snapshot())
        if loaded is None:
            self.finish({'available': False, 'version': 0, 'map': name})
            return
        meta = loaded[0].meta()
        meta.update(version=loaded[1], map=name)
        self.finish(meta)


class RouteMapImageHandler(ApiHandler):
    def get(self):
        _, loaded = route_map(self.ctx, self.ctx.modes.snapshot())
        png = loaded[0].png() if loaded else None
        if png is None:
            raise ApiError(404, 'api.no_map')
        self.set_header('Content-Type', 'image/png')
        self.finish(png)


class RouteDeleteHandler(ApiHandler):
    """ลบ Route (Admin) : {name}"""

    async def post(self):
        self.require_same_site_request()
        if not self.ctx.auth.enabled:
            raise ApiError(403, 'auth.disabled')
        if not self.is_admin():
            raise ApiError(401, 'auth.required')
        mode = self.ctx.modes.snapshot()
        name = startpose_map(self.ctx, mode)
        if name is None:
            raise ApiError(409, 'route.err.nomap')
        try:
            self.ctx.routes.delete(name, self.json_body().get('name'))
        except RouteError as e:
            raise ApiError(ROUTE_ERROR_STATUS.get(e.code, 500), e.code, **e.params)
        self.finish(dict(routes_info(self.ctx, mode), ok=True))


RUN_ERROR_STATUS = {'route.run.confirm_required': 409, 'route.run.busy': 409, 'route.run.goal_busy': 409,
                    'route.run.no_nav': 409, 'route.run.not_localized': 409, 'route.run.forever_confirm_required': 409,
                    'route.run.not_running': 409, 'route.run.not_paused': 409, 'route.run.pausing': 409,
                    'route.run.straight_unavailable': 503, 'route.run.straight_length': 409,
                    'route.run.straight_blocked': 409, 'route.run.straight_yaw': 409, 'route.run.straight_corner': 409}


def run_info(ctx, mode):
    """สถานะการรัน Route + สถานะ goal ปัจจุบัน (ระยะเหลือ/ข้อสันนิษฐานว่ารอสิ่งกีดขวาง ไว้แสดงบนแถบสถานะ)"""
    nav_up = nav_active(mode) and ctx.goal.server_ready()
    return {'run': ctx.runner.status(), 'goal': ctx.goal.status(nav_up=nav_up), 'running': nav_running(mode)}


class RouteRunHandler(ApiHandler):
    """รัน Route (Admin) : GET = สถานะ (ทุกคนดูได้) ; POST {name, confirmed, forever_confirmed?} = เริ่ม
    เซิร์ฟเวอร์บังคับ: Navigation รันนิ่ง · confirmed:true เป๊ะ · localize แล้ว · ตรวจทุกจุดกับแผนที่ก่อนปล่อยหุ่น ·
    ทีละ Route ทีละ goal · ทุกจุดผ่านด่านเดียวกับการส่ง goal เดี่ยว (ดู route_run.py)"""

    def get(self):
        self.finish(run_info(self.ctx, self.ctx.modes.snapshot()))

    async def post(self):
        self.require_same_site_request()
        if not self.ctx.auth.enabled:
            raise ApiError(403, 'auth.disabled')
        if not self.is_admin():
            raise ApiError(401, 'auth.required')
        data = self.json_body()
        mode = self.ctx.modes.snapshot()
        if not nav_running(mode):
            raise ApiError(409, 'nav.err.not_running')
        name, loaded = route_map(self.ctx, mode)
        if name is None:
            raise ApiError(409, 'route.err.nomap')
        if loaded is None:
            raise ApiError(409, 'route.err.no_map')
        route = self.ctx.routes.get(name, data.get('name'))
        if route is None:
            raise ApiError(404, 'route.err.missing')
        try:
            await asyncio.get_running_loop().run_in_executor(None, lambda: self.ctx.runner.start(
                data.get('name'), name, route, loaded[0].probe, self.ctx.goal.limits()['clearance'],
                data.get('confirmed'), data.get('forever_confirmed')))
        except RunError as e:
            raise ApiError(RUN_ERROR_STATUS.get(e.code, 500), e.code, **e.params)
        except RouteError as e:
            raise ApiError(ROUTE_ERROR_STATUS.get(e.code, 500), e.code, **e.params)
        except GoalError as e:
            raise ApiError(GOAL_ERROR_STATUS.get(e.code, 500), e.code, **e.params)
        self.finish(dict(run_info(self.ctx, mode), ok=True))


class RouteRunControlHandler(ApiHandler):
    """พัก / ต่อ / หยุด Route (Admin) ; หยุด = ยกเลิกทั้ง Route (ไม่ถามยืนยัน — ปุ่มนิรภัย) ใช้ได้ทุกเมื่อ"""

    action = None

    async def post(self):
        self.require_same_site_request()
        if not self.ctx.auth.enabled:
            raise ApiError(403, 'auth.disabled')
        if not self.is_admin():
            raise ApiError(401, 'auth.required')
        mode = self.ctx.modes.snapshot()
        loop = asyncio.get_running_loop()
        try:
            if self.action == 'stop':
                await halt_navigation(self.ctx)
            elif self.action == 'pause':
                await loop.run_in_executor(None, self.ctx.runner.pause)
            else:
                await loop.run_in_executor(None, self.ctx.runner.resume)
        except RunError as e:
            raise ApiError(RUN_ERROR_STATUS.get(e.code, 500), e.code, **e.params)
        except GoalError as e:
            raise ApiError(GOAL_ERROR_STATUS.get(e.code, 500), e.code, **e.params)
        self.finish(dict(run_info(self.ctx, mode), ok=True))


class RunPauseHandler(RouteRunControlHandler):
    action = 'pause'


class RunResumeHandler(RouteRunControlHandler):
    action = 'resume'


class RunStopHandler(RouteRunControlHandler):
    action = 'stop'


class NavRefreshHandler(ApiHandler):
    """Refresh localization (Admin) : สั่ง AMCL อัปเดตด้วยสแกนปัจจุบัน (หุ่นไม่เคลื่อนที่)
    เฉพาะตอน Navigation ทำงานนิ่ง"""

    async def post(self):
        self.require_same_site_request()
        if not self.ctx.auth.enabled:
            raise ApiError(403, 'auth.disabled')
        if not self.is_admin():
            raise ApiError(401, 'auth.required')
        if not nav_running(self.ctx.modes.snapshot()):
            raise ApiError(409, 'nav.err.not_running')
        try:
            await asyncio.get_running_loop().run_in_executor(None, self.ctx.initial_pose.refresh)
        except PoseError as e:
            raise ApiError(POSE_ERROR_STATUS.get(e.code, 500), e.code, **e.params)
        self.finish({'ok': True})


GOAL_ERROR_STATUS = {'nav.err.goal_invalid': 400, 'nav.err.goal_outside': 400, 'nav.err.goal_unknown': 400,
                     'nav.err.goal_tight': 400, 'nav.err.no_map': 409, 'nav.err.goal_confirm_required': 409,
                     'nav.err.goal_not_localized': 409, 'nav.err.goal_busy': 409, 'nav.err.goal_no_server': 503,
                     'nav.err.goal_rejected': 409, 'nav.err.goal_timeout': 504, 'nav.err.straight_distance': 400,
                     'nav.err.straight_no_pose': 409, 'nav.err.straight_outside': 409, 'nav.err.straight_unknown': 409,
                     'nav.err.straight_tight': 409, 'nav.err.line_length': 400, 'nav.err.line_start_far': 409,
                     'nav.err.line_heading': 409, 'nav.err.line_corner': 409}


class NavGoalHandler(ApiHandler):
    """ส่งหุ่นไปจุดหมาย : GET = สถานะ (ทุกคนดูได้) ; POST {x, y, yaw, confirmed} = Admin
    เฉพาะตอน Navigation ทำงานนิ่ง · ต้อง confirmed:true ทุกครั้ง · ต้อง localize แล้ว · จุดหมายต้องปลอดภัย · ทีละ goal
    ผิดข้อไหนไม่ส่งอะไรให้ Nav2 ; Nav2 ยังคงมีระบบหยุด/รอสิ่งกีดขวางของตัวเองครบ (เว็บไม่ข้าม)"""

    def get(self):
        mode = self.ctx.modes.snapshot()
        # Nav2 "ยังอยู่" = โหมด Navigation เปิดอยู่ และ action server ยังอยู่ใน graph (ไม่งั้น goal ที่ค้างจะไม่มีวันจบ)
        nav_up = nav_active(mode) and self.ctx.goal.server_ready()
        self.finish({'running': nav_running(mode), 'goal': self.ctx.goal.status(nav_up=nav_up),
                     'limits': self.ctx.goal.limits()})

    async def post(self):
        self.require_same_site_request()
        if not self.ctx.auth.enabled:
            raise ApiError(403, 'auth.disabled')
        if not self.is_admin():
            raise ApiError(401, 'auth.required')
        data = self.json_body()
        if not nav_running(self.ctx.modes.snapshot()):
            raise ApiError(409, 'nav.err.not_running')
        if self.ctx.runner.active():
            raise ApiError(409, 'route.run.active')
        status = self.ctx.snapshot()
        localized = (status.get('localization') or {}).get('quality') in LOCALIZED and status.get('pose') is not None
        try:
            goal = await asyncio.get_running_loop().run_in_executor(
                None, lambda: self.ctx.goal.send(data.get('x'), data.get('y'), data.get('yaw'),
                                                 self.ctx.live_map.probe, localized, data.get('confirmed')))
        except GoalError as e:
            raise ApiError(GOAL_ERROR_STATUS.get(e.code, 500), e.code, **e.params)
        self.finish({'ok': True, 'goal': goal})


def robot_pose(ctx):
    """(x, y, yaw เรเดียน) ของหุ่นจาก TF map→base_link ใน snapshot ; None ถ้ายังไม่รู้"""
    pose = ctx.snapshot().get('pose')
    return None if pose is None else (pose['x'], pose['y'], math.radians(pose['yaw_deg']))


class NavStraightHandler(ApiHandler):
    """Drive straight (Admin) : เดินตรงไปข้างหน้า distance เมตรตามทิศหุ่น ผ่านกติกา /api/nav/goal ทุกข้อ + ตรวจทั้งแนว
    POST /api/nav/goal/straight {distance, confirmed} = ส่งจริง ; POST .../straight/check {distance} = ตรวจล่วงหน้า
    (ไม่ส่งอะไร) คืนผลแนวเส้น ok/at/reason/min_clearance/soft_risk + จุดหมาย ไว้ให้หน้าเว็บแสดงตัวอย่างและคำเตือน"""

    dry_run = False

    async def post(self):
        self.require_same_site_request()
        if not self.ctx.auth.enabled:
            raise ApiError(403, 'auth.disabled')
        if not self.is_admin():
            raise ApiError(401, 'auth.required')
        data = self.json_body()
        if not nav_running(self.ctx.modes.snapshot()):
            raise ApiError(409, 'nav.err.not_running')
        if not self.dry_run and self.ctx.runner.active():
            raise ApiError(409, 'route.run.active')
        pose = robot_pose(self.ctx)
        loop = asyncio.get_running_loop()
        try:
            if self.dry_run:
                check = await loop.run_in_executor(None, lambda: self.ctx.goal.check_straight(
                    data.get('distance'), pose, self.ctx.live_map.probe_line))
                self.finish({'ok': True, 'check': check})
                return
            status = self.ctx.snapshot()
            localized = (status.get('localization') or {}).get('quality') in LOCALIZED
            goal, check = await loop.run_in_executor(None, lambda: self.ctx.goal.send_straight(
                data.get('distance'), pose, self.ctx.live_map.probe_line, self.ctx.live_map.probe, localized,
                data.get('confirmed')))
        except GoalError as e:
            raise ApiError(GOAL_ERROR_STATUS.get(e.code, 500), e.code, **e.params)
        self.finish({'ok': True, 'goal': goal, 'check': check})


class NavStraightCheckHandler(NavStraightHandler):
    dry_run = True


class NavLineHandler(ApiHandler):
    """วิ่งตรงตามเส้น (Admin) : จากจุดเริ่มไปจุดหมายเป็นเส้นตรงจริง ข้าม planner (ตัวควบคุมตามเส้นที่เว็บสร้างเส้นเดียว)
    POST /api/nav/line {from: {x, y} | null, to: {x, y, yaw}, confirmed} = ส่งจริง ; from เป็น null = เริ่มจากตำแหน่งหุ่นตอนนี้
    POST /api/nav/line/check {from, to} = ตรวจล่วงหน้า (ไม่ส่งอะไร) คืนผลแนว + หุ่นอยู่ที่จุดเริ่ม/หันตรงแนวไหม
    กติกาเดียวกับ /api/nav/goal ทุกข้อ + ทั้งแนวต้องปลอดภัย + หุ่นต้องอยู่ที่จุดเริ่มและหันตรงแนว (ดู nav_goal.check_line)"""

    dry_run = False

    async def post(self):
        self.require_same_site_request()
        if not self.ctx.auth.enabled:
            raise ApiError(403, 'auth.disabled')
        if not self.is_admin():
            raise ApiError(401, 'auth.required')
        data = self.json_body()
        if not nav_running(self.ctx.modes.snapshot()):
            raise ApiError(409, 'nav.err.not_running')
        if not self.dry_run and self.ctx.runner.active():
            raise ApiError(409, 'route.run.active')
        pose = robot_pose(self.ctx)
        loop = asyncio.get_running_loop()
        try:
            start, goal = self.endpoints(data, pose)
            if self.dry_run:
                check = await loop.run_in_executor(None, lambda: self.ctx.goal.check_line(
                    start, goal, pose, self.ctx.live_map.probe_line))
                self.finish({'ok': True, 'check': check})
                return
            status = self.ctx.snapshot()
            localized = (status.get('localization') or {}).get('quality') in LOCALIZED
            result, check = await loop.run_in_executor(None, lambda: self.ctx.goal.send_line(
                start, goal, pose, self.ctx.live_map.probe_line, localized, data.get('confirmed')))
        except GoalError as e:
            raise ApiError(GOAL_ERROR_STATUS.get(e.code, 500), e.code, **e.params)
        self.finish({'ok': True, 'goal': result, 'check': check})

    @staticmethod
    def endpoints(data, pose):
        """(start, goal) จาก body ; ชนิดผิด = nav.err.goal_invalid ; from เป็น null = ตำแหน่งหุ่น"""
        to, origin = data.get('to'), data.get('from')
        if not isinstance(to, dict) or not (origin is None or isinstance(origin, dict)):
            raise GoalError('nav.err.goal_invalid')
        if origin is None:
            if pose is None:
                raise GoalError('nav.err.straight_no_pose')
            start = (pose[0], pose[1])
        else:
            start = (origin.get('x'), origin.get('y'))
        return start, (to.get('x'), to.get('y'), to.get('yaw'))


class NavLineCheckHandler(NavLineHandler):
    dry_run = True


class NavGoalCancelHandler(ApiHandler):
    """ยกเลิกการเดินทาง (Admin) : ยกเลิก goal ที่ Nav2 + ส่งความเร็วศูนย์ ; ใช้ได้ทุกเมื่อที่ Navigation เปิดอยู่"""

    async def post(self):
        self.require_same_site_request()
        if not self.ctx.auth.enabled:
            raise ApiError(403, 'auth.disabled')
        if not self.is_admin():
            raise ApiError(401, 'auth.required')
        if not nav_active(self.ctx.modes.snapshot()):
            raise ApiError(409, 'nav.err.not_running')
        await halt_navigation(self.ctx)
        self.finish({'ok': True, 'goal': self.ctx.goal.status()})


class MapSelectedHandler(ApiHandler):
    """แผนที่ที่เลือกไว้ใช้กับ Navigation (ทุกคนดูได้)"""

    def get(self):
        self.finish(self.ctx.selection.info(in_use=nav_map_in_use(self.ctx, self.ctx.modes.snapshot())))


class MapSelectHandler(ApiHandler):
    """เลือกแผนที่ไปใช้กับ Navigation (Admin) : {name} ; name เป็น null = ยกเลิกการเลือก (กลับไปใช้ค่า config/default)
    มีผลตอนเปิด Navigation ครั้งถัดไป — ไม่สลับสดระหว่างที่ Nav รันอยู่ (ดู restart_needed ในคำตอบ)"""

    def post(self):
        self.require_same_site_request()
        if not self.ctx.auth.enabled:
            raise ApiError(403, 'auth.disabled')
        if not self.is_admin():
            raise ApiError(401, 'auth.required')
        data = self.json_body()
        if 'name' not in data:                                       # ขาด name ≠ ยกเลิก (ต้องส่ง null ชัดๆ)
            raise ApiError(400, 'map.err.name')
        try:
            self.ctx.selection.select(data['name'])
        except MapSaverError as e:
            raise ApiError(MAP_ERROR_STATUS.get(e.code, 500), e.code, **e.params)
        info = self.ctx.selection.info(in_use=nav_map_in_use(self.ctx, self.ctx.modes.snapshot()))
        info['ok'] = True
        self.finish(info)


class MapThumbHandler(ApiHandler):
    """ภาพย่อของแผนที่ที่บันทึกไว้ (ทุกคนดูได้เหมือนรายการ) : /api/map/saved/<ชื่อ>.png"""

    def get(self, name):
        try:
            png = self.ctx.saver.thumbnail(name)
        except MapSaverError as e:
            raise ApiError(MAP_ERROR_STATUS.get(e.code, 500), e.code, **e.params)
        self.set_header('Content-Type', 'image/png')
        self.finish(png)


class MapLibraryHandler(ApiHandler):
    """ฐานของคำสั่งจัดการคลังแผนที่ (Admin + CSRF) : ตรวจสิทธิ์ → อ่าน body → ห้ามแก้ไฟล์ตอน Navigation รันอยู่
    (map_server ของ Nav2 อ่านแผนที่ไฟล์นั้นอยู่)"""

    def begin(self, *fields):
        self.require_same_site_request()
        if not self.ctx.auth.enabled:
            raise ApiError(403, 'auth.disabled')
        if not self.is_admin():
            raise ApiError(401, 'auth.required')
        data = self.json_body()
        values = [data.get(f) for f in fields]
        if not all(self.ctx.saver.valid_name(v) for v in values):
            raise ApiError(400, 'map.err.name')
        if nav_active(self.ctx.modes.snapshot()):
            raise ApiError(409, 'map.err.nav_running')
        selected = self.ctx.selection.name()
        if selected is not None and values[0] == selected:            # แผนที่ที่ Nav จะใช้ครั้งหน้า ห้ามหายไปเอง
            raise ApiError(409, 'map.err.selected', name=selected)
        return values

    def run(self, action, *args):
        try:
            return action(*args)
        except MapSaverError as e:
            raise ApiError(MAP_ERROR_STATUS.get(e.code, 500), e.code, **e.params)


class MapRenameHandler(MapLibraryHandler):
    """เปลี่ยนชื่อแผนที่ : {name, new_name}"""

    def post(self):
        old, new = self.begin('name', 'new_name')
        self.run(self.ctx.saver.rename, old, new)
        if self.ctx.map_saved.get('name') == old:        # หน้า Mapping จำว่า "บันทึกแล้ว" → ชื่อต้องตามไปด้วย
            self.ctx.map_saved['name'] = new
        self.finish({'ok': True, 'name': new})


class MapDeleteHandler(MapLibraryHandler):
    """ลบแผนที่ (ย้ายไปถังขยะ กู้คืนด้วยมือได้) : {name}"""

    def post(self):
        (name,) = self.begin('name')
        self.run(self.ctx.saver.delete, name)
        self.finish({'ok': True, 'name': name})


class SlamParamsHandler(ApiHandler):
    def get(self):
        mode = self.ctx.modes.snapshot()
        running = mode['mode'] == 'MAPPING' or mode.get('target') == 'MAPPING'
        self.finish(self.ctx.slam.info(in_use=getattr(self.ctx, 'slam_in_use', None) if running else None))

    def post(self):
        self.require_same_site_request()
        if not self.ctx.auth.enabled:
            raise ApiError(403, 'auth.disabled')
        if not self.is_admin():
            raise ApiError(401, 'auth.required')
        values = self.json_body().get('values')
        try:
            self.ctx.slam.set_values(values)
        except SlamParamError as e:
            raise ApiError(400, e.code, **e.params)
        except OSError:
            raise ApiError(500, 'slam.err.save')
        self.finish({'ok': True, 'params': self.ctx.slam.info()})


class StatsHandler(ApiHandler):
    def get(self):
        self.finish(self.ctx.stats.summary())


class StatsResetHandler(ApiHandler):
    def post(self):
        self.require_same_site_request()
        if not self.ctx.auth.enabled:
            raise ApiError(403, 'auth.disabled')
        if not self.is_admin():
            raise ApiError(401, 'auth.required')
        self.ctx.stats.reset(who=self.request.remote_ip)
        self.reply(200, ok=True, stats=self.ctx.stats.summary())


class SessionHandler(ApiHandler):
    def get(self):
        # touch=False: หน้าเว็บถามเป็นระยะ ต้องไม่นับเป็นการใช้งาน (ไม่งั้นเซสชันไม่หมดอายุ)
        self.finish({'ok': True, 'admin': self.is_admin(touch=False),
                     'auth_enabled': self.ctx.auth.enabled})


class LoginHandler(ApiHandler):
    def post(self):
        self.require_same_site_request()
        data = self.json_body()
        result = self.ctx.auth.login(data.get('pin'), self.request.remote_ip)
        if result.ok:
            self.set_cookie(SESSION_COOKIE, result.token, httponly=True,
                            samesite='Strict', path='/')
            self.reply(200, ok=True, admin=True)
        elif result.error == 'disabled':
            self.reply(503, ok=False, error=msg('auth.disabled'))
        elif result.error == 'locked':
            self.set_header('Retry-After', str(result.retry_after))
            self.reply(429, ok=False, retry_after=result.retry_after,
                       error=msg('auth.locked', seconds=result.retry_after))
        else:
            self.reply(401, ok=False, error=msg('auth.bad_pin'))


class LogoutHandler(ApiHandler):
    def post(self):
        self.require_same_site_request()
        self.ctx.auth.logout(self.get_cookie(SESSION_COOKIE))
        self.clear_cookie(SESSION_COOKIE, path='/')
        self.reply(200, ok=True)


class ModeHandler(ApiHandler):
    def get(self):
        self.finish(self.ctx.modes.snapshot())

    def post(self):
        self.require_same_site_request()
        if not self.ctx.auth.enabled:
            raise ApiError(403, 'auth.disabled')
        if not self.is_admin():
            raise ApiError(401, 'auth.required')
        mode = self.json_body().get('mode')
        if mode not in MODES:
            raise ApiError(400, 'api.bad_mode')
        ok, message = self.ctx.modes.request_mode(mode)
        self.reply(202 if ok else 409, ok=ok, message=message,
                   mode=self.ctx.modes.snapshot())


class ModeResetHandler(ApiHandler):
    def post(self):
        self.require_same_site_request()
        if not self.ctx.auth.enabled:
            raise ApiError(403, 'auth.disabled')
        if not self.is_admin():
            raise ApiError(401, 'auth.required')
        ok, message = self.ctx.modes.request_reset()
        self.reply(202 if ok else 409, ok=ok, message=message, mode=self.ctx.modes.snapshot())


class DriveSocket(tornado.websocket.WebSocketHandler):
    """ขับด้วยมือ: เบราว์เซอร์ส่ง {t:'cmd', lin, ang, vmax, wmax} ~10 Hz ตอนนิ้วแตะค้าง (deadman)
    ผู้ขับได้ทีละเครื่องเดียว ; ทุกข้อความตรวจสิทธิ์ Admin + โหมดใหม่เสมอ ; ปิดการเชื่อมต่อ = หยุดทันที"""

    owner = None                       # ซ็อกเก็ตที่กำลังขับอยู่ (มีได้ตัวเดียว)
    STALE_S = 3.0                      # เจ้าของที่เงียบเกินนี้ ถือว่าหมดสิทธิ์ ให้เครื่องใหม่แย่งได้
    MAX_BYTES = 512
    MIN_GAP_S = 0.005                  # รับไม่เกิน ~200 ข้อความ/วินาที (เกินนั้นทิ้ง)

    def initialize(self, ctx):
        self.ctx = ctx
        self._last_msg = 0.0
        self._last_accept = 0.0

    def check_origin(self, origin):
        return urlparse(origin).netloc.lower() == self.request.host.lower()

    def _deny(self, code, close=True):
        if DriveSocket.owner is self:                              # หยุดเฉพาะเมื่อเป็นเครื่องที่กำลังขับอยู่
            self.ctx.drive.stop('denied')                          # (คนนอก/เครื่องอื่นห้ามขัดการขับของเจ้าของ)
        try:
            self.write_message({'t': 'denied', 'error': msg(code)})
        except tornado.websocket.WebSocketClosedError:
            return
        if close:
            self.close(1008)

    def open(self):
        auth, drive = self.ctx.auth, self.ctx.drive
        if not auth.enabled:
            return self._deny('auth.disabled')
        if not auth.validate(self.get_cookie(SESSION_COOKIE)):
            return self._deny('auth.required')
        if not drive_allowed(self.ctx.modes.snapshot()):
            return self._deny('drive.err.not_allowed')
        owner = DriveSocket.owner
        if owner is not None and owner is not self and time.monotonic() - owner._last_msg < self.STALE_S:
            return self._deny_keep_owner('drive.err.in_use')
        if owner is not None and owner is not self:
            owner.close(1001)                                      # เจ้าของเก่าเงียบไปแล้ว → ให้เครื่องใหม่ขับแทน
        DriveSocket.owner = self
        self._last_msg = time.monotonic()
        self.write_message({'t': 'ready', 'limits': drive.limits()})

    def _deny_keep_owner(self, code):
        # ปฏิเสธผู้มาใหม่โดยไม่แตะการขับของเจ้าของเดิม
        self.write_message({'t': 'denied', 'error': msg(code)})
        self.close(1008)

    def on_message(self, raw):
        if self is not DriveSocket.owner:
            return
        now = time.monotonic()
        if len(raw) > self.MAX_BYTES:
            self.ctx.drive.stop('bad_message')
            return self.close(1009)
        try:
            data = json.loads(raw)
            kind = data.get('t')
        except (ValueError, AttributeError):
            self.ctx.drive.stop('bad_message')
            return
        # จำกัดความถี่เฉพาะ "คำสั่งขับ" (ทิ้งตัวที่ถี่เกินได้ เพราะตัวถัดไปมาแทน) — ห้ามทิ้งคำสั่งหยุดเด็ดขาด
        if kind == 'cmd':
            if now - self._last_accept < self.MIN_GAP_S:
                return
            self._last_accept = now
        self._last_msg = now
        if not self.ctx.auth.validate(self.get_cookie(SESSION_COOKIE)):
            return self._deny('auth.required')
        if not drive_allowed(self.ctx.modes.snapshot()):
            return self._deny('drive.err.not_allowed')
        if kind == 'cmd':
            ok = self.ctx.drive.command(data.get('lin'), data.get('ang'), data.get('vmax'), data.get('wmax'))
            if not ok:
                self.write_message({'t': 'invalid'})
                return
        elif kind == 'stop':
            self.ctx.drive.stop('ws_stop')
        elif kind != 'hb':
            return
        self.write_message({'t': 'state', **self.ctx.drive.state()})

    def on_close(self):
        if DriveSocket.owner is self:
            DriveSocket.owner = None
            self.ctx.drive.stop('closed')                          # เชื่อมต่อขาด = หยุดทันที


class StatusSocket(tornado.websocket.WebSocketHandler):
    clients = set()

    def open(self):
        StatusSocket.clients.add(self)

    def on_close(self):
        StatusSocket.clients.discard(self)

    def on_message(self, _message):
        pass    # คำสั่งทั้งหมดไปทาง REST POST ; socket นี้ส่งสถานะอย่างเดียว


def make_app(ctx, web_dir):
    args = {'ctx': ctx}
    return tornado.web.Application([
        (r'/api/status', StatusHandler, args),
        (r'/api/history', HistoryHandler, args),
        (r'/api/map/live', MapMetaHandler, args),
        (r'/api/map/live.png', MapImageHandler, args),
        (r'/api/slam/params', SlamParamsHandler, args),
        (r'/api/scan/live', ScanHandler, args),
        (r'/api/drive/info', DriveInfoHandler, args),
        (r'/api/drive/stop', DriveStopHandler, args),
        (r'/api/map/save', MapSaveHandler, args),
        (r'/api/map/saved', MapSavedHandler, args),
        (r'/api/map/saved/([A-Za-z0-9_-]+)\.png', MapThumbHandler, args),
        (r'/api/nav/params', NavParamsHandler, args),
        (r'/api/nav/params/live', NavParamsLiveHandler, args),
        (r'/api/nav/params/apply', NavParamsApplyHandler, args),
        (r'/api/nav/initialpose', NavInitialPoseHandler, args),
        (r'/api/nav/startpose', NavStartPoseHandler, args),
        (r'/api/nav/refresh', NavRefreshHandler, args),
        (r'/api/routes', RoutesHandler, args),
        (r'/api/routes/delete', RouteDeleteHandler, args),
        (r'/api/routes/map', RouteMapHandler, args),
        (r'/api/routes/run', RouteRunHandler, args),
        (r'/api/routes/run/pause', RunPauseHandler, args),
        (r'/api/routes/run/resume', RunResumeHandler, args),
        (r'/api/routes/run/stop', RunStopHandler, args),
        (r'/api/routes/map.png', RouteMapImageHandler, args),
        (r'/api/nav/goal', NavGoalHandler, args),
        (r'/api/nav/goal/straight', NavStraightHandler, args),
        (r'/api/nav/goal/straight/check', NavStraightCheckHandler, args),
        (r'/api/nav/line', NavLineHandler, args),
        (r'/api/nav/line/check', NavLineCheckHandler, args),
        (r'/api/nav/goal/cancel', NavGoalCancelHandler, args),
        (r'/api/map/selected', MapSelectedHandler, args),
        (r'/api/map/select', MapSelectHandler, args),
        (r'/api/map/rename', MapRenameHandler, args),
        (r'/api/map/delete', MapDeleteHandler, args),
        (r'/api/stats', StatsHandler, args),
        (r'/api/stats/reset', StatsResetHandler, args),
        (r'/api/session', SessionHandler, args),
        (r'/api/login', LoginHandler, args),
        (r'/api/logout', LogoutHandler, args),
        (r'/api/mode', ModeHandler, args),
        (r'/api/mode/reset', ModeResetHandler, args),
        (r'/ws', StatusSocket),
        (r'/ws/drive', DriveSocket, args),
        (r'/', AssembledHandler, {'web_dir': web_dir, 'name': 'index.html', 'content_type': HTML_TYPE}),
        (r'/index\.html', AssembledHandler, {'web_dir': web_dir, 'name': 'index.html', 'content_type': HTML_TYPE}),
        (r'/app\.js', AssembledHandler, {'web_dir': web_dir, 'name': 'app.js', 'content_type': JS_TYPE}),
        (r'/(.*)', NoCacheStaticHandler,
         {'path': web_dir, 'default_filename': 'index.html'}),
    ], websocket_ping_interval=3, websocket_ping_timeout=6)      # ตรวจการเชื่อมต่อที่ตายเงียบ (ขับด้วยมือต้องรู้เร็ว)
