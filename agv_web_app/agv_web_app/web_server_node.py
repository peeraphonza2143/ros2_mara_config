"""
web_server_node — เว็บเซิร์ฟเวอร์ + ROS node ตัวเดียว

โครงสร้าง 2 thread (ห้ามเรียกข้ามกันตรง ๆ):
  - thread ROS    : rclpy executor รับ topic/TF แล้วเขียนค่าล่าสุดลง self._state (ใต้ lock)
  - thread tornado: เว็บเซิร์ฟเวอร์ + WebSocket; PeriodicCallback อ่าน snapshot() แล้ว
                    ส่งให้เบราว์เซอร์เอง → ไม่มีใครเขียน WebSocket จาก thread ของ ROS

ตอนนี้ node นี้ "ไม่ publish อะไรเลย" และไม่สั่งมอเตอร์ตรง ๆ
สิ่งเดียวที่สั่งได้คือ เปลี่ยนโหมด (เปิด/ปิด ros2 launch) ผ่าน ModeManager — ต้องมี PIN ผู้ดูแล
"""
import json
import math
import os
import shlex
import signal
import threading
import time

import rclpy
from rclpy.node import Node
from rclpy.qos import (DurabilityPolicy, QoSProfile, ReliabilityPolicy,
                       qos_profile_sensor_data)
from rclpy.executors import ExternalShutdownException, SingleThreadedExecutor
from ament_index_python.packages import get_package_share_directory
from geometry_msgs.msg import PoseWithCovarianceStamped, Twist
from nav_msgs.msg import OccupancyGrid, Odometry
from rcl_interfaces.msg import ParameterDescriptor
from sensor_msgs.msg import Imu, LaserScan
from tf2_ros import Buffer, TransformException, TransformListener

import tornado.ioloop
import tornado.websocket

from agv_web_app import sweep
from agv_web_app.activity import MOVING_HOLD_S, compute_activity
from agv_web_app.auth import AuthManager
from agv_web_app.drive_control import DriveController, drive_allowed
from agv_web_app.history import SAMPLE_HZ, SpeedHistory
from agv_web_app.laser_overlay import LaserOverlay
from agv_web_app.live_map import LiveMap, map_clear_needed
from agv_web_app.map_saver import MapSaver
from agv_web_app.map_selection import MapSelection
from agv_web_app.mode_manager import MAPPING, MANUAL, NAVIGATION, ModeManager
from agv_web_app.msg import msg
from agv_web_app.nav_goal import LOCALIZED, NavGoal
from agv_web_app.nav_live import NavLive, node_names
from agv_web_app.nav_params import NavParams
from agv_web_app.nav_pose import InitialPose
from agv_web_app.ros_params import RosGoalClient, RosNomotionUpdate, RosParamTransport, RosPosePublisher
from agv_web_app.slam_params import SlamParams
from agv_web_app import web_assets
from agv_web_app.route_run import RouteRunner
from agv_web_app.routes import RouteStore
from agv_web_app.saved_map import SavedMaps
from agv_web_app.start_pose import AutoStart, StartPoses
from agv_web_app.stats import StatsStore, active_mode
from agv_web_app.web_api import StatusSocket, make_app


def yaw_from_quaternion(q):
    return math.atan2(2.0 * (q.w * q.z + q.x * q.y),
                      1.0 - 2.0 * (q.y * q.y + q.z * q.z))


