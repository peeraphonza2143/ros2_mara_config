#!/usr/bin/env python3
"""
agv_control_app.py

AGV Control App (PyQt6) — เปิด/ปิดระบบ, monitor สถานะ, วิ่ง route, ขับ manual จากหน้าจอเดียว

ส่วนประกอบ:
  - AgvRosNode     (agv_ros_node.py)    : ฝั่ง ROS ทั้งหมด
  - SystemLauncher (system_launcher.py) : เปิด/ปิด ros2 launch application agv_bringup.launch.py
  - AgvControlWindow (ไฟล์นี้)           : หน้าจอ + state machine

State machine (โหมดของหุ่น มีได้ทีละโหมดเท่านั้น):

    IDLE ──กดปุ่มขับ──► MANUAL ──ปล่อยปุ่ม──► IDLE
    IDLE ──Start──► AUTO ──Pause──► PAUSED ──Resume──► AUTO
                     │                │
                     └─FAILED─► FAILED ──Resume──► AUTO
    Stop / STOP ALL : ทุกโหมด ──► IDLE

  Manual ขับได้เฉพาะ IDLE / MANUAL → กัน /cmd_vel ของคนกับของ Nav2 ชนกัน
"""

import math
import os
import signal
import sys
import time
from datetime import datetime

import rclpy
import yaml
from rclpy.signals import SignalHandlerOptions
from ament_index_python.packages import get_package_share_directory
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont, QKeyEvent
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
    QMessageBox, QPlainTextEdit, QPushButton, QTabWidget, QVBoxLayout, QWidget
)

from application.agv_ros_node import (
    RESULT_CANCELED, RESULT_FAILED, RESULT_REJECTED, RESULT_SUCCEEDED, AgvRosNode
)
from application.pyqt_motor_control_ui import (
    DEFAULT_SPEED_LEVEL, KEY_TO_DIRECTION, SPEED_LEVELS
)
from application.system_launcher import SystemLauncher

# ─── โหมด ───────────────────────────────────────────────────────────────────
IDLE = 'IDLE'
MANUAL = 'MANUAL'
AUTO = 'AUTO'
PAUSED = 'PAUSED'
FAILED = 'FAILED'

MODE_COLORS = {
    IDLE: '#607d8b',
    MANUAL: '#1976d2',
    AUTO: '#2e7d32',
    PAUSED: '#f9a825',
    FAILED: '#c62828',
}

GREEN, YELLOW, RED, GREY = '#2e7d32', '#f9a825', '#c62828', '#9e9e9e'

# ค่า default ถ้าใน agv_app.yaml ไม่มี key นั้น
DEFAULT_CFG = {
    'launch_package': 'application',
    'launch_file': 'agv_bringup.launch.py',
    'route_dir': '~/ros2_ws/src/my_robot_bringup/config',
    'data_dir': '~/agv_app_data',
    'topic_timeout': 1.0,
    'waiting_speed_threshold': 0.02,
    'waiting_time': 2.0,
    'loc_good': 0.15,
    'loc_fair': 0.30,
    'resume_search_back': 5,
    'resume_search_ahead': 15,
    'initial_cov_xy': 0.25,
    'initial_cov_yaw': 0.0685,
}

ROS_SPIN_PERIOD_MS = 20      # 50 Hz
ROS_SPIN_MAX_PER_TICK = 10   # ประมวลผล callback ได้สูงสุดกี่ตัวต่อรอบ (Humble ไม่มี spin_some)
UI_REFRESH_MS = 200
MANUAL_PUBLISH_MS = 100      # 10 Hz ระหว่างกดปุ่มขับค้าง
ZERO_BURST_COUNT = 5         # STOP: ส่ง /cmd_vel = 0 ซ้ำ 5 ครั้ง (0.5 s) ทับคำสั่งที่ค้างในระบบ
NEXT_LAP_DELAY_MS = 1000


# ─── helpers ────────────────────────────────────────────────────────────────
def load_config() -> dict:
    cfg = dict(DEFAULT_CFG)
    path = os.path.join(get_package_share_directory('application'), 'config', 'agv_app.yaml')
    try:
        with open(path) as f:
            cfg.update(yaml.safe_load(f) or {})
    except OSError:
        pass   # ไม่มีไฟล์ → ใช้ค่า default
    cfg['route_dir'] = os.path.expanduser(cfg['route_dir'])
    cfg['data_dir'] = os.path.expanduser(cfg['data_dir'])
    return cfg


