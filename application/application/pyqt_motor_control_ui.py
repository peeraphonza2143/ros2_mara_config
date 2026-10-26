#!/usr/bin/env python3
"""
pyqt_motor_control_ui.py

หน้าจอควบคุมหุ่นยนต์แบบ manual ด้วย PyQt6 - กดปุ่มบนจอหรือคีย์บอร์ดปุ่มลูกศร
ค้างไว้เพื่อขับ ปล่อยเพื่อหยุดทันที (dead-man's switch ในตัว ไม่ต้องพึ่ง
watchdog เพราะเป็นการควบคุมแบบ local ติดกับตัวหุ่นยนต์ ไม่ใช่ผ่านเครือข่าย)

Publish geometry_msgs/Twist ไปที่ /cmd_vel โดยตรง (Drive_Motor/drive_odom
subscribe topic นี้อยู่แล้ว)
"""

import subprocess
import sys

import rclpy
from rclpy.node import Node
from geometry_msgs.msg import Twist

from PyQt6.QtWidgets import (
    QApplication, QWidget, QPushButton, QLabel, QGridLayout, QVBoxLayout,
    QHBoxLayout, QMessageBox
)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont, QKeyEvent

KEY_TO_DIRECTION = {
    Qt.Key.Key_Up: 'forward',
    Qt.Key.Key_Down: 'backward',
    Qt.Key.Key_Left: 'left',
    Qt.Key.Key_Right: 'right',
}

# ระดับความเร็ว 1-5 -> (linear m/s, angular rad/s) ปรับพร้อมกันทั้งคู่ต่อระดับ
# index 0 = ระดับ 1 ... index 4 = ระดับ 5
SPEED_LEVELS = [
    (0.10, 0.4),
    (0.20, 0.7),
    (0.30, 1.0),
    (0.40, 1.3),
    (0.50, 1.6),
]
DEFAULT_SPEED_LEVEL = 3


class MotorControlNode(Node):
    """เก็บแค่ publisher กับพารามิเตอร์ระดับความเร็วเริ่มต้น ไม่ยุ่งกับ Qt โดยตรง"""

    def __init__(self):
        super().__init__('pyqt_motor_control_ui')
        self.declare_parameter('speed_level', DEFAULT_SPEED_LEVEL)  # 1-5

        level = self.get_parameter('speed_level').value
        self.speed_level = min(max(int(level), 1), len(SPEED_LEVELS))

        self.cmd_pub = self.create_publisher(Twist, '/cmd_vel', 10)

    def publish_cmd(self, v: float, w: float):
        msg = Twist()
        msg.linear.x = v
        msg.angular.z = w
        self.cmd_pub.publish(msg)


