# ROS2 driver สำหรับ WitMotion HWT901B (TTL) → sensor_msgs/Imu
# ต้องตั้งค่า IMU ด้วย imu_config ก่อน (6 แกน, 115200 baud, 100 Hz, packet 0x51/0x52/0x53)
#
# Packet (11 byte): 0x55 <type> <8 byte data = int16 x4 little-endian> <checksum>
#   0x51 accel  : ax ay az temp   → value / 32768 * 16 g
#   0x52 gyro   : wx wy wz temp   → value / 32768 * 2000 deg/s
#   0x53 angle  : roll pitch yaw version → value / 32768 * 180 deg
#
# แกนของ IMU: X = ขวา, Y = หน้า, Z = บน (ดู datasheet หน้า 9)
# node นี้ publish ตามแกนของตัว IMU ใน frame imu_link
# การหมุนให้ตรงกับ base_link (X หน้า, Y ซ้าย) ทำด้วย static TF base_link → imu_link
import math
import struct
import threading
import time

import rclpy
from rclpy.node import Node
from sensor_msgs.msg import Imu
import serial

GRAVITY = 9.80665          # m/s^2
FRAME_LEN = 11
HEADER = 0x55
TYPE_ACCEL = 0x51
TYPE_GYRO = 0x52
TYPE_ANGLE = 0x53


def quaternion_from_rpy(roll, pitch, yaw):
    """Euler ลำดับ Z-Y-X (ตาม datasheet) → quaternion (x, y, z, w)."""
    cr, sr = math.cos(roll / 2), math.sin(roll / 2)
    cp, sp = math.cos(pitch / 2), math.sin(pitch / 2)
    cy, sy = math.cos(yaw / 2), math.sin(yaw / 2)
    return (sr * cp * cy - cr * sp * sy,
            cr * sp * cy + sr * cp * sy,
            cr * cp * sy - sr * sp * cy,
            cr * cp * cy + sr * sp * sy)


def diag3(a, b, c):
    return [a, 0.0, 0.0,
            0.0, b, 0.0,
            0.0, 0.0, c]


