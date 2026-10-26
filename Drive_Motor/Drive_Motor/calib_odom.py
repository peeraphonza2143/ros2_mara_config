import rclpy
from rclpy.node import Node
from geometry_msgs.msg import Twist
import threading
import time
import math


class CalibNode(Node):
    def __init__(self):
        super().__init__('calib_odom_node')
        self.cmd_pub = self.create_publisher(Twist, '/cmd_vel', 10)

    def send_velocity(self, linear, angular, duration):
        """ส่ง cmd_vel ต่อเนื่อง duration วินาที แล้วหยุดอัตโนมัติ"""
        msg = Twist()
        msg.linear.x  = linear
        msg.angular.z = angular

        interval = 0.1                       # 10 Hz
        steps    = int(duration / interval)

        for _ in range(steps):
            self.cmd_pub.publish(msg)
            time.sleep(interval)

        self.cmd_pub.publish(Twist())        # stop


# ─────────────────────────────────────────────────────────────
#  calibration_routine  —  รันใน thread แยก ไม่ block ROS spin
# ─────────────────────────────────────────────────────────────
def calibration_routine(node: CalibNode):
    time.sleep(1.0)     # รอ publisher พร้อม

    SEP = "=" * 54
    print(f"\n{SEP}")
    print("   Odometry Calibration Tool  —  ZLAC8015D AMR")
    print(SEP)

    # ── รับค่าพารามิเตอร์ปัจจุบัน ──────────────────────────
    print("\n[1/2]  กรอกค่าพารามิเตอร์ปัจจุบันของหุ่นยนต์")
    R = float(input("  wheel_radius     (m) [default 0.1016] : ") or "0.1016")
    L = float(input("  wheel_separation (m) [default 0.50  ] : ") or "0.50")

    # ══════════════════════════════════════════════════════
    #  STEP 1 : Calibrate wheel_radius
    #  หลักการ : วัดระยะทางจริง แล้วหาว่า radius ที่แท้จริงเป็นเท่าไร
    #
    #  สูตร:  new_R = old_R × (ระยะจริง / ระยะที่คาดหวัง)
    #
    #  ทำไมต้องทำก่อน:
    #    wheel_radius ผิด → RPM ที่คำนวณผิด → ทุกอย่างผิดหมด
    #    ต้องแก้ก่อนแล้วค่อยวัด separation
    # ══════════════════════════════════════════════════════
    print(f"\n{SEP}")
    print("  STEP 1 — Calibrate wheel_radius")
    print(SEP)
    print("""
หลักการ:
  สั่งเดินตรงด้วยความเร็วคงที่ → วัดระยะทางจริงด้วยตลับเมตร
  เปรียบเทียบกับระยะที่คำนวณจาก RPM × เวลา

  ตั้งค่าทดสอบ :  0.2 m/s  เป็นเวลา  5 วินาที
  ระยะที่คาดหวัง:  1.00 เมตร
""")
    input("▶  วางหุ่นยนต์บนพื้นราบ ทำเครื่องหมายจุดเริ่มต้น  [Enter เพื่อเริ่ม]")

    LINEAR_SPEED  = 0.2             # m/s
    DURATION_1    = 5.0             # วินาที
    expected_dist = LINEAR_SPEED * DURATION_1   # 1.00 m

    print(f"\n  กำลังเดินตรง {expected_dist:.2f} m  ({DURATION_1:.0f} วินาที) ...")
    node.send_velocity(LINEAR_SPEED, 0.0, DURATION_1)
    print("  หยุดแล้ว ✓\n")

    print(f"  ระยะที่คาดหวัง : {expected_dist:.3f} m")
    actual_dist = float(input("  วัดระยะทางจริงที่หุ่นยนต์เดินได้ (m): "))

    new_R = R * (actual_dist / expected_dist)
    err_R = (new_R - R) / R * 100.0

    print(f"""
  ── ผล STEP 1 ──────────────────────────────
  wheel_radius เดิม : {R:.4f} m
  wheel_radius ใหม่ : {new_R:.4f} m  ({err_R:+.2f}%)
  {'  ล้อเล็กกว่าที่ตั้งค่าไว้' if new_R < R else '  ล้อใหญ่กว่าที่ตั้งค่าไว้'}
""")

    # ══════════════════════════════════════════════════════
    #  STEP 2 : Calibrate wheel_separation
    #  หลักการ : สั่งหมุน N รอบ → วัดรอบจริง → หา L ที่แท้จริง
    #
    #  สูตร:
    #    new_L = old_L × (รอบที่สั่ง / รอบจริง) × (new_R / old_R)
    #
    #  ทำไมต้องคูณ (new_R / old_R):
    #    ขณะทดสอบ node ยังใช้ R เก่าคำนวณ RPM อยู่
    #    ทำให้ความเร็วล้อจริงต่างจากที่สั่ง → ต้องชดเชย
    #
    #  ตีความผล:
    #    หมุนน้อยกว่าที่สั่ง → L ใหญ่เกินไป → ลด L
    #    หมุนมากกว่าที่สั่ง  → L เล็กเกินไป → เพิ่ม L
    # ══════════════════════════════════════════════════════
    N_ROT         = 3
    ANGULAR_SPEED = 1.0                            # rad/s
    expected_rad  = N_ROT * 2.0 * math.pi         # rad รวม
    DURATION_2    = expected_rad / ANGULAR_SPEED   # ≈ 18.8 วินาที

    print(f"{SEP}")
    print("  STEP 2 — Calibrate wheel_separation")
    print(SEP)
    print(f"""
หลักการ:
  สั่งหมุนรอบตัวเอง {N_ROT} รอบ → วัดรอบจริง → คำนวณ separation ที่แท้จริง

  ตั้งค่าทดสอบ :  {ANGULAR_SPEED} rad/s  เป็นเวลา  {DURATION_2:.1f} วินาที
  คาดหวัง      :  {N_ROT} รอบพอดี

วิธีวัด:
  ติดเทปสีที่หัวหุ่นยนต์  ทำเครื่องหมายทิศเริ่มต้นบนพื้น
  นับว่าเทปผ่านจุดเดิมกี่ครั้ง (= จำนวนรอบจริง)
""")
    input(f"▶  ทำเครื่องหมายทิศหัวหุ่นยนต์  [Enter เพื่อเริ่มหมุน {N_ROT} รอบ]")

    print(f"\n  กำลังหมุน {N_ROT} รอบ  ({DURATION_2:.1f} วินาที) ...")
    node.send_velocity(0.0, ANGULAR_SPEED, DURATION_2)
    print("  หยุดแล้ว ✓\n")

    print(f"  คาดหวัง: {N_ROT} รอบ")
    actual_rot = float(input("  หุ่นยนต์หมุนได้จริงกี่รอบ? (เช่น 2.9 หรือ 3.1): "))
    actual_rad = actual_rot * 2.0 * math.pi

    # สูตร: new_L = old_L × (expected / actual) × (new_R / old_R)
    new_L = L * (expected_rad / actual_rad) * (new_R / R)
    err_L = (new_L - L) / L * 100.0

    print(f"""
  ── ผล STEP 2 ──────────────────────────────
  wheel_separation เดิม : {L:.4f} m
  wheel_separation ใหม่ : {new_L:.4f} m  ({err_L:+.2f}%)
  {'  L ใหญ่เกินไป ลดลง' if new_L < L else '  L เล็กเกินไป เพิ่มขึ้น'}
""")

    # ── สรุปผลทั้งหมด ────────────────────────────────────────
    print(SEP)
    print("  สรุปผลการ Calibration")
    print(SEP)
    print(f"""
  wheel_radius     : {R:.4f}  →  {new_R:.4f} m  ({err_R:+.2f}%)
  wheel_separation : {L:.4f}  →  {new_L:.4f} m  ({err_L:+.2f}%)

  รัน odom_node ด้วยค่าที่ calibrate แล้ว:
    ros2 run Drive_Motor odom_node --ros-args \\
      -p wheel_radius:={new_R:.4f} \\
      -p wheel_separation:={new_L:.4f}
""")

    # ── บันทึก YAML (optional) ───────────────────────────────
    save = input("บันทึกค่าลง calib_params.yaml ? [y/N]: ").strip().lower()
    if save == 'y':
        yaml_lines = (
            "# ผลการ calibrate — ZLAC8015D Odometry\n"
            "zlac_odom_node:\n"
            "  ros__parameters:\n"
            f"    wheel_radius:     {new_R:.4f}   # m\n"
            f"    wheel_separation: {new_L:.4f}   # m\n"
            "    max_rpm:          60.0\n"
        )
        with open('calib_params.yaml', 'w') as f:
            f.write(yaml_lines)
        print("  บันทึกแล้ว → calib_params.yaml")
        print("  รันด้วย: ros2 run Drive_Motor odom_node"
              " --ros-args --params-file calib_params.yaml")

    print(f"\n{SEP}")
    print("  Calibration เสร็จสมบูรณ์")
    print(SEP + "\n")


# ─────────────────────────────────────────────────────────────
def main(args=None):
    rclpy.init(args=args)
    node = CalibNode()

    # รัน interactive routine ใน daemon thread
    # ─ daemon=True: ถ้า main thread ตาย thread นี้ตายตาม
    thread = threading.Thread(
        target=calibration_routine, args=(node,), daemon=True)
    thread.start()

    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.cmd_pub.publish(Twist())    # หยุด motor ก่อนออก
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