class ManualControlWindow(QWidget):
    """
    active_directions เก็บทิศทางที่ "กำลังถูกกด" อยู่ตอนนี้ (จากปุ่มหรือคีย์บอร์ด
    รวมกันเป็น set เดียว) แล้วคำนวณ v/w ใหม่ทุกครั้งที่ set เปลี่ยน - กด forward+left
    พร้อมกันจะได้ทั้งเดินหน้าและเลี้ยวพร้อมกัน (เหมือนรถบังคับจริง)
    """

    def __init__(self, publish_callback, initial_level=DEFAULT_SPEED_LEVEL):
        super().__init__()
        self.publish_callback = publish_callback
        self.speed_level = initial_level
        self.linear_speed, self.angular_speed = SPEED_LEVELS[self.speed_level - 1]
        self.active_directions = set()

        self.setWindowTitle("AMR Manual Control")
        self.setStyleSheet("background-color: #111111;")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)  # ต้องโฟกัสหน้าต่างก่อนถึงรับ key event
        self.showFullScreen()

        self._init_ui()

        # publish ซ้ำระหว่างกดค้าง (10 Hz) เผื่อ subscriber ฝั่ง drive_odom พลาด msg แรกไป
        self.publish_timer = QTimer()
        self.publish_timer.timeout.connect(self._publish_current_velocity)
        self.publish_timer.start(100)

    def _init_ui(self):
        title = QLabel("MANUAL CONTROL")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("color: #00aaff;")
        title.setFont(QFont("Arial", 28, QFont.Weight.Bold))

        hint = QLabel("ปุ่มบนจอ หรือคีย์บอร์ดปุ่มลูกศร (▲▼◄►) - กดค้างเพื่อขับ ปล่อยเพื่อหยุด | ESC ปิดหน้าจอ")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setStyleSheet("color: #aaaaaa;")
        hint.setFont(QFont("Arial", 14))

        # --- แถวปรับระดับความเร็ว: [-] SPEED LEVEL: x/5 [+] แล้วโชว์ค่า m/s, rad/s ที่ตั้งไว้ ---
        btn_speed_down = QPushButton("−")
        btn_speed_down.setFont(QFont("Arial", 20, QFont.Weight.Bold))
        btn_speed_down.setFixedSize(60, 60)
        btn_speed_down.setStyleSheet(
            "background-color: #2a2a2a; color: white; border-radius: 8px;")
        btn_speed_down.clicked.connect(lambda: self._change_speed_level(-1))

        btn_speed_up = QPushButton("+")
        btn_speed_up.setFont(QFont("Arial", 20, QFont.Weight.Bold))
        btn_speed_up.setFixedSize(60, 60)
        btn_speed_up.setStyleSheet(
            "background-color: #2a2a2a; color: white; border-radius: 8px;")
        btn_speed_up.clicked.connect(lambda: self._change_speed_level(1))

        self.speed_level_label = QLabel()
        self.speed_level_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.speed_level_label.setStyleSheet("color: #ffffff;")
        self.speed_level_label.setFont(QFont("Arial", 20, QFont.Weight.Bold))
        self.speed_level_label.setFixedWidth(180)

        speed_row = QGridLayout()
        speed_row.setSpacing(15)
        speed_row.addWidget(btn_speed_down, 0, 0)
        speed_row.addWidget(self.speed_level_label, 0, 1)
        speed_row.addWidget(btn_speed_up, 0, 2)

        self.speed_value_label = QLabel()
        self.speed_value_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.speed_value_label.setStyleSheet("color: #aaaaaa;")
        self.speed_value_label.setFont(QFont("Arial", 14))

        self._update_speed_labels()

        # --- แถวบนขวา: ปุ่ม Restart / Shutdown ตัวเครื่อง (แยกจากปุ่มขับ กันกดพลาด) ---
        btn_restart = QPushButton("⟲ RESTART")
        btn_restart.setFont(QFont("Arial", 12, QFont.Weight.Bold))
        btn_restart.setFixedSize(130, 44)
        btn_restart.setStyleSheet(
            "background-color: #cc8800; color: white; border-radius: 8px;")
        btn_restart.clicked.connect(self._on_restart_clicked)

        btn_shutdown = QPushButton("⏻ SHUTDOWN")
        btn_shutdown.setFont(QFont("Arial", 12, QFont.Weight.Bold))
        btn_shutdown.setFixedSize(130, 44)
        btn_shutdown.setStyleSheet(
            "background-color: #990000; color: white; border-radius: 8px;")
        btn_shutdown.clicked.connect(self._on_shutdown_clicked)

        top_bar = QHBoxLayout()
        top_bar.addStretch()
        top_bar.addWidget(btn_restart)
        top_bar.addWidget(btn_shutdown)

        btn_forward = self._make_button("▲ FORWARD", 'forward')
        btn_backward = self._make_button("▼ BACKWARD", 'backward')
        btn_left = self._make_button("◄ LEFT", 'left')
        btn_right = self._make_button("► RIGHT", 'right')

        btn_stop = QPushButton("STOP")
        btn_stop.setFont(QFont("Arial", 20, QFont.Weight.Bold))
        btn_stop.setFixedSize(140, 90)
        btn_stop.setStyleSheet(
            "background-color: #cc2222; color: white; border-radius: 10px;")
        btn_stop.clicked.connect(self._on_stop_clicked)

        grid = QGridLayout()
        grid.setSpacing(15)
        grid.addWidget(btn_forward, 0, 1)
        grid.addWidget(btn_left, 1, 0)
        grid.addWidget(btn_stop, 1, 1)
        grid.addWidget(btn_right, 1, 2)
        grid.addWidget(btn_backward, 2, 1)

        layout = QVBoxLayout()
        layout.addLayout(top_bar)
        layout.addWidget(title)
        layout.addWidget(hint)
        layout.addSpacing(10)
        layout.addLayout(speed_row)
        layout.setAlignment(speed_row, Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self.speed_value_label)
        layout.addStretch()
        layout.addLayout(grid)
        layout.addStretch()
        self.setLayout(layout)

    def _make_button(self, text, direction):
        btn = QPushButton(text)
        btn.setFont(QFont("Arial", 16, QFont.Weight.Bold))
        btn.setFixedSize(140, 90)
        btn.setStyleSheet(
            "background-color: #2a2a2a; color: white; border-radius: 10px;")
        # กด/ปล่อยเมาส์ ใช้ logic เดียวกับคีย์บอร์ด ผ่าน active_directions set เดียวกัน
        btn.pressed.connect(lambda d=direction: self._add_direction(d))
        btn.released.connect(lambda d=direction: self._remove_direction(d))
        return btn

    def _change_speed_level(self, delta):
        new_level = min(max(self.speed_level + delta, 1), len(SPEED_LEVELS))
        if new_level == self.speed_level:
            return  # ชนขอบบน/ล่างแล้ว ไม่ต้องทำอะไรต่อ
        self.speed_level = new_level
        self.linear_speed, self.angular_speed = SPEED_LEVELS[self.speed_level - 1]
        self._update_speed_labels()
        # ถ้ากำลังกดปุ่มขับอยู่ตอนปรับความเร็ว ให้ publish ค่าใหม่ทันทีโดยไม่ต้องรอปล่อยปุ่ม
        self._publish_current_velocity()

    def _update_speed_labels(self):
        self.speed_level_label.setText(f"SPEED: {self.speed_level}/{len(SPEED_LEVELS)}")
        self.speed_value_label.setText(
            f"Linear: {self.linear_speed:.2f} m/s   |   Angular: {self.angular_speed:.2f} rad/s")

    def _add_direction(self, direction):
        self.active_directions.add(direction)
        self._publish_current_velocity()

    def _remove_direction(self, direction):
        self.active_directions.discard(direction)
        self._publish_current_velocity()

    def _on_stop_clicked(self):
        self.active_directions.clear()
        self.publish_callback(0.0, 0.0)

    def _publish_current_velocity(self):
        forward = 'forward' in self.active_directions
        backward = 'backward' in self.active_directions
        left = 'left' in self.active_directions
        right = 'right' in self.active_directions

        if forward and not backward:
            v = self.linear_speed
        elif backward and not forward:
            v = -self.linear_speed
        else:
            v = 0.0

        if left and not right:
            w = self.angular_speed
        elif right and not left:
            w = -self.angular_speed
        else:
            w = 0.0

        self.publish_callback(v, w)

    def _on_restart_clicked(self):
        self._confirm_and_run("รีสตาร์ท", "reboot")

    def _on_shutdown_clicked(self):
        self._confirm_and_run("ปิด", "poweroff")

    def _confirm_and_run(self, action_label, systemctl_arg):
        # ยืนยันก่อนเสมอ เพราะ poweroff/reboot ทำลาย session ทันที ย้อนกลับไม่ได้
        reply = QMessageBox.question(
            self, f"ยืนยัน{action_label}เครื่อง",
            f"ต้องการ{action_label}เครื่องตอนนี้เลยหรือไม่?\nหุ่นยนต์จะหยุดทำงานทันที",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self.active_directions.clear()
        self.publish_callback(0.0, 0.0)  # หยุดหุ่นยนต์ให้ปลอดภัยก่อนเครื่องดับ

        try:
            subprocess.run(['systemctl', systemctl_arg], check=True)
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            QMessageBox.critical(
                self, "ทำรายการไม่สำเร็จ",
                f"เรียกคำสั่ง systemctl {systemctl_arg} ไม่สำเร็จ:\n{e}\n\n"
                "ลองรันคำสั่งนี้ตรง ๆ ใน terminal เพื่อตรวจสิทธิ์:\n"
                f"systemctl {systemctl_arg}",
            )

    def keyPressEvent(self, event: QKeyEvent):
        if event.isAutoRepeat():
            return  # กัน OS ส่ง repeat event รัว ๆ ตอนกดค้าง
        if event.key() == Qt.Key.Key_Escape:
            self.close()
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

    def closeEvent(self, event):
        # ปิดหน้าต่างต้องหยุดหุ่นยนต์ก่อนเสมอ กันเคสปิดหน้าจอตอนกำลังขับอยู่
        self.publish_callback(0.0, 0.0)
        super().closeEvent(event)


def main(args=None):
    rclpy.init(args=args)
    app = QApplication(sys.argv)

    node = MotorControlNode()
    window = ManualControlWindow(
        publish_callback=node.publish_cmd,
        initial_level=node.speed_level,
    )
    window.show()

    # spin rclpy จาก QTimer เพื่อใช้ event loop เดียวกับ Qt (เหมือน display_ui.py)
    ros_timer = QTimer()
    ros_timer.timeout.connect(lambda: rclpy.spin_once(node, timeout_sec=0))
    ros_timer.start(50)  # ~20 Hz

    exit_code = app.exec()

    node.publish_cmd(0.0, 0.0)  # หยุดหุ่นยนต์ก่อน shutdown เสมอ
    node.destroy_node()
    rclpy.shutdown()
    sys.exit(exit_code)


if __name__ == '__main__':
    main()