class Hwt901bNode(Node):
    def __init__(self):
        super().__init__('hwt901b_node')

        self.declare_parameter('port', '/dev/imu')
        self.declare_parameter('baud', 115200)
        self.declare_parameter('frame_id', 'imu_link')

        # covariance (variance) — ค่าเริ่มต้นสำหรับปรับจูนตอนทำ EKF
        # roll/pitch แม่นจาก accel อ้างอิงแรงโน้มถ่วง (~1 deg)
        # yaw ในโหมด 6 แกนเป็นค่าสะสมจาก gyro → drift ได้ จึงเชื่อน้อยกว่า
        self.declare_parameter('orientation_cov_rp', 0.0003)    # (1 deg)^2 ใน rad^2
        self.declare_parameter('orientation_cov_yaw', 0.01)     # (~6 deg)^2
        self.declare_parameter('angular_velocity_cov', 0.0001)  # (rad/s)^2
        self.declare_parameter('linear_acceleration_cov', 0.01)  # (0.1 m/s^2)^2
        # false = ไม่ส่ง orientation (covariance[0] = -1 ตาม REP-145)
        self.declare_parameter('publish_orientation', True)

        self.port = self.get_parameter('port').value
        self.baud = self.get_parameter('baud').value
        self.frame_id = self.get_parameter('frame_id').value
        self.publish_orientation = self.get_parameter('publish_orientation').value

        cov_rp = self.get_parameter('orientation_cov_rp').value
        cov_yaw = self.get_parameter('orientation_cov_yaw').value
        cov_w = self.get_parameter('angular_velocity_cov').value
        cov_a = self.get_parameter('linear_acceleration_cov').value
        self.orientation_cov = (diag3(cov_rp, cov_rp, cov_yaw)
                                if self.publish_orientation
                                else [-1.0] + [0.0] * 8)
        self.angular_velocity_cov = diag3(cov_w, cov_w, cov_w)
        self.linear_acceleration_cov = diag3(cov_a, cov_a, cov_a)

        self.pub = self.create_publisher(Imu, 'imu/data', 10)

        # ค่าล่าสุดของแต่ละ packet ในรอบนี้ (publish เมื่อ 0x53 มาถึง)
        self.accel = None
        self.gyro = None

        # สถิติสำหรับ log ทุก 10 วินาที
        self.n_published = 0
        self.n_bad_checksum = 0
        self.create_timer(10.0, self.log_stats)

        # อ่าน serial ใน thread แยก เพื่อไม่ให้ข้อมูลค้างใน buffer
        self.running = True
        self.reader = threading.Thread(target=self.read_loop, daemon=True)
        self.reader.start()

    # =========================================================
    # read_loop: เปิด port → อ่าน → แยก frame (reconnect เองถ้าหลุด)
    # =========================================================
    def read_loop(self):
        while self.running and rclpy.ok():
            try:
                with serial.Serial(self.port, self.baud, timeout=0.1) as ser:
                    self.get_logger().info(f'Opened {self.port} @ {self.baud} baud')
                    ser.reset_input_buffer()
                    buf = bytearray()
                    while self.running and rclpy.ok():
                        buf += ser.read(ser.in_waiting or 1)
                        buf = self.consume_frames(buf)
            except (serial.SerialException, OSError) as e:
                self.get_logger().warn(f'Serial error: {e} → reconnect in 1 s')
                time.sleep(1.0)

    def consume_frames(self, buf):
        """แยก frame ที่ครบออกจาก buf แล้วคืนส่วนที่เหลือ (ยังไม่ครบ frame)."""
        i = 0
        while len(buf) - i >= FRAME_LEN:
            if buf[i] != HEADER:
                i += 1                       # resync: หา 0x55 ตัวถัดไป
                continue
            frame = buf[i:i + FRAME_LEN]
            if (sum(frame[:10]) & 0xFF) != frame[10]:
                self.n_bad_checksum += 1
                i += 1                       # 0x55 นี้อาจเป็นแค่ data → เลื่อน 1 byte
                continue
            self.handle_frame(frame[1], frame[2:10])
            i += FRAME_LEN
        return buf[i:]

    def handle_frame(self, ptype, data):
        # '<hhhh' = int16 x4 แบบ little-endian, signed (ค่าติดลบได้)
        v = struct.unpack('<hhhh', data)
        if ptype == TYPE_ACCEL:
            self.accel = [x / 32768.0 * 16.0 * GRAVITY for x in v[:3]]
        elif ptype == TYPE_GYRO:
            self.gyro = [math.radians(x / 32768.0 * 2000.0) for x in v[:3]]
        elif ptype == TYPE_ANGLE:
            rpy = [math.radians(x / 32768.0 * 180.0) for x in v[:3]]
            self.publish(rpy)

    # =========================================================
    # publish: รวม accel + gyro + angle ของรอบเดียวกันเป็น 1 message
    # =========================================================
    def publish(self, rpy):
        if self.accel is None or self.gyro is None:
            return                          # ยังได้ไม่ครบทุก packet ตั้งแต่เริ่ม

        msg = Imu()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.header.frame_id = self.frame_id

        if self.publish_orientation:
            qx, qy, qz, qw = quaternion_from_rpy(*rpy)
            msg.orientation.x = qx
            msg.orientation.y = qy
            msg.orientation.z = qz
            msg.orientation.w = qw
        msg.orientation_covariance = self.orientation_cov

        msg.angular_velocity.x, msg.angular_velocity.y, msg.angular_velocity.z = self.gyro
        msg.angular_velocity_covariance = self.angular_velocity_cov

        (msg.linear_acceleration.x, msg.linear_acceleration.y,
         msg.linear_acceleration.z) = self.accel
        msg.linear_acceleration_covariance = self.linear_acceleration_cov

        self.pub.publish(msg)
        self.n_published += 1

    def log_stats(self):
        self.get_logger().info(
            f'published {self.n_published / 10.0:.1f} Hz | '
            f'bad checksum {self.n_bad_checksum}')
        self.n_published = 0

    def destroy_node(self):
        self.running = False
        self.reader.join(timeout=1.0)
        super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
    node = Hwt901bNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
