"""
ทดสอบ REST API (login / session / mode) ด้วย tornado.testing + ModeManager กับระบบปลอม
ไม่ต้องมี ROS/หุ่น

รัน:  cd ~/ros2_ws/src/agv_web_app && python3 -m unittest discover -s test -v
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

import numpy as np
from PIL import Image
from tornado.httpclient import HTTPRequest
from tornado.testing import AsyncHTTPTestCase, gen_test
from tornado.websocket import websocket_connect

from test_nav_goal import FakeClient
from test_nav_live import FakeTransport
from test_nav_pose import Clock, FakePublisher

from agv_web_app.auth import AuthManager
from agv_web_app.drive_control import DriveController
from agv_web_app.laser_overlay import LaserOverlay
from agv_web_app.map_saver import MapSaver
from agv_web_app.map_selection import MapSelection
from agv_web_app.nav_goal import NavGoal
from agv_web_app.nav_live import NavLive
from agv_web_app.nav_params import NavParams
from agv_web_app.nav_pose import InitialPose
from agv_web_app.route_run import RouteRunner
from agv_web_app.routes import RouteStore
from agv_web_app.saved_map import SavedMaps
from agv_web_app.start_pose import AutoStart, StartPoses
from agv_web_app.history import SpeedHistory
from agv_web_app.mode_manager import ModeManager
from agv_web_app.live_map import LiveMap
from agv_web_app.slam_params import SlamParams
from agv_web_app.stats import StatsStore
from agv_web_app.web_api import CSRF_HEADER, CSRF_VALUE, SESSION_COOKIE, DriveSocket, make_app

FAKE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fake_system.py')
NODE_OF = {'MAPPING': 'slam_toolbox', 'MANUAL': 'ekf_filter_node', 'NAVIGATION': 'bt_navigator'}
HEADERS = {CSRF_HEADER: CSRF_VALUE, 'Content-Type': 'application/json'}
FAKE_SAVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fake_map_saver.py')
FACTORY = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                       'agv_s2_bringup', 'config', 'mapper_params_s2_factory.yaml')


NAV_DEFAULTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'agv_s2_bringup', 'config',
                            'nav2_params_s2.yaml')


class FakeContext:
    """แทน web_server_node: มี snapshot(), auth, modes"""

    def __init__(self, pin, tmp):
        self.state_file = os.path.join(tmp, 'graph.txt')
        open(self.state_file, 'w').close()
        self.auth = AuthManager(pin, max_fails=3, lockout_seconds=30)
        self.live_map = LiveMap()          # ต้องเป็นของแต่ละเทสต์ (เคยเป็นแอตทริบิวต์ของคลาส → ข้อมูลรั่วข้ามเทสต์)
        self.slam = SlamParams(FACTORY, os.path.join(tmp, 'appdata'))
        self.now = 1000.0                  # นาฬิกาปลอมของตัวควบคุมการขับ (ทดสอบ ramp โดยไม่ต้องรอจริง)
        self.drive = DriveController(clock=lambda: self.now)
        self.overlay = LaserOverlay()
        self.saver = MapSaver(os.path.join(tmp, 'maps'), command=[sys.executable, FAKE_SAVER, 'ok'], timeout=5.0)
        self.map_saved = {'hash': None, 'name': None, 'time': None}
        self.selection = MapSelection(os.path.join(tmp, 'nav', 'selected_map.json'), self.saver)
        self.nav_map_in_use = None
        self.navp = NavParams(NAV_DEFAULTS, os.path.join(tmp, 'appdata'))
        self.nav_params_in_use = None
        self.transport = FakeTransport()                    # แทน node ของ Nav2 (ค่าตั้งต้น = nav2_params_s2.yaml)
        self.navlive = NavLive(self.transport)
        self.pose_pub = FakePublisher()                     # แทน AMCL ที่ฟัง /initialpose
        self.pose_clock = Clock()
        self.refreshes, self.refresh_ok = 0, True           # แทน service /request_nomotion_update ของ AMCL
        self.initial_pose = InitialPose(self.pose_pub, sleep=self.pose_clock.sleep, clock=self.pose_clock,
                                        refresher=self.refresher)
        self.startposes = StartPoses(os.path.join(tmp, 'nav', 'start_poses.json'), self.saver.valid_name)
        self.routes = RouteStore(os.path.join(tmp, 'nav', 'routes.json'), self.saver.valid_name)
        self.saved_maps = SavedMaps(self.saver)
        self.rounds_done = 0
        self.autostart = AutoStart(self.startposes, self.initial_pose, self.live_map.meta,
                                   lambda: (self.nav_map_in_use or {}).get('name'), sleep=self.pose_clock.sleep,
                                   clock=self.pose_clock)
        self.goal_client = FakeClient()                     # แทน action server navigate_to_pose ของ Nav2
        self.goal = NavGoal(self.goal_client, accept_timeout=1.0)
        self.zero_calls = 0
        self.quality = 'good'                               # คุณภาพ localization ที่ snapshot() รายงาน
        self.slam_in_use = None
        commands = {m: [sys.executable, FAKE, self.state_file, n, 'normal', '0.2']
                    for m, n in NODE_OF.items()}
        self.modes = ModeManager(commands, self.list_nodes, os.path.join(tmp, 'logs'),
                                 sigint_timeout=1.0, sigterm_timeout=1.0, settle_timeout=2.0,
                                 poll_interval=0.05, ready_timeout={m: 3.0 for m in NODE_OF})
        # ตัวรัน Route : ไม่เปิด thread (เทสต์เรียก tick() เองเพื่อคุมจังหวะ) ; ใช้ของ FakeContext ทั้งหมด
        self.runner = RouteRunner(self.goal, self.live_map.probe, lambda: self.quality in ('good', 'fair'),
                                  self._nav_up, self.stop_motion, on_round=self._round_done, threaded=False,
                                  loc_grace=0.0,                      # เข้ม (ไม่รอ) ; เทสต์ที่ต้องการรอตั้งเอง
                                  pose=lambda: self.robot_pose, path_probe=self.live_map.probe_path)
        self.robot_pose = (0.0, 0.0, 0.0)                    # (x, y, yaw) ของหุ่นสำหรับ Route โหมด straight (None = ไม่รู้)

    def _nav_up(self):
        mode = self.modes.snapshot()
        return (mode['mode'] == 'NAVIGATION' and mode['state'] == 'RUNNING' and not mode.get('target')
                and self.goal.server_ready())

    def _round_done(self):
        self.rounds_done += 1

    def list_nodes(self):
        with open(self.state_file) as f:
            return f.read().split()

    history = SpeedHistory()
    stats = StatsStore('')
    live_map = LiveMap()

    def snapshot(self):
        return {'mode': self.modes.snapshot(), 'hello': 'status', 'localization': {'quality': self.quality},
                'pose': {'x': 0.0, 'y': 0.0, 'yaw_deg': 0.0}}

    def stop_motion(self):
        self.zero_calls += 1

    def refresher(self):
        self.refreshes += 1
        return self.refresh_ok


class ApiBase(AsyncHTTPTestCase):
    """โครงร่วม (ไม่มีเทสต์ในตัว) — คลาสลูกกำหนด pin"""
    pin = '4321'

    def get_app(self):
        self.tmp = tempfile.mkdtemp(prefix='api_')
        self.ctx = FakeContext(self.pin, self.tmp)
        web_dir = os.path.join(self.tmp, 'web')
        os.makedirs(web_dir)
        with open(os.path.join(web_dir, 'index.html'), 'w') as f:
            f.write('<html>ok</html>')
        return make_app(self.ctx, web_dir)

    def tearDown(self):
        self.ctx.modes.shutdown(10.0)
        super().tearDown()
        shutil.rmtree(self.tmp, True)

    # ── ตัวช่วย ────────────────────────────────────────────────────────
    def post(self, path, body=None, headers=None, raw=None):
        h = dict(HEADERS)
        h.update(headers or {})
        data = raw if raw is not None else json.dumps(body or {})
        return self.fetch(path, method='POST', body=data, headers=h)

    def login(self, pin=None):
        resp = self.post('/api/login', {'pin': pin or self.pin})
        cookie = resp.headers.get('Set-Cookie', '')
        token = cookie.split(';')[0].split('=', 1)[1] if SESSION_COOKIE in cookie else None
        return resp, token

    def set_mode(self, mode, token=None, **kw):
        headers = {'Cookie': f'{SESSION_COOKIE}={token}'} if token else {}
        headers.update(kw.get('headers', {}))
        return self.post('/api/mode', {'mode': mode}, headers=headers)

    def wait_mode(self, mode, timeout=8.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            snap = json.loads(self.fetch('/api/mode').body)
            if snap['mode'] == mode and snap['state'] == 'RUNNING' and snap['target'] is None:
                return snap
            time.sleep(0.05)
        self.fail(f'รอโหมด {mode} ไม่ทัน: {snap}')


class AssembledAssetsApiTest(ApiBase):
    """index.html / app.js ถูกประกอบจากชิ้นส่วนตอนส่ง ; ไฟล์อื่นยังส่งตามเดิม"""

    def get_app(self):
        app = super().get_app()
        web = os.path.join(self.tmp, 'web')
        os.makedirs(os.path.join(web, 'pages'))
        os.makedirs(os.path.join(web, 'js'))
        with open(os.path.join(web, 'pages', 'p.html'), 'w') as f:
            f.write('<p>piece</p>\n')
        with open(os.path.join(web, 'js', 'a.js'), 'w') as f:
            f.write('var a = 1;\n')
        with open(os.path.join(web, 'index.html'), 'w') as f:
            f.write('<html>\n<!-- @include pages/p.html -->\n</html>\n')
        with open(os.path.join(web, 'app.js'), 'w') as f:
            f.write('(function () {\n// @include js/a.js\n})();\n')
        with open(os.path.join(web, 'style.css'), 'w') as f:
            f.write('body{}')
        return app

    def test_root_and_index_are_assembled(self):
        for path in ('/', '/index.html'):
            resp = self.fetch(path)
            self.assertEqual(resp.code, 200, path)
            self.assertEqual(resp.body.decode(), '<html>\n<p>piece</p>\n</html>\n', path)
            self.assertTrue(resp.headers['Content-Type'].startswith('text/html'))
            self.assertEqual(resp.headers['Cache-Control'], 'no-cache')

    def test_app_js_is_assembled(self):
        resp = self.fetch('/app.js')
        self.assertEqual((resp.code, resp.body.decode()), (200, '(function () {\nvar a = 1;\n})();\n'))
        self.assertTrue(resp.headers['Content-Type'].startswith('application/javascript'))

    def test_other_files_are_served_unchanged(self):
        resp = self.fetch('/style.css')
        self.assertEqual((resp.code, resp.body), (200, b'body{}'))

    def test_missing_piece_gives_500_with_reason_not_a_broken_page(self):
        os.remove(os.path.join(self.tmp, 'web', 'js', 'a.js'))
        resp = self.fetch('/app.js')
        self.assertEqual(resp.code, 500)
        self.assertIn('web assets error', resp.body.decode())
        self.assertNotIn('(function', resp.body.decode())
        self.assertEqual(self.fetch('/').code, 200)                # หน้าอื่นที่ประกอบได้ยังปกติ


class ApiTest(ApiBase):
    """เปิดใช้ PIN"""

    # ── สาธารณะ ────────────────────────────────────────────────────────
    def test_status_and_static_are_public(self):
        body = json.loads(self.fetch('/api/status').body)
        self.assertEqual(body['hello'], 'status')
        self.assertEqual(body['mode']['mode'], 'STANDBY')
        self.assertIn(b'ok', self.fetch('/').body)

    def test_history_endpoint_is_public_and_validates_window(self):
        self.ctx.history = SpeedHistory()
        now = time.time()
        for i in range(5):
            self.ctx.history.add(now - 4 + i, 0.1 * i, None if i == 2 else 0.0)
        body = json.loads(self.fetch('/api/history?window=60').body)
        self.assertEqual(len(body['t']), 5)
        self.assertEqual(body['window'], 60.0)
        self.assertIsNone(body['w'][2])                      # ช่องว่างส่งเป็น null
        self.assertEqual(json.loads(self.fetch('/api/history').body)['window'], 120.0)
        self.assertEqual(self.fetch('/api/history?window=abc').code, 400)

    def test_live_map_endpoints_are_public(self):
        self.ctx.live_map = LiveMap()
        meta = json.loads(self.fetch('/api/map/live').body)
        self.assertFalse(meta['available'])
        resp = self.fetch('/api/map/live.png')
        self.assertEqual(resp.code, 404)
        self.assertEqual(json.loads(resp.body)['error']['code'], 'api.no_map')
        self.ctx.live_map.update(3, 2, 0.05, -1.0, -2.0, 0.0, [0, 100, -1, 50, 0, 100])
        meta = json.loads(self.fetch('/api/map/live').body)
        self.assertTrue(meta['available'])
        self.assertEqual((meta['width'], meta['height']), (3, 2))
        resp = self.fetch('/api/map/live.png')
        self.assertEqual(resp.code, 200)
        self.assertEqual(resp.headers['Content-Type'], 'image/png')
        self.assertTrue(resp.body.startswith(b'\x89PNG'))
        self.assertIn('no-store', resp.headers['Cache-Control'])
        self.assertEqual(self.post('/api/map/live', {}).code, 405)     # อ่านอย่างเดียว

    def test_stats_endpoint_is_public_read_only(self):
        now = time.time()
        self.ctx.stats = StatsStore('')
        for i in range(5):
            self.ctx.stats.update(now - 2 + i * 0.5, 0.2, 'NAVIGATION')
        body = json.loads(self.fetch('/api/stats').body)
        self.assertGreater(body['today']['distance_m'], 0)
        self.assertEqual(len(body['days']), 7)
        self.assertEqual(body['timeline'][0]['mode'], 'NAVIGATION')
        self.assertEqual((body['rounds'], body['total']['rounds']), (0, 0))
        # อ่านอย่างเดียว: POST ไม่ได้ (ไม่มี endpoint รีเซ็ตในขั้นนี้)
        self.assertEqual(self.post('/api/stats', {}).code, 405)

    def test_session_endpoint_reports_not_admin(self):
        body = json.loads(self.fetch('/api/session').body)
        self.assertEqual((body['admin'], body['auth_enabled']), (False, True))

    # ── login ──────────────────────────────────────────────────────────
    def test_login_sets_secure_cookie_flags(self):
        resp, token = self.login()
        self.assertEqual(resp.code, 200)
        self.assertTrue(token)
        cookie = resp.headers['Set-Cookie']
        self.assertIn('HttpOnly', cookie)
        self.assertIn('SameSite=Strict', cookie)
        session = self.fetch('/api/session', headers={'Cookie': f'{SESSION_COOKIE}={token}'})
        self.assertTrue(json.loads(session.body)['admin'])

    def test_polling_session_does_not_keep_it_alive_but_real_actions_do(self):
        clock = [1000.0]
        self.ctx.auth._clock = lambda: clock[0]
        _, token = self.login()
        cookie = {'Cookie': f'{SESSION_COOKIE}={token}'}

        def is_admin():
            return json.loads(self.fetch('/api/session', headers=cookie).body)['admin']

        clock[0] += 20 * 60
        self.assertTrue(is_admin())                  # ถามสถานะที่นาทีที่ 20 (ไม่ต่ออายุ)
        clock[0] += 20 * 60                          # นาทีที่ 40 > 30 นับจากใช้งานจริงครั้งล่าสุด
        self.assertFalse(is_admin(), 'การถามสถานะทำให้เซสชันไม่หมดอายุ')

        _, token = self.login()                      # ล็อกอินใหม่
        cookie = {'Cookie': f'{SESSION_COOKIE}={token}'}
        clock[0] += 20 * 60
        self.assertEqual(self.set_mode('STANDBY', token).code, 202)   # สั่งงานจริง = ต่ออายุ
        clock[0] += 20 * 60                          # ผ่านไป 40 นาทีนับจากล็อกอิน แต่ 20 จากสั่งงาน
        self.assertTrue(is_admin())

    def test_wrong_pin_is_401_and_sets_no_cookie(self):
        resp, token = self.login('0000')
        self.assertEqual(resp.code, 401)
        self.assertIsNone(token)

    def test_lockout_returns_429_with_retry_after(self):
        for _ in range(2):
            self.assertEqual(self.login('0000')[0].code, 401)
        resp, _ = self.login('0000')               # ครั้งที่ 3 = max_fails
        self.assertEqual(resp.code, 429)
        self.assertEqual(resp.headers['Retry-After'], '30')
        self.assertEqual(self.login()[0].code, 429)  # PIN ถูกก็ยังโดนล็อก

    def test_logout_ends_session(self):
        _, token = self.login()
        cookie = {'Cookie': f'{SESSION_COOKIE}={token}'}
        self.assertEqual(self.post('/api/logout', headers=cookie).code, 200)
        self.assertEqual(self.set_mode('MANUAL', token).code, 401)

    # ── กัน CSRF / ข้อมูลผิดรูป ────────────────────────────────────────
    def test_post_without_csrf_header_rejected(self):
        resp = self.fetch('/api/login', method='POST', body='{"pin": "4321"}',
                          headers={'Content-Type': 'application/json'})
        self.assertEqual(resp.code, 403)

    def test_cross_origin_post_rejected(self):
        resp = self.post('/api/login', {'pin': self.pin}, headers={'Origin': 'http://evil.example'})
        self.assertEqual(resp.code, 403)

    def test_same_origin_post_allowed(self):
        origin = f'http://127.0.0.1:{self.get_http_port()}'    # ตรงกับ Host ที่ client ส่ง
        resp = self.post('/api/login', {'pin': self.pin}, headers={'Origin': origin})
        self.assertEqual(resp.code, 200)

    def test_malformed_json_is_400(self):
        self.assertEqual(self.post('/api/login', raw='{not json').code, 400)
        self.assertEqual(self.post('/api/login', raw='[1, 2]').code, 400)

    # ── ค่า SLAM ที่ปรับได้ ──────────────────────────────────────────────
    def slam_post(self, values, token=None, **kw):
        headers = {'Cookie': f'{SESSION_COOKIE}={token}'} if token else {}
        headers.update(kw.get('headers', {}))
        return self.post('/api/slam/params', {'values': values}, headers=headers)

    def slam_rows(self, body):
        return {r['key']: r for g in body['groups'] for r in g['params']}

    def test_slam_params_are_public_to_read(self):
        resp = self.fetch('/api/slam/params')
        self.assertEqual(resp.code, 200)
        body = json.loads(resp.body)
        rows = self.slam_rows(body)
        self.assertEqual(len(rows), 12)
        self.assertEqual(rows['map_update_interval']['default'], 3.0)
        self.assertEqual(body['modified'], 0)
        self.assertIsNone(body['in_use'])

    def test_slam_params_write_requires_admin_and_csrf(self):
        _, token = self.login()
        self.assertEqual(self.slam_post({'resolution': 0.1}).code, 401)
        self.assertEqual(self.slam_post({'resolution': 0.1}, 'forged').code, 401)
        resp = self.fetch('/api/slam/params', method='POST', body=json.dumps({'values': {'resolution': 0.1}}),
                          headers={'Cookie': f'{SESSION_COOKIE}={token}', 'Content-Type': 'application/json'})
        self.assertEqual(resp.code, 403)
        evil = {'Origin': 'http://evil.example'}
        self.assertEqual(self.slam_post({'resolution': 0.1}, token, headers=evil).code, 403)
        self.assertEqual(json.loads(self.fetch('/api/slam/params').body)['modified'], 0)     # ไม่มีอะไรเปลี่ยน

    def test_admin_saves_slam_params(self):
        _, token = self.login()
        resp = self.slam_post({'resolution': 0.1, 'do_loop_closing': False}, token)
        self.assertEqual(resp.code, 200)
        body = json.loads(resp.body)
        self.assertTrue(body['ok'])
        rows = self.slam_rows(body['params'])
        self.assertEqual((rows['resolution']['value'], rows['resolution']['default'], rows['resolution']['modified']),
                         (0.1, 0.05, True))
        self.assertIs(rows['do_loop_closing']['value'], False)
        self.assertEqual(body['params']['modified'], 2)
        again = json.loads(self.fetch('/api/slam/params').body)
        self.assertEqual(again['modified'], 2)

    def test_invalid_slam_values_are_400_with_translatable_errors(self):
        _, token = self.login()
        resp = self.slam_post({'resolution': 5}, token)
        self.assertEqual(resp.code, 400)
        err = json.loads(resp.body)['error']
        self.assertEqual((err['code'], err['params']),
                         ('slam.err.range', {'key': 'resolution', 'min': 0.02, 'max': 0.2}))
        for values, code in (({'resolution': 'x'}, 'slam.err.type'), ({'odom_frame': 'x'}, 'slam.err.unknown'),
                             ({}, 'slam.err.empty'), (None, 'slam.err.empty'), ('abc', 'slam.err.empty'),
                             ({'loop_match_minimum_response_coarse': 0.9}, 'slam.err.loop_order')):
            resp = self.slam_post(values, token)
            self.assertEqual((resp.code, json.loads(resp.body)['error']['code']), (400, code), str(values))
        self.assertEqual(json.loads(self.fetch('/api/slam/params').body)['modified'], 0)

    def test_malformed_slam_body_is_400(self):
        _, token = self.login()
        resp = self.post('/api/slam/params', raw='{nope', headers={'Cookie': f'{SESSION_COOKIE}={token}'})
        self.assertEqual(resp.code, 400)

    def test_slam_values_in_use_are_shown_only_while_mapping(self):
        _, token = self.login()
        self.ctx.slam_in_use = {'resolution': 0.1}
        self.assertIsNone(json.loads(self.fetch('/api/slam/params').body)['in_use'])        # Standby: ยังไม่มีการใช้
        self.set_mode('MAPPING', token)
        self.wait_mode('MAPPING')
        self.assertEqual(json.loads(self.fetch('/api/slam/params').body)['in_use'], {'resolution': 0.1})
        self.set_mode('STANDBY', token)
        self.wait_mode('STANDBY')
        self.assertIsNone(json.loads(self.fetch('/api/slam/params').body)['in_use'])

    # ── Reset all (ปิดระบบหุ่นทั้งหมด) : Admin เท่านั้น ─────────────────────
    def reset_all(self, token=None, **kw):
        headers = {'Cookie': f'{SESSION_COOKIE}={token}'} if token else {}
        headers.update(kw.get('headers', {}))
        return self.post('/api/mode/reset', {}, headers=headers)

    def test_reset_all_requires_admin_and_csrf(self):
        _, token = self.login()
        self.assertEqual(self.set_mode('NAVIGATION', token).code, 202)
        self.wait_mode('NAVIGATION')
        self.assertEqual(self.reset_all().code, 401)
        self.assertEqual(self.reset_all('forged-token').code, 401)
        resp = self.fetch('/api/mode/reset', method='POST', body='{}', headers={'Cookie': f'{SESSION_COOKIE}={token}'})
        self.assertEqual(resp.code, 403)
        self.assertEqual(self.reset_all(token, headers={'Origin': 'http://evil.example'}).code, 403)
        self.assertEqual(self.fetch('/api/mode/reset').code, 405)
        self.assertEqual(self.ctx.list_nodes(), ['bt_navigator'])          # ไม่มีอะไรถูกปิด

    def test_admin_reset_all_stops_the_system(self):
        _, token = self.login()
        self.set_mode('MAPPING', token)
        self.wait_mode('MAPPING')
        resp = self.reset_all(token)
        self.assertEqual(resp.code, 202)
        body = json.loads(resp.body)
        self.assertTrue(body['ok'])
        self.assertEqual(body['message']['code'], 'mode.resetting')
        snap = self.wait_mode('STANDBY')
        self.assertEqual(snap['message']['code'], 'mode.reset.done')
        self.assertEqual(self.ctx.list_nodes(), [])

    def test_reset_all_while_busy_is_409(self):
        _, token = self.login()
        self.assertEqual(self.set_mode('NAVIGATION', token).code, 202)
        resp = self.reset_all(token)
        self.assertEqual(resp.code, 409)
        self.assertFalse(json.loads(resp.body)['ok'])
        self.wait_mode('NAVIGATION')

    def test_mode_snapshot_includes_leftover_field(self):
        self.assertIn('leftover', json.loads(self.fetch('/api/mode').body))

    # ── รีเซ็ตสถิติ (Admin เท่านั้น) ─────────────────────────────────────
    def fill_stats(self):
        now = time.time()
        self.ctx.stats = StatsStore('')
        for i in range(10):
            self.ctx.stats.update(now - 5 + i * 0.5, 0.2, 'NAVIGATION')
        self.assertGreater(self.ctx.stats.summary()['total']['distance_m'], 0)

    def reset_stats(self, token=None, **kw):
        headers = {'Cookie': f'{SESSION_COOKIE}={token}'} if token else {}
        headers.update(kw.get('headers', {}))
        return self.post('/api/stats/reset', {}, headers=headers)

    def test_stats_reset_requires_admin(self):
        self.fill_stats()
        self.assertEqual(self.reset_stats().code, 401)
        self.assertEqual(self.reset_stats('forged-token').code, 401)
        self.assertGreater(self.ctx.stats.summary()['total']['distance_m'], 0)   # ไม่ถูกลบ

    def test_stats_reset_requires_csrf_header(self):
        self.fill_stats()
        _, token = self.login()
        resp = self.fetch('/api/stats/reset', method='POST', body='{}',
                          headers={'Cookie': f'{SESSION_COOKIE}={token}'})
        self.assertEqual(resp.code, 403)
        resp = self.reset_stats(token, headers={'Origin': 'http://evil.example'})
        self.assertEqual(resp.code, 403)
        self.assertGreater(self.ctx.stats.summary()['total']['distance_m'], 0)

    def test_stats_reset_is_post_only(self):
        self.assertEqual(self.fetch('/api/stats/reset').code, 405)

    def test_admin_can_reset_stats(self):
        self.fill_stats()
        _, token = self.login()
        resp = self.reset_stats(token)
        self.assertEqual(resp.code, 200)
        body = json.loads(resp.body)
        self.assertTrue(body['ok'])
        self.assertEqual(body['stats']['total']['distance_m'], 0.0)
        self.assertEqual(body['stats']['timeline'], [])
        self.assertEqual(json.loads(self.fetch('/api/stats').body)['total']['distance_m'], 0.0)

    # ── เปลี่ยนโหมด ────────────────────────────────────────────────────
    def test_mode_requires_login(self):
        resp = self.set_mode('NAVIGATION')
        self.assertEqual(resp.code, 401)
        self.assertEqual(self.ctx.list_nodes(), [])     # ไม่มีอะไรถูกเปิด

    def test_mode_rejects_bad_session_token(self):
        self.assertEqual(self.set_mode('NAVIGATION', 'forged-token').code, 401)

    def test_invalid_mode_is_400(self):
        _, token = self.login()
        self.assertEqual(self.set_mode('TURBO', token).code, 400)
        self.assertEqual(self.set_mode(None, token).code, 400)

    def test_admin_can_switch_modes_end_to_end(self):
        _, token = self.login()
        resp = self.set_mode('NAVIGATION', token)
        self.assertEqual(resp.code, 202)
        self.wait_mode('NAVIGATION')
        self.assertEqual(self.ctx.list_nodes(), ['bt_navigator'])
        self.assertEqual(self.set_mode('STANDBY', token).code, 202)
        self.wait_mode('STANDBY')
        self.assertEqual(self.ctx.list_nodes(), [])

    def test_busy_request_is_409(self):
        _, token = self.login()
        self.assertEqual(self.set_mode('NAVIGATION', token).code, 202)
        resp = self.set_mode('MAPPING', token)
        self.assertEqual(resp.code, 409)
        self.assertFalse(json.loads(resp.body)['ok'])
        self.wait_mode('NAVIGATION')


class AuthDisabledApiTest(ApiBase):
    """ไม่ตั้ง admin_pin → ฟังก์ชันสั่งงานปิดทั้งหมด"""
    pin = ''

    def test_login_disabled_is_503(self):
        self.assertEqual(self.post('/api/login', {'pin': ''}).code, 503)

    def test_mode_is_403_and_nothing_starts(self):
        resp = self.set_mode('NAVIGATION')
        self.assertEqual(resp.code, 403)
        self.assertEqual(self.ctx.list_nodes(), [])

    def test_slam_params_write_is_403_when_auth_disabled(self):
        self.assertEqual(self.post('/api/slam/params', {'values': {'resolution': 0.1}}).code, 403)

    def test_reset_all_is_403_when_auth_disabled(self):
        self.assertEqual(self.post('/api/mode/reset', {}).code, 403)

    def test_stats_reset_is_403_when_auth_disabled(self):
        self.ctx.stats = StatsStore('')
        self.ctx.stats.update(time.time() - 1, 0.2, 'MANUAL')
        self.ctx.stats.update(time.time(), 0.2, 'MANUAL')
        self.assertEqual(self.post('/api/stats/reset', {}).code, 403)
        self.assertGreater(self.ctx.stats.summary()['total']['distance_m'], 0)

    def test_session_reports_auth_disabled(self):
        body = json.loads(self.fetch('/api/session').body)
        self.assertEqual((body['admin'], body['auth_enabled']), (False, False))

    def test_status_is_still_public(self):
        self.assertEqual(self.fetch('/api/status').code, 200)


class DriveBase(ApiBase):
    """ตัวช่วยทดสอบ WebSocket ขับด้วยมือ (setUp ทำขั้นตอนที่ต้องเรียก fetch แบบ sync ให้เสร็จก่อนเข้า event loop)"""

    def setUp(self):
        DriveSocket.owner = None
        super().setUp()
        self.addCleanup(setattr, DriveSocket, 'owner', None)

    def cookie(self, token):
        return {'Cookie': f'{SESSION_COOKIE}={token}'}

    def url(self, path='/ws/drive'):
        return f'ws://127.0.0.1:{self.get_http_port()}{path}'

    async def connect(self, token=None, origin=True):
        headers = dict(self.cookie(token)) if token else {}
        if origin:
            headers['Origin'] = f'http://127.0.0.1:{self.get_http_port()}'
        return await websocket_connect(HTTPRequest(self.url(), headers=headers))

    @staticmethod
    async def recv(ws):
        raw = await ws.read_message()
        if raw is None:
            return None
        await gen_sleep(0.012)         # เว้นจังหวะเหมือนเบราว์เซอร์จริง (เซิร์ฟเวอร์ทิ้งคำสั่งที่ถี่เกิน 200/วินาที)
        return json.loads(raw)

    async def drive_ready(self, token):
        ws = await self.connect(token)
        first = await self.recv(ws)
        self.assertEqual(first['t'], 'ready', first)
        return ws

    async def apost(self, path, body, token):
        headers = dict(HEADERS)
        headers.update(self.cookie(token))
        return await self.http_client.fetch(self.get_url(path), method='POST', body=json.dumps(body), headers=headers,
                                            raise_error=False)

    def spin(self, seconds=0.5):
        """เดินนาฬิกาปลอมแล้วเรียก tick แทน timer ของ ROS ; คืนความเร็วสุดท้ายที่จะถูก publish"""
        last = None
        for _ in range(int(round(seconds / 0.05))):
            self.ctx.now += 0.05
            out = self.ctx.drive.tick()
            last = out if out is not None else last
        return last

    @staticmethod
    def cmd(lin=1.0, ang=0.0, **kw):
        return json.dumps(dict({'t': 'cmd', 'lin': lin, 'ang': ang}, **kw))


class DriveGateTest(DriveBase):
    """สิทธิ์และโหมด"""

    def setUp(self):
        super().setUp()
        _, self.token = self.login()

    @gen_test
    async def test_requires_login(self):
        ws = await self.connect()
        msg = await self.recv(ws)
        self.assertEqual((msg['t'], msg['error']['code']), ('denied', 'auth.required'))
        self.assertIsNone(await self.recv(ws))                         # ปิดการเชื่อมต่อ

    @gen_test
    async def test_refused_in_standby(self):
        ws = await self.connect(self.token)
        msg = await self.recv(ws)
        self.assertEqual((msg['t'], msg['error']['code']), ('denied', 'drive.err.not_allowed'))

    def test_refused_in_navigation_because_nav2_owns_cmd_vel(self):
        self.set_mode('NAVIGATION', self.token)
        self.wait_mode('NAVIGATION')

        async def body():
            ws = await self.connect(self.token)
            self.assertEqual((await self.recv(ws))['error']['code'], 'drive.err.not_allowed')
        self.io_loop.run_sync(body)

    def test_cross_origin_websocket_is_rejected(self):
        self.set_mode('MAPPING', self.token)
        self.wait_mode('MAPPING')

        async def body():
            with self.assertRaises(Exception):
                await websocket_connect(HTTPRequest(self.url(), headers=dict(self.cookie(self.token),
                                                                             Origin='http://evil.example')))
        self.io_loop.run_sync(body)


class DriveSocketTest(DriveBase):
    """ขับในโหมด Mapping (setUp เข้าโหมดไว้ให้แล้ว)"""

    def setUp(self):
        super().setUp()
        _, self.token = self.login()
        self.set_mode('MAPPING', self.token)
        self.wait_mode('MAPPING')

    @gen_test
    async def test_command_flow_and_hard_limits(self):
        ws = await self.drive_ready(self.token)
        info = json.loads((await self.http_client.fetch(self.get_url('/api/drive/info'))).body)
        self.assertEqual(info['limits']['max_linear'], 0.5)
        self.assertTrue(info['in_use'])
        last = None
        for _ in range(40):
            ws.write_message(self.cmd(1.0, -1.0, vmax=99, wmax=99))
            reply = await self.recv(ws)
            self.assertEqual(reply['t'], 'state')
            last = self.spin(0.05) or last
        self.assertTrue(self.ctx.drive.active)
        self.assertAlmostEqual(last[0], 0.5, places=3)                  # ถูกตัดที่เพดาน 0.5 ไม่ใช่ 99
        self.assertAlmostEqual(last[1], -1.0, places=3)

    @gen_test
    async def test_closing_the_socket_stops_the_robot_immediately(self):
        ws = await self.drive_ready(self.token)
        for _ in range(10):
            ws.write_message(self.cmd())
            await self.recv(ws)
            await gen_sleep(0.01)                          # เว้นจังหวะ (เซิร์ฟเวอร์ทิ้งข้อความที่ถี่เกิน 200/วินาที)
        self.spin(0.3)
        self.assertTrue(self.ctx.drive.active)
        ws.close()
        for _ in range(100):
            if not self.ctx.drive.active:
                break
            await gen_sleep(0.02)
        self.assertEqual(self.ctx.drive.tick(), (0.0, 0.0))            # ศูนย์ทันที ไม่ ramp
        self.assertEqual(self.ctx.drive.state()['reason'], 'closed')
        self.assertIsNone(DriveSocket.owner)

    @gen_test
    async def test_stop_message(self):
        ws = await self.drive_ready(self.token)
        ws.write_message(self.cmd())
        await self.recv(ws)
        ws.write_message(json.dumps({'t': 'stop'}))
        reply = await self.recv(ws)
        self.assertEqual((reply['t'], reply['reason']), ('state', 'ws_stop'))
        self.assertFalse(self.ctx.drive.active)

    @gen_test
    async def test_second_device_is_refused_and_does_not_disturb_the_first(self):
        ws1 = await self.drive_ready(self.token)
        ws1.write_message(self.cmd())
        await self.recv(ws1)
        ws2 = await self.connect(self.token)
        self.assertEqual((await self.recv(ws2))['error']['code'], 'drive.err.in_use')
        self.assertTrue(self.ctx.drive.active)                                           # คนแรกยังขับอยู่
        try:
            ws2.write_message(json.dumps({'t': 'stop'}))           # เครื่องที่ถูกปฏิเสธส่งอะไรมาก็ไม่มีผล
        except Exception:                                                                # noqa: BLE001
            pass
        await gen_sleep(0.1)
        self.assertTrue(self.ctx.drive.active)

    @gen_test
    async def test_unauthenticated_connection_cannot_interrupt_the_driver(self):
        ws1 = await self.drive_ready(self.token)
        ws1.write_message(self.cmd())
        await self.recv(ws1)
        for _ in range(3):
            bad = await self.connect()
            await self.recv(bad)
        self.assertTrue(self.ctx.drive.active)

    @gen_test
    async def test_silent_owner_can_be_replaced(self):
        old = DriveSocket.STALE_S
        DriveSocket.STALE_S = 0.2
        self.addCleanup(setattr, DriveSocket, 'STALE_S', old)
        ws1 = await self.drive_ready(self.token)
        await gen_sleep(0.4)
        ws2 = await self.drive_ready(self.token)                                         # เครื่องแรกเงียบ → แย่งได้
        ws2.write_message(self.cmd())
        self.assertEqual((await self.recv(ws2))['t'], 'state')
        self.assertIsNone(await self.recv(ws1))                                          # เครื่องแรกถูกปิด

    @gen_test
    async def test_mode_change_while_driving_stops_and_denies(self):
        ws = await self.drive_ready(self.token)
        ws.write_message(self.cmd())
        await self.recv(ws)
        resp = await self.apost('/api/mode', {'mode': 'STANDBY'}, self.token)
        self.assertEqual(resp.code, 202)
        reply = None
        for _ in range(200):
            ws.write_message(self.cmd())
            reply = await self.recv(ws)
            if reply is None or reply['t'] == 'denied':
                break
            await gen_sleep(0.05)
        self.assertTrue(reply is None or reply['error']['code'] == 'drive.err.not_allowed', reply)
        self.assertFalse(self.ctx.drive.active)

    @gen_test
    async def test_logout_while_driving_stops_and_denies(self):
        ws = await self.drive_ready(self.token)
        ws.write_message(self.cmd())
        await self.recv(ws)
        await self.apost('/api/logout', {}, self.token)
        ws.write_message(self.cmd())
        reply = await self.recv(ws)
        self.assertEqual((reply['t'], reply['error']['code']), ('denied', 'auth.required'))
        self.assertFalse(self.ctx.drive.active)

    @gen_test
    async def test_invalid_numbers_stop_the_robot(self):
        ws = await self.drive_ready(self.token)
        for bad in ('{"t": "cmd", "lin": NaN, "ang": 0}', '{"t": "cmd", "lin": "x", "ang": 0}',
                    '{"t": "cmd", "ang": 0}',
                    '{"t": "cmd", "lin": 1, "ang": 0, "vmax": Infinity}'):
            ws.write_message(self.cmd())
            await self.recv(ws)
            await gen_sleep(0.01)
            ws.write_message(bad)
            self.assertEqual((await self.recv(ws))['t'], 'invalid', bad)
            self.assertEqual(self.ctx.drive.state()['reason'], 'invalid')
            self.assertEqual(self.ctx.drive.tick(), (0.0, 0.0), bad)

    @gen_test
    async def test_garbage_and_oversized_messages(self):
        ws = await self.drive_ready(self.token)
        ws.write_message(self.cmd())
        await self.recv(ws)
        await gen_sleep(0.01)
        ws.write_message('not json at all')
        await gen_sleep(0.1)
        self.assertFalse(self.ctx.drive.active)                                          # ข้อความเสีย = หยุด
        ws.write_message(self.cmd())
        await self.recv(ws)
        await gen_sleep(0.01)
        ws.write_message('x' * 2000)
        self.assertIsNone(await self.recv(ws))                                           # ใหญ่เกินไป = ตัดการเชื่อมต่อ
        self.assertFalse(self.ctx.drive.active)

    @gen_test
    async def test_heartbeat_does_not_move_the_robot(self):
        ws = await self.drive_ready(self.token)
        ws.write_message(json.dumps({'t': 'hb'}))
        self.assertEqual((await self.recv(ws))['t'], 'state')
        self.assertFalse(self.ctx.drive.active)

    @gen_test
    async def test_message_flood_is_throttled_not_fatal(self):
        ws = await self.drive_ready(self.token)
        for _ in range(300):
            ws.write_message(self.cmd())
        await gen_sleep(0.3)
        self.assertTrue(self.ctx.drive.active)                 # ยังทำงานปกติ ไม่พังจากข้อความถี่
        ws.write_message(json.dumps({'t': 'stop'}))
        await gen_sleep(0.1)


class DriveRestAndMapTest(DriveBase):
    """REST ของการขับ/สแกน/บันทึกแผนที่"""

    def mapping_session(self):
        _, token = self.login()
        self.set_mode('MAPPING', token)
        self.wait_mode('MAPPING')
        return token

    def test_drive_info_is_public(self):
        body = json.loads(self.fetch('/api/drive/info').body)
        self.assertEqual(body['allowed'], False)
        self.assertEqual(body['limits']['default_linear'], 0.25)
        self.mapping_session()
        self.assertEqual(json.loads(self.fetch('/api/drive/info').body)['allowed'], True)

    def test_stop_button_requires_admin_csrf_and_a_drive_mode(self):
        self.assertEqual(self.post('/api/drive/stop', {}).code, 401)
        _, token = self.login()
        self.assertEqual(self.post('/api/drive/stop', {}, headers=self.cookie(token)).code, 409)    # Standby
        resp = self.fetch('/api/drive/stop', method='POST', body='{}', headers=self.cookie(token))
        self.assertEqual(resp.code, 403)
        self.set_mode('MAPPING', token)
        self.wait_mode('MAPPING')
        self.ctx.drive.command(1.0, 0.0)
        resp = self.post('/api/drive/stop', {}, headers=self.cookie(token))
        self.assertEqual(resp.code, 200)
        self.assertFalse(self.ctx.drive.active)
        self.assertEqual(self.ctx.drive.state()['reason'], 'stop_button')
        self.assertEqual(self.fetch('/api/drive/stop').code, 405)

    def test_stop_button_in_navigation_cancels_the_goal_and_zeroes(self):
        _, token = self.login()
        self.set_mode('NAVIGATION', token)
        self.wait_mode('NAVIGATION')
        self.ctx.live_map.update(200, 160, 0.05, -3.0, -2.0, 0.0, [0] * (200 * 160))
        self.ctx.goal.send(1.0, 1.0, 0.0, self.ctx.live_map.probe, True, True)
        resp = self.post('/api/drive/stop', {}, headers=self.cookie(token))
        self.assertEqual(resp.code, 200)
        self.assertEqual((self.ctx.goal.status()['state'], self.ctx.goal_client.cancels, self.ctx.zero_calls),
                         ('canceling', 1, 1))

    def test_stop_button_is_refused_in_standby(self):
        _, token = self.login()
        body = json.loads(self.post('/api/drive/stop', {}, headers=self.cookie(token)).body)
        self.assertEqual(body['error']['code'], 'drive.err.not_allowed')
        self.assertEqual((self.ctx.goal_client.cancels, self.ctx.zero_calls), (0, 0))

    def test_scan_endpoint_is_public(self):
        self.assertFalse(json.loads(self.fetch('/api/scan/live').body)['available'])
        self.ctx.overlay.update([1.0, 2.0], 0.0, 0.1, 0.1, 10.0, (0.0, 0.0, 0.0))
        body = json.loads(self.fetch('/api/scan/live').body)
        self.assertTrue(body['available'])
        self.assertEqual(body['count'], 2)
        self.assertEqual(self.post('/api/scan/live', {}).code, 405)

    # ── บันทึกแผนที่ ────────────────────────────────────────────────────
    def fill_map(self):
        self.ctx.live_map.update(4, 4, 0.05, 0.0, 0.0, 0.0, [0] * 16)

    def save(self, token, name, **kw):
        body = {'name': name}
        body.update(kw)
        return self.post('/api/map/save', body, headers=self.cookie(token) if token else {})

    def test_save_map_requires_admin_and_csrf(self):
        self.fill_map()
        token = self.mapping_session()
        self.assertEqual(self.save(None, 'a').code, 401)
        self.assertEqual(self.save('forged', 'a').code, 401)
        resp = self.fetch('/api/map/save', method='POST', body='{"name": "a"}', headers=self.cookie(token))
        self.assertEqual(resp.code, 403)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, 'maps')))
        self.assertEqual(self.fetch('/api/map/save').code, 405)

    def test_save_map_validates_name_and_state(self):
        _, token = self.login()
        for bad in ('', '../x', 'a b', None, 5, 'a' * 49):
            resp = self.save(token, bad)
            self.assertEqual((resp.code, json.loads(resp.body)['error']['code']), (400, 'map.err.name'), repr(bad))
        resp = self.save(token, 'room1')                                    # Standby
        self.assertEqual((resp.code, json.loads(resp.body)['error']['code']), (409, 'map.err.not_mapping'))
        self.set_mode('MAPPING', token)
        self.wait_mode('MAPPING')
        resp = self.save(token, 'room1')                                    # ยังไม่มีแผนที่
        self.assertEqual((resp.code, json.loads(resp.body)['error']['code']), (409, 'map.err.no_map'))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, 'maps')))

    def test_save_map_success_marks_it_saved(self):
        token = self.mapping_session()
        self.fill_map()
        meta = self.ctx.live_map.meta()
        resp = self.save(token, 'room1')
        self.assertEqual(resp.code, 200, resp.body)
        body = json.loads(resp.body)
        self.assertEqual((body['ok'], body['name']), (True, 'room1'))
        self.assertTrue(body['path'].endswith('/maps/room1.yaml') and os.path.exists(body['path']))
        self.assertEqual(self.ctx.map_saved['hash'], meta['hash'])
        self.assertEqual(self.ctx.map_saved['name'], 'room1')
        saved = json.load(open(os.path.join(self.tmp, 'maps', 'room1.json')))
        self.assertEqual(saved['size_m'], {'w': 0.2, 'h': 0.2})
        self.assertIn('slam', saved)
        last = json.loads(self.fetch('/api/map/saved').body)
        self.assertEqual([m['name'] for m in last['maps']], ['room1'])
        self.assertEqual(last['last']['name'], 'room1')

    def test_save_map_name_clash_needs_overwrite(self):
        token = self.mapping_session()
        self.fill_map()
        self.assertEqual(self.save(token, 'room1').code, 200)
        resp = self.save(token, 'room1')
        self.assertEqual((resp.code, json.loads(resp.body)['error']),
                         (409, {'code': 'map.err.exists', 'params': {'name': 'room1'}}))
        self.assertEqual(self.save(token, 'room1', overwrite='yes').code, 409)      # ต้องเป็น true จริง ไม่ใช่ข้อความ
        self.assertEqual(self.save(token, 'room1', overwrite=True).code, 200)

    def test_save_map_tool_failure_is_reported_and_nothing_remains(self):
        token = self.mapping_session()
        self.fill_map()
        self.ctx.saver = MapSaver(os.path.join(self.tmp, 'maps'), command=[sys.executable, FAKE_SAVER, 'fail'],
                                  timeout=5.0)
        resp = self.save(token, 'room1')
        err = json.loads(resp.body)['error']
        self.assertEqual((resp.code, err['code']), (500, 'map.err.save_failed'))
        self.assertIn('Failed to spin', err['params']['detail'])
        self.assertEqual(os.listdir(os.path.join(self.tmp, 'maps')), [])
        self.assertIsNone(self.ctx.map_saved['hash'])                       # ล้มเหลว = ไม่ถือว่าบันทึกแล้ว


class SaveNonBlockingTest(DriveBase):
    def setUp(self):
        super().setUp()
        _, self.token = self.login()
        self.set_mode('MAPPING', self.token)
        self.wait_mode('MAPPING')
        self.ctx.live_map.update(4, 4, 0.05, 0.0, 0.0, 0.0, [0] * 16)
        self.ctx.saver = MapSaver(os.path.join(self.tmp, 'maps'), command=[sys.executable, FAKE_SAVER, 'slow'],
                                  timeout=1.5)

    @gen_test(timeout=15)
    async def test_save_map_does_not_block_other_requests(self):
        import asyncio
        slow = asyncio.ensure_future(self.apost('/api/map/save', {'name': 'slowmap'}, self.token))
        await gen_sleep(0.4)
        t0 = time.time()
        resp = await self.http_client.fetch(self.get_url('/api/status'))      # เซิร์ฟเวอร์ยังตอบระหว่างรอบันทึก
        self.assertEqual(resp.code, 200)
        self.assertLess(time.time() - t0, 0.5)
        done = await slow
        self.assertEqual(json.loads(done.body)['error']['code'], 'map.err.timeout')


class MapLibraryApiTest(DriveBase):
    """REST ของคลังแผนที่: ภาพย่อ (ทุกคน) + เปลี่ยนชื่อ/ลบ (Admin)"""

    def setUp(self):
        super().setUp()
        self.maps = os.path.join(self.tmp, 'maps')
        self.ctx.saver.save('room1')                       # ตัวแทน map_saver_cli สร้าง .pgm/.yaml จริง
        self.ctx.saver.save('room2')
        _, self.token = self.login()
        self.h = self.cookie(self.token)

    def names(self):
        return [m['name'] for m in json.loads(self.fetch('/api/map/saved').body)['maps']]

    def files(self):
        return sorted(os.listdir(self.maps))

    def err(self, resp):
        return json.loads(resp.body)['error']

    def test_rename_and_delete_require_admin_and_csrf(self):
        before = self.files()
        for path, body in (('/api/map/rename', {'name': 'room1', 'new_name': 'x1'}),
                           ('/api/map/delete', {'name': 'room1'})):
            self.assertEqual(self.post(path, body).code, 401, path)
            self.assertEqual(self.post(path, body, headers=self.cookie('forged')).code, 401, path)
            resp = self.fetch(path, method='POST', body=json.dumps(body), headers=self.h)          # ไม่มีเฮดเดอร์ CSRF
            self.assertEqual(resp.code, 403, path)
            self.assertEqual(self.fetch(path).code, 405, path)
        self.assertEqual(self.files(), before)

    def test_bad_names_are_400_and_touch_nothing(self):
        before = self.files()
        for bad in ('', '../room1', 'a b', None, 5, ['room1'], 'a' * 49):
            for path, body in (('/api/map/rename', {'name': bad, 'new_name': 'ok1'}),
                               ('/api/map/rename', {'name': 'room1', 'new_name': bad}),
                               ('/api/map/delete', {'name': bad})):
                resp = self.post(path, body, headers=self.h)
                self.assertEqual((resp.code, self.err(resp)['code']), (400, 'map.err.name'), (path, bad))
        self.assertEqual(self.post('/api/map/rename', {'name': 'room1'}, headers=self.h).code, 400)    # ขาด new_name
        self.assertEqual(self.files(), before)

    def test_rename_success_updates_the_list_and_the_saved_marker(self):
        self.ctx.map_saved.update({'hash': 'h', 'name': 'room1', 'time': 5.0})
        resp = self.post('/api/map/rename', {'name': 'room1', 'new_name': 'hall'}, headers=self.h)
        self.assertEqual((resp.code, json.loads(resp.body)), (200, {'ok': True, 'name': 'hall'}))
        self.assertEqual(sorted(self.names()), ['hall', 'room2'])
        self.assertEqual(self.ctx.map_saved['name'], 'hall')     # หน้า Mapping ยังรู้ว่า "บันทึกแล้ว" ในชื่อใหม่
        self.assertEqual(yaml_image(os.path.join(self.maps, 'hall.yaml')), 'hall.pgm')
        other = self.ctx.map_saved
        self.post('/api/map/rename', {'name': 'room2', 'new_name': 'lab'}, headers=self.h)
        self.assertEqual(other['name'], 'hall')                  # เปลี่ยนชื่ออันอื่นต้องไม่ยุ่งกับเครื่องหมายนี้

    def test_rename_errors(self):
        resp = self.post('/api/map/rename', {'name': 'room1', 'new_name': 'room2'}, headers=self.h)
        self.assertEqual((resp.code, self.err(resp)), (409, {'code': 'map.err.exists', 'params': {'name': 'room2'}}))
        resp = self.post('/api/map/rename', {'name': 'room1', 'new_name': 'room1'}, headers=self.h)
        self.assertEqual((resp.code, self.err(resp)['code']), (400, 'map.err.same_name'))
        resp = self.post('/api/map/rename', {'name': 'nope', 'new_name': 'x1'}, headers=self.h)
        self.assertEqual((resp.code, self.err(resp)['code']), (404, 'map.err.not_found'))
        self.assertEqual(sorted(self.names()), ['room1', 'room2'])

    def test_delete_success_goes_to_trash(self):
        resp = self.post('/api/map/delete', {'name': 'room1'}, headers=self.h)
        self.assertEqual((resp.code, json.loads(resp.body)), (200, {'ok': True, 'name': 'room1'}))
        self.assertEqual(self.names(), ['room2'])
        self.assertTrue(os.path.isdir(os.path.join(self.maps, '.trash')))
        resp = self.post('/api/map/delete', {'name': 'room1'}, headers=self.h)         # ลบซ้ำ
        self.assertEqual((resp.code, self.err(resp)['code']), (404, 'map.err.not_found'))

    def test_nothing_can_be_changed_while_navigation_is_running(self):
        self.set_mode('NAVIGATION', self.token)
        self.wait_mode('NAVIGATION')
        before = self.files()
        for path, body in (('/api/map/rename', {'name': 'room1', 'new_name': 'x1'}),
                           ('/api/map/delete', {'name': 'room1'})):
            resp = self.post(path, body, headers=self.h)
            self.assertEqual((resp.code, self.err(resp)['code']), (409, 'map.err.nav_running'), path)
        self.assertEqual(self.files(), before)
        self.set_mode('STANDBY', self.token)
        self.wait_mode('STANDBY')
        # พ้นโหมด Nav แล้วต้องทำได้
        self.assertEqual(self.post('/api/map/delete', {'name': 'room1'}, headers=self.h).code, 200)

    def test_thumbnail_is_public_png(self):
        resp = self.fetch('/api/map/saved/room1.png')                      # ไม่ล็อกอิน
        self.assertEqual(resp.code, 200)
        self.assertEqual(resp.headers['Content-Type'], 'image/png')
        self.assertTrue(resp.body.startswith(b'\x89PNG'))
        self.assertEqual(self.fetch('/api/map/saved/room1.png', method='POST', body='').code, 405)

    def test_thumbnail_errors(self):
        resp = self.fetch('/api/map/saved/nope.png')
        self.assertEqual((resp.code, self.err(resp)['code']), (404, 'map.err.not_found'))
        for bad in ('..%2Froom1.png', 'room1.yaml.png', '%2e%2e.png', 'a%20b.png'):
            code = self.fetch('/api/map/saved/' + bad).code
            self.assertIn(code, (400, 404), bad)
            self.assertNotEqual(self.fetch('/api/map/saved/' + bad).headers.get('Content-Type'), 'image/png', bad)
        self.post('/api/map/delete', {'name': 'room1'}, headers=self.h)
        self.assertEqual(self.fetch('/api/map/saved/room1.png').code, 404)             # ลบแล้วภาพย่อต้องหายด้วย


class NavParamsApiTest(DriveBase):
    """REST ของค่า Nav2 ที่ปรับได้ (บันทึกไว้ใช้ตอนเปิด Navigation ครั้งถัดไป)"""

    def setUp(self):
        super().setUp()
        _, self.token = self.login()
        self.h = self.cookie(self.token)

    def save(self, values, headers=None):
        return self.post('/api/nav/params', {'values': values}, headers=self.h if headers is None else headers)

    def info(self):
        return json.loads(self.fetch('/api/nav/params').body)

    def err(self, resp):
        return json.loads(resp.body)['error']

    def test_get_is_public_and_has_every_group(self):
        info = self.info()                                                          # ไม่ล็อกอิน
        self.assertEqual((info['available'], info['modified'], info['in_use'], info['restart_needed']),
                         (True, 0, None, False))
        self.assertEqual([g['id'] for g in info['groups']], ['speed', 'path', 'obstacle', 'goal', 'costmap'])
        self.assertEqual(self.post('/api/nav/params', {}).code, 401)               # แต่เขียนไม่ได้

    def test_save_requires_admin_and_csrf(self):
        self.assertEqual(self.post('/api/nav/params', {'values': {'xy_goal_tolerance': 0.2}}).code, 401)
        self.assertEqual(self.save({'xy_goal_tolerance': 0.2}, headers=self.cookie('forged')).code, 401)
        resp = self.fetch('/api/nav/params', method='POST', body='{"values": {"xy_goal_tolerance": 0.2}}',
                          headers=self.h)
        self.assertEqual(resp.code, 403)                                            # ไม่มีเฮดเดอร์ CSRF
        self.assertEqual(self.info()['modified'], 0)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, 'appdata', 'nav', 'nav_overrides.json')))
        self.assertEqual(self.fetch('/api/nav/params', method='PUT', body='{}').code, 405)

    def test_save_success_is_visible_to_everyone_and_persisted(self):
        resp = self.save({'desired_linear_vel': 0.3, 'use_collision_detection': False})
        body = json.loads(resp.body)
        self.assertEqual((resp.code, body['ok'], body['params']['modified']), (200, True, 2))
        rows = {r['key']: r for g in self.info()['groups'] for r in g['params']}
        self.assertEqual((rows['desired_linear_vel']['value'], rows['desired_linear_vel']['modified']), (0.3, True))
        self.assertEqual(rows['use_collision_detection']['value'], False)
        self.assertEqual(self.ctx.navp.extra_args()[0].split(':=')[0], 'nav2_params')    # ถูกส่งให้ launch ตอนเปิด Nav
        saved = json.load(open(os.path.join(self.tmp, 'appdata', 'nav', 'nav_overrides.json')))
        self.assertEqual(saved, {'desired_linear_vel': 0.3, 'use_collision_detection': False})

    def test_setting_a_value_back_to_default_clears_it(self):
        default = self.ctx.navp.default('xy_goal_tolerance')
        self.save({'xy_goal_tolerance': 0.3})
        self.assertEqual(self.info()['modified'], 1)
        self.save({'xy_goal_tolerance': default})
        self.assertEqual(self.info()['modified'], 0)
        self.assertEqual(self.ctx.navp.extra_args(), [])

    def test_validation_errors_are_400_with_translatable_codes_and_change_nothing(self):
        cases = [
            ({'desired_linear_vel': 5}, 'nav.err.range', {'key': 'desired_linear_vel', 'min': 0.05, 'max': 0.5}),
            ({'desired_linear_vel': 'fast'}, 'nav.err.type', {'key': 'desired_linear_vel'}),
            ({'use_collision_detection': 1}, 'nav.err.type', {'key': 'use_collision_detection'}),
            ({'footprint': '[[1,1]]'}, 'nav.err.unknown', {'key': 'footprint'}),
            ({}, 'nav.err.empty', {}),
            ('x', 'nav.err.empty', {}),
            ({'min_lookahead_dist': 1.4}, 'nav.err.lookahead_order', {}),
        ]
        for values, code, params in cases:
            resp = self.save(values)
            self.assertEqual((resp.code, self.err(resp)), (400, {'code': code, 'params': params}), repr(values))
        resp = self.post('/api/nav/params', {}, headers=self.h)                      # ไม่มี values เลย
        self.assertEqual((resp.code, self.err(resp)['code']), (400, 'nav.err.empty'))
        resp = self.post('/api/nav/params', raw='{bad', headers=self.h)
        self.assertEqual(resp.code, 400)
        self.assertEqual(self.info()['modified'], 0)

    def test_one_bad_value_rejects_the_whole_request(self):
        resp = self.save({'desired_linear_vel': 0.3, 'xy_goal_tolerance': 99})
        self.assertEqual(resp.code, 400)
        self.assertEqual(self.info()['modified'], 0)

    def test_save_failure_is_500_and_keeps_the_old_values(self):
        self.save({'xy_goal_tolerance': 0.3})
        with mock.patch('agv_web_app.nav_params.os.replace', side_effect=OSError('disk full')):
            resp = self.save({'xy_goal_tolerance': 0.4})
        self.assertEqual((resp.code, self.err(resp)['code']), (500, 'nav.err.save'))
        self.assertEqual(self.ctx.navp.value('xy_goal_tolerance'), 0.3)

    def test_unavailable_when_the_defaults_file_cannot_be_read(self):
        self.ctx.navp = NavParams(None, os.path.join(self.tmp, 'x'))
        self.assertFalse(self.info()['available'])
        resp = self.save({'xy_goal_tolerance': 0.3})
        self.assertEqual((resp.code, self.err(resp)['code']), (400, 'nav.err.no_defaults'))

    def test_nav_running_reports_in_use_and_restart_needed(self):
        self.set_mode('NAVIGATION', self.token)
        self.wait_mode('NAVIGATION')
        self.ctx.nav_params_in_use = self.ctx.navp.effective()                      # ตัวแทนที่ node จำไว้ตอนเปิดโหมด
        info = self.info()
        self.assertEqual((info['in_use'] == self.ctx.navp.effective(), info['restart_needed']), (True, False))
        # บันทึกระหว่างรัน = ได้ แต่ยังไม่มีผล
        body = json.loads(self.save({'desired_linear_vel': 0.3}).body)
        self.assertTrue(body['params']['restart_needed'])
        self.assertEqual(body['params']['in_use']['desired_linear_vel'], self.ctx.navp.default('desired_linear_vel'))
        self.save({'desired_linear_vel': self.ctx.navp.default('desired_linear_vel')})
        self.assertFalse(self.info()['restart_needed'])

    def test_stale_in_use_is_hidden_when_navigation_is_not_running(self):
        self.ctx.nav_params_in_use = self.ctx.navp.effective()                      # ค่าค้างจากรอบก่อน
        self.save({'desired_linear_vel': 0.3})
        info = self.info()
        self.assertEqual((info['in_use'], info['restart_needed']), (None, False))


class NavRunningTest(unittest.TestCase):
    """นิยามของ "Navigation รันนิ่ง" (ปรับสดได้เฉพาะตอนนี้) : เปิดเสร็จแล้วและไม่ได้กำลังเปลี่ยนโหมด"""

    def test_only_a_settled_navigation_counts(self):
        from agv_web_app.web_api import nav_running
        self.assertTrue(nav_running({'mode': 'NAVIGATION', 'state': 'RUNNING', 'target': None}))
        self.assertTrue(nav_running({'mode': 'NAVIGATION', 'state': 'RUNNING'}))
        for mode in ({'mode': 'NAVIGATION', 'state': 'RUNNING', 'target': 'STANDBY'},     # กำลังปิด
                     {'mode': 'NAVIGATION', 'state': 'STOPPING', 'target': 'STANDBY'},
                     {'mode': 'STANDBY', 'state': 'STARTING', 'target': 'NAVIGATION'},    # กำลังเปิด
                     {'mode': 'NAVIGATION', 'state': 'ERROR', 'target': None},
                     {'mode': 'MAPPING', 'state': 'RUNNING', 'target': None},
                     {'mode': 'STANDBY', 'state': 'RUNNING', 'target': None}):
            self.assertFalse(nav_running(mode), mode)


class NavPoseApiTest(DriveBase):
    """REST วางตำแหน่งเริ่มต้น: Admin · Nav รันนิ่ง · อยู่ในกรอบแผนที่ · มี AMCL ฟังอยู่ · หุ่นไม่เคลื่อนที่"""

    def setUp(self):
        super().setUp()
        _, self.token = self.login()
        self.h = self.cookie(self.token)
        self.pub = self.ctx.pose_pub
        self.ctx.live_map.update(200, 160, 0.05, -3.0, -2.0, 0.0, [0] * (200 * 160))      # แผนที่ 10 × 8 m ที่ (-3, -2)

    def run_nav(self):
        self.set_mode('NAVIGATION', self.token)
        self.wait_mode('NAVIGATION')

    def put(self, x=1.0, y=2.0, yaw=0.5, headers=None, **extra):
        return self.post('/api/nav/initialpose', dict({'x': x, 'y': y, 'yaw': yaw}, **extra),
                         headers=self.h if headers is None else headers)

    def err(self, resp):
        return json.loads(resp.body)['error']

    def test_requires_admin_and_csrf(self):
        self.run_nav()
        body = {'x': 1.0, 'y': 2.0, 'yaw': 0.0}
        self.assertEqual(self.post('/api/nav/initialpose', body).code, 401)
        self.assertEqual(self.put(headers=self.cookie('forged')).code, 401)
        # ไม่มีเฮดเดอร์ CSRF
        resp = self.fetch('/api/nav/initialpose', method='POST', body=json.dumps(body), headers=self.h)
        self.assertEqual(resp.code, 403)
        self.assertEqual(self.fetch('/api/nav/initialpose').code, 405)
        self.assertEqual(self.pub.sent, [])

    def test_only_while_navigation_runs(self):
        resp = self.put()                                                                  # Standby
        self.assertEqual((resp.code, self.err(resp)['code']), (409, 'nav.err.not_running'))
        self.set_mode('MANUAL', self.token)
        self.wait_mode('MANUAL')
        self.assertEqual(self.put().code, 409)
        self.assertEqual((self.pub.sent, self.pub.polls), ([], 0))

    def test_success_publishes_three_times_and_returns_the_pose(self):
        self.run_nav()
        resp = self.put(1.0, 2.0, 0.5)
        body = json.loads(resp.body)
        self.assertEqual((resp.code, body['ok']), (200, True))
        self.assertEqual((body['pose']['x'], body['pose']['y'], body['pose']['yaw']), (1.0, 2.0, 0.5))
        self.assertEqual(len(self.pub.sent), 3)
        self.assertEqual({(x, y, yaw) for x, y, yaw, _ in self.pub.sent}, {(1.0, 2.0, 0.5)})

    def test_yaw_is_wrapped(self):
        self.run_nav()
        body = json.loads(self.put(yaw=7.0).body)
        self.assertAlmostEqual(body['pose']['yaw'], 7.0 - 2 * 3.141592653589793)

    def test_invalid_numbers_and_outside_map_are_400_and_publish_nothing(self):
        self.run_nav()
        for bad in ('a', None, True, [1], {}, float('inf'), 1e9):
            resp = self.put(x=bad)
            self.assertEqual((resp.code, self.err(resp)['code']), (400, 'nav.err.pose_invalid'), repr(bad))
        resp = self.post('/api/nav/initialpose', {'x': 1.0}, headers=self.h)                 # ขาด y/yaw
        self.assertEqual((resp.code, self.err(resp)['code']), (400, 'nav.err.pose_invalid'))
        self.assertEqual(self.post('/api/nav/initialpose', raw='{bad', headers=self.h).code, 400)
        for x, y in ((8.0, 0.0), (-3.5, 0.0), (0.0, 7.0), (0.0, -2.5)):
            resp = self.put(x=x, y=y)
            self.assertEqual((resp.code, self.err(resp)['code']), (400, 'nav.err.pose_outside'), (x, y))
        self.assertEqual(self.pub.sent, [])

    def test_no_map_and_no_listener_are_409_and_publish_nothing(self):
        self.run_nav()
        self.pub.count = 0
        resp = self.put()
        self.assertEqual((resp.code, self.err(resp)['code']), (409, 'nav.err.no_listener'))
        self.pub.count = 1
        self.ctx.live_map.clear()
        resp = self.put()
        self.assertEqual((resp.code, self.err(resp)['code']), (409, 'nav.err.no_map'))
        self.assertEqual(self.pub.sent, [])

    def test_busy_is_409(self):
        self.run_nav()
        self.ctx.initial_pose._lock.acquire()
        try:
            resp = self.put()
            self.assertEqual((resp.code, self.err(resp)['code']), (409, 'nav.err.busy'))
        finally:
            self.ctx.initial_pose._lock.release()
        self.assertEqual(self.pub.sent, [])


class NavLiveApiTest(DriveBase):
    """REST ของการปรับค่า Nav2 สด: ใช้ได้เฉพาะตอน Navigation รันนิ่ง · Admin · ค่าเสี่ยงต้องยืนยัน · all-or-nothing"""

    def setUp(self):
        super().setUp()
        _, self.token = self.login()
        self.h = self.cookie(self.token)
        self.t = self.ctx.transport

    def run_nav(self):
        self.set_mode('NAVIGATION', self.token)
        self.wait_mode('NAVIGATION')

    def apply(self, values, headers=None, **extra):
        return self.post('/api/nav/params/apply', dict({'values': values}, **extra),
                         headers=self.h if headers is None else headers)

    def err(self, resp):
        return json.loads(resp.body)['error']

    def live_state(self, node, name):
        return self.t.state[(node, name)]

    def test_apply_requires_admin_and_csrf(self):
        self.run_nav()
        body = {'values': {'xy_goal_tolerance': 0.2}}
        self.assertEqual(self.post('/api/nav/params/apply', body).code, 401)
        self.assertEqual(self.apply({'xy_goal_tolerance': 0.2}, headers=self.cookie('forged')).code, 401)
        resp = self.fetch('/api/nav/params/apply', method='POST', body=json.dumps(body), headers=self.h)   # ไม่มี CSRF
        self.assertEqual(resp.code, 403)
        self.assertEqual(self.fetch('/api/nav/params/apply').code, 405)
        self.assertEqual(self.t.sets, [])

    def test_not_running_is_refused_before_anything_is_sent(self):
        resp = self.apply({'xy_goal_tolerance': 0.2})                                # Standby
        self.assertEqual((resp.code, self.err(resp)['code']), (409, 'nav.err.not_running'))
        self.set_mode('MANUAL', self.token)
        self.wait_mode('MANUAL')
        self.assertEqual(self.apply({'xy_goal_tolerance': 0.2}).code, 409)           # โหมดอื่นก็ไม่ได้
        self.assertEqual((self.t.gets, self.t.sets), ([], []))

    def test_safe_change_is_applied_live_and_reported(self):
        self.run_nav()
        resp = self.apply({'xy_goal_tolerance': 0.2})
        body = json.loads(resp.body)
        self.assertEqual((resp.code, body['ok']), (200, True))
        self.assertEqual(body['applied']['xy_goal_tolerance']['to'], 0.2)
        self.assertEqual(body['live']['xy_goal_tolerance'], 0.2)
        self.assertEqual(self.live_state('/controller_server', 'general_goal_checker.xy_goal_tolerance'), 0.2)
        self.assertEqual(self.ctx.nav_params_in_use['xy_goal_tolerance'], 0.2)       # "ค่าที่ใช้อยู่" อัปเดตตามจริง

    def test_applied_but_unsaved_values_are_flagged_and_saving_clears_the_flag(self):
        self.run_nav()
        self.ctx.nav_params_in_use = self.ctx.navp.effective()                       # ตัวแทนที่ node จำไว้ตอนเปิดโหมด
        body = json.loads(self.apply({'xy_goal_tolerance': 0.2}).body)
        self.assertTrue(body['params']['restart_needed'])                            # ใช้อยู่ ≠ ที่บันทึกไว้
        self.assertEqual(body['params']['in_use']['xy_goal_tolerance'], 0.2)
        # ยังไม่บันทึก
        self.assertEqual(self.ctx.navp.value('xy_goal_tolerance'), self.ctx.navp.default('xy_goal_tolerance'))
        saved = json.loads(self.post('/api/nav/params', {'values': {'xy_goal_tolerance': 0.2}}, headers=self.h).body)
        self.assertFalse(saved['params']['restart_needed'])                          # บันทึกแล้ว = ตรงกับที่ใช้อยู่

    def test_risky_change_needs_confirmed_true_and_nothing_changes_without_it(self):
        self.run_nav()
        resp = self.apply({'desired_linear_vel': 0.3, 'xy_goal_tolerance': 0.2})
        self.assertEqual((resp.code, self.err(resp)),
                         (409, {'code': 'nav.err.confirm_required', 'params': {'keys': 'desired_linear_vel'}}))
        for not_true in ('yes', 1, 'true', None, False):
            self.assertEqual(self.apply({'desired_linear_vel': 0.3}, confirmed=not_true).code, 409, repr(not_true))
        self.assertEqual(self.t.sets, [])
        resp = self.apply({'desired_linear_vel': 0.3, 'xy_goal_tolerance': 0.2}, confirmed=True)
        self.assertEqual((resp.code, sorted(json.loads(resp.body)['applied'])),
                         (200, ['desired_linear_vel', 'xy_goal_tolerance']))

    def test_validation_errors_are_400_and_nothing_is_read_or_sent(self):
        self.run_nav()
        cases = [({'desired_linear_vel': 5}, 'nav.err.range'), ({'desired_linear_vel': 'fast'}, 'nav.err.type'),
                 ({'use_collision_detection': 1}, 'nav.err.type'), ({'footprint': 'x'}, 'nav.err.unknown'),
                 ({}, 'nav.err.empty'), ('x', 'nav.err.empty')]
        for values, code in cases:
            resp = self.apply(values, confirmed=True)
            self.assertEqual((resp.code, self.err(resp)['code']), (400, code), repr(values))
        resp = self.post('/api/nav/params/apply', {}, headers=self.h)
        self.assertEqual((resp.code, self.err(resp)['code']), (400, 'nav.err.empty'))
        self.assertEqual(self.post('/api/nav/params/apply', raw='{bad', headers=self.h).code, 400)
        self.assertEqual((self.t.gets, self.t.sets), ([], []))

    def test_lookahead_order_is_checked_against_live_values(self):
        self.run_nav()
        self.t.state[('/controller_server', 'FollowPath.max_lookahead_dist')] = 0.6
        resp = self.apply({'min_lookahead_dist': 0.7})
        self.assertEqual((resp.code, self.err(resp)['code']), (400, 'nav.err.lookahead_order'))
        self.assertEqual(self.t.sets, [])

    def test_nav2_problems_map_to_clear_statuses_and_leave_nav2_unchanged(self):
        self.run_nav()
        before = self.t.snapshot()
        self.t.reject[('/controller_server', 'general_goal_checker.xy_goal_tolerance')] = 'not allowed'
        resp = self.apply({'xy_goal_tolerance': 0.2})
        self.assertEqual((resp.code, self.err(resp)),
                         (409, {'code': 'nav.err.rejected',
                                'params': {'key': 'xy_goal_tolerance', 'reason': 'not allowed'}}))
        self.t.reject.clear()
        self.t.coerce[('/controller_server', 'general_goal_checker.xy_goal_tolerance')] = 0.25
        resp = self.apply({'xy_goal_tolerance': 0.2})
        self.assertEqual((resp.code, self.err(resp)['code']), (500, 'nav.err.verify'))
        self.t.coerce.clear()
        self.t.down.add('/local_costmap/local_costmap')
        resp = self.apply({'xy_goal_tolerance': 0.2})
        self.assertEqual((resp.code, self.err(resp)['code']), (503, 'nav.err.unreachable'))
        self.t.down.clear()
        self.assertEqual(self.t.snapshot(), before)
        self.assertIsNone(self.ctx.nav_params_in_use)                                # ล้มเหลว = ไม่อัปเดตค่าที่ใช้อยู่

    def test_rollback_failure_is_500_and_busy_is_409(self):
        self.run_nav()
        self.t.reject[('/local_costmap/local_costmap', 'inflation_layer.inflation_radius')] = 'no'
        self.t.fail_set_calls = {4}
        resp = self.apply({'xy_goal_tolerance': 0.2, 'inflation_radius': 0.7})
        self.assertEqual((resp.code, self.err(resp)['code']), (500, 'nav.err.rollback_failed'))
        self.t.reject.clear()
        self.t.fail_set_calls = set()
        self.ctx.navlive._lock.acquire()
        try:
            resp = self.apply({'xy_goal_tolerance': 0.2})
            self.assertEqual((resp.code, self.err(resp)['code']), (409, 'nav.err.busy'))
        finally:
            self.ctx.navlive._lock.release()

    def test_live_values_are_public_and_only_while_navigation_runs(self):
        body = json.loads(self.fetch('/api/nav/params/live').body)                   # ไม่ล็อกอิน + Standby
        self.assertEqual(body, {'running': False, 'values': None, 'mixed': []})
        self.run_nav()
        self.t.state[('/controller_server', 'FollowPath.desired_linear_vel')] = 0.25
        body = json.loads(self.fetch('/api/nav/params/live').body)
        self.assertEqual((body['running'], body['values']['desired_linear_vel'], body['mixed']), (True, 0.25, []))
        self.assertEqual(self.post('/api/nav/params/live', {}).code, 405)

    def test_live_read_reports_unreachable_nav2(self):
        self.run_nav()
        self.t.down.add('/controller_server')
        resp = self.fetch('/api/nav/params/live')
        self.assertEqual((resp.code, self.err(resp)),
                         (503, {'code': 'nav.err.unreachable', 'params': {'node': '/controller_server'}}))


class MapSelectApiTest(DriveBase):
    """REST ของการเลือกแผนที่ไปใช้กับ Navigation + กฎ "ลบ/เปลี่ยนชื่อแผนที่ที่ถูกเลือกไม่ได้" """

    def setUp(self):
        super().setUp()
        self.maps = os.path.join(self.tmp, 'maps')
        self.ctx.saver.save('room1')
        self.ctx.saver.save('room2')
        _, self.token = self.login()
        self.h = self.cookie(self.token)

    def select(self, name, headers=None):
        return self.post('/api/map/select', {'name': name}, headers=self.h if headers is None else headers)

    def selected(self):
        return json.loads(self.fetch('/api/map/selected').body)

    def err(self, resp):
        return json.loads(resp.body)['error']

    def test_select_requires_admin_and_csrf(self):
        self.assertEqual(self.post('/api/map/select', {'name': 'room1'}).code, 401)
        self.assertEqual(self.select('room1', headers=self.cookie('forged')).code, 401)
        resp = self.fetch('/api/map/select', method='POST', body='{"name": "room1"}', headers=self.h)    # ไม่มี CSRF
        self.assertEqual(resp.code, 403)
        self.assertEqual(self.fetch('/api/map/select').code, 405)
        self.assertIsNone(self.selected()['name'])

    def test_selected_is_public_and_shows_the_default_at_first(self):
        body = self.selected()                                                      # ไม่ล็อกอิน
        self.assertEqual((body['name'], body['available'], body['choice']['source']), (None, False, 'default'))
        self.assertEqual((body['in_use'], body['restart_needed']), (None, False))
        self.assertEqual(self.post('/api/map/selected', {}).code, 405)

    def test_select_success_is_visible_to_everyone_and_returns_the_status(self):
        resp = self.select('room2')
        body = json.loads(resp.body)
        self.assertEqual((resp.code, body['ok'], body['name'], body['available']), (200, True, 'room2', True))
        self.assertEqual(body['choice'], {'source': 'selected', 'name': 'room2',
                                          'file': os.path.join(self.maps, 'room2.yaml')})
        self.assertEqual(self.selected()['name'], 'room2')

    def test_select_validates_input_and_changes_nothing(self):
        self.select('room1')
        for bad in ('', '../room1', 'a b', 5, ['room1'], 'a' * 49):
            resp = self.select(bad)
            self.assertEqual((resp.code, self.err(resp)['code']), (400, 'map.err.name'), repr(bad))
        resp = self.post('/api/map/select', {}, headers=self.h)                      # ขาด name ≠ ยกเลิกการเลือก
        self.assertEqual((resp.code, self.err(resp)['code']), (400, 'map.err.name'))
        resp = self.select('nope')
        self.assertEqual((resp.code, self.err(resp)),
                         (404, {'code': 'map.err.not_found', 'params': {'name': 'nope'}}))
        self.assertEqual(self.selected()['name'], 'room1')

    def test_null_clears_the_selection(self):
        self.select('room1')
        resp = self.select(None)
        self.assertEqual((resp.code, json.loads(resp.body)['name']), (200, None))
        self.assertEqual(self.selected()['choice']['source'], 'default')
        self.assertEqual(self.select(None).code, 200)                                # ยกเลิกซ้ำไม่ล้ม

    def test_selected_map_cannot_be_deleted_or_renamed(self):
        self.select('room1')
        before = sorted(os.listdir(self.maps))
        for path, body in (('/api/map/delete', {'name': 'room1'}),
                           ('/api/map/rename', {'name': 'room1', 'new_name': 'hall'})):
            resp = self.post(path, body, headers=self.h)
            self.assertEqual((resp.code, self.err(resp)), (409, {'code': 'map.err.selected',
                                                                 'params': {'name': 'room1'}}), path)
        self.assertEqual(sorted(os.listdir(self.maps)), before)
        self.assertEqual(self.selected()['name'], 'room1')

    def test_other_maps_are_still_editable_and_selection_can_be_moved_then_released(self):
        self.select('room1')
        self.assertEqual(self.post('/api/map/rename', {'name': 'room2', 'new_name': 'lab'}, headers=self.h).code, 200)
        self.assertEqual(self.post('/api/map/delete', {'name': 'lab'}, headers=self.h).code, 200)
        self.ctx.saver.save('room3')
        self.select('room3')                                                         # ย้ายการเลือก → room1 ลบได้
        self.assertEqual(self.post('/api/map/delete', {'name': 'room1'}, headers=self.h).code, 200)
        self.select(None)                                                            # ปล่อยการเลือก → room3 ลบได้
        self.assertEqual(self.post('/api/map/delete', {'name': 'room3'}, headers=self.h).code, 200)

    def test_nav_running_reports_in_use_and_restart_needed(self):
        self.select('room1')
        self.set_mode('NAVIGATION', self.token)
        self.wait_mode('NAVIGATION')
        self.ctx.nav_map_in_use = self.ctx.selection.choice()                        # ตัวแทนที่ node จำไว้ตอนเปิดโหมด
        body = self.selected()
        self.assertEqual((body['in_use']['name'], body['restart_needed']), ('room1', False))
        resp = self.select('room2')                          # เลือกใหม่ระหว่างที่ Nav รัน = ได้ แต่ยังไม่มีผล
        body = json.loads(resp.body)
        self.assertEqual((resp.code, body['in_use']['name'], body['name'], body['restart_needed']),
                         (200, 'room1', 'room2', True))
        self.select('room1')
        self.assertFalse(self.selected()['restart_needed'])

    def test_stale_in_use_is_hidden_when_navigation_is_not_running(self):
        self.ctx.nav_map_in_use = {'source': 'default', 'name': None, 'file': None}      # ค่าค้างจากรอบก่อน
        self.select('room1')
        body = self.selected()
        self.assertEqual((body['in_use'], body['restart_needed']), (None, False))


def yaml_image(path):
    import yaml
    with open(path) as f:
        return yaml.safe_load(f)['image']


class AuthDisabledDriveTest(DriveBase):
    pin = ''

    def test_everything_that_moves_or_writes_is_403(self):
        self.assertEqual(self.post('/api/drive/stop', {}).code, 403)
        self.assertEqual(self.post('/api/map/save', {'name': 'a'}).code, 403)
        self.assertEqual(self.post('/api/map/rename', {'name': 'a', 'new_name': 'b'}).code, 403)
        self.assertEqual(self.post('/api/map/delete', {'name': 'a'}).code, 403)
        self.assertEqual(self.post('/api/map/select', {'name': 'a'}).code, 403)
        self.assertEqual(self.post('/api/nav/params', {'values': {'xy_goal_tolerance': 0.2}}).code, 403)
        self.assertEqual(self.post('/api/nav/params/apply', {'values': {'xy_goal_tolerance': 0.2}}).code, 403)
        self.assertEqual(self.post('/api/nav/initialpose', {'x': 1.0, 'y': 1.0, 'yaw': 0.0}).code, 403)

    @gen_test
    async def test_drive_socket_is_denied(self):
        ws = await self.connect()
        msg = await self.recv(ws)
        self.assertEqual((msg['t'], msg['error']['code']), ('denied', 'auth.disabled'))


async def gen_sleep(seconds):
    import asyncio
    await asyncio.sleep(seconds)


class NavGoalSetup(DriveBase):
    """โครงร่วมของเทสต์ goal/straight (ไม่มีเทสต์ในตัว) : แผนที่ 10 × 8 m ที่ (-3, -2) มีผนังที่ x = 6.5"""

    def setUp(self):
        super().setUp()
        _, self.token = self.login()
        self.h = self.cookie(self.token)
        self.client = self.ctx.goal_client
        grid = [0] * (200 * 160)                                    # แผนที่ 10 × 8 m ที่ (-3, -2) ว่างทั้งหมด
        for row in range(160):
            grid[row * 200 + 190] = 100                             # ผนังที่ x = 6.5
        self.ctx.live_map.update(200, 160, 0.05, -3.0, -2.0, 0.0, grid)

    def run_nav(self):
        self.set_mode('NAVIGATION', self.token)
        self.wait_mode('NAVIGATION')

    def go(self, x=1.0, y=1.0, yaw=0.5, headers=None, **extra):
        body = dict({'x': x, 'y': y, 'yaw': yaw, 'confirmed': True}, **extra)
        return self.post('/api/nav/goal', body, headers=self.h if headers is None else headers)

    def err(self, resp):
        return json.loads(resp.body)['error']['code']


class NavGoalApiTest(NavGoalSetup):
    """REST ส่งหุ่นไปจุดหมาย: Admin · Nav รันนิ่ง · confirmed · localized · จุดปลอดภัย · ทีละ goal · ยกเลิก"""

    def test_requires_admin_and_csrf(self):
        self.run_nav()
        body = {'x': 1.0, 'y': 1.0, 'yaw': 0.0, 'confirmed': True}
        self.assertEqual(self.post('/api/nav/goal', body).code, 401)
        self.assertEqual(self.go(headers=self.cookie('forged')).code, 401)
        resp = self.fetch('/api/nav/goal', method='POST', body=json.dumps(body), headers=self.h)
        self.assertEqual(resp.code, 403)
        self.assertEqual(self.post('/api/nav/goal/cancel', {}).code, 401)
        self.assertEqual(self.client.sent, [])

    def test_only_while_navigation_runs(self):
        resp = self.go()
        self.assertEqual((resp.code, self.err(resp)), (409, 'nav.err.not_running'))
        self.set_mode('MANUAL', self.token)
        self.wait_mode('MANUAL')
        self.assertEqual(self.go().code, 409)
        self.assertEqual(self.client.sent, [])

    def test_confirmed_is_mandatory(self):
        self.run_nav()
        for bad in (None, False, 'true', 1):
            resp = self.post('/api/nav/goal', {'x': 1.0, 'y': 1.0, 'yaw': 0.0, 'confirmed': bad}, headers=self.h)
            self.assertEqual((resp.code, self.err(resp)), (409, 'nav.err.goal_confirm_required'), repr(bad))
        resp = self.post('/api/nav/goal', {'x': 1.0, 'y': 1.0, 'yaw': 0.0}, headers=self.h)
        self.assertEqual(self.err(resp), 'nav.err.goal_confirm_required')
        self.assertEqual(self.client.sent, [])

    def test_success_sends_exactly_one_goal(self):
        self.run_nav()
        resp = self.go(1.0, 1.5, 0.5)
        body = json.loads(resp.body)
        self.assertEqual((resp.code, body['ok'], body['goal']['state']), (200, True, 'active'))
        self.assertEqual(self.client.sent, [(1.0, 1.5, 0.5)])

    def test_not_localized_is_refused(self):
        self.run_nav()
        for quality in ('bad', 'unknown'):
            self.ctx.quality = quality
            resp = self.go()
            self.assertEqual((resp.code, self.err(resp)), (409, 'nav.err.goal_not_localized'), quality)
        self.ctx.quality = 'fair'
        self.assertEqual(self.go().code, 200)
        self.assertEqual(len(self.client.sent), 1)

    def test_unsafe_goals_are_400_and_send_nothing(self):
        self.run_nav()
        cases = [(50.0, 1.0, 'nav.err.goal_outside'), (6.52, 1.0, 'nav.err.goal_unknown'),
                 (6.2, 1.0, 'nav.err.goal_tight'), (6.15, 1.0, 'nav.err.goal_tight')]
        for x, y, code in cases:
            resp = self.go(x, y)
            self.assertEqual((resp.code, self.err(resp)), (400, code), (x, y))
        for bad in ('a', None, True, float('inf')):
            self.assertEqual(self.err(self.go(x=bad)), 'nav.err.goal_invalid')
        self.assertEqual(self.client.sent, [])

    def test_no_map_is_refused(self):
        self.run_nav()
        self.ctx.live_map.clear()
        resp = self.go()
        self.assertEqual((resp.code, self.err(resp)), (409, 'nav.err.no_map'))

    def test_no_action_server_and_rejected(self):
        self.run_nav()
        self.client.is_ready = False
        resp = self.go()
        self.assertEqual((resp.code, self.err(resp)), (503, 'nav.err.goal_no_server'))
        self.client.is_ready, self.client.accept = True, 'no'
        resp = self.go()
        self.assertEqual((resp.code, self.err(resp)), (409, 'nav.err.goal_rejected'))

    def test_second_goal_while_busy_is_refused(self):
        self.run_nav()
        self.assertEqual(self.go().code, 200)
        resp = self.go(2.0, 2.0)
        self.assertEqual((resp.code, self.err(resp)), (409, 'nav.err.goal_busy'))
        self.assertEqual(len(self.client.sent), 1)

    def test_status_is_public_and_follows_nav2(self):
        resp = self.fetch('/api/nav/goal')                                  # ไม่ต้อง login
        body = json.loads(resp.body)
        self.assertEqual((resp.code, body['running'], body['goal']['state']), (200, False, 'idle'))
        self.run_nav()
        self.go()
        self.client.listener.on_feedback({'distance_remaining': 2.5, 'eta': 9.0, 'recoveries': 1, 'elapsed': 3.0})
        body = json.loads(self.fetch('/api/nav/goal').body)
        self.assertEqual((body['running'], body['goal']['state'], body['goal']['feedback']['distance_remaining']),
                         (True, 'active', 2.5))
        self.client.listener.on_result('succeeded')
        self.assertEqual(json.loads(self.fetch('/api/nav/goal').body)['goal']['state'], 'succeeded')

    def test_cancel_cancels_goal_and_zeroes(self):
        self.run_nav()
        self.go()
        resp = self.post('/api/nav/goal/cancel', {}, headers=self.h)
        body = json.loads(resp.body)
        self.assertEqual((resp.code, body['ok'], body['goal']['state']), (200, True, 'canceling'))
        self.assertEqual((self.client.cancels, self.ctx.zero_calls), (1, 1))
        self.client.listener.on_result('canceled')
        self.assertEqual(json.loads(self.fetch('/api/nav/goal').body)['goal']['state'], 'canceled')

    def test_cancel_without_goal_still_zeroes(self):
        self.run_nav()
        self.assertEqual(self.post('/api/nav/goal/cancel', {}, headers=self.h).code, 200)
        self.assertEqual((self.client.cancels, self.ctx.zero_calls), (0, 1))

    def test_cancel_when_nav_not_running(self):
        resp = self.post('/api/nav/goal/cancel', {}, headers=self.h)
        self.assertEqual((resp.code, self.err(resp)), (409, 'nav.err.not_running'))
        self.assertEqual(self.ctx.zero_calls, 0)

    def test_action_server_vanishing_marks_goal_lost(self):
        self.run_nav()
        self.go()
        self.client.is_ready = False                                        # Nav2 ล่มกลางทาง (โหมดยังเป็น Navigation)
        goal = json.loads(self.fetch('/api/nav/goal').body)['goal']
        self.assertEqual((goal['state'], goal['reason']), ('failed', 'nav.goal.lost'))
        self.client.is_ready = True
        self.assertEqual(self.go().code, 200)                               # ไม่ค้าง: ส่งใหม่ได้

    def test_leaving_navigation_marks_goal_lost(self):
        self.run_nav()
        self.go()
        self.set_mode('STANDBY', self.token)
        self.wait_mode('STANDBY')
        goal = json.loads(self.fetch('/api/nav/goal').body)['goal']
        self.assertEqual((goal['state'], goal['reason']), ('failed', 'nav.goal.lost'))


class NavStartPoseApiTest(DriveBase):
    """จุดเริ่มต้นประจำแผนที่ + Refresh localization ผ่าน REST"""

    def setUp(self):
        super().setUp()
        _, self.token = self.login()
        self.h = self.cookie(self.token)
        self.ctx.live_map.update(200, 160, 0.05, -3.0, -2.0, 0.0, [0] * (200 * 160))
        self.ctx.nav_map_in_use = {'source': 'selected', 'name': 'lab', 'file': '/x/lab.yaml'}

    def run_nav(self):
        self.set_mode('NAVIGATION', self.token)
        self.wait_mode('NAVIGATION')

    def sp(self, action, headers=None):
        return self.post('/api/nav/startpose', {'action': action}, headers=self.h if headers is None else headers)

    def err(self, resp):
        return json.loads(resp.body)['error']['code']

    def test_get_is_public_and_empty_at_first(self):
        body = json.loads(self.fetch('/api/nav/startpose').body)
        self.assertEqual((body['pose'], body['auto']['state']), (None, 'idle'))

    def test_requires_admin_and_csrf(self):
        self.run_nav()
        self.assertEqual(self.post('/api/nav/startpose', {'action': 'save'}).code, 401)
        self.assertEqual(self.sp('save', headers=self.cookie('forged')).code, 401)
        resp = self.fetch('/api/nav/startpose', method='POST', body=json.dumps({'action': 'save'}), headers=self.h)
        self.assertEqual(resp.code, 403)
        self.assertEqual(self.post('/api/nav/refresh', {}).code, 401)
        self.assertEqual(self.ctx.refreshes, 0)

    def test_save_uses_current_pose_when_localized(self):
        self.run_nav()
        self.ctx.snapshot = lambda: {'mode': self.ctx.modes.snapshot(), 'localization': {'quality': 'good'},
                                     'pose': {'x': 1.5, 'y': 0.5, 'yaw_deg': 90.0}}
        resp = self.sp('save')
        body = json.loads(resp.body)
        self.assertEqual((resp.code, body['map']), (200, 'lab'))
        self.assertEqual((body['pose']['x'], body['pose']['y'], body['pose']['yaw_deg']), (1.5, 0.5, 90.0))
        self.assertEqual(json.loads(self.fetch('/api/nav/startpose').body)['pose']['x'], 1.5)

    def test_save_refused_when_not_localized_or_no_pose(self):
        self.run_nav()
        self.ctx.quality = 'bad'
        resp = self.sp('save')
        self.assertEqual((resp.code, self.err(resp)), (409, 'nav.err.goal_not_localized'))
        self.ctx.quality = 'good'
        self.ctx.snapshot = lambda: {'mode': self.ctx.modes.snapshot(), 'localization': {'quality': 'good'},
                                     'pose': None}
        self.assertEqual(self.sp('save').code, 409)
        self.assertIsNone(self.ctx.startposes.get('lab'))

    def test_save_and_apply_only_while_navigation_runs(self):
        self.assertEqual(self.err(self.sp('save')), 'nav.err.startpose_nomap')       # Standby + ไม่ได้เลือกแผนที่
        self.ctx.saver.save('lab')                                      # แผนที่ที่เลือกไว้ (ชื่อรู้ ไฟล์ครบ)
        self.ctx.selection.select('lab')
        self.ctx.startposes.set('lab', 1.0, 1.0, 0.0, self.ctx.live_map.meta())
        for action in ('save', 'apply'):
            resp = self.sp(action)                                                   # Standby
            self.assertEqual((resp.code, self.err(resp)), (409, 'nav.err.not_running'), action)
        self.set_mode('MANUAL', self.token)
        self.wait_mode('MANUAL')
        for action in ('save', 'apply'):
            resp = self.sp(action)
            self.assertEqual((resp.code, self.err(resp)), (409, 'nav.err.not_running'), action)
        self.assertEqual(self.sp('clear').code, 200)                                 # ลืมจุดได้แม้ Nav ไม่รัน
        self.assertEqual(self.ctx.pose_pub.sent, [])
        self.assertIsNone(self.ctx.startposes.get('lab'))

    def test_apply_sends_pose_and_refreshes(self):
        self.run_nav()
        self.ctx.startposes.set('lab', 1.0, 2.0, 0.5, self.ctx.live_map.meta())
        resp = self.sp('apply')
        self.assertEqual(resp.code, 200)
        self.assertEqual({(x, y, yaw) for x, y, yaw, _ in self.ctx.pose_pub.sent}, {(1.0, 2.0, 0.5)})
        self.assertEqual(self.ctx.refreshes, 2)
        self.assertEqual(json.loads(resp.body)['auto']['state'], 'applied')

    def test_apply_without_saved_pose(self):
        self.run_nav()
        resp = self.sp('apply')
        self.assertEqual((resp.code, self.err(resp)), (404, 'nav.err.startpose_none'))
        self.assertEqual(self.ctx.pose_pub.sent, [])

    def test_unnamed_map_has_no_start_pose(self):
        self.ctx.nav_map_in_use = {'source': 'default', 'name': None, 'file': None}
        self.run_nav()
        resp = self.sp('save')
        self.assertEqual((resp.code, self.err(resp)), (409, 'nav.err.startpose_nomap'))
        self.assertEqual(json.loads(self.fetch('/api/nav/startpose').body)['map'], None)

    def test_clear_and_bad_action(self):
        self.run_nav()
        self.ctx.startposes.set('lab', 1.0, 2.0, 0.5, self.ctx.live_map.meta())
        self.assertEqual(self.sp('clear').code, 200)
        self.assertIsNone(self.ctx.startposes.get('lab'))
        self.assertEqual(self.sp('explode').code, 400)

    def test_autostart_applies_saved_pose_when_navigation_runs(self):
        self.ctx.startposes.set('lab', 1.0, 2.0, 0.5, self.ctx.live_map.meta())
        self.run_nav()
        self.ctx.autostart.tick(self.ctx.modes.snapshot())
        self.ctx.autostart.thread.join(5)
        self.assertEqual(self.ctx.autostart.status()['state'], 'applied')
        self.assertEqual(len(self.ctx.pose_pub.sent), 3)
        self.assertEqual(json.loads(self.fetch('/api/nav/startpose').body)['auto']['state'], 'applied')

    def test_refresh_button(self):
        resp = self.post('/api/nav/refresh', {}, headers=self.h)
        self.assertEqual((resp.code, self.err(resp)), (409, 'nav.err.not_running'))
        self.run_nav()
        self.assertEqual(self.post('/api/nav/refresh', {}, headers=self.h).code, 200)
        self.assertEqual(self.ctx.refreshes, 1)
        self.ctx.refresh_ok = False
        resp = self.post('/api/nav/refresh', {}, headers=self.h)
        self.assertEqual((resp.code, self.err(resp)), (503, 'nav.err.refresh_failed'))


class RoutesBase(NavGoalSetup):
    """โครงร่วมของเทสต์ Route (ไม่มีเทสต์ในตัว) : แผนที่ 'lab' 10 × 8 m ที่บันทึกไว้และถูกเลือกไว้ ผนังที่ x = 6.5"""

    def write_map(self, name, wall_col=190, unknown_from_row=None):
        px = np.full((160, 200), 254, dtype=np.uint8)                # 10 × 8 m ที่ (-3, -2) ว่างทั้งหมด
        px[:, wall_col] = 0                                          # ผนัง (ที่ 190 → x = 6.5)
        if unknown_from_row is not None:
            px[unknown_from_row:, :100] = 205                        # ยังไม่เคยสำรวจ (ซ้ายล่างของภาพ: x < 2.0, y < 0.0)
        os.makedirs(self.ctx.saver.maps_dir, exist_ok=True)
        Image.fromarray(px, mode='L').save(self.ctx.saver.path(name, '.pgm'))
        with open(self.ctx.saver.path(name, '.yaml'), 'w') as f:
            f.write(f'image: {name}.pgm\nresolution: 0.05\norigin: [-3.0, -2.0, 0.0]\nnegate: 0\n'
                    'occupied_thresh: 0.65\nfree_thresh: 0.25\n')

    def setUp(self):
        super().setUp()
        self.write_map('lab')
        self.ctx.selection.select('lab')                             # แผนที่ที่เลือกไว้ (Navigation ยังไม่ได้รัน)

    def body(self, name='R', points=None, **extra):
        pts = [{'name': 'A', 'x': 1.0, 'y': 1.0, 'yaw': 0.0, 'wait': 5},
               {'name': 'B', 'x': 3.0, 'y': 2.0, 'yaw': 1.5, 'wait': 0}] if points is None else points
        return dict({'name': name, 'loop': 1, 'points': pts}, **extra)

    def save(self, headers=None, **kw):
        return self.post('/api/routes', self.body(**kw), headers=self.h if headers is None else headers)


class RoutesApiTest(RoutesBase):
    """REST Route: ดูได้ทุกคน · แก้/ลบ = Admin + CSRF · ใช้ได้โดยไม่ต้องรัน Navigation ·
    ตรวจทุกจุดกับ "ไฟล์แผนที่ที่บันทึกไว้" (เหมือนที่ Nav2 โหลด) ไม่ใช่แผนที่สดที่บังเอิญโหลดอยู่"""

    def test_get_is_public_and_empty_at_first(self):
        body = json.loads(self.fetch('/api/routes').body)
        self.assertEqual((body['map'], body['routes'], body['running'], body['map_ready']), ('lab', {}, False, True))
        self.assertEqual(body['limits']['clearance'], 0.40)

    def test_requires_admin_and_csrf(self):
        self.assertEqual(self.post('/api/routes', self.body()).code, 401)
        self.assertEqual(self.save(headers=self.cookie('forged')).code, 401)
        resp = self.fetch('/api/routes', method='POST', body=json.dumps(self.body()), headers=self.h)
        self.assertEqual(resp.code, 403)
        self.assertEqual(self.post('/api/routes/delete', {'name': 'R'}).code, 401)
        self.assertEqual(self.ctx.routes.routes('lab'), {})

    def test_save_works_without_navigation_running(self):
        resp = self.save()
        body = json.loads(resp.body)
        self.assertEqual((resp.code, body['running'], sorted(body['routes'])), (200, False, ['R']))
        self.assertEqual(self.ctx.routes.get('lab', 'R')['points'][1]['name'], 'B')

    def test_save_list_update_rename_delete(self):
        resp = self.save()
        body = json.loads(resp.body)
        self.assertEqual((resp.code, body['map'], sorted(body['routes'])), (200, 'lab', ['R']))
        self.assertEqual(self.err(self.save()), 'route.err.exists')                    # สร้างใหม่ชื่อซ้ำ
        self.assertEqual(self.save(previous='R', loop=3).code, 200)                     # แก้ตัวเอง
        self.assertEqual(json.loads(self.fetch('/api/routes').body)['routes']['R']['loop'], 3)
        self.assertEqual(self.save(name='R2', previous='R').code, 200)                  # เปลี่ยนชื่อ
        self.assertEqual(sorted(json.loads(self.fetch('/api/routes').body)['routes']), ['R2'])
        resp = self.post('/api/routes/delete', {'name': 'R2'}, headers=self.h)
        self.assertEqual((resp.code, json.loads(resp.body)['routes']), (200, {}))
        resp = self.post('/api/routes/delete', {'name': 'R2'}, headers=self.h)
        self.assertEqual((resp.code, self.err(resp)), (404, 'route.err.missing'))

    def test_auto_heading_flag_is_stored_returned_and_validated(self):
        pts = [{'name': 'A', 'x': 1.0, 'y': 1.0, 'yaw': 0.0, 'auto': True},
               {'name': 'B', 'x': 3.0, 'y': 2.0, 'yaw': 0.5}]
        self.assertEqual(self.save(points=pts).code, 200)
        got = json.loads(self.fetch('/api/routes').body)['routes']['R']['points']
        self.assertEqual([p['auto'] for p in got], [True, False])           # ไม่ระบุ = ตั้งเอง
        bad = [dict(pts[0], auto='yes'), pts[1]]
        resp = self.save(name='R2', points=bad)
        self.assertEqual((resp.code, self.err(resp)), (400, 'route.err.point_invalid'))
        self.assertEqual(json.loads(resp.body)['error']['params']['n'], 1)

    def test_every_point_is_checked_against_the_saved_map_file(self):
        self.write_map('lab', unknown_from_row=120)                  # แถวภาพ 120 ลงไป (y < 0, x < 2) ยังไม่เคยสำรวจ
        for x, y, code in ((6.45, 1.0, 'route.err.point_tight'),         # ติดผนัง x = 6.5
                           (6.5, 1.0, 'route.err.point_unknown'),         # บนผนัง
                           (0.0, -1.0, 'route.err.point_unknown'),        # พื้นที่ที่ยังไม่เคยสำรวจ (เทา 205)
                           (50.0, 1.0, 'route.err.point_outside')):
            pts = [{'name': 'ok', 'x': 1.0, 'y': 1.0, 'yaw': 0.0}, {'name': 'bad', 'x': x, 'y': y, 'yaw': 0.0}]
            resp = self.save(points=pts)
            self.assertEqual((resp.code, self.err(resp)), (400, code), (x, y))
            self.assertEqual(json.loads(resp.body)['error']['params']['n'], 2)
        self.assertEqual(self.ctx.routes.routes('lab'), {})                # ไม่มีอะไรถูกบันทึกครึ่งๆ กลางๆ

    def test_live_map_of_another_mode_does_not_decide(self):
        # แผนที่สดที่ "เต็มไปด้วยผนัง"
        self.ctx.live_map.update(200, 160, 0.05, -3.0, -2.0, 0.0, [100] * (200 * 160))
        self.assertEqual(self.save().code, 200)                      # ไม่เกี่ยว: ตรวจกับไฟล์ที่บันทึกไว้เท่านั้น

    def test_changed_map_file_is_picked_up(self):
        self.assertEqual(self.save().code, 200)
        self.write_map('lab', wall_col=100)                          # ผนังใหม่ที่ x = -3 + 100 × 0.05 = 2.0
        os.utime(self.ctx.saver.path('lab', '.pgm'), (1, 1))
        resp = self.save(name='R2', points=[{'name': 'w', 'x': 2.0, 'y': 1.0, 'yaw': 0.0}])
        self.assertEqual((resp.code, self.err(resp)), (400, 'route.err.point_unknown'))

    def test_unreadable_map_file_refuses_saving(self):
        with open(self.ctx.saver.path('lab', '.yaml'), 'w') as f:
            f.write('not a map')
        body = json.loads(self.fetch('/api/routes').body)
        self.assertEqual((body['map'], body['map_ready']), ('lab', False))
        resp = self.save()
        self.assertEqual((resp.code, self.err(resp)), (409, 'route.err.no_map'))

    def test_map_without_a_name_cannot_hold_routes(self):
        self.ctx.selection.select(None)
        body = json.loads(self.fetch('/api/routes').body)
        self.assertEqual((body['map'], body['map_ready']), (None, False))
        resp = self.save()
        self.assertEqual((resp.code, self.err(resp)), (409, 'route.err.nomap'))

    def test_bad_bodies_are_400(self):
        for bad in (self.body(name=''), self.body(points=[]), self.body(loop=-1), self.body(points=['x'])):
            self.assertEqual(self.post('/api/routes', bad, headers=self.h).code, 400)
        self.assertEqual(self.post('/api/routes', None, headers=self.h, raw='[1]').code, 400)
        self.assertEqual(self.ctx.routes.routes('lab'), {})

    def test_routes_follow_the_map_navigation_uses_when_it_runs(self):
        self.save()
        self.write_map('hall')
        self.ctx.nav_map_in_use = {'source': 'selected', 'name': 'hall', 'file': self.ctx.saver.path('hall')}
        self.run_nav()
        body = json.loads(self.fetch('/api/routes').body)
        self.assertEqual((body['map'], body['routes'], body['running']), ('hall', {}, True))
        self.assertEqual(self.save(name='H').code, 200)
        self.assertEqual(sorted(self.ctx.routes.routes('lab')), ['R'])
        self.assertEqual(sorted(self.ctx.routes.routes('hall')), ['H'])

    def test_saved_map_endpoints(self):
        meta = json.loads(self.fetch('/api/routes/map').body)
        self.assertEqual((meta['available'], meta['map'], meta['width'], meta['height'], meta['resolution']),
                         (True, 'lab', 200, 160, 0.05))
        self.assertEqual((meta['origin']['x'], meta['origin']['y']), (-3.0, -2.0))
        self.assertEqual(meta['size_m'], {'w': 10.0, 'h': 8.0})
        png = self.fetch('/api/routes/map.png')
        self.assertEqual((png.code, png.headers['Content-Type']), (200, 'image/png'))
        self.assertTrue(png.body.startswith(b'\x89PNG'))
        self.write_map('other')
        self.ctx.selection.select('other')
        meta2 = json.loads(self.fetch('/api/routes/map').body)
        self.assertEqual(meta2['map'], 'other')
        self.assertNotEqual(meta2['version'], meta['version'])        # ต่างแผนที่ → version ไม่ซ้ำ (เว็บโหลดภาพใหม่)
        self.ctx.selection.select(None)
        self.assertEqual(json.loads(self.fetch('/api/routes/map').body)['available'], False)
        self.assertEqual(self.fetch('/api/routes/map.png').code, 404)

    def test_routes_are_per_map(self):
        self.save()
        self.write_map('hall')
        self.ctx.selection.select('hall')
        self.assertEqual(json.loads(self.fetch('/api/routes').body)['routes'], {})
        self.assertEqual(sorted(self.ctx.routes.routes('lab')), ['R'])


class RouteStraightApiTest(RoutesBase):
    """REST Route โหมด straight : บันทึกโหมด · ตรวจทุกช่วงก่อนปล่อยหุ่น · ส่งเส้นตรงให้ตัวควบคุม ไม่ผ่าน planner"""

    A, B, C = (1.0, 1.0, 0.0), (3.0, 1.0, 1.5708), (3.0, 3.0, 0.0)

    def setUp(self):
        super().setUp()
        self.ctx.nav_map_in_use = {'source': 'selected', 'name': 'lab', 'file': self.ctx.saver.path('lab')}
        self.run_nav()
        self.ctx.robot_pose = self.A

    def pts(self, *points, **over):
        return [dict({'name': n, 'x': p[0], 'y': p[1], 'yaw': p[2], 'wait': 0}, **over) for n, p in zip('ABC', points)]

    def save_route(self, name='S', mode='straight', points=None, **extra):
        return self.save(name=name, points=points or self.pts(self.A, self.B, self.C), mode=mode, **extra)

    def go(self, name='S', **extra):
        return self.post('/api/routes/run', dict({'name': name, 'confirmed': True}, **extra), headers=self.h)

    def arrive(self, pose):
        self.ctx.robot_pose = pose
        self.client.listener.on_result('succeeded')
        return self.ctx.runner.tick()

    def test_mode_is_saved_listed_and_defaults_to_navigate(self):
        self.assertEqual(self.save_route().code, 200)
        self.assertEqual(self.save_route(name='N', mode='navigate').code, 200)
        resp = self.save_route(name='X', mode=None)                      # null ตรงๆ ไม่ใช่ "ไม่ระบุ"
        self.assertEqual((resp.code, self.err(resp)), (400, 'route.err.mode'))
        routes = json.loads(self.fetch('/api/routes').body)['routes']
        self.assertEqual((routes['S']['mode'], routes['N']['mode']), ('straight', 'navigate'))
        resp = self.post('/api/routes', {'name': 'D', 'loop': 1, 'points': self.pts(self.A)}, headers=self.h)    # ไม่ส่ง mode เลย
        self.assertEqual(json.loads(resp.body)['routes']['D']['mode'], 'navigate')

    def test_straight_route_rules_at_save(self):
        resp = self.save_route(mode='curvy')
        self.assertEqual((resp.code, self.err(resp)), (400, 'route.err.mode'))
        resp = self.save_route(points=self.pts(self.A))
        self.assertEqual((resp.code, self.err(resp)), (400, 'route.err.straight_points'))
        resp = self.save_route(points=self.pts(self.A, (1.1, 1.0, 0.0)))
        self.assertEqual((resp.code, self.err(resp)), (400, 'route.err.segment_short'))
        self.assertEqual(json.loads(self.fetch('/api/routes').body)['routes'], {})

    def test_runs_every_segment_as_a_straight_line(self):
        self.save_route()
        resp = self.go()
        body = json.loads(resp.body)
        self.assertEqual((resp.code, body['run']['state'], body['run']['mode'], body['run']['index']), (200, 'running', 'straight', 1))
        first = self.client.lines[0]
        self.assertEqual((first[0][:2], first[-1][:2]), ((1.0, 1.0), (3.0, 1.0)))
        self.assertTrue(all(p[1] == 1.0 for p in first))
        self.assertEqual(self.client.sent, [], 'ต้องไม่ผ่าน NavigateToPose')
        self.arrive(self.B)
        second = self.client.lines[1]
        self.assertEqual((second[0][:2], second[-1][:2]), ((3.0, 1.0), (3.0, 3.0)))
        self.arrive(self.C)
        self.assertEqual(json.loads(self.fetch('/api/routes/run').body)['run']['state'], 'succeeded')

    def test_robot_must_be_at_the_first_point(self):
        self.save_route()
        self.ctx.robot_pose = (2.0, 1.0, 0.0)
        resp = self.go()
        self.assertEqual((resp.code, self.err(resp)), (409, 'nav.err.line_start_far'))
        self.ctx.robot_pose = (1.0, 1.0, 1.5)
        resp = self.go()
        self.assertEqual((resp.code, self.err(resp)), (409, 'nav.err.line_heading'))
        self.assertEqual((self.client.lines, json.loads(self.fetch('/api/routes/run').body)['run']['state']), ([], 'idle'))

    def test_point_heading_must_face_the_next_segment(self):
        self.save_route(points=self.pts(self.A, (3.0, 1.0, 0.0), self.C))
        resp = self.go()
        self.assertEqual((resp.code, self.err(resp)), (409, 'route.run.straight_yaw'))
        self.assertEqual(json.loads(resp.body)['error']['params']['n'], 2)
        self.assertEqual(self.client.lines, [])

    def test_blocked_segment_is_refused_before_anything_moves(self):
        self.save_route()
        grid = [0] * (200 * 160)
        for row in range(160):
            grid[row * 200 + 190] = 100
        for row in range(75, 86):                                   # สิ่งกีดขวางที่ (3.0, 2.0) กลางช่วง B → C
            for col in range(115, 126):
                grid[row * 200 + col] = 100
        self.ctx.live_map.update(200, 160, 0.05, -3.0, -2.0, 0.0, grid)
        resp = self.go()
        body = json.loads(resp.body)
        self.assertEqual((resp.code, self.err(resp), body['error']['params']['n']), (409, 'route.run.straight_blocked', 3))
        self.assertEqual(self.client.lines, [])

    def test_pass_through_point_is_saved_and_driven_as_one_rounded_path(self):
        pts = self.pts(self.A, (3.0, 1.0, 1.5708), (3.0, 4.0, 0.0))
        pts[1]['stop'] = False
        resp = self.save_route(points=pts)
        self.assertEqual(resp.code, 200)
        saved = json.loads(self.fetch('/api/routes').body)['routes']['S']['points']
        self.assertEqual([p['stop'] for p in saved], [True, False, True])
        resp = self.go()
        body = json.loads(resp.body)
        self.assertEqual((resp.code, body['run']['index'], body['run']['current']['name']), (200, 2, 'C'))
        self.assertEqual(len(self.client.lines), 1)                                     # จุด B ไม่ใช่จุดหยุด : ส่งเส้นเดียวไปถึง C
        path = self.client.lines[0]
        self.assertEqual((path[0][:2], path[-1][:2]), ((1.0, 1.0), (3.0, 4.0)))
        self.assertGreater(min(((x - 3.0) ** 2 + (y - 1.0) ** 2) ** 0.5 for x, y, _ in path), 0.3)   # โค้งมนที่ B
        self.arrive((3.0, 4.0, 0.0))
        self.assertEqual(json.loads(self.fetch('/api/routes/run').body)['run']['state'], 'succeeded')

    def test_pass_through_only_in_straight_mode_and_corner_must_be_wide_enough(self):
        pts = self.pts(self.A, (3.0, 1.0, 0.0), (3.0, 3.0, 0.0))
        pts[1]['stop'] = False
        resp = self.save_route(name='N', mode='navigate', points=pts)
        self.assertEqual((resp.code, self.err(resp)), (400, 'route.err.pass_navigate'))
        tight = self.pts((1.0, 1.0, 0.0), (1.8, 1.0, 1.5708), (1.8, 1.8, 0.0))
        tight[1]['stop'] = False
        self.assertEqual(self.save_route(name='T', points=tight).code, 200)
        self.ctx.robot_pose = (1.0, 1.0, 0.0)
        resp = self.go(name='T')
        body = json.loads(resp.body)
        self.assertEqual((resp.code, self.err(resp), body['error']['params']['n']), (409, 'route.run.straight_corner', 2))
        self.assertEqual(self.client.lines, [])

    def test_stop_cancels_the_line_and_navigate_routes_still_work(self):
        self.save_route()
        self.go()
        self.assertEqual(self.post('/api/routes/run/stop', {}, headers=self.h).code, 200)
        self.assertGreaterEqual(self.client.cancels, 1)
        self.assertEqual(json.loads(self.fetch('/api/routes/run').body)['run']['state'], 'canceled')
        self.client.listener.on_result('canceled')
        self.save_route(name='N', mode='navigate')
        resp = self.go(name='N')
        self.assertEqual((resp.code, len(self.client.sent)), (200, 1))                  # โหมดเดิมยังไปผ่าน NavigateToPose


class RouteRunApiTest(RoutesBase):
    """REST รัน Route: เซิร์ฟเวอร์บังคับกติกา · ■ / Cancel trip ยกเลิกทั้ง Route · พัก/ต่อ · ไม่ให้สั่ง goal ซ้อน"""

    def setUp(self):
        super().setUp()
        self.ctx.nav_map_in_use = {'source': 'selected', 'name': 'lab', 'file': self.ctx.saver.path('lab')}
        self.run_nav()
        self.assertEqual(self.save(name='R', points=[
            {'name': 'A', 'x': 1.0, 'y': 1.0, 'yaw': 0.0, 'wait': 0},
            {'name': 'B', 'x': 3.0, 'y': 2.0, 'yaw': 1.5, 'wait': 2},
            {'name': 'C', 'x': 4.0, 'y': 1.0, 'yaw': 0.0, 'wait': 0}]).code, 200)

    def go(self, name='R', headers=None, **extra):
        body = dict({'name': name, 'confirmed': True}, **extra)
        return self.post('/api/routes/run', body, headers=self.h if headers is None else headers)

    def ctl(self, action):
        return self.post(f'/api/routes/run/{action}', {}, headers=self.h)

    def state(self):
        return json.loads(self.fetch('/api/routes/run').body)['run']['state']

    def arrive(self):
        self.client.listener.on_result('succeeded')
        return self.ctx.runner.tick()

    def test_get_is_public_and_idle_at_first(self):
        body = json.loads(self.fetch('/api/routes/run').body)
        self.assertEqual((body['run']['state'], body['running'], body['goal']['state']), ('idle', True, 'idle'))

    def test_requires_admin_csrf_and_confirmed(self):
        self.assertEqual(self.post('/api/routes/run', {'name': 'R', 'confirmed': True}).code, 401)
        self.assertEqual(self.go(headers=self.cookie('forged')).code, 401)
        resp = self.fetch('/api/routes/run', method='POST', headers=self.h,
                          body=json.dumps({'name': 'R', 'confirmed': True}))
        self.assertEqual(resp.code, 403)
        for ctl in ('pause', 'resume', 'stop'):
            self.assertEqual(self.post(f'/api/routes/run/{ctl}', {}).code, 401, ctl)
        for bad in (False, None, 1, 'true'):
            resp = self.post('/api/routes/run', {'name': 'R', 'confirmed': bad}, headers=self.h)
            self.assertEqual((resp.code, self.err(resp)), (409, 'route.run.confirm_required'), bad)
        self.assertEqual(self.client.sent, [])
        self.assertEqual(self.state(), 'idle')

    def test_only_while_navigation_runs(self):
        self.set_mode('STANDBY', self.token)
        self.wait_mode('STANDBY')
        resp = self.go()
        self.assertEqual((resp.code, self.err(resp)), (409, 'nav.err.not_running'))
        self.assertEqual(self.client.sent, [])

    def test_unknown_route_and_missing_body(self):
        resp = self.go(name='nope')
        self.assertEqual((resp.code, self.err(resp)), (404, 'route.err.missing'))
        resp = self.post('/api/routes/run', {'confirmed': True}, headers=self.h)
        self.assertEqual(resp.code, 404)
        self.assertEqual(self.client.sent, [])

    def test_not_localized_sends_nothing(self):
        self.ctx.quality = 'bad'
        resp = self.go()
        self.assertEqual((resp.code, self.err(resp)), (409, 'route.run.not_localized'))
        self.assertEqual(self.client.sent, [])

    def test_uncertain_position_waits_when_the_server_allows_it(self):
        self.ctx.runner._loc_grace = 15.0
        self.ctx.quality = 'bad'
        resp = self.go()
        body = json.loads(resp.body)
        self.assertEqual((resp.code, body['run']['state'], body['run']['phase']), (200, 'running', 'locating'))
        self.assertEqual((body['run']['loc_grace'], body['run']['loc_left']), (15.0, 15.0))
        self.assertEqual(self.client.sent, [], 'ตำแหน่งไม่น่าเชื่อถือ = ห้ามส่ง goal')
        self.assertTrue(self.ctx.runner.tick())
        self.assertEqual(self.client.sent, [])
        self.ctx.quality = 'good'                                            # AMCL ฟื้น
        self.assertTrue(self.ctx.runner.tick())
        self.assertEqual(len(self.client.sent), 1)
        self.assertEqual(json.loads(self.fetch('/api/routes/run').body)['run']['phase'], 'going')

    def test_every_point_is_rechecked_against_the_current_map_file_before_moving(self):
        self.write_map('lab', wall_col=126)                          # ผนังใหม่ที่ x = 3.3 : จุด B (3.0) ห่าง 0.3 < 0.4
        os.utime(self.ctx.saver.path('lab', '.pgm'), (1, 1))
        resp = self.go()
        self.assertEqual(resp.code, 400)
        self.assertEqual(self.err(resp), 'route.err.point_tight')
        self.assertEqual(json.loads(resp.body)['error']['params']['n'], 2)
        self.assertEqual(self.client.sent, [], 'จุดที่ 2 ไม่ผ่าน = ไม่ปล่อยหุ่นออกเลย แม้จุดแรกจะผ่าน')
        self.assertEqual(self.state(), 'idle')

    def test_unreadable_map_file_refuses_to_run(self):
        with open(self.ctx.saver.path('lab', '.yaml'), 'w') as f:
            f.write('not a map')
        resp = self.go()
        self.assertEqual((resp.code, self.err(resp)), (409, 'route.err.no_map'))
        self.assertEqual(self.client.sent, [])

    def test_runs_the_route_point_by_point_and_counts_the_round(self):
        resp = self.go()
        body = json.loads(resp.body)
        self.assertEqual((resp.code, body['run']['state']), (200, 'running'))
        self.assertEqual((body['run']['route'], body['run']['total']), ('R', 3))
        self.assertEqual(len(self.client.sent), 1)
        self.assertTrue(self.arrive())                                       # ถึง A → B
        self.assertTrue(self.arrive())                                       # ถึง B → รอ 2 วินาที
        run = json.loads(self.fetch('/api/routes/run').body)['run']
        self.assertEqual((run['phase'], run['index']), ('waiting', 1))
        self.assertEqual(len(self.client.sent), 2)
        self.ctx.runner._clock = lambda: time.monotonic() + 10               # พ้นเวลารอ
        self.assertTrue(self.ctx.runner.tick())
        self.assertEqual(len(self.client.sent), 3)
        self.assertFalse(self.arrive())
        self.assertEqual((self.state(), self.ctx.rounds_done), ('succeeded', 1))

    def test_no_manual_goal_straight_or_pose_while_running_but_allowed_after(self):
        self.go()
        resp = self.go_manual()
        self.assertEqual((resp.code, self.err(resp)), (409, 'route.run.active'))
        resp = self.post('/api/nav/goal/straight', {'distance': 2.0, 'confirmed': True}, headers=self.h)
        self.assertEqual((resp.code, self.err(resp)), (409, 'route.run.active'))
        resp = self.post('/api/nav/initialpose', {'x': 1.0, 'y': 1.0, 'yaw': 0.0}, headers=self.h)
        self.assertEqual((resp.code, self.err(resp)), (409, 'route.run.active'))
        check = self.post('/api/nav/goal/straight/check', {'distance': 2.0}, headers=self.h)
        self.assertEqual(check.code, 200)                                    # ตรวจล่วงหน้า (ไม่ส่ง) ทำได้
        self.assertEqual(self.ctl('stop').code, 200)
        self.client.listener.on_result('canceled')
        self.assertEqual(self.go_manual().code, 200)

    def go_manual(self):
        return self.post('/api/nav/goal', {'x': 1.0, 'y': 1.0, 'yaw': 0.5, 'confirmed': True}, headers=self.h)

    def test_second_route_or_goal_in_progress_is_refused(self):
        self.go()
        resp = self.go()
        self.assertEqual((resp.code, self.err(resp)), (409, 'route.run.busy'))
        self.assertEqual(len(self.client.sent), 1)

    def test_single_goal_in_progress_blocks_starting_a_route(self):
        self.assertEqual(self.go_manual().code, 200)
        resp = self.go()
        self.assertEqual((resp.code, self.err(resp)), (409, 'route.run.goal_busy'))

    def test_forever_needs_the_extra_confirmation(self):
        body = json.loads(self.fetch('/api/routes').body)['routes']['R']
        self.assertEqual(self.save(name='R', previous='R', loop=0, points=body['points']).code, 200)
        resp = self.go()
        self.assertEqual((resp.code, self.err(resp)), (409, 'route.run.forever_confirm_required'))
        self.assertEqual(self.client.sent, [])
        self.assertEqual(self.go(forever_confirmed=True).code, 200)

    def test_stop_cancels_the_whole_route_and_the_goal(self):
        self.go()
        zero0 = self.ctx.zero_calls
        resp = self.ctl('stop')
        self.assertEqual((resp.code, json.loads(resp.body)['run']['state']), (200, 'canceled'))
        self.assertEqual((self.client.cancels >= 1, self.ctx.zero_calls > zero0), (True, True))
        self.client.listener.on_result('succeeded')                          # ผลของ goal เก่ามาช้า
        self.assertFalse(self.ctx.runner.tick())
        self.assertEqual(len(self.client.sent), 1, 'หยุดแล้วห้ามส่งจุดถัดไป')

    def test_top_bar_stop_and_cancel_trip_cancel_the_route_too(self):
        for path in ('/api/drive/stop', '/api/nav/goal/cancel'):
            self.go()
            self.assertEqual(self.post(path, {}, headers=self.h).code, 200, path)
            self.assertEqual(self.state(), 'canceled', path)
            self.client.listener.on_result('canceled')

    def test_stop_with_no_route_is_harmless(self):
        zero0 = self.ctx.zero_calls
        resp = self.ctl('stop')
        self.assertEqual((resp.code, json.loads(resp.body)['run']['state']), (200, 'idle'))
        self.assertGreater(self.ctx.zero_calls, zero0)                       # ยังส่งศูนย์ (เหมือน Cancel trip เดิม)

    def test_pause_and_resume(self):
        self.go()
        resp = self.ctl('pause')
        run = json.loads(resp.body)['run']
        self.assertEqual((resp.code, run['state'], run['pause_settled']), (200, 'paused', False))
        resp = self.ctl('resume')                                            # goal เก่ายังไม่หยุดสนิท
        self.assertEqual((resp.code, self.err(resp)), (409, 'route.run.pausing'))
        self.client.listener.on_result('canceled')
        self.assertTrue(json.loads(self.fetch('/api/routes/run').body)['run']['pause_settled'])
        self.ctx.quality = 'bad'
        resp = self.ctl('resume')
        self.assertEqual((resp.code, self.err(resp)), (409, 'route.run.not_localized'))
        self.assertEqual(self.state(), 'paused')
        self.ctx.quality = 'good'
        resp = self.ctl('resume')
        self.assertEqual((resp.code, json.loads(resp.body)['run']['state']), (200, 'running'))
        self.assertEqual(self.client.sent[-1][:2], self.client.sent[0][:2], 'ต่อ = ส่งจุดเดิมซ้ำ')
        self.assertEqual(self.ctl('resume').code, 409)                       # ไม่ได้พักอยู่
        self.assertEqual(self.ctl('pause').code, 200)
        self.assertEqual(self.ctl('pause').code, 409)                        # พักอยู่แล้ว

    def test_failure_is_reported_in_status_and_stops_everything(self):
        self.go()
        self.client.listener.on_result('failed')
        self.ctx.runner.tick()
        run = json.loads(self.fetch('/api/routes/run').body)['run']
        self.assertEqual((run['state'], run['reason']['code']), ('failed', 'nav.goal.aborted'))
        self.assertEqual(len(self.client.sent), 1)

    def test_leaving_navigation_fails_the_route(self):
        self.go()
        self.set_mode('STANDBY', self.token)
        self.wait_mode('STANDBY')
        self.assertFalse(self.ctx.runner.tick())
        self.assertEqual(self.state(), 'failed')


class NavLineApiTest(NavGoalSetup):
    """วิ่งตรงตามเส้นผ่าน REST : ข้าม planner (FollowPath) · กติกาเดียวกับ goal ปกติ · หุ่นต้องอยู่ที่จุดเริ่มและหันตรงแนว"""

    def line(self, start=None, to=(3.0, 0.0, 0.0), headers=None, check=False, **extra):
        body = {'from': start, 'to': {'x': to[0], 'y': to[1], 'yaw': to[2]}, 'confirmed': True}
        body.update(extra)
        path = '/api/nav/line/check' if check else '/api/nav/line'
        return self.post(path, body, headers=self.h if headers is None else headers)

    def set_pose(self, x=0.0, y=0.0, yaw_deg=0.0, quality='good'):
        self.ctx.snapshot = lambda: {'mode': self.ctx.modes.snapshot(), 'localization': {'quality': quality},
                                     'pose': None if x is None else {'x': x, 'y': y, 'yaw_deg': yaw_deg}}

    def test_requires_admin_csrf_and_running_navigation(self):
        for check in (False, True):
            self.assertEqual(self.line(headers={}, check=check).code, 401)
            self.assertEqual(self.line(headers=self.cookie('forged'), check=check).code, 401)
            resp = self.fetch('/api/nav/line' + ('/check' if check else ''), method='POST',
                              body=json.dumps({'to': {'x': 3, 'y': 0, 'yaw': 0}}), headers=self.h)
            self.assertEqual(resp.code, 403)
            resp = self.line(check=check)                                                # Standby
            self.assertEqual((resp.code, self.err(resp)), (409, 'nav.err.not_running'))
        self.assertEqual((self.client.lines, self.client.sent), ([], []))

    def test_sends_a_straight_path_from_the_robot_position(self):
        self.run_nav()
        self.set_pose(1.0, 1.0, 90.0)
        resp = self.line(to=(1.0, 4.0, 0.3))
        body = json.loads(resp.body)
        self.assertEqual((resp.code, body['goal']['state'], body['goal']['line'], body['check']['ok']),
                         (200, 'active', {'x0': 1.0, 'y0': 1.0}, True))
        pts = self.client.lines[0]
        self.assertTrue(all(abs(p[0] - 1.0) < 1e-9 for p in pts))                        # ตรงแนว x = 1 ตลอดทาง
        self.assertEqual((pts[0][:2], pts[-1]), ((1.0, 1.0), (1.0, 4.0, 0.3)))
        self.assertEqual(self.client.sent, [], 'ต้องไม่ผ่าน NavigateToPose')

    def test_explicit_start_point(self):
        self.run_nav()
        self.set_pose(0.1, 0.0, 0.0)
        resp = self.line(start={'x': 0.0, 'y': 0.0}, to=(2.0, 0.0, 0.0))
        self.assertEqual(resp.code, 200)
        self.assertEqual(self.client.lines[0][0][:2], (0.0, 0.0))

    def test_robot_must_be_at_the_start_and_face_the_line(self):
        self.run_nav()
        self.set_pose(2.0, 0.0, 0.0)
        resp = self.line(start={'x': 0.0, 'y': 0.0})
        self.assertEqual((resp.code, self.err(resp)), (409, 'nav.err.line_start_far'))
        self.assertAlmostEqual(json.loads(resp.body)['error']['params']['found'], 2.0)
        self.set_pose(0.0, 0.0, 90.0)
        resp = self.line()
        self.assertEqual((resp.code, self.err(resp)), (409, 'nav.err.line_heading'))
        self.assertEqual(self.client.lines, [])

    def test_check_is_a_dry_run(self):
        self.run_nav()
        self.set_pose(0.0, 0.0, 90.0)
        body = json.loads(self.line(check=True, confirmed=None).body)                   # ไม่ต้องมี confirmed
        self.assertEqual((body['ok'], body['check']['ok'], body['check']['start']['ok']), (True, False, False))
        self.assertAlmostEqual(body['check']['start']['heading_deg'], 90.0)
        self.assertEqual((self.client.lines, self.ctx.goal.status()['state']), ([], 'idle'))

    def test_blocked_line_and_confirmation_and_localization(self):
        self.run_nav()
        resp = self.line(to=(8.0, 0.0, 0.0))                                            # ผนังที่ x = 6.5
        self.assertEqual((resp.code, self.err(resp)), (409, 'nav.err.straight_tight'))
        resp = self.line(confirmed=None)
        self.assertEqual((resp.code, self.err(resp)), (409, 'nav.err.goal_confirm_required'))
        self.set_pose(quality='bad')
        resp = self.line()
        self.assertEqual((resp.code, self.err(resp)), (409, 'nav.err.goal_not_localized'))
        self.assertEqual(self.client.lines, [])

    def test_bad_bodies_are_refused(self):
        self.run_nav()
        for body in ({}, {'to': 5}, {'to': {'x': 3, 'y': 0}}, {'to': {'x': 3, 'y': 0, 'yaw': 0}, 'from': 7},
                     {'to': {'x': 3, 'y': 0, 'yaw': 0}, 'from': {'x': 'a', 'y': 0}}):
            resp = self.post('/api/nav/line', dict(body, confirmed=True), headers=self.h)
            self.assertEqual((resp.code, self.err(resp)), (400, 'nav.err.goal_invalid'), body)
        resp = self.line(to=(0.1, 0.0, 0.0))
        self.assertEqual((resp.code, self.err(resp)), (400, 'nav.err.line_length'))
        self.assertEqual(self.client.lines, [])

    def test_unknown_robot_position(self):
        self.run_nav()
        self.set_pose(x=None)
        resp = self.line()
        self.assertEqual((resp.code, self.err(resp)), (409, 'nav.err.straight_no_pose'))

    def test_one_job_at_a_time_and_stop_cancels_it(self):
        self.run_nav()
        self.assertEqual(self.line().code, 200)
        resp = self.line()
        self.assertEqual((resp.code, self.err(resp)), (409, 'nav.err.goal_busy'))
        resp = self.go()
        self.assertEqual((resp.code, self.err(resp)), (409, 'nav.err.goal_busy'))
        self.assertEqual(len(self.client.lines), 1)
        status = json.loads(self.fetch('/api/nav/goal').body)['goal']
        self.assertEqual((status['state'], status['line']), ('active', {'x0': 0.0, 'y0': 0.0}))
        resp = self.post('/api/nav/goal/cancel', {}, headers=self.h)                    # ปุ่ม ■ ใน Navigation
        self.assertEqual(resp.code, 200)
        self.assertGreaterEqual(self.client.cancels, 1)
        self.assertGreaterEqual(self.ctx.zero_calls, 1)                                  # ส่งความเร็วศูนย์ซ้ำด้วย

    def test_refused_while_a_route_runs_and_server_down(self):
        self.run_nav()
        self.client.line_is_ready = False
        resp = self.line()
        self.assertEqual((resp.code, self.err(resp)), (503, 'nav.err.goal_no_server'))
        self.assertEqual(self.client.lines, [])


class NavStraightApiTest(NavGoalSetup):
    """Drive straight ผ่าน REST : จุดหมายอยู่ข้างหน้าตามทิศหุ่น · ตรวจทั้งแนว · กติกาเดียวกับ goal ปกติ"""

    def straight(self, distance=5.0, headers=None, check=False, **extra):
        body = dict({'distance': distance, 'confirmed': True}, **extra)
        path = '/api/nav/goal/straight/check' if check else '/api/nav/goal/straight'
        return self.post(path, body, headers=self.h if headers is None else headers)

    def set_pose(self, x=0.0, y=0.0, yaw_deg=0.0, quality='good'):
        self.ctx.snapshot = lambda: {'mode': self.ctx.modes.snapshot(), 'localization': {'quality': quality},
                                     'pose': None if x is None else {'x': x, 'y': y, 'yaw_deg': yaw_deg}}

    def test_requires_admin_csrf_and_running_navigation(self):
        for check in (False, True):
            self.assertEqual(self.straight(headers={}, check=check).code, 401)
            self.assertEqual(self.straight(headers=self.cookie('forged'), check=check).code, 401)
            resp = self.fetch('/api/nav/goal/straight' + ('/check' if check else ''), method='POST',
                              body=json.dumps({'distance': 5.0}), headers=self.h)
            self.assertEqual(resp.code, 403)
            resp = self.straight(check=check)                                            # Standby
            self.assertEqual((resp.code, self.err(resp)), (409, 'nav.err.not_running'))
        self.assertEqual(self.client.sent, [])

    def test_goal_is_ahead_of_the_robot(self):
        self.run_nav()
        self.set_pose(1.0, 1.0, 90.0)
        resp = self.straight(4.0)
        body = json.loads(resp.body)
        self.assertEqual((resp.code, body['goal']['state'], body['check']['ok']), (200, 'active', True))
        x, y, yaw = self.client.sent[0]
        self.assertAlmostEqual(x, 1.0)
        self.assertAlmostEqual(y, 5.0)
        self.assertAlmostEqual(yaw, 3.141592653589793 / 2)

    def test_check_is_a_dry_run(self):
        self.run_nav()
        body = json.loads(self.straight(5.0, check=True, confirmed=None).body)      # ตรวจล่วงหน้าไม่ต้องมี confirmed
        self.assertEqual((body['ok'], body['check']['ok'], body['check']['soft_risk']), (True, True, False))
        self.assertAlmostEqual(body['check']['goal']['x'], 5.0)
        self.assertEqual((self.client.sent, self.ctx.goal.status()['state']), ([], 'idle'))

    def test_blocked_line_check_and_send(self):
        self.run_nav()
        check = json.loads(self.straight(8.0, check=True).body)['check']
        self.assertEqual((check['ok'], check['reason']), (False, 'tight'))
        self.assertAlmostEqual(check['at'], 6.1, delta=0.15)                         # ผนัง x=6.5 ลบ 0.40
        resp = self.straight(8.0)
        self.assertEqual((resp.code, self.err(resp)), (409, 'nav.err.straight_tight'))
        self.assertAlmostEqual(json.loads(resp.body)['error']['params']['at'], 6.1, delta=0.15)
        self.assertEqual(self.client.sent, [])

    def test_soft_risk_near_a_wall(self):
        self.run_nav()
        self.set_pose(0.0, 5.55, 0.0)
        check = json.loads(self.straight(3.0, check=True).body)['check']
        self.assertTrue(check['ok'])
        self.assertTrue(check['soft_risk'])                  # ห่างขอบแผนที่ 0.45 m (ผ่าน แต่ใกล้กว่า 0.55)
        self.assertGreaterEqual(check['min_clearance'], 0.4)

    def test_distance_validation(self):
        self.run_nav()
        for bad, code in ((11.0, 'nav.err.straight_distance'), (0.1, 'nav.err.straight_distance'),
                          (-5.0, 'nav.err.straight_distance'), ('a', 'nav.err.goal_invalid'),
                          (None, 'nav.err.goal_invalid'), (True, 'nav.err.goal_invalid')):
            for check in (False, True):
                resp = self.straight(bad, check=check)
                self.assertEqual((resp.code, self.err(resp)), (400, code), (bad, check))
        params = json.loads(self.straight(11.0).body)['error']['params']
        self.assertEqual((params['min'], params['max']), (0.5, 10.0))
        self.assertEqual(self.client.sent, [])

    def test_same_safety_rules_as_a_goal(self):
        self.run_nav()
        resp = self.post('/api/nav/goal/straight', {'distance': 3.0}, headers=self.h)
        self.assertEqual((resp.code, self.err(resp)), (409, 'nav.err.goal_confirm_required'))
        self.set_pose(quality='bad')
        resp = self.straight(3.0)
        self.assertEqual((resp.code, self.err(resp)), (409, 'nav.err.goal_not_localized'))
        self.set_pose(x=None)
        resp = self.straight(3.0, check=True)
        self.assertEqual((resp.code, self.err(resp)), (409, 'nav.err.straight_no_pose'))
        self.assertEqual(self.client.sent, [])

    def test_one_at_a_time_and_limits_are_reported(self):
        self.run_nav()
        self.assertEqual(self.straight(3.0).code, 200)
        resp = self.straight(2.0)
        self.assertEqual((resp.code, self.err(resp)), (409, 'nav.err.goal_busy'))
        self.assertEqual(len(self.client.sent), 1)
        limits = json.loads(self.fetch('/api/nav/goal').body)['limits']
        self.assertEqual((limits['straight_min'], limits['straight_max'], limits['clearance']), (0.5, 10.0, 0.4))
