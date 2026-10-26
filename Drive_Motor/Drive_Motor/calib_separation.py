import rclpy
from rclpy.node import Node
from geometry_msgs.msg import Twist
import time
import math


# ──────────────────────────────────────────────
#  calib_separation.py
#  ใช้ทดสอบ wheel_separation
#
#  วิธีคำนวณ (ทำเองหลัง script หยุด):
#
#    new_separation = old_separation × (360 / องศาจริง)
#
#  ตัวอย่าง:
#    old_separation = 0.470 m
#    หมุนได้จริง    = 340°
#    new_separation = 0.470 × (360 / 340) = 0.4977 m
#
#  ตีความ:
#    องศาจริง < 360 → L เล็กเกินไป → new_L มากขึ้น
#    องศาจริง > 360 → L ใหญ่เกินไป → new_L น้อยลง
# ──────────────────────────────────────────────

ANGULAR_SPEED = 0.5                              # rad/s — ความเร็วหมุน
DURATION      = (2 * math.pi) / ANGULAR_SPEED   # 1 รอบ = 360° ≈ 12.6 วินาที


def main(args=None):
    rclpy.init(args=args)
    node = Node('calib_separation_node')
    pub  = node.create_publisher(Twist, '/cmd_vel', 10)

    time.sleep(1.0)

    print('─' * 48)
    print('  Calibrate wheel_separation  (1 รอบ / 360°)')
    print('─' * 48)
    print(f'  ความเร็วหมุน : {ANGULAR_SPEED} rad/s')
    print(f'  เวลา         : {DURATION:.1f} วินาที')
    print()
    print('  วิธีเตรียม:')
    print('  → วาง robot ให้หันหน้าตั้งฉากกับกำแพง ระยะ ~50 cm')
    print('  → ทำเครื่องหมายทิศทางหน้า robot บนพื้น (เส้น / เทป)')
    print()
    input('▶  พร้อมแล้วกด Enter...')

    msg = Twist()
    msg.angular.z = ANGULAR_SPEED

    end_time = time.time() + DURATION
    while time.time() < end_time:
        pub.publish(msg)
        time.sleep(0.1)

    pub.publish(Twist())
    print()
    print('  หยุดแล้ว ✓')
    print()
    print('  วัดมุมที่ robot หันอยู่เทียบกับเส้นเดิม:')
    print('  → ถ้าหน้า robot ตรงเส้นพอดี = 360°  (ไม่ต้องแก้)')
    print('  → ถ้าหน้า robot เลยเส้นไป   = มากกว่า 360°')
    print('  → ถ้าหน้า robot ยังไม่ถึงเส้น = น้อยกว่า 360°')
    print()
    print('  คำนวณ:')
    print('  new_separation = old_separation × (360 / องศาจริง)')
    print('─' * 48)

    node.destroy_node()
    rclpy.shutdown()


if __name__ == '__main__':
    main()