def load_waypoints(path: str):
    """YAML → [(name, x, y, yaw), ...] รูปแบบเดียวกับ agv_waypoint_node"""
    with open(path) as f:
        data = yaml.safe_load(f)
    return [(str(wp['name']), float(wp['x']), float(wp['y']), float(wp['yaw']))
            for wp in data['waypoints']]


def list_route_files(route_dir: str):
    """คืน [(filename, จำนวนจุด)] เฉพาะ YAML ที่มี key 'waypoints'"""
    routes = []
    try:
        names = sorted(os.listdir(route_dir))
    except OSError:
        return routes
    for name in names:
        if not name.endswith(('.yaml', '.yml')):
            continue
        try:
            with open(os.path.join(route_dir, name)) as f:
                data = yaml.safe_load(f)
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(data, dict) and isinstance(data.get('waypoints'), list):
            routes.append((name, len(data['waypoints'])))
    return routes


def no_focus(widget):
    """ปุ่มไม่แย่ง keyboard focus → ลูกศร / Spacebar ส่งถึงหน้าต่างหลักเสมอ"""
    widget.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    return widget


# ─── หน้าจอหลัก ─────────────────────────────────────────────────────────────
class AgvControlWindow(QWidget):

    def __init__(self, node: AgvRosNode, cfg: dict, launcher):
        super().__init__()
        self.node = node
        self.cfg = cfg
        self.launcher = launcher

        self.mode = IDLE

        # route
        self.route = []            # [(name, x, y, yaw)]
        self.route_name = ''
        self.lap = 0
        self.sent_start = 0        # index ของ waypoint แรกใน goal ปัจจุบัน
        self.sent_count = 0        # จำนวน waypoint ใน goal ปัจจุบัน
        self.resume_hint = 0       # index ที่ค้างอยู่ตอน Pause / FAILED
        self.pending_next_lap = False

        # waiting detection
        self.slow_since = None
        self.waiting = False

        # manual
        self.active_directions = set()
        self.speed_level = DEFAULT_SPEED_LEVEL
        self.zero_burst = 0

        self.prev_nav2_active = False
        self.closing = False

        os.makedirs(os.path.join(cfg['data_dir'], 'logs'), exist_ok=True)
        self.home_path = os.path.join(cfg['data_dir'], 'home_pose.yaml')
        self.home_pose = self._load_home()

        self._build_ui()
        self.refresh_routes()

        self.launcher.output.connect(self._append_system_output)
        self.launcher.running_changed.connect(self._on_system_running_changed)

        self.ui_timer = QTimer(self)
        self.ui_timer.timeout.connect(self._tick)
        self.ui_timer.start(UI_REFRESH_MS)

        self.manual_timer = QTimer(self)
        self.manual_timer.timeout.connect(self._manual_tick)
        self.manual_timer.start(MANUAL_PUBLISH_MS)

        self.log_event('App started')
        self._tick()

    # ====================================================================== UI
    def _build_ui(self):
        self.setWindowTitle('AGV Control')
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.resize(1000, 760)
        root = QVBoxLayout(self)

        # ── แถวบน: ไฟสถานะ + START/STOP SYSTEM ──
        top = QHBoxLayout()
        self.lights = {}
        for key, text in [('odom', 'Motor'), ('scan', 'Lidar'), ('scan_filtered', 'Filter'),
                          ('nav2', 'Nav2'), ('loc', 'Localize')]:
            lbl = QLabel(text)
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setMinimumWidth(90)
            lbl.setFont(QFont('Sans', 11, QFont.Weight.Bold))
            top.addWidget(lbl)
            self.lights[key] = lbl
        top.addStretch()
        self.btn_sys_start = no_focus(QPushButton('▶ START SYSTEM'))
        self.btn_sys_stop = no_focus(QPushButton('■ STOP SYSTEM'))
        for b in (self.btn_sys_start, self.btn_sys_stop):
            b.setMinimumHeight(40)
        self.btn_sys_start.clicked.connect(self.start_system)
        self.btn_sys_stop.clicked.connect(self.stop_system)
        top.addWidget(self.btn_sys_start)
        top.addWidget(self.btn_sys_stop)
        root.addLayout(top)

        # ── แถบโหมด ──
        self.mode_label = QLabel()
        self.mode_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.mode_label.setFont(QFont('Sans', 18, QFont.Weight.Bold))
        self.mode_label.setMinimumHeight(48)
        root.addWidget(self.mode_label)

        self.hint_label = QLabel()
        self.hint_label.setStyleSheet('color: #555;')
        root.addWidget(self.hint_label)

        # ── กลาง: Route | Status | Manual ──
        mid = QHBoxLayout()
        mid.addWidget(self._build_route_box(), 3)
        mid.addWidget(self._build_status_box(), 3)
        mid.addWidget(self._build_manual_box(), 2)
        root.addLayout(mid)

        # ── STOP ALL ──
        self.btn_stop_all = no_focus(QPushButton('🛑  STOP ALL  (Spacebar)'))
        self.btn_stop_all.setMinimumHeight(70)
        self.btn_stop_all.setFont(QFont('Sans', 20, QFont.Weight.Bold))
        self.btn_stop_all.setStyleSheet(
            'QPushButton { background-color: #c62828; color: white; border-radius: 8px; }'
            'QPushButton:pressed { background-color: #8e0000; }')
        self.btn_stop_all.clicked.connect(self.stop_all)
        root.addWidget(self.btn_stop_all)

        # ── แท็บ log ──
        tabs = no_focus(QTabWidget())
        self.events_view = no_focus(QPlainTextEdit())
        self.events_view.setReadOnly(True)
        self.events_view.setMaximumBlockCount(1000)
        self.system_view = no_focus(QPlainTextEdit())
        self.system_view.setReadOnly(True)
        self.system_view.setMaximumBlockCount(2000)
        self.system_view.setFont(QFont('Monospace', 9))
        tabs.addTab(self.events_view, 'Events')
        tabs.addTab(self.system_view, 'System Output')
        tabs.setMinimumHeight(180)
        root.addWidget(tabs)

    def _build_route_box(self):
        box = QGroupBox('Route')
        lay = QVBoxLayout(box)

        row = QHBoxLayout()
        self.route_combo = no_focus(QComboBox())
        self.btn_refresh = no_focus(QPushButton('⟳'))
        self.btn_refresh.setMaximumWidth(40)
        self.btn_refresh.clicked.connect(self.refresh_routes)
        row.addWidget(self.route_combo, 1)
        row.addWidget(self.btn_refresh)
        lay.addLayout(row)

        self.home_label = QLabel()
        lay.addWidget(self.home_label)
        row = QHBoxLayout()
        self.btn_save_home = no_focus(QPushButton('Save Current as Home'))
        self.btn_set_home = no_focus(QPushButton('📍 Set Home Pose'))
        self.btn_save_home.clicked.connect(self.save_home)
        self.btn_set_home.clicked.connect(self.set_home_pose)
        row.addWidget(self.btn_save_home)
        row.addWidget(self.btn_set_home)
        lay.addLayout(row)

        grid = QGridLayout()
        self.btn_start = no_focus(QPushButton('▶ Start'))
        self.btn_pause = no_focus(QPushButton('⏸ Pause'))
        self.btn_resume = no_focus(QPushButton('⏯ Resume'))
        self.btn_stop = no_focus(QPushButton('■ Stop'))
        for i, b in enumerate((self.btn_start, self.btn_pause, self.btn_resume, self.btn_stop)):
            b.setMinimumHeight(48)
            b.setFont(QFont('Sans', 12, QFont.Weight.Bold))
            grid.addWidget(b, i // 2, i % 2)
        self.btn_start.clicked.connect(self.start_route)
        self.btn_pause.clicked.connect(self.pause_route)
        self.btn_resume.clicked.connect(self.resume_route)
        self.btn_stop.clicked.connect(self.stop_route)
        lay.addLayout(grid)
        lay.addStretch()
        return box

    def _build_status_box(self):
        box = QGroupBox('Status')
        grid = QGridLayout(box)
        self.status = {}
        rows = [('route', 'Route'), ('lap', 'Lap'), ('progress', 'Waypoint'),
                ('distance', 'Distance left'), ('recoveries', 'Recoveries'),
                ('speed', 'Speed'), ('loc', 'Localization'), ('pose', 'Pose (map)')]
        for i, (key, text) in enumerate(rows):
            grid.addWidget(QLabel(text + ':'), i, 0)
            val = QLabel('-')
            val.setFont(QFont('Sans', 11, QFont.Weight.Bold))
            grid.addWidget(val, i, 1)
            self.status[key] = val
        grid.setRowStretch(len(rows), 1)
        return box

    def _build_manual_box(self):
        box = QGroupBox('Manual (ปุ่มลูกศรได้)')
        lay = QVBoxLayout(box)

        row = QHBoxLayout()
        b_down = no_focus(QPushButton('−'))
        b_up = no_focus(QPushButton('+'))
        b_down.clicked.connect(lambda: self._change_speed_level(-1))
        b_up.clicked.connect(lambda: self._change_speed_level(+1))
        self.speed_level_label = QLabel()
        self.speed_level_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row.addWidget(b_down)
        row.addWidget(self.speed_level_label, 1)
        row.addWidget(b_up)
        lay.addLayout(row)
        self.speed_buttons = [b_down, b_up]

        grid = QGridLayout()
        self.dir_buttons = []
        for text, direction, r, c in [('▲', 'forward', 0, 1), ('◀', 'left', 1, 0),
                                      ('▶', 'right', 1, 2), ('▼', 'backward', 2, 1)]:
            b = no_focus(QPushButton(text))
            b.setMinimumSize(60, 60)
            b.setFont(QFont('Sans', 18))
            b.pressed.connect(lambda d=direction: self._add_direction(d))
            b.released.connect(lambda d=direction: self._remove_direction(d))
            grid.addWidget(b, r, c)
            self.dir_buttons.append(b)
        lay.addLayout(grid)
        lay.addStretch()
        self._update_speed_label()
        return box

    # ================================================================= logging
    def log_event(self, text: str):
        now = datetime.now()
        line = f'{now.strftime("%H:%M:%S")}  {text}'
        self.events_view.appendPlainText(line)
        path = os.path.join(self.cfg['data_dir'], 'logs', now.strftime('%Y-%m-%d') + '.log')
        try:
            with open(path, 'a') as f:
                f.write(line + '\n')
        except OSError:
            pass

    def _append_system_output(self, line: str):
        self.system_view.appendPlainText(line)

    # ================================================================== system
    def start_system(self):
        self.log_event('START SYSTEM')
        self.launcher.start()

    def stop_system(self):
        if self.mode in (AUTO, PAUSED, FAILED, MANUAL):
            self.stop_all()
        self.log_event('STOP SYSTEM')
        self.launcher.stop()

    def _on_system_running_changed(self, running: bool):
        if not running:
            self.log_event('System stopped')
            # ตำแหน่งจาก AMCL ของรอบก่อนใช้ไม่ได้แล้ว
            self.node.amcl_pose = None
            self.node.amcl_sigma = None
            if self.closing:
                self.close()

    # ==================================================================== mode
    def set_mode(self, mode: str):
        if mode != self.mode:
            self.mode = mode
            self.slow_since = None
            self.waiting = False

    # =================================================================== route
    def refresh_routes(self):
        current = self.route_combo.currentData()
        self.route_combo.clear()
        for name, n in list_route_files(self.cfg['route_dir']):
            self.route_combo.addItem(f'{name}  ({n} จุด)', name)
        idx = self.route_combo.findData(current)
        if idx >= 0:
            self.route_combo.setCurrentIndex(idx)

    def _nav_ready(self) -> bool:
        return self.node.nav2_active and self.node.nav_server_ready()

    def start_route(self):
        if self.mode != IDLE or not self._nav_ready():
            return
        name = self.route_combo.currentData()
        if not name:
            return
        path = os.path.join(self.cfg['route_dir'], name)
        try:
            route = load_waypoints(path)
        except Exception as e:  # noqa: BLE001 — แสดงทุก error ของไฟล์ให้ผู้ใช้เห็น
            self.log_event(f'โหลด route ไม่ได้: {name}: {e}')
            return
        if not route:
            self.log_event(f'route ว่าง: {name}')
            return
        self.route = route
        self.route_name = name
        self.lap = 1
        self.log_event(f'START route {name} ({len(route)} จุด) รอบ {self.lap}')
        self._send_from(0)

    def _send_from(self, index: int):
        self.pending_next_lap = False
        self.sent_start = index
        self.sent_count = len(self.route) - index
        if self.node.send_route(self.route[index:], self._on_route_done):
            self.set_mode(AUTO)
        else:
            self.resume_hint = index
            self.set_mode(FAILED)
            self.log_event('ส่ง goal ไม่ได้ (Nav2 action server ไม่พร้อม)')

    def _progress_index(self) -> int:
        """index ของ waypoint ที่กำลังวิ่งไปหา คำนวณจาก feedback ของ Nav2"""
        fb = self.node.feedback
        if fb is None:
            return self.sent_start
        done = self.sent_count - int(fb.number_of_poses_remaining)
        return min(max(self.sent_start + done, 0), len(self.route) - 1)

    def _on_route_done(self, result: str):
        if self.mode != AUTO:
            return
        if result == RESULT_SUCCEEDED:
            self.log_event(f'<<< จบรอบ {self.lap} สำเร็จ')
            self.lap += 1
            self.pending_next_lap = True
            QTimer.singleShot(NEXT_LAP_DELAY_MS, self._next_lap)
        elif result == RESULT_CANCELED:
            self.resume_hint = self._progress_index()
            self.set_mode(PAUSED)
            self.log_event('goal ถูกยกเลิกจากภายนอก (เช่น RViz) → PAUSED')
        else:
            self.resume_hint = self._progress_index()
            self.set_mode(FAILED)
            reason = 'Nav2 ไม่รับ goal' if result == RESULT_REJECTED else 'Nav2 ABORTED'
            self.log_event(f'!!! FAILED ({reason}) ที่ waypoint '
                           f'{self.route[self.resume_hint][0]} — รอกด Resume หรือ Stop')
            QApplication.beep()

    def _next_lap(self):
        # ถ้าระหว่างรอ 1 วินาทีมีคนกด Stop / Pause / STOP ALL ไปแล้ว ไม่ต้องเริ่มรอบใหม่
        if not self.pending_next_lap or self.mode != AUTO:
            return
        self.log_event(f'START รอบ {self.lap}')
        self._send_from(0)

    def pause_route(self):
        if self.mode != AUTO:
            return
        self.resume_hint = self._progress_index()
        self.pending_next_lap = False
        self.node.cancel_route()
        self.zero_burst = ZERO_BURST_COUNT
        self.set_mode(PAUSED)
        self.log_event(f'PAUSE ที่ waypoint {self.route[self.resume_hint][0]}')

    def _nearest_index(self, hint: int) -> int:
        """หา waypoint ที่ใกล้หุ่นที่สุด เฉพาะช่วงรอบ ๆ hint (กันเลือกจุดของช่วงที่เส้นทางตัดกัน)"""
        pose = self.node.amcl_pose
        if pose is None:
            return hint
        lo = max(0, hint - int(self.cfg['resume_search_back']))
        hi = min(len(self.route), hint + int(self.cfg['resume_search_ahead']) + 1)
        x, y, _ = pose
        return min(range(lo, hi),
                   key=lambda i: math.hypot(self.route[i][1] - x, self.route[i][2] - y))

    def resume_route(self):
        if self.mode not in (PAUSED, FAILED) or not self._nav_ready():
            return
        index = self._nearest_index(self.resume_hint)
        self.log_event(f'RESUME จาก waypoint {self.route[index][0]} (รอบ {self.lap})')
        self._send_from(index)

    def stop_route(self):
        if self.mode not in (AUTO, PAUSED, FAILED):
            return
        self.node.cancel_route()
        self.pending_next_lap = False
        self.zero_burst = ZERO_BURST_COUNT
        self.set_mode(IDLE)
        self.log_event('STOP route')

    def stop_all(self):
        self.node.cancel_route()
        self.pending_next_lap = False
        self.active_directions.clear()
        self.node.publish_cmd(0.0, 0.0)
        self.zero_burst = ZERO_BURST_COUNT
        self.set_mode(IDLE)
        self.log_event('🛑 STOP ALL')

    # ==================================================================== home
    def _load_home(self):
        try:
            with open(self.home_path) as f:
                d = yaml.safe_load(f)
            return float(d['x']), float(d['y']), float(d['yaw'])
        except (OSError, TypeError, KeyError, ValueError, yaml.YAMLError):
            return None

    def save_home(self):
        pose = self.node.amcl_pose
        if pose is None:
            return
        x, y, yaw = pose
        reply = QMessageBox.question(
            self, 'บันทึก Home',
            f'บันทึกตำแหน่งปัจจุบันเป็น Home?\n\nx = {x:.3f}  y = {y:.3f}  '
            f'yaw = {math.degrees(yaw):.1f}°\n\n'
            'ตรวจใน RViz ก่อนว่าตำแหน่งนี้ถูกต้อง (scan ทับกับแผนที่)',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            with open(self.home_path, 'w') as f:
                yaml.safe_dump({'x': round(x, 4), 'y': round(y, 4), 'yaw': round(yaw, 4),
                                'saved_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')}, f)
        except OSError as e:
            self.log_event(f'บันทึก Home ไม่ได้: {e}')
            return
        self.home_pose = (x, y, yaw)
        self.log_event(f'บันทึก Home: x={x:.3f} y={y:.3f} yaw={yaw:.3f}')

    def set_home_pose(self):
        if self.home_pose is None or self.mode != IDLE:
            return
        reply = QMessageBox.question(
            self, 'Set Home Pose',
            'หุ่นจอดอยู่ที่จุด Home และหันหัวตรงตามที่บันทึกไว้แล้วใช่ไหม?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        self.node.publish_initial_pose(*self.home_pose)
        self.log_event('Set Home Pose → /initialpose')

    # ================================================================== manual
    def _change_speed_level(self, delta: int):
        self.speed_level = min(max(self.speed_level + delta, 1), len(SPEED_LEVELS))
        self._update_speed_label()

    def _update_speed_label(self):
        v, w = SPEED_LEVELS[self.speed_level - 1]
        self.speed_level_label.setText(
            f'SPEED {self.speed_level}/{len(SPEED_LEVELS)}\n{v:.2f} m/s · {w:.1f} rad/s')

    def _manual_allowed(self) -> bool:
        return self.mode in (IDLE, MANUAL)

    def _add_direction(self, direction: str):
        if not self._manual_allowed():
            return
        self.active_directions.add(direction)
        self.set_mode(MANUAL)
        self._publish_manual()

    def _remove_direction(self, direction: str):
        self.active_directions.discard(direction)
        if self.mode == MANUAL and not self.active_directions:
            self.node.publish_cmd(0.0, 0.0)
            self.set_mode(IDLE)

    def _publish_manual(self):
        d = self.active_directions
        lin, ang = SPEED_LEVELS[self.speed_level - 1]
        # กดทิศตรงข้ามพร้อมกัน (เช่น หน้า+หลัง) → หักล้างกันเป็น 0
        v = 0.0
        if 'forward' in d and 'backward' not in d:
            v = lin
        elif 'backward' in d and 'forward' not in d:
            v = -lin
        w = 0.0
        if 'left' in d and 'right' not in d:
            w = ang
        elif 'right' in d and 'left' not in d:
            w = -ang
        self.node.publish_cmd(v, w)

    def _manual_tick(self):
        if self.zero_burst > 0:
            self.zero_burst -= 1
            self.node.publish_cmd(0.0, 0.0)
        elif self.mode == MANUAL and self.active_directions:
            self._publish_manual()

    def keyPressEvent(self, event: QKeyEvent):
        if event.isAutoRepeat():
            return
        if event.key() == Qt.Key.Key_Space:
            self.stop_all()
            return
        direction = KEY_TO_DIRECTION.get(event.key())
        if direction:
            self._add_direction(direction)

    def keyReleaseEvent(self, event: QKeyEvent):
        if event.isAutoRepeat():
            return
        direction = KEY_TO_DIRECTION.get(event.key())
        if direction:
            self._remove_direction(direction)

    def focusOutEvent(self, event):
        # สลับหน้าต่างระหว่างกดลูกศรค้าง → จะไม่ได้ keyRelease → หยุดไว้ก่อน
        if self.mode == MANUAL:
            self.active_directions.clear()
            self.node.publish_cmd(0.0, 0.0)
            self.set_mode(IDLE)
        super().focusOutEvent(event)

    # ==================================================================== tick
    def _tick(self):
        node = self.node
        now = time.monotonic()

        # --- Nav2 active → inactive ระหว่างวิ่ง ---
        if self.prev_nav2_active and not node.nav2_active:
            self.log_event('Nav2 ไม่ active แล้ว')
            if self.mode == AUTO:
                self.resume_hint = self._progress_index()
                node.cancel_route()
                self.set_mode(FAILED)
                QApplication.beep()
        self.prev_nav2_active = node.nav2_active

        # --- มอเตอร์เงียบระหว่าง AUTO ---
        if self.mode == AUTO and not node.is_alive('odom'):
            self.resume_hint = self._progress_index()
            node.cancel_route()
            self.set_mode(FAILED)
            self.log_event('!!! ไม่ได้รับ /odom (motor driver หยุด?) → FAILED')
            QApplication.beep()

        # --- ตรวจ "หยุดรอ obstacle" ---
        if self.mode == AUTO and not self.pending_next_lap:
            thr = self.cfg['waiting_speed_threshold']
            slow = abs(node.linear_vel) < thr and abs(node.angular_vel) < thr
            if slow:
                if self.slow_since is None:
                    self.slow_since = now
                elif not self.waiting and now - self.slow_since > self.cfg['waiting_time']:
                    self.waiting = True
                    self.log_event('WAITING — หุ่นหยุดนิ่ง (obstacle?)')
            else:
                if self.waiting:
                    self.log_event(f'วิ่งต่อ หลังหยุดรอ {now - self.slow_since:.1f} s')
                self.slow_since = None
                self.waiting = False

        self._update_lights()
        self._update_status()
        self._update_buttons()

    def _set_light(self, key: str, color: str):
        self.lights[key].setStyleSheet(
            f'background-color: {color}; color: white; border-radius: 6px; padding: 6px;')

    def _loc_quality(self):
        sigma = self.node.amcl_sigma
        if self.node.amcl_pose is None or sigma is None:
            return GREY, 'ไม่มีข้อมูล'
        if sigma < self.cfg['loc_good']:
            return GREEN, f'GOOD (σ {sigma:.2f} m)'
        if sigma < self.cfg['loc_fair']:
            return YELLOW, f'FAIR (σ {sigma:.2f} m)'
        return RED, f'POOR (σ {sigma:.2f} m)'

    def _update_lights(self):
        for key in ('odom', 'scan', 'scan_filtered'):
            self._set_light(key, GREEN if self.node.is_alive(key) else RED)
        self._set_light('nav2', GREEN if self.node.nav2_active else RED)
        self._set_light('loc', self._loc_quality()[0])

    def _update_status(self):
        text = self.mode
        if self.mode == AUTO and self.waiting:
            text = 'AUTO — WAITING (หยุดรอ)'
            color = MODE_COLORS[PAUSED]
        else:
            color = MODE_COLORS[self.mode]
        self.mode_label.setText(text)
        self.mode_label.setStyleSheet(
            f'background-color: {color}; color: white; border-radius: 8px;')

        s = self.status
        in_route = self.mode in (AUTO, PAUSED, FAILED)
        s['route'].setText(self.route_name if in_route else '-')
        s['lap'].setText(str(self.lap) if in_route else '-')
        fb = self.node.feedback
        if in_route and self.route:
            idx = self._progress_index() if self.mode == AUTO else self.resume_hint
            s['progress'].setText(f'{self.route[idx][0]}  ({idx + 1}/{len(self.route)})')
        else:
            s['progress'].setText('-')
        if self.mode == AUTO and fb is not None:
            s['distance'].setText(f'{fb.distance_remaining:.1f} m')
            s['recoveries'].setText(str(fb.number_of_recoveries))
        else:
            s['distance'].setText('-')
            s['recoveries'].setText('-')
        s['speed'].setText(f'{self.node.linear_vel:+.2f} m/s   {self.node.angular_vel:+.2f} rad/s')
        s['loc'].setText(self._loc_quality()[1])
        pose = self.node.amcl_pose
        s['pose'].setText(f'x {pose[0]:.2f}  y {pose[1]:.2f}  yaw {math.degrees(pose[2]):.0f}°'
                          if pose else '-')
        self.home_label.setText(
            'Home: ' + (f'x {self.home_pose[0]:.2f}  y {self.home_pose[1]:.2f}  '
                        f'yaw {math.degrees(self.home_pose[2]):.0f}°'
                        if self.home_pose else 'ยังไม่ได้บันทึก'))

    def _update_buttons(self):
        running = self.launcher.is_running()
        self.btn_sys_start.setEnabled(not running)
        self.btn_sys_stop.setEnabled(running)

        nav_ok = self._nav_ready()
        has_pose = self.node.amcl_pose is not None
        has_route = self.route_combo.count() > 0
        idle = self.mode == IDLE

        self.btn_start.setEnabled(idle and nav_ok and has_pose and has_route)
        self.btn_pause.setEnabled(self.mode == AUTO)
        self.btn_resume.setEnabled(self.mode in (PAUSED, FAILED) and nav_ok)
        self.btn_stop.setEnabled(self.mode in (AUTO, PAUSED, FAILED))
        self.route_combo.setEnabled(idle)
        self.btn_refresh.setEnabled(idle)
        self.btn_save_home.setEnabled(idle and has_pose)
        self.btn_set_home.setEnabled(
            idle and self.node.amcl_listening() and self.home_pose is not None)
        manual_ok = self._manual_allowed()
        for b in self.dir_buttons + self.speed_buttons:
            b.setEnabled(manual_ok)

        # บอกเหตุผลว่าทำไมยังกด Start ไม่ได้
        if idle:
            if not self.node.nav2_active and self.node.amcl_listening() and not has_pose:
                hint = ('Nav2 รอ initial pose — กด Set Home Pose '
                        'หรือใช้ 2D Pose Estimate ใน RViz')
            elif not self.node.nav2_active:
                hint = 'รอ Nav2 active — กด START SYSTEM หรือรอให้ระบบเปิดเสร็จ'
            elif not has_pose:
                hint = 'ยังไม่มีตำแหน่งจาก AMCL — กด Set Home Pose หรือใช้ 2D Pose Estimate ใน RViz'
            elif not has_route:
                hint = f'ไม่พบไฟล์ route ใน {self.cfg["route_dir"]}'
            else:
                hint = ('พร้อมวิ่ง — หลัง Set Home ค่า σ จะสูง (POOR) เป็นปกติจนหุ่นเริ่มเคลื่อนที่ '
                        'ให้ตรวจใน RViz ว่า scan ทับแผนที่')
        elif self.mode == MANUAL:
            hint = 'กำลังขับ manual — ปล่อยปุ่มเพื่อหยุด'
        elif self.mode == AUTO:
            hint = 'กำลังวิ่ง route — Manual ถูกล็อก'
        elif self.mode == PAUSED:
            hint = 'หยุดชั่วคราว — Resume เพื่อวิ่งต่อจากจุดที่ใกล้ที่สุด หรือ Stop เพื่อเลิก'
        else:
            hint = 'FAILED — ตรวจหน้างาน แล้วกด Resume หรือ Stop (Stop แล้วจึงขับ manual ได้)'
        self.hint_label.setText(hint)

    # =================================================================== close
    def closeEvent(self, event):
        # หยุดหุ่นก่อนเสมอ
        self.node.cancel_route()
        for _ in range(3):
            self.node.publish_cmd(0.0, 0.0)

        if self.launcher.is_running():
            if not self.closing:
                reply = QMessageBox.question(
                    self, 'ปิด App',
                    'ระบบ (motor / lidar / Nav2) ที่เปิดจาก app ยังทำงานอยู่\n'
                    'ปิดระบบแล้วออกจาก app?',
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Cancel)
                if reply != QMessageBox.StandardButton.Yes:
                    event.ignore()
                    return
                self.closing = True
                self.set_mode(IDLE)
                self.log_event('กำลังปิดระบบก่อนออกจาก app ...')
                self.launcher.stop()
            event.ignore()   # รอ running_changed(False) แล้วจะเรียก close() อีกครั้ง
            return

        self.log_event('App closed')
        super().closeEvent(event)


def main(args=None):
    # ปิด signal handler ของ rclpy แล้วจัดการ Ctrl+C เอง (ด้านล่าง)
    # ไม่งั้น rclpy จะ shutdown ทันทีโดยไม่ได้หยุดหุ่น / ปิดระบบให้เรียบร้อย
    rclpy.init(args=args, signal_handler_options=SignalHandlerOptions.NO)
    cfg = load_config()
    node = AgvRosNode(cfg)

    app = QApplication(sys.argv)
    launcher = SystemLauncher(cfg['launch_package'], cfg['launch_file'])
    window = AgvControlWindow(node, cfg, launcher)
    window.show()

    # spin ROS ใน event loop ของ Qt (thread เดียวกัน) — วนหลายครั้งต่อรอบให้ทัน topic ถี่ ๆ
    def spin_ros():
        for _ in range(ROS_SPIN_MAX_PER_TICK):
            rclpy.spin_once(node, timeout_sec=0)

    ros_timer = QTimer()
    ros_timer.timeout.connect(spin_ros)
    ros_timer.start(ROS_SPIN_PERIOD_MS)

    # Ctrl+C / kill ใน terminal → ปิดหน้าต่างอย่างเป็นขั้นตอน (หยุดหุ่น + ถามเรื่องปิดระบบ)
    # Python จะรัน handler นี้ได้ก็ต่อเมื่อมีโค้ด Python ทำงาน — ros_timer ด้านบนช่วยตรงนี้
    signal.signal(signal.SIGINT, lambda *_: window.close())
    signal.signal(signal.SIGTERM, lambda *_: window.close())

    exit_code = app.exec()

    node.publish_cmd(0.0, 0.0)
    node.destroy_node()
    rclpy.shutdown()
    sys.exit(exit_code)


if __name__ == '__main__':
    main()
