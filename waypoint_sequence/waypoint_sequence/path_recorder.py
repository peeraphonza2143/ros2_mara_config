"""
path_recorder.py — บันทึกเส้นทางจาก /amcl_pose ขณะขับด้วย teleop
กด Ctrl+C เพื่อหยุดและบันทึกไฟล์ YAML ที่ใช้กับ agv_waypoint_node ได้ทันที
"""

import math
import os
import yaml
from datetime import datetime

import rclpy
from rclpy.node import Node
from geometry_msgs.msg import PoseWithCovarianceStamped


# ── ค่าปรับแต่ง ──────────────────────────────────────────────────────────────
MIN_DIST = 0.20          # บันทึกจุดใหม่เมื่อหุ่นเคลื่อนที่ครบ (เมตร)
OUTPUT_DIR = os.path.expanduser('~/ros2_ws/src/my_robot_bringup/config')
# ─────────────────────────────────────────────────────────────────────────────


def quat_to_yaw(qz: float, qw: float) -> float:
    return math.atan2(2.0 * qw * qz, 1.0 - 2.0 * qz * qz)


class PathRecorder(Node):
    def __init__(self):
        super().__init__('path_recorder')
        self._poses: list[dict] = []
        self._last_x: float | None = None
        self._last_y: float | None = None

        self.create_subscription(
            PoseWithCovarianceStamped,
            '/amcl_pose',
            self._amcl_cb,
            10,
        )
        self.get_logger().info(
            f'[path_recorder] พร้อมแล้ว — ขับหุ่นยนต์ด้วย teleop ได้เลย\n'
            f'  บันทึกทุก {MIN_DIST} m | กด Ctrl+C เพื่อบันทึกไฟล์'
        )

    def _amcl_cb(self, msg: PoseWithCovarianceStamped) -> None:
        x = msg.pose.pose.position.x
        y = msg.pose.pose.position.y
        yaw = quat_to_yaw(
            msg.pose.pose.orientation.z,
            msg.pose.pose.orientation.w,
        )

        # บันทึกจุดแรกเสมอ
        if self._last_x is None:
            self._record(x, y, yaw)
            return

        dist = math.hypot(x - self._last_x, y - self._last_y)
        if dist >= MIN_DIST:
            self._record(x, y, yaw)

    def _record(self, x: float, y: float, yaw: float) -> None:
        idx = len(self._poses) + 1
        self._poses.append({'name': f'P{idx:03d}', 'x': round(x, 4),
                            'y': round(y, 4), 'yaw': round(yaw, 4)})
        self._last_x = x
        self._last_y = y
        self.get_logger().info(f'  บันทึก P{idx:03d}  x={x:.3f}  y={y:.3f}  yaw={yaw:.3f}')

    def save(self) -> str:
        if not self._poses:
            self.get_logger().warn('ไม่มีข้อมูลที่บันทึกไว้')
            return ''

        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f'agv_recorded_{timestamp}.yaml'
        filepath = os.path.join(OUTPUT_DIR, filename)

        os.makedirs(OUTPUT_DIR, exist_ok=True)
        with open(filepath, 'w') as f:
            # header comment
            f.write('# Recorded path — สร้างโดย path_recorder.py\n')
            f.write(f'# บันทึกเมื่อ: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}\n')
            f.write(f'# จำนวนจุด: {len(self._poses)}\n\n')
            yaml.dump({'waypoints': self._poses}, f,
                      default_flow_style=False, allow_unicode=True)

        self.get_logger().info(
            f'\n{"="*55}\n'
            f'  บันทึกสำเร็จ: {filepath}\n'
            f'  จำนวนจุด   : {len(self._poses)}\n'
            f'\n  วิธีใช้งาน:\n'
            f'  ros2 run waypoint_sequence agv_waypoint_node {filepath}\n'
            f'{"="*55}'
        )
        return filepath


def main() -> None:
    rclpy.init()
    node = PathRecorder()

    try:
        while rclpy.ok():
            rclpy.spin_once(node, timeout_sec=0.1)
    except KeyboardInterrupt:
        pass
    finally:
        node.save()
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
