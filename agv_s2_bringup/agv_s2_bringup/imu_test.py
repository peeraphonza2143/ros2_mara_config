# สคริปต์ทดสอบ IMU (Phase 3) — subscribe /imu/data จาก hwt901b_node
# ต้องรัน hwt901b_node ไว้ก่อนใน terminal อื่น
#
#   ros2 run agv_s2_bringup imu_test drift                  # Test A: วางนิ่ง 5 นาที
#   ros2 run agv_s2_bringup imu_test direction              # Test B: หมุนทวนเข็ม → gyro z ต้องเป็นบวก
#   ros2 run agv_s2_bringup imu_test deadband               # Test C: หมุนช้ามาก 90 องศา
#   ros2 run agv_s2_bringup imu_test drift --duration 60    # กำหนดเวลาเอง (วินาที)
#
# เปรียบเทียบ yaw 2 แหล่ง:
#   yaw_imu  = yaw ที่ firmware ของ IMU คำนวณเอง (จาก orientation)
#   yaw_gyro = เรา integrate angular_velocity.z เอง  ← นี่คือสิ่งที่ EKF จะใช้จริง
# ถ้า 2 ค่านี้ต่างกันมาก แปลว่า gyro ที่ส่งออกมาถูกตัดค่า (deadband) ก่อนถึงเรา
import argparse
import math
import os
import signal
import statistics
import time
from datetime import datetime

import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from sensor_msgs.msg import Imu

TESTS = {
    'drift': {
        'duration': 300.0,
        'title': 'Test A: Drift ตอนนิ่ง',
        'howto': 'วาง IMU / รถ ให้นิ่งสนิท ห้ามแตะ ตลอดการทดสอบ',
    },
    'direction': {
        'duration': 10.0,
        'title': 'Test B: ทิศการหมุน (REP-103)',
        'howto': 'หมุน IMU "ทวนเข็มนาฬิกา" (มองจากด้านบน) ประมาณ 90 องศา แล้วหยุดนิ่ง',
    },
    'deadband': {
        'duration': 40.0,
        'title': 'Test C: Deadband ตอนหมุนช้า',
        'howto': 'หมุน IMU ทวนเข็ม "ช้า ๆ สม่ำเสมอ" ให้ครบ 90 องศาพอดี ใช้เวลา ~30 วินาที '
                 '(ใช้ขอบโต๊ะ / ไม้ฉากเป็นตัวอ้างอิง 0 และ 90 องศา) แล้วหยุดนิ่ง',
    },
}
COUNTDOWN = 3


def yaw_from_quaternion(q):
    return math.atan2(2.0 * (q.w * q.z + q.x * q.y),
                      1.0 - 2.0 * (q.y * q.y + q.z * q.z))


def wrap(angle):
    """บีบมุมให้อยู่ใน [-pi, pi] — ใช้ตอนหาผลต่าง yaw ข้ามรอยต่อ ±180 องศา."""
    return math.atan2(math.sin(angle), math.cos(angle))


class ImuTest(Node):
    def __init__(self):
        super().__init__('imu_test')
        self.create_subscription(Imu, 'imu/data', self.callback, 50)
        self.recording = False
        self.reset()

    def reset(self):
        self.n = 0
        self.gz = []
        self.yaw_start = None
        self.yaw_imu = 0.0         # unwrap แล้ว (หมุนเกิน 180 องศาได้)
        self.yaw_prev = None
        self.yaw_gyro = 0.0
        self.t_prev = None
        self.gz_prev = None
        self.max_gap = 0.0

    def callback(self, msg):
        if not self.recording:
            return
        t = msg.header.stamp.sec + msg.header.stamp.nanosec * 1e-9
        yaw = yaw_from_quaternion(msg.orientation)
        gz = msg.angular_velocity.z

        if self.t_prev is None:
            self.yaw_prev = yaw
        else:
            dt = t - self.t_prev
            self.max_gap = max(self.max_gap, dt)
            # yaw_imu: สะสมผลต่างทีละ step → unwrap อัตโนมัติ
            self.yaw_imu += wrap(yaw - self.yaw_prev)
            # yaw_gyro: integrate แบบ trapezoid (เฉลี่ยค่า 2 จุดแล้วคูณ dt)
            self.yaw_gyro += 0.5 * (gz + self.gz_prev) * dt
            self.yaw_prev = yaw

        self.t_prev = t
        self.gz_prev = gz
        self.gz.append(gz)
        self.n += 1


