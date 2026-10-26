import rclpy
from rclpy.node import Node
from geometry_msgs.msg import Twist
import time


# ──────────────────────────────────────────────
#  calib_radius.py
#  ใช้ทดสอบ wheel_radius
#
#  วิธีคำนวณ (ทำเองหลัง script หยุด):
#
#    new_radius = old_radius × (ระยะจริง / ระยะที่คาดหวัง)
#
#  ตัวอย่าง:
#    old_radius      = 0.1016 m
#    ระยะที่คาดหวัง  = 0.2 × 5 = 1.00 m
#    ระยะจริงที่วัดได้ = 0.97 m
#    new_radius      = 0.1016 × (0.97 / 1.00) = 0.0985 m
# ──────────────────────────────────────────────

LINEAR_SPEED = 0.2   # m/s  — ความเร็วเดินหน้า
DURATION     = 5.0   # วินาที


def main(args=None):
    rclpy.init(args=args)
    node = Node('calib_radius_node')
    pub  = node.create_publisher(Twist, '/cmd_vel', 10)

    # รอ publisher พร้อม
    time.sleep(1.0)

    print('─' * 40)
    print('  Calibrate wheel_radius')
    print('─' * 40)
    print(f'  ความเร็ว  : {LINEAR_SPEED} m/s')
    print(f'  เวลา      : {DURATION} วินาที')
    print(f'  ระยะคาดหวัง: {LINEAR_SPEED * DURATION:.2f} m')
    print()
    input('▶  ทำเครื่องหมายจุดเริ่มต้น แล้วกด Enter...')

    # ── ส่งคำสั่งเดินหน้า ──────────────────────
    msg = Twist()
    msg.linear.x = LINEAR_SPEED

    end_time = time.time() + DURATION
    while time.time() < end_time:
        pub.publish(msg)
        time.sleep(0.1)   # 10 Hz

    # หยุด
    pub.publish(Twist())
    print()
    print('  หยุดแล้ว ✓')
    print()
    print('  วัดระยะทางจริง แล้วคำนวณ:')
    print('  new_radius = 0.1016 × (ระยะจริง / '
          f'{LINEAR_SPEED * DURATION:.2f})')
    print('─' * 40)

    node.destroy_node()
    rclpy.shutdown()


if __name__ == '__main__':
    main()
