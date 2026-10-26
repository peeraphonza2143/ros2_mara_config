#!/usr/bin/env python3
"""
agv_ros_node.py

ฝั่ง ROS ของ AGV Control App — ไม่มีโค้ด Qt อยู่ในไฟล์นี้เลย
หน้าจอ (agv_control_app.py) เป็นคนอ่านค่าจาก node นี้และเรียก method ต่าง ๆ

หน้าที่:
  - ฟัง /odom, /scan, /scan_filtered, /amcl_pose เพื่อทำไฟสถานะ + ความเร็ว + คุณภาพ localization
  - ถาม Nav2 ว่า active หรือยัง ผ่าน service /lifecycle_manager_navigation/is_active
  - ส่ง / ยกเลิก goal ของ action navigate_through_poses (แบบ callback ไม่ block หน้าจอ)
  - publish /cmd_vel (manual + หยุด) และ /initialpose (Set Home Pose)

ทุก callback ถูกเรียกจาก rclpy.spin_once() ที่หน้าจอเรียกผ่าน QTimer
จึงอยู่ใน thread เดียวกับ Qt ทั้งหมด
"""

import math
import time

from action_msgs.msg import GoalStatus
from geometry_msgs.msg import PoseStamped, PoseWithCovarianceStamped, Twist
from nav2_msgs.action import NavigateThroughPoses
from nav_msgs.msg import Odometry
from rclpy.action import ActionClient
from rclpy.node import Node
from rclpy.qos import (
    DurabilityPolicy, QoSProfile, ReliabilityPolicy, qos_profile_sensor_data
)
from sensor_msgs.msg import LaserScan
from std_srvs.srv import Trigger

# ผลลัพธ์ของ route ที่ส่งกลับไปให้หน้าจอ
RESULT_SUCCEEDED = 'SUCCEEDED'
RESULT_FAILED = 'FAILED'        # Nav2 ABORTED (เช่น หาเส้นทางไม่ได้ / recovery หมด)
RESULT_CANCELED = 'CANCELED'    # ถูกยกเลิก (จาก app เอง หรือจากที่อื่น เช่น RViz)
RESULT_REJECTED = 'REJECTED'    # Nav2 ไม่รับ goal


def yaw_to_quat(yaw: float):
    """yaw (rad) -> (z, w) ของ quaternion สำหรับหุ่นที่หมุนในแนวราบเท่านั้น"""
    return math.sin(yaw / 2.0), math.cos(yaw / 2.0)


def quat_to_yaw(z: float, w: float) -> float:
    return math.atan2(2.0 * w * z, 1.0 - 2.0 * z * z)


