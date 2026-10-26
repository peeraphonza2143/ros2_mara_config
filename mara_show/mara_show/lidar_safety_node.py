import math
import rclpy
from rclpy.node import Node
from sensor_msgs.msg import LaserScan
from std_msgs.msg import Bool


class LidarSafetyNode(Node):
    def __init__(self):
        super().__init__('lidar_safety_node')

        # --- พารามิเตอร์ตำแหน่ง LiDAR เทียบกับ base_link ---
        # ค่าต้องตรงกับ static_transform_publisher ใน
        # my_robot_bringup/launch/my_robot_bringup.launch.py (base_link_to_laser)
        # ถ้าย้ายตำแหน่งติดตั้ง LiDAR ในอนาคต ต้องแก้ 2 ที่นี้ให้ตรงกัน
        self.declare_parameter('laser_offset_x', 0.27)
        self.declare_parameter('laser_offset_y', 0.0)
        self.declare_parameter('laser_yaw', math.pi)

        # --- พารามิเตอร์ขนาดหุ่นยนต์ ---
        # ค่าต้องตรงกับ footprint ใน my_robot_bringup/config/nav2_params.yaml
        # (กว้าง 0.575m ยาว 0.65m → ครึ่งความยาว/ความกว้างตามด้านล่าง)
        self.declare_parameter('robot_half_length', 0.325)
        self.declare_parameter('robot_half_width', 0.2875)

        # --- พารามิเตอร์ safety ---
        self.declare_parameter('scan_topic', '/scan_filtered')
        self.declare_parameter('safety_distance', 0.9)
        self.declare_parameter('debounce_count', 3)

        self.laser_offset_x = self.get_parameter('laser_offset_x').value
        self.laser_offset_y = self.get_parameter('laser_offset_y').value
        self.laser_yaw = self.get_parameter('laser_yaw').value
        self.half_length = self.get_parameter('robot_half_length').value
        self.half_width = self.get_parameter('robot_half_width').value
        self.safety_distance = self.get_parameter('safety_distance').value
        self.debounce_count = self.get_parameter('debounce_count').value
        scan_topic = self.get_parameter('scan_topic').value

        self.cos_yaw = math.cos(self.laser_yaw)
        self.sin_yaw = math.sin(self.laser_yaw)

        # ตัวนับ debounce กัน object กระพริบ/noise จาก sensor
        # ต้องเจอ (หรือไม่เจอ) ติดกันครบ debounce_count scan ถึงจะเปลี่ยนสถานะจริง
        self.hit_count = 0
        self.clear_count = 0
        self.stop_state = False

        self.stop_pub = self.create_publisher(Bool, '/obstacle_stop', 10)
        self.scan_sub = self.create_subscription(
            LaserScan, scan_topic, self.scan_callback, 10)

        self.get_logger().info(
            f'lidar_safety_node เริ่มทำงาน (scan_topic={scan_topic}, '
            f'safety_distance={self.safety_distance}m)')

    # =========================================================
    # scan_callback: รับ LaserScan → เช็คโซนหน้า/หลัง → publish /obstacle_stop
    # =========================================================
    def scan_callback(self, msg: LaserScan):
        front_hit = False
        back_hit = False

        # ขอบเขตโซนตรวจจับ (พิกัด base_link, แกน +X = หน้าหุ่นยนต์)
        front_near = self.half_length
        front_far = self.half_length + self.safety_distance
        back_near = -self.half_length
        back_far = -(self.half_length + self.safety_distance)

        angle = msg.angle_min
        for r in msg.ranges:
            # ข้ามจุดที่อ่านค่าไม่ได้ (inf/nan) หรืออยู่นอกช่วงวัดของเซนเซอร์
            if math.isfinite(r) and msg.range_min <= r <= msg.range_max:
                # polar (r, angle) ในเฟรม laser → Cartesian ในเฟรม laser
                lx = r * math.cos(angle)
                ly = r * math.sin(angle)

                # แปลงจากเฟรม laser → base_link ด้วย static transform
                # (หมุนด้วย laser_yaw แล้วเลื่อนด้วย laser_offset)
                bx = self.laser_offset_x + self.cos_yaw * lx - self.sin_yaw * ly
                by = self.laser_offset_y + self.sin_yaw * lx + self.cos_yaw * ly

                # เช็คเฉพาะแนวกว้างตรงตัวรถ (ตัด object ด้านข้างออกอัตโนมัติ)
                if -self.half_width <= by <= self.half_width:
                    if front_near <= bx <= front_far:
                        front_hit = True
                    elif back_far <= bx <= back_near:
                        back_hit = True

            angle += msg.angle_increment

            if front_hit and back_hit:
                break  # เจอครบทั้งสองโซนแล้ว ไม่ต้องวนต่อ

        self._update_stop_state(front_hit, back_hit)

    def _update_stop_state(self, front_hit: bool, back_hit: bool):
        any_hit = front_hit or back_hit

        if any_hit:
            self.hit_count += 1
            self.clear_count = 0
        else:
            self.clear_count += 1
            self.hit_count = 0

        if not self.stop_state and self.hit_count >= self.debounce_count:
            self.stop_state = True
            side = 'หน้า+หลัง' if front_hit and back_hit else ('หน้า' if front_hit else 'หลัง')
            self.get_logger().warn(f'พบ object ด้าน{side} ในระยะ {self.safety_distance}m → หยุดหุ่นยนต์')
        elif self.stop_state and self.clear_count >= self.debounce_count:
            self.stop_state = False
            self.get_logger().info('โซนหน้า/หลังโล่งแล้ว → ปล่อยให้เคลื่อนที่ต่อ')

        self.stop_pub.publish(Bool(data=self.stop_state))


def main(args=None):
    rclpy.init(args=args)
    node = LidarSafetyNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
