"""
node ROS ปลอม ใช้ทดสอบ ModeManager กับ ROS graph จริง (แทน nav_s2 / mapping_s2 / ekf_s2)

ใช้: python3 fake_ros_node.py <ชื่อ node>
  ⚠ รันใน ROS_DOMAIN_ID แยกเสมอ (เช่น 77) ไม่ให้ชื่อ node ปลอมไปปนกับระบบหุ่นจริง
"""
import sys

import rclpy
from rclpy.node import Node


def main():
    rclpy.init()
    node = Node(sys.argv[1])
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == '__main__':
    main()