def spin_for(node, seconds, live=False):
    end = time.time() + seconds
    next_print = time.time() + 1.0
    while time.time() < end and rclpy.ok():
        rclpy.spin_once(node, timeout_sec=0.05)
        if live and time.time() >= next_print:
            next_print += 1.0
            remain = end - time.time()
            gz = math.degrees(node.gz[-1]) if node.gz else 0.0
            print(f'  เหลือ {remain:5.0f} s | yaw_imu {math.degrees(node.yaw_imu):+8.2f} deg '
                  f'| yaw_gyro {math.degrees(node.yaw_gyro):+8.2f} deg '
                  f'| gyro z {gz:+7.2f} deg/s', flush=True)


def summarize(node, test, duration, expected, outdir):
    if node.n < 10:
        print('\n[FAIL] ได้ข้อมูลน้อยเกินไป → hwt901b_node รันอยู่หรือไม่? (ros2 topic hz /imu/data)')
        return

    gz_deg = [math.degrees(g) for g in node.gz]
    yaw_imu = math.degrees(node.yaw_imu)
    yaw_gyro = math.degrees(node.yaw_gyro)
    zeros = sum(1 for g in node.gz if g == 0.0) / node.n * 100
    nonzero = [abs(g) for g in gz_deg if g != 0.0]

    lines = []
    out = lines.append

    out('=' * 64)
    out(f'สรุปผล {TESTS[test]["title"]}')
    out('=' * 64)
    out(f'  samples          : {node.n} ({node.n / duration:.1f} Hz), '
          f'ช่องว่างนานสุด {node.max_gap * 1000:.0f} ms')
    out(f'  yaw_imu          : {yaw_imu:+.3f} deg   (firmware คำนวณ)')
    out(f'  yaw_gyro         : {yaw_gyro:+.3f} deg   (integrate gyro z = สิ่งที่ EKF ใช้)')
    out(f'  gyro z [deg/s]   : min {min(gz_deg):+.3f}  max {max(gz_deg):+.3f}  '
          f'mean {statistics.mean(gz_deg):+.4f}  std {statistics.pstdev(gz_deg):.4f}')
    out(f'  gyro z = 0 พอดี  : {zeros:.1f}% ของ samples')
    if nonzero:
        out(f'  |gyro z| เล็กสุดที่ไม่ใช่ 0 : {min(nonzero):.3f} deg/s '
              f'(1 LSB = {2000 / 32768:.3f} deg/s)')
    out('-' * 64)

    if test == 'drift':
        per_min_imu = yaw_imu / (duration / 60.0)
        per_min_gyro = yaw_gyro / (duration / 60.0)
        out(f'  drift yaw_imu    : {per_min_imu:+.3f} deg/min')
        out(f'  drift yaw_gyro   : {per_min_gyro:+.3f} deg/min')
        ok = abs(yaw_imu) < 1.0 and abs(yaw_gyro) < 1.0
        out(f'  [{"PASS" if ok else "FAIL"}] เกณฑ์: yaw เปลี่ยน < 1 deg ตลอดการทดสอบ')

    elif test == 'direction':
        peak = max(gz_deg, key=abs)
        out(f'  gyro z สูงสุด    : {peak:+.2f} deg/s')
        ok = peak > 5.0 and yaw_gyro > 0 and yaw_imu > 0
        out(f'  [{"PASS" if ok else "FAIL"}] เกณฑ์: หมุนทวนเข็ม → gyro z และ yaw ต้องเป็นบวก')
        if not ok and peak < -5.0:
            out('  → ได้ค่าลบ: IMU อาจคว่ำอยู่ (Z ชี้ลง) หรือหมุนผิดทิศ')

    elif test == 'deadband':
        err_imu = yaw_imu - expected
        err_gyro = yaw_gyro - expected
        out(f'  มุมจริง (ที่ตั้งใจหมุน) : {expected:+.1f} deg')
        out(f'  error yaw_imu    : {err_imu:+.2f} deg ({abs(err_imu) / expected * 100:.1f}%)')
        out(f'  error yaw_gyro   : {err_gyro:+.2f} deg ({abs(err_gyro) / expected * 100:.1f}%)')
        ok = abs(err_gyro) / expected < 0.05
        out(f'  [{"PASS" if ok else "FAIL"}] เกณฑ์: yaw_gyro คลาดเคลื่อน < 5% ของมุมจริง')
        if abs(yaw_imu - yaw_gyro) > 0.05 * expected:
            out('  → yaw_imu กับ yaw_gyro ต่างกันมาก: gyro ที่ส่งออกถูกตัดค่า (deadband) '
                  'ก่อนถึง ROS — EKF จะได้ข้อมูลน้อยกว่าความจริงตอนเลี้ยวช้า')

    # เขียนไฟล์ "ก่อน" พิมพ์ขึ้นจอ → ถ้ามีอะไรขัดจังหวะระหว่างพิมพ์ ผลก็ยังไม่หาย
    text = '\n'.join(lines)
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, f'imu_{test}_{datetime.now():%Y%m%d_%H%M%S}.txt')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text + '\n')

    print('\n' + text)
    print(f'\nบันทึกผลที่: {path}', flush=True)


