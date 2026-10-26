import math
import rclpy
from rclpy.node import Node
from geometry_msgs.msg import Twist
from nav_msgs.msg import Odometry
from std_msgs.msg import Bool


class ShuttleNode(Node):
    def __init__(self):
        super().__init__('shuttle_node')

        self.declare_parameter('linear_speed', 0.17)     # m/s
        self.declare_parameter('travel_distance', 1.0)  # m ต่อ 1 leg (เดินหน้า หรือ ถอยหลัง)
        self.declare_parameter('control_rate_hz', 20.0)  # ให้ตรงกับ odom_node (20Hz)
        # accel_limit: อัตราเร่ง/หน่วงสูงสุดที่ยอมให้ cmd_vel เปลี่ยนได้ต่อวินาที (m/s^2)
        # ใช้ทำ ramp ความเร็วแบบนุ่มนวล กันหุ่นยนต์กระตุกตอนออกตัว/หยุด/สลับทิศ
        # ค่า 0.2 m/s^2 -> จาก 0 ถึง linear_speed(0.2 m/s) ใช้เวลา ~1 วินาที
        self.declare_parameter('accel_limit', 0.17)

        self.linear_speed = self.get_parameter('linear_speed').value
        self.travel_distance = self.get_parameter('travel_distance').value
        rate_hz = self.get_parameter('control_rate_hz').value
        self.accel_limit = self.get_parameter('accel_limit').value
        self.dt = 1.0 / rate_hz

        # --- state machine: 'FORWARD' <-> 'BACKWARD' วนไม่รู้จบ ---
        self.state = 'FORWARD'
        self.loop_count = 1

        # --- ตัวแปรอ้างอิงระยะทาง (อ่านจาก /odom) ---
        self.have_odom = False
        self.current_x = 0.0
        self.current_y = 0.0
        self.ref_x = 0.0
        self.ref_y = 0.0

        # --- สถานะ safety จาก lidar_safety_node ---
        # ตั้งค่าเริ่มต้นเป็น "หยุด" (fail-safe): ห้ามขยับจนกว่าจะได้รับ
        # ข้อความจาก /obstacle_stop อย่างน้อย 1 ครั้งก่อน เผื่อกรณี
        # lidar_safety_node ยังไม่ทันสตาร์ท หรือไม่ได้ถูกรันเลย
        self.obstacle_stop = True
        self.safety_seen = False

        # ความเร็วที่ publish ออกจริง (หลัง ramp) เริ่มจาก 0 เสมอ
        self.commanded_speed = 0.0

        self.cmd_pub = self.create_publisher(Twist, '/cmd_vel', 10)
        self.odom_sub = self.create_subscription(
            Odometry, '/odom', self.odom_callback, 10)
        self.stop_sub = self.create_subscription(
            Bool, '/obstacle_stop', self.stop_callback, 10)

        self.create_timer(1.0 / rate_hz, self.control_loop)

        self.get_logger().info(
            f'shuttle_node เริ่มทำงาน (speed={self.linear_speed} m/s, '
            f'distance={self.travel_distance} m/leg) รอสัญญาณ odom และ lidar_safety_node...')

    def odom_callback(self, msg: Odometry):
        self.current_x = msg.pose.pose.position.x
        self.current_y = msg.pose.pose.position.y
        if not self.have_odom:
            # จุดเริ่มต้นของ leg แรก (FORWARD) คือตำแหน่งปัจจุบันตอนได้ odom ครั้งแรก
            self.ref_x = self.current_x
            self.ref_y = self.current_y
            self.have_odom = True

    def stop_callback(self, msg: Bool):
        self.obstacle_stop = msg.data
        self.safety_seen = True

    # =========================================================
    # control_loop: state machine เดินหน้า-ถอยหลัง 1 เมตรวนซ้ำ
    # หา target_speed ก่อน แล้วค่อย ramp ความเร็วจริงที่ publish เข้าหา
    # target_speed ทีละนิดตาม accel_limit กันกระตุก (ใช้ ramp เดียวกัน
    # ทั้งตอนออกตัว, สลับทิศ, และตอนโดน obstacle สั่งหยุดด้วย)
    # =========================================================
    def control_loop(self):
        # ยังไม่พร้อม (รอ odom ครั้งแรก / รอสัญญาณ safety ครั้งแรก / โดน obstacle บัง)
        # → เป้าหมายคือหยุด (0) แต่ยังไม่ขยับ reference point ของ leg ปัจจุบัน
        if not self.have_odom or not self.safety_seen or self.obstacle_stop:
            target_speed = 0.0
        else:
            distance = math.hypot(self.current_x - self.ref_x, self.current_y - self.ref_y)
            if distance >= self.travel_distance:
                self._switch_leg()
            target_speed = self.linear_speed if self.state == 'FORWARD' else -self.linear_speed

        self.commanded_speed = self._ramp_towards(self.commanded_speed, target_speed)

        twist = Twist()
        twist.linear.x = self.commanded_speed
        twist.angular.z = 0.0
        self.cmd_pub.publish(twist)

    def _ramp_towards(self, current: float, target: float) -> float:
        max_step = self.accel_limit * self.dt
        diff = target - current
        if abs(diff) <= max_step:
            return target
        return current + math.copysign(max_step, diff)

    def _switch_leg(self):
        # ตำแหน่งปัจจุบันกลายเป็นจุดอ้างอิงของ leg ถัดไป
        self.ref_x = self.current_x
        self.ref_y = self.current_y

        if self.state == 'FORWARD':
            self.state = 'BACKWARD'
            self.get_logger().info(
                f'[รอบที่ {self.loop_count}] ครบ {self.travel_distance}m → ถอยหลังกลับจุดเดิม')
        else:
            self.state = 'FORWARD'
            self.loop_count += 1
            self.get_logger().info(f'[รอบที่ {self.loop_count}] กลับถึงจุดเดิม → เดินหน้าอีกครั้ง')


def main(args=None):
    rclpy.init(args=args)
    node = ShuttleNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        # หยุดหุ่นยนต์ให้สนิทก่อน shutdown ทุกครั้ง กัน cmd_vel ค้างความเร็วสุดท้าย
        node.cmd_pub.publish(Twist())
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