class AgvRosNode(Node):

    def __init__(self, cfg: dict):
        super().__init__('agv_control_app')
        self.cfg = cfg

        # เวลาล่าสุดที่ได้รับข้อความของแต่ละ topic (time.monotonic) — ใช้ทำไฟสถานะ
        self.last_rx = {'odom': None, 'scan': None, 'scan_filtered': None, 'amcl': None}

        self.linear_vel = 0.0
        self.angular_vel = 0.0
        self.amcl_pose = None      # (x, y, yaw) ใน frame map
        self.amcl_sigma = None     # ส่วนเบี่ยงเบนมาตรฐานของตำแหน่ง (m)
        self.nav2_active = False
        self.feedback = None       # NavigateThroughPoses.Feedback ล่าสุด

        # --- Subscribers ---
        self.create_subscription(Odometry, '/odom', self._odom_cb, 10)
        # lidar ส่งข้อมูลถี่และยอมทิ้งได้ → ใช้ sensor QoS (best effort)
        self.create_subscription(
            LaserScan, '/scan', lambda _: self._mark('scan'), qos_profile_sensor_data)
        self.create_subscription(
            LaserScan, '/scan_filtered', lambda _: self._mark('scan_filtered'),
            qos_profile_sensor_data)
        # AMCL publish /amcl_pose แบบ transient_local → subscribe ให้ตรงกัน
        # จะได้ค่าล่าสุดทันทีแม้ AMCL publish ไปก่อนที่ app จะเปิด
        amcl_qos = QoSProfile(
            depth=1,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
        )
        self.create_subscription(
            PoseWithCovarianceStamped, '/amcl_pose', self._amcl_cb, amcl_qos)

        # --- Publishers ---
        self.cmd_pub = self.create_publisher(Twist, '/cmd_vel', 10)
        self.initpose_pub = self.create_publisher(
            PoseWithCovarianceStamped, '/initialpose', 10)

        # --- Nav2 ---
        self.nav_client = ActionClient(self, NavigateThroughPoses, 'navigate_through_poses')
        self.is_active_cli = self.create_client(
            Trigger, '/lifecycle_manager_navigation/is_active')
        self._is_active_future = None
        self.create_timer(2.0, self._check_nav2_active)

        # สถานะของ goal ปัจจุบัน
        # _goal_seq เพิ่มทุกครั้งที่ส่ง goal ใหม่ ใช้ทิ้งผลลัพธ์ของ goal เก่าที่มาช้า
        self._goal_seq = 0
        self._goal_handle = None

    # ------------------------------------------------------------------ status
    def _mark(self, key: str):
        self.last_rx[key] = time.monotonic()

    def is_alive(self, key: str) -> bool:
        t = self.last_rx[key]
        return t is not None and (time.monotonic() - t) < self.cfg['topic_timeout']

    def _odom_cb(self, msg: Odometry):
        self._mark('odom')
        self.linear_vel = msg.twist.twist.linear.x
        self.angular_vel = msg.twist.twist.angular.z

    def _amcl_cb(self, msg: PoseWithCovarianceStamped):
        self._mark('amcl')
        p = msg.pose.pose
        self.amcl_pose = (p.position.x, p.position.y,
                          quat_to_yaw(p.orientation.z, p.orientation.w))
        cov = msg.pose.covariance
        self.amcl_sigma = math.sqrt(max(cov[0], cov[7], 0.0))   # var(x), var(y)

    def _check_nav2_active(self):
        # ถ้า request ก่อนหน้ายังไม่ตอบ ไม่ต้องส่งซ้อน
        if self._is_active_future is not None and not self._is_active_future.done():
            return
        if not self.is_active_cli.service_is_ready():
            self.nav2_active = False
            return
        self._is_active_future = self.is_active_cli.call_async(Trigger.Request())
        self._is_active_future.add_done_callback(self._is_active_done)

    def _is_active_done(self, future):
        try:
            self.nav2_active = bool(future.result().success)
        except Exception:  # noqa: BLE001 — service หายระหว่างรอคำตอบ
            self.nav2_active = False

    def amcl_listening(self) -> bool:
        """AMCL พร้อมรับ /initialpose หรือยัง (มี subscriber บน topic)

        ใช้แทนการรอ Nav2 active เพราะ Nav2 จะ active ไม่ได้จนกว่าจะมี initial pose
        (global costmap รอ TF map->base_link ซึ่ง AMCL จะส่งหลังได้ initial pose เท่านั้น)
        """
        return self.initpose_pub.get_subscription_count() > 0

    # ----------------------------------------------------------------- command
    def publish_cmd(self, v: float, w: float):
        msg = Twist()
        msg.linear.x = float(v)
        msg.angular.z = float(w)
        self.cmd_pub.publish(msg)

    def publish_initial_pose(self, x: float, y: float, yaw: float):
        msg = PoseWithCovarianceStamped()
        msg.header.frame_id = 'map'
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.pose.pose.position.x = float(x)
        msg.pose.pose.position.y = float(y)
        qz, qw = yaw_to_quat(yaw)
        msg.pose.pose.orientation.z = qz
        msg.pose.pose.orientation.w = qw
        # ความไม่แน่นอนเริ่มต้น (เหมือนค่าที่ RViz 2D Pose Estimate ส่ง)
        msg.pose.covariance[0] = self.cfg['initial_cov_xy']
        msg.pose.covariance[7] = self.cfg['initial_cov_xy']
        msg.pose.covariance[35] = self.cfg['initial_cov_yaw']
        self.initpose_pub.publish(msg)

    # -------------------------------------------------------------- navigation
    def nav_server_ready(self) -> bool:
        return self.nav_client.server_is_ready()

    def send_route(self, waypoints, on_done) -> bool:
        """
        ส่ง waypoints [(name, x, y, yaw), ...] เป็น goal เดียว
        on_done(result: str) จะถูกเรียกครั้งเดียวเมื่อ goal จบ (ถ้ายังเป็น goal ล่าสุด)
        """
        if not waypoints or not self.nav_server_ready():
            return False

        goal = NavigateThroughPoses.Goal()
        stamp = self.get_clock().now().to_msg()
        for _, x, y, yaw in waypoints:
            ps = PoseStamped()
            ps.header.frame_id = 'map'
            ps.header.stamp = stamp
            ps.pose.position.x = float(x)
            ps.pose.position.y = float(y)
            qz, qw = yaw_to_quat(yaw)
            ps.pose.orientation.z = qz
            ps.pose.orientation.w = qw
            goal.poses.append(ps)

        self._goal_seq += 1
        seq = self._goal_seq
        self._goal_handle = None
        self.feedback = None

        send_future = self.nav_client.send_goal_async(goal, feedback_callback=self._feedback_cb)
        send_future.add_done_callback(lambda f: self._goal_response(f, seq, on_done))
        return True

    def _goal_response(self, future, seq, on_done):
        if seq != self._goal_seq:
            # goal นี้ถูกยกเลิกหรือมี goal ใหม่กว่าแล้วระหว่างรอ Nav2 ตอบรับ
            # (เช่น กด Stop ทันทีหลังกด Start) → ยกเลิกทิ้งทันทีที่ได้ handle
            handle = future.result()
            if handle is not None and handle.accepted:
                handle.cancel_goal_async()
            return

        handle = future.result()
        if handle is None or not handle.accepted:
            on_done(RESULT_REJECTED)
            return

        self._goal_handle = handle
        handle.get_result_async().add_done_callback(
            lambda f: self._result_done(f, seq, on_done))

    def _result_done(self, future, seq, on_done):
        if seq != self._goal_seq:
            return  # ผลของ goal เก่า ไม่ต้องสนใจ
        self._goal_handle = None
        status = future.result().status
        if status == GoalStatus.STATUS_SUCCEEDED:
            on_done(RESULT_SUCCEEDED)
        elif status == GoalStatus.STATUS_CANCELED:
            on_done(RESULT_CANCELED)
        else:
            on_done(RESULT_FAILED)

    def _feedback_cb(self, feedback_msg):
        self.feedback = feedback_msg.feedback

    def cancel_route(self):
        """ยกเลิก goal ปัจจุบัน (ถ้ามี) และทำให้ผลลัพธ์ที่จะตามมาถูกทิ้ง"""
        if self._goal_handle is not None:
            self._goal_handle.cancel_goal_async()
            self._goal_handle = None
        # เพิ่ม seq เพื่อไม่ให้ on_done ของ goal นี้ไปเปลี่ยนสถานะหน้าจออีก
        self._goal_seq += 1
        self.feedback = None
