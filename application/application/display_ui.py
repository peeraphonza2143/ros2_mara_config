#!/usr/bin/env python3
"""
display_ui.py

PyQt6 fullscreen status display for the robot's onboard screen.
Shows "WIFI CONTROL MODE" plus live connection status, driven by
/wifi_control/client_connected (std_msgs/Bool) published by
cmd_vel_watchdog.py.

Requires a working X11/Wayland session (DISPLAY env var set) to run,
so make sure the mini PC is logged into a desktop session (e.g. via
auto-login) before this node starts.
"""

import sys

import rclpy
from rclpy.node import Node
from std_msgs.msg import Bool

from PyQt6.QtWidgets import QApplication, QWidget, QLabel, QVBoxLayout
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont


class DisplayNode(Node):
    """Thin ROS2 node: just subscribes and forwards status to the Qt window."""

    def __init__(self, update_callback):
        super().__init__('wifi_display_ui')
        self.declare_parameter('ssid', 'AMR_Robot')
        self.declare_parameter('ip', '192.168.4.1')
        self.update_callback = update_callback
        self.create_subscription(
            Bool, '/wifi_control/client_connected', self.status_cb, 10)

    def status_cb(self, msg: Bool):
        self.update_callback(msg.data)


class WifiControlWindow(QWidget):
    def __init__(self, ssid="AMR_Robot", ip="192.168.4.1"):
        super().__init__()
        self.ssid = ssid
        self.ip = ip
        self.init_ui()

    def init_ui(self):
        self.setWindowTitle("AMR Control Status")
        self.setStyleSheet("background-color: #111111;")
        self.showFullScreen()  # fullscreen on the robot's onboard display

        layout = QVBoxLayout()
        layout.setSpacing(20)

        self.title_label = QLabel("\U0001F4F6  WIFI CONTROL MODE")
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title_label.setStyleSheet("color: #00aaff;")
        self.title_label.setFont(QFont("Arial", 48, QFont.Weight.Bold))

        self.info_label = QLabel(f"SSID: {self.ssid}   |   IP: {self.ip}")
        self.info_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.info_label.setStyleSheet("color: #aaaaaa;")
        self.info_label.setFont(QFont("Arial", 18))

        self.status_label = QLabel("\u25CF  WAITING FOR CONNECTION")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setStyleSheet("color: #ffaa00;")
        self.status_label.setFont(QFont("Arial", 28, QFont.Weight.Bold))

        layout.addStretch()
        layout.addWidget(self.title_label)
        layout.addWidget(self.info_label)
        layout.addStretch()
        layout.addWidget(self.status_label)
        layout.addStretch()

        self.setLayout(layout)

    def set_connected(self, connected: bool):
        if connected:
            self.status_label.setText("\u25CF  CLIENT CONNECTED - CONTROLLING")
            self.status_label.setStyleSheet("color: #00ff66;")
        else:
            self.status_label.setText("\u25CF  WAITING FOR CONNECTION")
            self.status_label.setStyleSheet("color: #ffaa00;")

    def keyPressEvent(self, event):
        # ESC closes the window - useful for debugging, consider removing
        # or locking this down for a real deployment.
        if event.key() == Qt.Key.Key_Escape:
            self.close()


def main(args=None):
    rclpy.init(args=args)
    app = QApplication(sys.argv)

    # Read ssid/ip parameters via a small helper node
    temp_node = Node('wifi_display_param_loader')
    temp_node.declare_parameter('ssid', 'AMR_Robot')
    temp_node.declare_parameter('ip', '192.168.4.1')
    ssid = temp_node.get_parameter('ssid').value
    ip = temp_node.get_parameter('ip').value
    temp_node.destroy_node()

    window = WifiControlWindow(ssid=ssid, ip=ip)
    window.show()

    node = DisplayNode(update_callback=window.set_connected)

    # Drive rclpy spinning from a QTimer so it shares the Qt event loop
    # instead of needing a separate thread.
    ros_timer = QTimer()
    ros_timer.timeout.connect(lambda: rclpy.spin_once(node, timeout_sec=0))
    ros_timer.start(50)  # ~20 Hz

    exit_code = app.exec()

    node.destroy_node()
    rclpy.shutdown()
    sys.exit(exit_code)


if __name__ == '__main__':
    main()
