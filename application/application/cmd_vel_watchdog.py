#!/usr/bin/env python3
"""
cmd_vel_watchdog.py

Safety watchdog node for WiFi manual control mode.

Subscribes to /cmd_vel_web (commands coming from the web joystick via
rosbridge) and republishes them to /cmd_vel, but:
  - Clamps linear/angular values to safe limits.
  - If no new command arrives within `timeout_sec`, automatically publishes
    a zero-velocity Twist to stop the robot (protects against WiFi drop,
    browser freeze, tab close, etc).
  - Publishes a Bool on /wifi_control/client_connected so other nodes
    (e.g. the PyQt6 display) can show live connection status.
"""

import time

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy
from geometry_msgs.msg import Twist
from std_msgs.msg import Bool


class CmdVelWatchdog(Node):
    def __init__(self):
        super().__init__('cmd_vel_watchdog')

        # --- Parameters ---
        self.declare_parameter('input_topic', '/cmd_vel_web')
        self.declare_parameter('output_topic', '/cmd_vel')
        self.declare_parameter('timeout_sec', 0.5)      # stop if silent longer than this
        self.declare_parameter('check_rate_hz', 10.0)   # watchdog check frequency
        self.declare_parameter('max_linear_x', 0.8)      # safety clamp (m/s)
        self.declare_parameter('max_angular_z', 2.0)     # safety clamp (rad/s)

        input_topic = self.get_parameter('input_topic').value
        output_topic = self.get_parameter('output_topic').value
        self.timeout_sec = self.get_parameter('timeout_sec').value
        check_rate = self.get_parameter('check_rate_hz').value
        self.max_linear_x = self.get_parameter('max_linear_x').value
        self.max_angular_z = self.get_parameter('max_angular_z').value

        qos = QoSProfile(
            reliability=ReliabilityPolicy.RELIABLE,
            history=HistoryPolicy.KEEP_LAST,
            depth=10
        )

        self.last_msg_time = None
        self.is_active = False  # whether we are currently allowing motion

        self.sub = self.create_subscription(
            Twist, input_topic, self.cmd_vel_callback, qos)
        self.pub = self.create_publisher(Twist, output_topic, qos)
        self.status_pub = self.create_publisher(
            Bool, '/wifi_control/client_connected', qos)

        period = 1.0 / check_rate
        self.timer = self.create_timer(period, self.watchdog_check)

        self.get_logger().info(
            f'Watchdog started: {input_topic} -> {output_topic} '
            f'(timeout={self.timeout_sec}s, '
            f'clamp linear={self.max_linear_x}, angular={self.max_angular_z})'
        )

    def clamp(self, value, limit):
        return max(-limit, min(limit, value))

    def cmd_vel_callback(self, msg: Twist):
        # Safety clamp in case the client sends an out-of-range value
        msg.linear.x = self.clamp(msg.linear.x, self.max_linear_x)
        msg.angular.z = self.clamp(msg.angular.z, self.max_angular_z)

        self.last_msg_time = time.monotonic()

        if not self.is_active:
            self.get_logger().info('Client connected: allowing motion commands')
            self.is_active = True
            self.status_pub.publish(Bool(data=True))

        self.pub.publish(msg)

    def watchdog_check(self):
        if self.last_msg_time is None:
            return  # never received a command yet, nothing to do

        elapsed = time.monotonic() - self.last_msg_time
        if elapsed > self.timeout_sec and self.is_active:
            self.get_logger().warn(
                f'No cmd_vel received for {elapsed:.2f}s -> STOPPING robot'
            )
            self.pub.publish(Twist())  # all-zero Twist
            self.is_active = False
            self.status_pub.publish(Bool(data=False))


def main(args=None):
    rclpy.init(args=args)
    node = CmdVelWatchdog()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        # Publish one last stop command on shutdown, just in case
        node.pub.publish(Twist())
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
