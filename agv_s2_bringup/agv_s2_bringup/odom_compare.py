# เทียบ wheel odom (/odom) กับ EKF (/odometry/filtered) — Phase 6
# ต้องรัน ekf_s2.launch.py ไว้ก่อน แล้วขับรถด้วย teleop ใน terminal อื่น
#
#   ros2 run agv_s2_bringup odom_compare rotate               # Test 1: หมุนอยู่กับที่ 3 รอบ (1080 deg)
#   ros2 run agv_s2_bringup odom_compare rotate --turns 1     # หมุน 1 รอบ
#   ros2 run agv_s2_bringup odom_compare loop                 # Test 2: วิ่งวนกลับจุดเดิม (ทวนเข็ม 1 รอบ)
#   ros2 run agv_s2_bringup odom_compare loop --turns 0       # วิ่งไป-กลับแบบไม่วน (yaw สุดท้าย = 0)
#
# บันทึก: กด Ctrl+C เมื่อรถกลับถึงจุดอ้างอิง → สรุปผลบนจอ + บันทึกลงไฟล์ใน test_results/
#
# ทุกแหล่งถูก "ตั้งศูนย์" ณ วินาทีที่เริ่มบันทึก (ตำแหน่ง 0,0 และ yaw 0)
# จึงเทียบกันได้ตรง ๆ แม้ก่อนหน้านั้นแต่ละแหล่งจะ drift ไปไม่เท่ากัน
import argparse
import math
import os
import signal
import time
from datetime import datetime

import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from nav_msgs.msg import Odometry

SOURCES = {'wheel': '/odom', 'ekf': '/odometry/filtered'}
COUNTDOWN = 3


def yaw_from_quaternion(q):
    return math.atan2(2.0 * (q.w * q.z + q.x * q.y),
                      1.0 - 2.0 * (q.y * q.y + q.z * q.z))


def wrap(angle):
    return math.atan2(math.sin(angle), math.cos(angle))


class Track:
    """เก็บ pose ของ 1 แหล่ง เทียบกับจุดเริ่มบันทึก."""

    def __init__(self):
        self.origin = None        # (x0, y0, yaw0) ณ จุดเริ่ม
        self.yaw_prev = None
        self.yaw_total = 0.0      # yaw สะสมแบบ unwrap (หมุนหลายรอบได้)
        self.x = self.y = 0.0     # ตำแหน่งใน frame ของจุดเริ่ม
        self.prev_xy = None
        self.path = 0.0           # ระยะทางที่วิ่งจริง (รวมทุกช่วง)
        self.n = 0

    def update(self, msg):
        p = msg.pose.pose.position
        yaw = yaw_from_quaternion(msg.pose.pose.orientation)
        if self.origin is None:
            self.origin = (p.x, p.y, yaw)
            self.yaw_prev = yaw
            self.prev_xy = (p.x, p.y)
        x0, y0, yaw0 = self.origin

        self.yaw_total += wrap(yaw - self.yaw_prev)
        self.yaw_prev = yaw

        # หมุนตำแหน่งเข้า frame ของจุดเริ่ม: R(-yaw0) * (p - p0)
        dx, dy = p.x - x0, p.y - y0
        c, s = math.cos(-yaw0), math.sin(-yaw0)
        self.x, self.y = c * dx - s * dy, s * dx + c * dy

        self.path += math.hypot(p.x - self.prev_xy[0], p.y - self.prev_xy[1])
        self.prev_xy = (p.x, p.y)
        self.n += 1


class OdomCompare(Node):
    def __init__(self):
        super().__init__('odom_compare')
        self.recording = False
        self.tracks = {name: Track() for name in SOURCES}
        for name, topic in SOURCES.items():
            self.create_subscription(
                Odometry, topic, lambda msg, n=name: self.callback(n, msg), 20)

    def callback(self, name, msg):
        if self.recording:
            self.tracks[name].update(msg)


