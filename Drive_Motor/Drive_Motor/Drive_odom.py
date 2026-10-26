import rclpy
from rclpy.node import Node
from geometry_msgs.msg import Twist, TransformStamped
from nav_msgs.msg import Odometry
from sensor_msgs.msg import JointState
from tf2_ros import TransformBroadcaster
from zlac8015d.ZLAC8015D import Controller
import math


class ZlacOdomNode(Node):
    def __init__(self):
        super().__init__('zlac_odom_node')

        # --- พารามิเตอร์หุ่นยนต์ (ต้องวัดจากตัวจริง) ---
        self.declare_parameter('serial_port', '/dev/motor_rs485')
        self.declare_parameter('wheel_separation', 0.455)  # เมตร (calibrated)
        self.declare_parameter('wheel_radius', 0.06538)     # เมตร (ล้อ 8 นิ้ว, calibrated)
        self.declare_parameter('max_rpm', 60.0)

        port         = self.get_parameter('serial_port').value
        self.L       = self.get_parameter('wheel_separation').value
        self.R       = self.get_parameter('wheel_radius').value
        self.max_rpm = self.get_parameter('max_rpm').value

        # --- ลำดับ init มอเตอร์ (ห้ามสลับขั้นตอน) ---
        self.motors = Controller(port=port)
        self.motors.disable_motor()
        self.motors.set_accel_time(200, 200)
        self.motors.set_decel_time(200, 200)
        self.motors.set_mode(3)   # mode 3 = velocity control
        self.motors.enable_motor()
        self.get_logger().info('ZLAC8015D connected')

        # --- ตัวแปรสะสมตำแหน่ง (odometry state) ---
        # เริ่มต้นที่จุดกำเนิด (0, 0) หันหน้าไปทาง +X
        self.x     = 0.0
        self.y     = 0.0
        self.theta = 0.0   # rad (yaw)
        self.last_time = self.get_clock().now()

        # --- Publishers ---
        self.odom_pub  = self.create_publisher(Odometry,    '/odom',         10)
        self.joint_pub = self.create_publisher(JointState,  '/joint_states', 10)

        # TF broadcaster ส่ง transform odom → base_link ให้ Nav2 / RViz ใช้
        self.tf_broadcaster = TransformBroadcaster(self)

        # --- Subscriber ---
        self.cmd_sub = self.create_subscription(
            Twist, '/cmd_vel', self.cmd_callback, 10)

        # --- timer 20 Hz: อ่าน encoder + คำนวณ + publish ---
        self.create_timer(0.05, self.odom_callback)

    # =========================================================
    # cmd_callback: รับ /cmd_vel แล้วส่ง RPM ไปมอเตอร์
    # =========================================================
    def cmd_callback(self, msg):
        v = msg.linear.x    # m/s
        w = msg.angular.z   # rad/s

        # Differential Drive Kinematics: v, w → ความเร็วล้อแต่ละข้าง (m/s)
        left_vel  = v - (w * self.L / 2.0)
        right_vel = v + (w * self.L / 2.0)

        # m/s → RPM  |  ล้อซ้ายคูณ -1 เพราะติดตั้งหันกลับด้าน
        left_rpm  = -1.0 * (left_vel  * 60.0) / (2.0 * math.pi * self.R)
        right_rpm =         (right_vel * 60.0) / (2.0 * math.pi * self.R)

        left_rpm  = max(-self.max_rpm, min(self.max_rpm, left_rpm))
        right_rpm = max(-self.max_rpm, min(self.max_rpm, right_rpm))

        try:
            self.motors.set_rpm(int(left_rpm), int(right_rpm))
        except Exception as e:
            self.get_logger().warn(f'Motor communication error: {e}')

    # =========================================================
    # odom_callback: อ่าน encoder → dead reckoning → publish
    # =========================================================
    def odom_callback(self):
        now = self.get_clock().now()
        dt  = (now - self.last_time).nanoseconds / 1e9   # วินาที
        self.last_time = now

        # อ่าน RPM จริงจากมอเตอร์
        try:
            l_rpm, r_rpm = self.motors.get_rpm()
        except Exception as e:
            self.get_logger().warn(f'Encoder read error: {e}')
            return

        # RPM → ความเร็วล้อ (m/s)  |  ล้อซ้ายกลับสัญญาณ เพราะหันกลับด้าน
        left_vel  = -1.0 * l_rpm * 2.0 * math.pi * self.R / 60.0
        right_vel =         r_rpm * 2.0 * math.pi * self.R / 60.0

        # ความเร็วหุ่นยนต์รวม (Inverse Diff-Drive Kinematics)
        v = (left_vel  + right_vel) / 2.0   # เชิงเส้น (m/s)
        w = (right_vel - left_vel)  / self.L  # เชิงมุม (rad/s)

        # Dead Reckoning: สะสมตำแหน่งจากความเร็ว × เวลา (Euler integration)
        self.x     += v * math.cos(self.theta) * dt
        self.y     += v * math.sin(self.theta) * dt
        self.theta += w * dt

        # Quaternion จาก yaw (หมุนรอบแกน Z เท่านั้น → qx=qy=0)
        qz = math.sin(self.theta / 2.0)
        qw = math.cos(self.theta / 2.0)

        # --- Publish /odom ---
        odom = Odometry()
        odom.header.stamp    = now.to_msg()
        odom.header.frame_id = 'odom'        # frame อ้างอิง
        odom.child_frame_id  = 'base_link'   # frame ของตัวหุ่นยนต์

        # ตำแหน่งสะสม
        odom.pose.pose.position.x    = self.x
        odom.pose.pose.position.y    = self.y
        odom.pose.pose.orientation.z = qz
        odom.pose.pose.orientation.w = qw

        # ความเร็วปัจจุบัน
        odom.twist.twist.linear.x  = v
        odom.twist.twist.angular.z = w

        self.odom_pub.publish(odom)

        # --- Broadcast TF: odom → base_link ---
        # Nav2 ต้องการ TF นี้เพื่อรู้ว่าหุ่นยนต์อยู่ที่ไหนใน map
        tf_msg = TransformStamped()
        tf_msg.header.stamp    = now.to_msg()
        tf_msg.header.frame_id = 'odom'
        tf_msg.child_frame_id  = 'base_link'
        tf_msg.transform.translation.x = self.x
        tf_msg.transform.translation.y = self.y
        tf_msg.transform.rotation.z    = qz
        tf_msg.transform.rotation.w    = qw

        self.tf_broadcaster.sendTransform(tf_msg)

        # --- Publish /joint_states (RPM → rad/s สำหรับ RViz) ---
        js = JointState()
        js.header.stamp = now.to_msg()
        js.name     = ['left_wheel_joint', 'right_wheel_joint']
        js.velocity = [
            l_rpm * 2.0 * math.pi / 60.0,
            r_rpm * 2.0 * math.pi / 60.0
        ]
        self.joint_pub.publish(js)


def main(args=None):
    rclpy.init(args=args)
    node = ZlacOdomNode()
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
