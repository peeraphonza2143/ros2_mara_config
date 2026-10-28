import rclpy
from rclpy.node import Node
from geometry_msgs.msg import Twist
from sensor_msgs.msg import JointState
from zlac8015d.ZLAC8015D import Controller
import math

from Drive_Motor.watchdog import CmdWatchdog

class ZlacDriverNode(Node):
    def __init__(self):
        super().__init__('zlac_driver_node')

        # --- พารามิเตอร์หุ่นยนต์ (ต้องวัดจากตัวจริง) ---
        # wheel_radius: รัศมีล้อ 8 นิ้ว = 0.1016 เมตร
        # wheel_separation: ระยะห่างระหว่างกึ่งกลางล้อซ้าย-ขวา
        self.declare_parameter('serial_port', '/dev/motor_rs485')
        self.declare_parameter('wheel_separation', 0.50)   # เมตร
        self.declare_parameter('wheel_radius', 0.1016)     # เมตร (ล้อ 8 นิ้ว)
        self.declare_parameter('max_rpm', 60.0)
        # วินาที: ไม่ได้รับ /cmd_vel นานเกินนี้ → สั่งมอเตอร์หยุด ; 0.0 = ปิด (พฤติกรรมเดิม มอเตอร์วิ่งค้างตามคำสั่งล่าสุด)
        self.declare_parameter('cmd_timeout', 0.0)

        port         = self.get_parameter('serial_port').value
        self.L       = self.get_parameter('wheel_separation').value
        self.R       = self.get_parameter('wheel_radius').value
        self.max_rpm = self.get_parameter('max_rpm').value
        self.watchdog = CmdWatchdog(self.get_parameter('cmd_timeout').value)
        self._watchdog_warned = False

        # --- ลำดับ init มอเตอร์ (ห้ามสลับขั้นตอน) ---
        # disable ก่อนเสมอ → ตั้งค่า → enable
        self.motors = Controller(port=port)
        self.motors.disable_motor()
        self.motors.set_accel_time(200, 200)   # ms: ความนุ่มนวลตอนเร่ง
        self.motors.set_decel_time(200, 200)   # ms: ความนุ่มนวลตอนหยุด
        self.motors.set_mode(3)                # mode 3 = velocity control
        self.motors.enable_motor()
        self.get_logger().info('ZLAC8015D connected')

        # --- pub/sub ---
        self.cmd_sub = self.create_subscription(
            Twist, '/cmd_vel', self.cmd_callback, 10)

        self.joint_pub = self.create_publisher(
            JointState, '/joint_states', 10)

        # --- timer อ่าน encoder 20 Hz (ทุก 0.05 วินาที) ---
        self.create_timer(0.05, self.encoder_callback)
        if self.watchdog.enabled:
            self.get_logger().info(
                f'cmd_timeout = {self.watchdog.timeout:.2f} s (ไม่มี /cmd_vel เกินนี้ → หยุดมอเตอร์)')

    def _now_s(self):
        return self.get_clock().now().nanoseconds * 1e-9

    def check_watchdog(self):
        """หมดเวลาแล้วสั่งหยุด ; สั่งไม่สำเร็จ (RS485 ขาด) จะลองใหม่ทุกรอบจนกว่าจะสำเร็จ"""
        now = self._now_s()
        if not self.watchdog.expired(now):
            return
        try:
            self.motors.set_rpm(0, 0)
        except Exception as e:
            if not self._watchdog_warned:
                self.get_logger().warn(f'watchdog สั่งหยุดไม่สำเร็จ (จะลองใหม่): {e}')
                self._watchdog_warned = True
            return
        self.watchdog.mark_tripped()
        self._watchdog_warned = False
        self.get_logger().warn(
            f'ไม่ได้รับ /cmd_vel นาน {self.watchdog.age(now):.2f} s (> cmd_timeout) → หยุดมอเตอร์')

    def cmd_callback(self, msg):
        self.watchdog.feed(self._now_s())
        v = msg.linear.x    # ความเร็วเชิงเส้น (m/s)
        w = msg.angular.z   # ความเร็วเชิงมุม (rad/s)

        # Differential Drive Kinematics: แปลง v, w → ความเร็วล้อแต่ละข้าง (m/s)
        left_vel  = v - (w * self.L / 2.0)
        right_vel = v + (w * self.L / 2.0)

        # แปลง m/s → RPM  (RPM = vel / circumference * 60)
        # ล้อซ้ายคูณ -1 เพราะติดตั้งหันกลับด้านกับล้อขวา (mechanical flip)
        left_rpm  = -1.0 * (left_vel  * 60.0) / (2.0 * math.pi * self.R)
        right_rpm =         (right_vel * 60.0) / (2.0 * math.pi * self.R)

        # จำกัด RPM ไม่เกินค่าสูงสุด เพื่อป้องกันมอเตอร์รับโหลดเกิน
        left_rpm  = max(-self.max_rpm, min(self.max_rpm, left_rpm))
        right_rpm = max(-self.max_rpm, min(self.max_rpm, right_rpm))

        # ส่งคำสั่งไปบอร์ด พร้อม error handling กรณี RS485 ขาดหาย
        try:
            self.motors.set_rpm(int(left_rpm), int(right_rpm))
        except Exception as e:
            self.get_logger().warn(f'Motor communication error: {e}')

    def encoder_callback(self):
        self.check_watchdog()
        # อ่านค่า RPM จริงจากมอเตอร์ผ่าน RS485
        l_rpm, r_rpm = self.motors.get_rpm()

        # publish /joint_states เพื่อให้ RViz / Nav2 อ่านความเร็วล้อได้
        js = JointState()
        js.header.stamp = self.get_clock().now().to_msg()
        js.name     = ['left_wheel_joint', 'right_wheel_joint']
        js.velocity = [
            l_rpm * 2.0 * math.pi / 60.0,   # RPM → rad/s
            r_rpm * 2.0 * math.pi / 60.0
        ]
        self.joint_pub.publish(js)

def main(args=None):
    rclpy.init(args=args)
    node = ZlacDriverNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        # หยุดมอเตอร์ก่อน shutdown ทุกครั้ง
        node.motors.set_rpm(0, 0)
        node.motors.disable_motor()
        node.destroy_node()
        rclpy.shutdown()

if __name__ == '__main__':
    main()