def spin_for(node, seconds):
    end = time.time() + seconds
    while time.time() < end and rclpy.ok():
        rclpy.spin_once(node, timeout_sec=0.05)


def live_line(node):
    parts = []
    for name, t in node.tracks.items():
        if t.n == 0:
            parts.append(f'{name}: no data')
        else:
            parts.append(f'{name}: yaw {math.degrees(t.yaw_total):+8.1f} deg '
                         f'xy ({t.x:+6.2f}, {t.y:+6.2f})')
    return '  ' + ' | '.join(parts)


def summarize(node, args, duration):
    expected_yaw = 360.0 * args.turns
    lines = []
    out = lines.append

    out('=' * 70)
    out(f'สรุปผล odom_compare: {args.test}   ({datetime.now():%Y-%m-%d %H:%M:%S})')
    out('=' * 70)
    out(f'  เวลาบันทึก        : {duration:.1f} s')
    out(f'  yaw ที่ตั้งใจ      : {expected_yaw:+.1f} deg  ({args.turns} รอบ, ทวนเข็ม = บวก)')
    if args.test == 'loop':
        out('  ตำแหน่งที่ตั้งใจ   : (0.00, 0.00) = กลับจุดเริ่ม')
    out('-' * 70)
    out(f'  {"source":<7}{"samples":>8}{"yaw [deg]":>12}{"yaw err":>10}'
        f'{"x [m]":>9}{"y [m]":>9}{"pos err":>9}{"path [m]":>10}')

    results = {}
    for name, t in node.tracks.items():
        if t.n == 0:
            out(f'  {name:<7}  ไม่มีข้อมูล ({SOURCES[name]}) → รัน ekf_s2.launch.py หรือยัง?')
            continue
        yaw = math.degrees(t.yaw_total)
        yaw_err = yaw - expected_yaw
        pos_err = math.hypot(t.x, t.y)
        results[name] = (yaw, yaw_err, pos_err, t.path)
        out(f'  {name:<7}{t.n:>8}{yaw:>+12.2f}{yaw_err:>+10.2f}'
            f'{t.x:>+9.3f}{t.y:>+9.3f}{pos_err:>9.3f}{t.path:>10.2f}')
    out('-' * 70)

    if 'wheel' in results and 'ekf' in results:
        w_yaw_err, e_yaw_err = abs(results['wheel'][1]), abs(results['ekf'][1])
        better = 'EKF' if e_yaw_err < w_yaw_err else 'wheel'
        out(f'  yaw แม่นกว่า       : {better}  '
            f'(|err| wheel {w_yaw_err:.2f} vs ekf {e_yaw_err:.2f} deg)')
        if args.test == 'loop':
            w_pos, e_pos = results['wheel'][2], results['ekf'][2]
            better = 'EKF' if e_pos < w_pos else 'wheel'
            out(f'  ตำแหน่งแม่นกว่า    : {better}  '
                f'(err wheel {w_pos:.3f} vs ekf {e_pos:.3f} m)')

    # แนะนำ wheel_separation: yaw จากล้อ = (d_right - d_left) / L
    # → ถ้าล้อรายงาน yaw มาก/น้อยกว่าจริง แปลว่า L ใน code น้อย/มากเกินไป
    # L_new = L_old * yaw_wheel / yaw_true   (ใช้ได้ดีเฉพาะ test หมุนอยู่กับที่)
    if args.test == 'rotate' and 'wheel' in results and expected_yaw != 0:
        yaw_w = results['wheel'][0]
        out('  ' + '-' * 40)
        out(f'  wheel_separation ปัจจุบัน : {args.wheel_separation:.4f} m')
        if yaw_w != 0:
            out(f'  แนะนำ (อ้างอิงมุมจริง)    : '
                f'{args.wheel_separation * yaw_w / expected_yaw:.4f} m')
            if 'ekf' in results and results['ekf'][0] != 0:
                out(f'  แนะนำ (อ้างอิง gyro/EKF) : '
                    f'{args.wheel_separation * yaw_w / results["ekf"][0]:.4f} m')
        out('  (ถ้า 2 ค่าใกล้กัน = เชื่อถือได้ | ถ้าต่างกันมาก = จุดอ้างอิงตอนหยุดอาจไม่ตรง)')
    out('=' * 70)

    # เขียนไฟล์ "ก่อน" พิมพ์ขึ้นจอ → ถ้ามีอะไรขัดจังหวะระหว่างพิมพ์ ผลก็ยังไม่หาย
    text = '\n'.join(lines)
    os.makedirs(args.outdir, exist_ok=True)
    path = os.path.join(args.outdir, f'odom_{args.test}_{datetime.now():%Y%m%d_%H%M%S}.txt')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text + '\n')

    print('\n' + text)
    print(f'\nบันทึกผลที่: {path}', flush=True)