class StatusNode(Node):
    """รับข้อมูลจากหุ่น เก็บค่าล่าสุด และสรุปเป็น dict ผ่าน snapshot()"""

    def __init__(self):
        super().__init__('web_server_node')

        # พารามิเตอร์ที่เป็น "ตัวเลข" ประกาศแบบ dynamic_typing + แปลงเป็น float เอง
        # เพราะ ROS ไม่ยอมให้ YAML เขียน `30` (int) ใส่พารามิเตอร์ที่ประกาศเป็น 30.0 (double)
        # → ผู้ใช้เขียนแบบไหนก็ไม่ล้ม (บทเรียน: ทดสอบด้วยไฟล์ YAML ตัวจริงเสมอ)
        self._num_defaults = {}

        def number(name, default):
            self._num_defaults[name] = default
            self.declare_parameter(name, default, ParameterDescriptor(dynamic_typing=True))

        self.declare_parameter('port', 8080)
        number('push_hz', 5.0)
        number('topic_timeout', 1.0)
        number('loc_good', 0.15)
        number('loc_fair', 0.30)
        # ── ขับด้วยมือ (จอยบนหน้าเว็บ): เพดานจริงอยู่ที่นี่ หน้าเว็บส่งเกินก็ถูกตัด ──
        number('manual_max_linear', 0.5)         # m/s
        number('manual_max_angular', 1.0)        # rad/s
        number('manual_default_linear', 0.25)
        number('manual_default_angular', 0.6)
        number('manual_linear_accel', 0.6)       # m/s² (เร่ง)
        number('manual_angular_accel', 1.5)      # rad/s²
        number('manual_linear_decel', 1.5)       # m/s² (เบรก)
        number('manual_angular_decel', 3.0)      # rad/s²
        number('manual_heartbeat_timeout', 0.3)  # ไม่ได้รับคำสั่งนานเกินนี้ → ลดความเร็วลงศูนย์ (deadman)
        number('scan_overlay_hz', 5.0)
        number('scan_overlay_points', 360)
        number('moving_speed', 0.02)         # |v| (m/s) หรือ |w| (rad/s) เกินนี้ = กำลังเคลื่อนที่
        self.declare_parameter('odom_topic', '/odometry/filtered')
        self.declare_parameter('scan_topic', '/scan_filtered')
        self.declare_parameter('imu_topic', '/imu/data')
        self.declare_parameter('map_topic', '/map')
        self.declare_parameter('cmd_vel_topic', '/cmd_vel')
        self.declare_parameter('maps_dir', '')                   # ว่าง = <data_dir>/maps
        self.declare_parameter('slam_defaults_file', '')      # ว่าง = mapper_params_s2_factory.yaml ของ agv_s2_bringup
        self.declare_parameter('nav_defaults_file', '')       # ว่าง = nav2_params_s2.yaml ของ agv_s2_bringup
        number('drive_straight_max', 10.0)   # m ระยะสูงสุดต่อคำสั่ง Drive straight
        number('goal_clearance', 0.40)       # m ห่างผนังขั้นต่ำของจุดหมายที่ส่งจากเว็บ
        number('route_point_timeout', 600.0)  # s ต่อหนึ่งจุดของ Route (รวม Nav2 หยุดรอสิ่งกีดขวาง) เกิน = หยุด
        number('route_pass_radius', 0.9)      # m Route โหมด straight : รัศมีเลี้ยวโค้งมนที่จุด "ผ่านไปเลย" (ลดให้พอดีช่วงสั้น)
        number('route_loc_grace', 15.0)       # s Route: ตำแหน่งไม่น่าเชื่อถือ → ปรับเองแล้วรอได้เท่านี้ (0 = ไม่รอ)
        number('route_max_run', 7200.0)       # s รวมทั้งงานของ Route (ไม่นับเวลาที่พัก) เกิน = หยุด — กันวนไม่จบจนลืม
        self.declare_parameter('map_frame', 'map')
        self.declare_parameter('base_frame', 'base_link')
        # dynamic_typing: ให้รับ "ตัวเลข" ได้โดยไม่ล้ม (YAML ที่ไม่ใส่ quote จะถูกอ่านเป็นเลข)
        # แล้วค่อยปฏิเสธเองพร้อมข้อความที่เข้าใจง่าย
        self.declare_parameter('admin_pin', '', ParameterDescriptor(dynamic_typing=True))
        number('session_minutes', 30.0)
        self.declare_parameter('data_dir', '~/agv_app_data')
        launch = 'ros2 launch agv_s2_bringup '
        self.declare_parameter('cmd_mapping', launch + 'mapping_s2.launch.py rviz:=false')
        self.declare_parameter('cmd_manual', launch + 'ekf_s2.launch.py')
        self.declare_parameter('cmd_navigation', launch + 'nav_s2.launch.py rviz:=false')
        self.declare_parameter('nav_map_file', '')
        number('mode_ready_timeout_mapping', 40.0)
        number('mode_ready_timeout_manual', 30.0)
        number('mode_ready_timeout_navigation', 60.0)
        number('mode_sigint_timeout', 20.0)
        number('mode_sigterm_timeout', 10.0)
        number('mode_settle_timeout', 30.0)

        self.port = int(self.get_parameter('port').value)
        self.push_hz = self._num('push_hz')
        self.timeout = self._num('topic_timeout')
        self.loc_good = self._num('loc_good')
        self.loc_fair = self._num('loc_fair')
        self.moving_speed = self._num('moving_speed')
        self.map_frame = self.get_parameter('map_frame').value
        self.base_frame = self.get_parameter('base_frame').value

        self.drive = DriveController(
            max_linear=self._num('manual_max_linear'), max_angular=self._num('manual_max_angular'),
            default_linear=self._num('manual_default_linear'), default_angular=self._num('manual_default_angular'),
            linear_accel=self._num('manual_linear_accel'), angular_accel=self._num('manual_angular_accel'),
            linear_decel=self._num('manual_linear_decel'), angular_decel=self._num('manual_angular_decel'),
            heartbeat_timeout=self._num('manual_heartbeat_timeout'))
        self.overlay = LaserOverlay(max_points=int(self._num('scan_overlay_points')),
                                    on_error=lambda text: self.get_logger().warn(text))
        self._overlay_min_dt = 1.0 / max(0.5, self._num('scan_overlay_hz'))
        self._overlay_last = 0.0
        maps_dir = self.get_parameter('maps_dir').value or os.path.join(self.get_parameter('data_dir').value, 'maps')
        self.saver = MapSaver(maps_dir, on_error=lambda text: self.get_logger().warn(text))
        self.map_saved = {'hash': None, 'name': None, 'time': None}     # แผนที่ล่าสุดที่บันทึกแล้ว (เทียบด้วย hash)
        nav_map_file = os.path.expanduser(self.get_parameter('nav_map_file').value)
        if nav_map_file and not os.path.isfile(nav_map_file):
            self.get_logger().warn(f'nav_map_file ไม่พบไฟล์: {nav_map_file}')
        self.selection = MapSelection(
            os.path.join(self.get_parameter('data_dir').value, 'nav', 'selected_map.json'), self.saver,
            fallback=nav_map_file, on_error=lambda text: self.get_logger().warn(text))
        self.nav_map_in_use = None          # แผนที่ที่ Navigation เปิดไว้ล่าสุด (จำตอนเปิดโหมด)

        self._lock = threading.Lock()
        self._state = {
            'pose': None,           # (x, y, yaw_rad) จาก TF map→base_link
            'speed': None,          # (v, w) จาก odom
            'sigma': None,          # σ ตำแหน่งจาก /amcl_pose (m)
            'last_motion': None,    # เวลา (monotonic) ล่าสุดที่ความเร็วเกินเกณฑ์
            'nav2_up': False,       # พบ node bt_navigator ใน ROS graph
            'node_names': [],       # cache ชื่อ node ใน ROS graph (ให้ ModeManager อ่านข้าม thread)
            'seen': {'lidar': None, 'imu': None, 'ekf': None, 'amcl': None},
        }

        # QoS: sensor_data (best effort) รับได้ทั้ง publisher แบบ reliable/best effort
        self.create_subscription(
            Odometry, self.get_parameter('odom_topic').value,
            self._on_odom, qos_profile_sensor_data)
        self.create_subscription(
            LaserScan, self.get_parameter('scan_topic').value,
            self._on_scan, qos_profile_sensor_data)
        self.create_subscription(
            Imu, self.get_parameter('imu_topic').value,
            self._on_imu, qos_profile_sensor_data)
        # /amcl_pose ของ Nav2 เป็น reliable + transient local — ต้องตรงกันถึงจะเห็นข้อมูล
        amcl_qos = QoSProfile(depth=1,
                              reliability=ReliabilityPolicy.RELIABLE,
                              durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self.create_subscription(
            PoseWithCovarianceStamped, '/amcl_pose', self._on_amcl, amcl_qos)
        # /map ของ slam_toolbox / map_server เป็น reliable + transient local (latched) เช่นกัน
        self.live_map = LiveMap(on_error=lambda text: self.get_logger().warn(text))
        self._map_owner = None          # โหมดล่าสุดที่เห็น — เปลี่ยนโหมดแล้วลืมแผนที่เก่า (กันเห็นแผนที่ค้างเป็น "สด")
        self.create_subscription(
            OccupancyGrid, self.get_parameter('map_topic').value, self._on_map, amcl_qos)

        # ผู้ publish เดียวของเว็บแอป (ยังไม่มี joystick): ส่งความเร็ว 0 ตอน Reset all / ปิดแอป
        # เพื่อให้หุ่นหยุดก่อนปิดระบบ
        self._cmd_pub = self.create_publisher(Twist, self.get_parameter('cmd_vel_topic').value, 10)
        self._tf_buffer = Buffer()
        self._tf_listener = TransformListener(self._tf_buffer, self)
        self.create_timer(0.2, self._update_pose)
        self.create_timer(1.0, self._update_graph)
        self.history = SpeedHistory()
        self.create_timer(1.0 / SAMPLE_HZ, self._sample_history)

        pin = self.get_parameter('admin_pin').value
        if not isinstance(pin, str):
            # PIN แบบเลขจะเสียเลข 0 นำหน้า ("0000" → 0) จึงไม่ยอมรับ ปิดฟังก์ชันสั่งงานไว้ก่อน
            self.get_logger().error(
                'admin_pin ถูกอ่านเป็นตัวเลข — ต้องใส่เครื่องหมายคำพูด เช่น admin_pin: "1234" '
                '(ฟังก์ชันสั่งงานถูกปิดจนกว่าจะแก้)')
            pin = ''
        self.auth = AuthManager(pin, self._num('session_minutes'))
        self.slam = SlamParams(self._slam_defaults_path(), self.get_parameter('data_dir').value,
                               on_error=lambda text: self.get_logger().warn(text))
        # ค่า SLAM ที่ใช้ตอนเปิด Mapping ครั้งล่าสุด (ไว้แสดงเป็นอ่านอย่างเดียวระหว่างรัน)
        self.slam_in_use = None
        self.navp = NavParams(self._nav_defaults_path(), self.get_parameter('data_dir').value,
                              on_error=lambda text: self.get_logger().warn(text))
        self.nav_params_in_use = None       # ค่า Nav2 ที่ใช้อยู่ตอนนี้ (จำตอนเปิดโหมด และอัปเดตหลังปรับสด)
        self.navlive = NavLive(RosParamTransport(self, node_names()),
                               on_event=lambda text: self.get_logger().info(text))
        self.initial_pose = InitialPose(RosPosePublisher(self), on_event=lambda text: self.get_logger().info(text),
                                        refresher=RosNomotionUpdate(self))
        self.goal = NavGoal(RosGoalClient(self), on_event=lambda text: self.get_logger().info(text),
                            clearance=self._num('goal_clearance'), max_straight=self._num('drive_straight_max'))
        self.modes = self._make_mode_manager()
        self.startposes = StartPoses(os.path.join(self.get_parameter('data_dir').value, 'nav', 'start_poses.json'),
                                     self.saver.valid_name, on_error=lambda text: self.get_logger().warn(text))
        self.saved_maps = SavedMaps(self.saver, on_error=lambda text: self.get_logger().warn(text))
        self.routes = RouteStore(os.path.join(self.get_parameter('data_dir').value, 'nav', 'routes.json'),
                                 self.saver.valid_name, on_error=lambda text: self.get_logger().warn(text))
        self.autostart = AutoStart(self.startposes, self.initial_pose, self.live_map.meta,
                                   lambda: (self.nav_map_in_use or {}).get('name'),
                                   on_event=lambda text: self.get_logger().info(text))
        self.create_timer(1.0, lambda: self.autostart.tick(self.modes.snapshot()))
        self.stats = StatsStore(
            os.path.join(os.path.expanduser(self.get_parameter('data_dir').value), 'stats.json'),
            noise_speed=self.moving_speed,
            on_error=lambda text: self.get_logger().warn(text),
            on_event=lambda text: self.get_logger().info(text))
        self.runner = RouteRunner(self.goal, self.live_map.probe, self._localized_now, self._nav_up, self.stop_motion,
                                  on_round=self.stats.add_round, on_event=lambda text: self.get_logger().info(text),
                                  point_timeout=self._num('route_point_timeout'), max_run=self._num('route_max_run'),
                                  refresh=self.initial_pose.refresh, loc_grace=self._num('route_loc_grace'),
                                  pose=self._pose_now, path_probe=self.live_map.probe_path,
                                  pass_radius=self._num('route_pass_radius'))
        self.create_timer(1.0 / SAMPLE_HZ, self._update_stats)
        self.create_timer(0.05, self._drive_tick)                 # 20 Hz
        if not self.auth.enabled:
            self.get_logger().warn(
                'admin_pin ว่าง — ปิดฟังก์ชันสั่งงาน (เปลี่ยนโหมด) ทั้งหมด ดูสถานะได้อย่างเดียว')

        self.get_logger().info(
            f'web_server_node พร้อม — พอร์ต {self.port}, push {self.push_hz} Hz')

    def _nav_defaults_path(self):
        path = self.get_parameter('nav_defaults_file').value
        if path:
            return os.path.expanduser(path)
        try:
            share = get_package_share_directory('agv_s2_bringup')
            return os.path.join(share, 'config', 'nav2_params_s2.yaml')
        except Exception:                         # noqa: BLE001
            # ไม่พบแพ็กเกจ → ปิดการปรับค่า Nav2 (NavParams แจ้งเตือนเอง)
            return None

    def _slam_defaults_path(self):
        path = self.get_parameter('slam_defaults_file').value
        if path:
            return os.path.expanduser(path)
        try:
            share = get_package_share_directory('agv_s2_bringup')
            return os.path.join(share, 'config', 'mapper_params_s2_factory.yaml')
        except Exception:                         # noqa: BLE001
            # ไม่พบแพ็กเกจ → ปิดการปรับค่า SLAM (SlamParams แจ้งเตือนเอง)
            return None

    def _slam_extra_args(self):
        """อาร์กิวเมนต์ slam_params_file:= ตอนเปิด Mapping + จำค่าที่ใช้จริงไว้แสดงบนหน้าเว็บ"""
        args = self.slam.extra_args()
        self.slam_in_use = self.slam.effective() or None
        return args

    def _nav_extra_args(self):
        """อาร์กิวเมนต์ map_file:= ตอนเปิด Navigation (เลือกผ่านเว็บ > nav_map_file > default ของ launch)
        + จำแผนที่ที่ใช้จริงไว้แสดงบนหน้าเว็บ"""
        args, choice = self.selection.launch_args()
        self.nav_map_in_use = choice
        args = args + self.navp.extra_args()                    # nav2_params:= เมื่อผู้ใช้ปรับค่า Nav2 ไว้
        self.nav_params_in_use = self.navp.effective() or None
        return args

    def _make_mode_manager(self):
        def param(name):
            return self.get_parameter(name).value

        commands = {
            MAPPING: shlex.split(param('cmd_mapping')),
            MANUAL: shlex.split(param('cmd_manual')),
            NAVIGATION: shlex.split(param('cmd_navigation')),
        }
        data_dir = os.path.expanduser(param('data_dir'))
        return ModeManager(
            commands, self.node_names,
            os.path.join(data_dir, 'logs'),
            run_dir=os.path.join(data_dir, 'run'),
            stop_motion=self._publish_zero,
            sweep=sweep.stop_system_processes,
            extra_args={MAPPING: self._slam_extra_args, NAVIGATION: self._nav_extra_args},
            ready_timeout={MAPPING: self._num('mode_ready_timeout_mapping'),
                           MANUAL: self._num('mode_ready_timeout_manual'),
                           NAVIGATION: self._num('mode_ready_timeout_navigation')},
            sigint_timeout=self._num('mode_sigint_timeout'),
            sigterm_timeout=self._num('mode_sigterm_timeout'),
            settle_timeout=self._num('mode_settle_timeout'))

    def _publish_zero(self):
        """ส่งความเร็ว 0 ที่ /cmd_vel ~0.5 วินาที (เรียกจาก thread ของ ModeManager ; ล้มเหลวต้องไม่ขวางการปิดระบบ)"""
        self.drive.stop('reset')                  # ไม่ให้ตัวควบคุมจอยส่งคำสั่งทับระหว่างนี้
        zero = Twist()
        for _ in range(10):
            try:
                if not rclpy.ok():
                    return
                self._cmd_pub.publish(zero)
            except Exception:                     # noqa: BLE001
                return
            time.sleep(0.05)

    def _pose_now(self):
        """(x, y, yaw เรเดียน) ของหุ่นจาก TF map→base_link ; None ถ้ายังไม่รู้ (ใช้กับ Route โหมด straight)"""
        with self._lock:
            return self._state['pose']

    def _localized_now(self):
        """ตำแหน่งหุ่นเชื่อถือได้พอไหม (กติกาเดียวกับการส่ง goal : good/fair และมี pose)"""
        with self._lock:
            pose, sigma = self._state['pose'], self._state['sigma']
        return pose is not None and self._quality(sigma) in LOCALIZED

    def _nav_up(self):
        """Navigation รันนิ่งและ action server ของ Nav2 ยังอยู่ใน graph"""
        mode = self.modes.snapshot()
        return (mode['mode'] == 'NAVIGATION' and mode['state'] == 'RUNNING' and not mode.get('target')
                and self.goal.server_ready())

    def stop_motion(self):
        """ส่งความเร็ว 0 ซ้ำ (ชั้นที่สองต่อจากการยกเลิก goal) — บล็อก ~0.5 วินาที เรียกจากเธรดงาน"""
        self._publish_zero()

    def _num(self, name):
        """อ่านพารามิเตอร์ตัวเลขเป็น float ถ้าอ่านไม่ได้ให้แจ้ง error แล้วใช้ค่า default"""
        value = self.get_parameter(name).value
        try:
            return float(value)
        except (TypeError, ValueError):
            default = self._num_defaults[name]
            self.get_logger().error(
                f'พารามิเตอร์ {name} ต้องเป็นตัวเลข (ได้ "{value}") — ใช้ค่า default {default}')
            return default

    def node_names(self):
        """ชื่อ node ใน ROS graph จาก cache (ปลอดภัยเรียกจากทุก thread)"""
        with self._lock:
            return list(self._state['node_names'])

    # ── callbacks (รันใน thread ROS) ─────────────────────────────────────
    def _stamp(self, key):
        self._state['seen'][key] = time.monotonic()

    def _on_odom(self, msg):
        v, w = msg.twist.twist.linear.x, msg.twist.twist.angular.z
        with self._lock:
            self._state['speed'] = (v, w)
            if abs(v) > self.moving_speed or abs(w) > self.moving_speed:
                self._state['last_motion'] = time.monotonic()
            self._stamp('ekf')

    def _on_scan(self, scan):
        with self._lock:
            self._stamp('lidar')
        self._update_overlay(scan)

    def _update_overlay(self, scan):
        """แปลงสแกนเป็นจุดในเฟรม map ให้หน้าเว็บวาด (จำกัดความถี่ ~5 Hz ; ไม่มี TF ก็ข้ามไป)"""
        now = time.monotonic()
        if now - self._overlay_last < self._overlay_min_dt:
            return
        self._overlay_last = now
        try:
            tf = self._tf_buffer.lookup_transform(self.map_frame, scan.header.frame_id, rclpy.time.Time())
        except TransformException:
            return
        t = tf.transform.translation
        self.overlay.update(scan.ranges, scan.angle_min, scan.angle_increment, scan.range_min, scan.range_max,
                            (t.x, t.y, yaw_from_quaternion(tf.transform.rotation)))

    def _drive_tick(self):
        """publish ความเร็วจาก DriveController (เฉพาะตอนกำลังขับ/ช่วงส่งศูนย์หลังหยุด) ; โหมดไม่อนุญาต → หยุดทันที"""
        if self.drive.active and not drive_allowed(self.modes.snapshot()):
            self.drive.stop('mode')
        out = self.drive.tick()
        if out is None:
            return
        cmd = Twist()
        cmd.linear.x, cmd.angular.z = out
        try:
            self._cmd_pub.publish(cmd)
        except Exception:                         # noqa: BLE001 — context กำลังปิด
            pass

    def _on_imu(self, _msg):
        with self._lock:
            self._stamp('imu')

    def _on_amcl(self, msg):
        cov = msg.pose.covariance
        sigma = math.sqrt(max(cov[0], 0.0) + max(cov[7], 0.0))
        with self._lock:
            self._state['sigma'] = sigma
            self._stamp('amcl')

    def _on_map(self, msg):
        info = msg.info
        self.live_map.update(
            info.width, info.height, info.resolution,
            info.origin.position.x, info.origin.position.y,
            yaw_from_quaternion(info.origin.orientation), msg.data)

    def _update_pose(self):
        try:
            tf = self._tf_buffer.lookup_transform(
                self.map_frame, self.base_frame, rclpy.time.Time())
        except TransformException:
            pose = None
        else:
            t = tf.transform.translation
            pose = (t.x, t.y, yaw_from_quaternion(tf.transform.rotation))
        with self._lock:
            self._state['pose'] = pose

    def _sample_history(self):
        # ไม่มีข้อมูลความเร็ว (EKF เงียบเกิน timeout) → เก็บ None ให้เส้นกราฟขาดเป็นช่วง
        now = time.monotonic()
        with self._lock:
            seen = self._state['seen']['ekf']
            fresh = seen is not None and now - seen <= self.timeout
            speed = self._state['speed'] if fresh else None
        v, w = speed if speed else (None, None)
        self.history.add(time.time(), v, w)

    def _update_stats(self):
        now = time.monotonic()
        with self._lock:
            seen = self._state['seen']['ekf']
            fresh = seen is not None and now - seen <= self.timeout
            speed = self._state['speed'] if fresh else None
        snap = self.modes.snapshot()
        if snap['mode'] != self._map_owner:
            old, self._map_owner = self._map_owner, snap['mode']
            if map_clear_needed(old, snap['mode']):
                self.live_map.clear()
        mode = active_mode(snap)
        wall = time.time()
        self.stats.update(wall, None if speed is None else speed[0], mode)
        self.stats.save(wall)

    def _update_graph(self):
        # เรียกROS graph ที่นี่ (thread ROS) แล้วเก็บ cache ไว้ให้ thread อื่นอ่าน
        # "ระบบนำทางทำงานอยู่" = เห็น bt_navigator ใน graph (ขั้นต่ำ: โผล่ก่อน lifecycle active)
        names = [name for name, _ns in self.get_node_names_and_namespaces()]
        with self._lock:
            self._state['node_names'] = names
            self._state['nav2_up'] = 'bt_navigator' in names

    # ── ส่งออกให้เว็บ (เรียกจาก thread tornado) ──────────────────────────
    def _quality(self, sigma):
        if sigma is None:
            return 'unknown'
        if sigma <= self.loc_good:
            return 'good'
        if sigma <= self.loc_fair:
            return 'fair'
        return 'bad'

    @staticmethod
    def _readiness(nav2_up, alive, pose, quality):
        """แปลงข้อมูลดิบเป็น "พร้อมใช้งานไหม" (คำนวณที่นี่ที่เดียว ให้ทุก client ใช้ร่วมกัน)

        แต่ละข้อ: level = ok | warn | bad ; text = สิ่งที่เกิดขึ้น ; hint = สิ่งที่ควรทำ
        (text/hint เป็น msg() ให้หน้าเว็บแปลภาษา)
        เรียงตามลำดับความสำคัญ → hint ของข้อแรกที่ไม่ผ่านคือคำแนะนำหลัก
        """
        def item(key, level, text, hint=None):
            return {'key': key, 'level': level, 'text': text, 'hint': hint}

        items = []

        if nav2_up:
            items.append(item('nav2', 'ok', msg('ready.nav2.ok')))
        else:
            items.append(item('nav2', 'bad', msg('ready.nav2.bad'), msg('ready.nav2.hint')))

        labels = {'lidar': 'LiDAR', 'imu': 'IMU', 'ekf': 'EKF'}
        missing = [name for key, name in labels.items() if not alive[key]]
        if not missing:
            items.append(item('sensors', 'ok', msg('ready.sensors.ok')))
        else:
            items.append(item('sensors', 'bad', msg('ready.sensors.bad', missing=missing),
                              msg('ready.sensors.hint')))

        if pose is None:
            items.append(item('localization', 'bad', msg('ready.loc.none'),
                              msg('ready.loc.none.hint')))
        elif quality == 'bad':
            items.append(item('localization', 'bad', msg('ready.loc.bad'),
                              msg('ready.loc.bad.hint')))
        elif quality == 'fair':
            items.append(item('localization', 'warn', msg('ready.loc.fair')))
        elif quality == 'good':
            items.append(item('localization', 'ok', msg('ready.loc.good')))
        else:
            items.append(item('localization', 'ok', msg('ready.loc.ok')))

        failing = [i for i in items if i['level'] == 'bad']
        ready = not failing
        return {
            'ready': ready,
            'headline': msg('ready.headline.ready' if ready else 'ready.headline.not_ready'),
            'hint': None if ready else failing[0]['hint'],
            'items': items,
        }

    def snapshot(self):
        now = time.monotonic()
        with self._lock:
            st = self._state
            alive = {k: (v is not None and now - v <= self.timeout)
                     for k, v in st['seen'].items()}
            pose = st['pose']
            speed = st['speed'] if alive['ekf'] else None
            sigma = st['sigma']
            nav2_up = st['nav2_up']
            last_motion = st['last_motion']
        quality = self._quality(sigma)
        mode = self.modes.snapshot()
        map_meta = self.live_map.meta()
        readiness = self._readiness(nav2_up, alive, pose, quality)
        moving = (alive['ekf'] and last_motion is not None
                  and now - last_motion <= MOVING_HOLD_S)
        return {
            'mode': mode,
            'readiness': readiness,
            'activity': compute_activity(mode, readiness, moving),
            'time': time.time(),
            'map': {'available': bool(map_meta.get('available')),
                    'unsaved': bool(map_meta.get('available')) and map_meta.get('hash') != self.map_saved['hash'],
                    'saved_name': self.map_saved['name']},
            'pose': None if pose is None else {
                'x': round(pose[0], 3),
                'y': round(pose[1], 3),
                'yaw_deg': round(math.degrees(pose[2]), 1),
            },
            'speed': None if speed is None else {
                'v': round(speed[0], 3),
                'w': round(speed[1], 3),
            },
            'localization': {
                'quality': quality,
                'sigma': None if sigma is None else round(sigma, 3),
            },
            'sensors': {
                'lidar': alive['lidar'],
                'imu': alive['imu'],
                'ekf': alive['ekf'],
            },
        }


def main(args=None):
    rclpy.init(args=args)
    node = StatusNode()

    executor = SingleThreadedExecutor()
    executor.add_node(node)

    def spin_ros():
        try:
            executor.spin()
        except ExternalShutdownException:
            pass            # Ctrl+C / SIGTERM ปิด context → ปิดตามปกติ ไม่ใช่ข้อผิดพลาด
        except Exception:
            if rclpy.ok():  # โยน exception ทั้งที่ยังไม่ได้ปิด = ผิดปกติจริง
                raise

    ros_thread = threading.Thread(target=spin_ros, daemon=True)
    ros_thread.start()

    web_dir = os.path.join(get_package_share_directory('agv_web_app'), 'web')
    web_assets.check(web_dir)                                   # ชิ้นส่วนหน้าเว็บขาด/ผิด → ล้มตั้งแต่เริ่ม พร้อมเหตุผล
    app = make_app(node, web_dir)
    try:
        app.listen(node.port, address='0.0.0.0', max_body_size=4096)
    except OSError as e:
        node.get_logger().error(
            f'เปิดพอร์ต {node.port} ไม่ได้: {e} (มี web_server_node ตัวเก่าค้างอยู่หรือไม่?)')
        executor.shutdown()
        node.destroy_node()
        rclpy.shutdown()
        return

    def broadcast():
        if not StatusSocket.clients:
            return
        payload = json.dumps(node.snapshot())
        for client in list(StatusSocket.clients):
            try:
                client.write_message(payload)
            except tornado.websocket.WebSocketClosedError:
                StatusSocket.clients.discard(client)

    loop = tornado.ioloop.IOLoop.current()

    def stop_when_ros_down():
        # rclpy ดักสัญญาณ Ctrl+C/SIGTERM แล้วปิด context เอง แต่ tornado ไม่รู้ → ต้องคอยเช็ก
        if not rclpy.ok():
            loop.stop()

    tornado.ioloop.PeriodicCallback(broadcast, 1000.0 / node.push_hz).start()
    tornado.ioloop.PeriodicCallback(stop_when_ros_down, 200).start()
    node.get_logger().info(f'เปิดเว็บที่ http://<IP ของเครื่อง>:{node.port}')
    try:
        loop.start()
    except KeyboardInterrupt:
        pass
    finally:
        # ปิดระบบที่แอปเปิดไว้ก่อน (กันระบบกำพร้ายึดมอเตอร์/เซนเซอร์)
        # ใช้ print: ถึงตรงนี้ context ของ ROS มักถูกปิดแล้ว (logger จะฟ้อง rosout เสียงดัง)
        # Ctrl+C ครั้งเดียวส่ง SIGINT มา 2 ครั้ง (จาก terminal และจาก ros2 launch) ครั้งที่สองมาตอนกำลังปิดระบบ
        # → เคยทำให้ขั้นตอนปิดถูกขัดจังหวะแล้วระบบ Mapping/Nav2 ค้างกำพร้า จึงเมินสัญญาณจนปิดเสร็จ
        # (เวลาปิดมีเพดานอยู่แล้วใน ModeManager: SIGINT 20 s → SIGTERM 10 s → SIGKILL)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        print('web_server_node: กำลังปิดระบบที่แอปเปิดไว้ (ถ้ามี) อาจใช้เวลาถึง ~35 วินาที โปรดรอ…', flush=True)
        node.runner.stop()                       # Route ที่กำลังวิ่ง: ยกเลิก goal + ศูนย์ก่อนปิดระบบ
        node.drive.stop('shutdown')
        node.modes.shutdown()
        node.stats.save(force=True)
        executor.shutdown()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