def main():
    ap = argparse.ArgumentParser(description='ทดสอบ IMU (Phase 3)')
    ap.add_argument('test', choices=sorted(TESTS))
    ap.add_argument('--duration', type=float, default=None, help='วินาที')
    ap.add_argument('--expected', type=float, default=90.0,
                    help='มุมที่หมุนจริง (deg) สำหรับ deadband test')
    ap.add_argument('--outdir', default=os.path.expanduser(
        '~/ros2_ws/src/agv_s2_bringup/test_results'))
    args, _ = ap.parse_known_args()
    duration = args.duration or TESTS[args.test]['duration']

    rclpy.init()
    node = ImuTest()

    print(f'\n{TESTS[args.test]["title"]}  ({duration:.0f} วินาที)')
    print(f'วิธีทำ: {TESTS[args.test]["howto"]}\n')
    spin_for(node, 1.0)   # ให้ subscription เชื่อมกับ publisher ก่อน
    for i in range(COUNTDOWN, 0, -1):
        print(f'  เริ่มใน {i} ...', flush=True)
        spin_for(node, 1.0)
    print('  >>> เริ่มบันทึก <<<', flush=True)

    node.reset()
    node.recording = True
    t0 = time.time()
    try:
        spin_for(node, duration, live=True)
    except (KeyboardInterrupt, ExternalShutdownException):
        print('\n  (หยุดก่อนครบเวลา — สรุปจากข้อมูลที่มี)')
    # Ctrl+C ครั้งแรก = หยุดบันทึก → ไม่รับ Ctrl+C ซ้ำระหว่างสรุปผล/เขียนไฟล์
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    node.recording = False
    # ใช้เวลาที่บันทึกจริง (ถ้ากด Ctrl+C ก่อนครบ Hz และ drift/min จะได้ไม่ผิด)
    summarize(node, args.test, time.time() - t0, args.expected, args.outdir)

    node.destroy_node()
    if rclpy.ok():
        rclpy.shutdown()


if __name__ == '__main__':
    main()