def main():
    ap = argparse.ArgumentParser(description='เทียบ wheel odom กับ EKF')
    ap.add_argument('test', choices=['rotate', 'loop'])
    ap.add_argument('--turns', type=float, default=None,
                    help='จำนวนรอบที่หมุน (ทวนเข็ม = บวก) default: rotate=3, loop=1')
    ap.add_argument('--duration', type=float, default=None,
                    help='หยุดอัตโนมัติหลัง N วินาที (default: รอ Ctrl+C)')
    ap.add_argument('--wheel-separation', type=float, default=0.455,
                    help='ค่า wheel_separation ที่ wheel_odom_node ใช้อยู่ (m)')
    ap.add_argument('--outdir', default=os.path.expanduser(
        '~/ros2_ws/src/agv_s2_bringup/test_results'))
    args, _ = ap.parse_known_args()
    if args.turns is None:
        args.turns = 3.0 if args.test == 'rotate' else 1.0

    rclpy.init()
    node = OdomCompare()

    howto = {
        'rotate': f'จอดให้ขอบรถตรงกับเส้นอ้างอิง (ขอบกำแพง / เทปบนพื้น) แล้วหมุนอยู่กับที่ '
                  f'ทวนเข็ม {args.turns:g} รอบ ให้กลับมาตรงเส้นเดิมพอดี จากนั้นกด Ctrl+C',
        'loop': 'ทำเครื่องหมายจุดเริ่ม (เทปที่ล้อ + แนวขอบรถ) วิ่งเป็นวงทวนเข็มแล้วกลับมาจอด '
                'ให้ตรงเครื่องหมายเดิมทั้งตำแหน่งและทิศ จากนั้นกด Ctrl+C',
    }[args.test]
    print(f'\nodom_compare: {args.test}')
    print(f'วิธีทำ: {howto}\n')

    spin_for(node, 1.0)
    for i in range(COUNTDOWN, 0, -1):
        print(f'  เริ่มใน {i} ...', flush=True)
        spin_for(node, 1.0)
    print('  >>> เริ่มบันทึก (Ctrl+C เมื่อถึงจุดอ้างอิง) <<<', flush=True)

    node.recording = True
    t0 = time.time()
    next_print = t0 + 1.0
    try:
        while rclpy.ok():
            rclpy.spin_once(node, timeout_sec=0.05)
            now = time.time()
            if now >= next_print:
                next_print += 1.0
                print(live_line(node), flush=True)
            if args.duration and now - t0 >= args.duration:
                break
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    # Ctrl+C ครั้งแรก = หยุดบันทึก → ไม่รับ Ctrl+C ซ้ำระหว่างสรุปผล/เขียนไฟล์
    # (บน terminal จริงสัญญาณอาจมาถึงซ้ำ หรือผู้ใช้กดย้ำ แล้วผลจะหายก่อนบันทึก)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    node.recording = False
    summarize(node, args, time.time() - t0)

    node.destroy_node()
    if rclpy.ok():
        rclpy.shutdown()


if __name__ == '__main__':
    main()
