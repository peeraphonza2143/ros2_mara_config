# ทดสอบความแม่นยำการจอดของ Nav2: วิ่งไป-กลับ จุด A ↔ จุด B วนซ้ำ หยุดที่แต่ละจุด N วินาที
# ต้องรัน nav_s2.launch.py และตั้ง 2D Pose Estimate ใน RViz ก่อน
#
#   # 1) หาพิกัดจุด: ขับรถไปจอดที่จุดที่ต้องการ แล้วอ่านตำแหน่งปัจจุบัน
#   ros2 run agv_s2_bringup nav_repeat_test here
#
#   # 2) วิ่งทดสอบ (yaw หน่วยองศา) — กด Ctrl+C เพื่อหยุด (รถจะหยุดทันที แล้วสรุปผล)
#   ros2 run agv_s2_bringup nav_repeat_test run --a 0.0 1.0 0 --b 2.7 1.0 180
#   ros2 run agv_s2_bringup nav_repeat_test run --a 0.0 1.0 0 --b 2.7 1.0 180 --cycles 10 --wait 3
#
# ตำแหน่งที่วัด = TF map → base_link (AMCL + EKF) "หลังรถหยุดนิ่ง"
#   ไม่ใช้ /amcl_pose เพราะ AMCL publish เฉพาะเมื่อรถขยับเกิน update_min_d (0.25 m)
#   → ค่าตอนจอดอาจเป็นค่าเก่าจากก่อนรถหยุด
# หมายเหตุ: นี่คือตำแหน่งที่ "ระบบคิดว่า" รถอยู่ — ความแม่นจริงต้องเทียบกับเทปบนพื้นด้วย
import argparse
import csv
import math
import os
import signal
import statistics
import time
from datetime import datetime

import rclpy
from rclpy.duration import Duration
from rclpy.executors import ExternalShutdownException
from rclpy.signals import SignalHandlerOptions
from rclpy.time import Time
from geometry_msgs.msg import PoseStamped
from nav2_simple_commander.robot_navigator import BasicNavigator, TaskResult
from tf2_ros import Buffer, TransformListener

SETTLE_TIME = 1.0      # วินาที: รอรถหยุดนิ่งก่อนวัด (เป็นส่วนหนึ่งของเวลาหยุดรอ)
MEASURE_TIME = 0.5     # วินาที: เฉลี่ยตำแหน่งช่วงนี้ ลด noise ของ AMCL


def yaw_from_quaternion(q):
    return math.atan2(2.0 * (q.w * q.z + q.x * q.y),
                      1.0 - 2.0 * (q.y * q.y + q.z * q.z))


def wrap(angle):
    return math.atan2(math.sin(angle), math.cos(angle))


def make_pose(navigator, x, y, yaw):
    pose = PoseStamped()
    pose.header.frame_id = 'map'
    pose.header.stamp = navigator.get_clock().now().to_msg()
    pose.pose.position.x = x
    pose.pose.position.y = y
    pose.pose.orientation.z = math.sin(yaw / 2.0)
    pose.pose.orientation.w = math.cos(yaw / 2.0)
    return pose


class PoseReader:
    """อ่านตำแหน่งรถจาก TF map → base_link.

    ใช้ node แยกของตัวเอง + spin thread ของตัวเอง — ห้ามใช้ node ของ BasicNavigator
    เพราะ BasicNavigator ก็ spin node ตัวเองอยู่ → 2 thread spin node เดียวกัน = RCLError
    """

    def __init__(self):
        self.node = rclpy.create_node('nav_repeat_test_tf')
        self.buffer = Buffer()
        self.listener = TransformListener(self.buffer, self.node, spin_thread=True)

    def destroy(self):
        # หยุด executor ของ TF thread ก่อน rclpy.shutdown()
        # ไม่งั้น thread จะ raise ExternalShutdownException ตอนปิดโปรแกรม
        self.listener.executor.shutdown(timeout_sec=1.0)   # ไม่ใส่ timeout = รอได้ไม่สิ้นสุด
        self.listener.dedicated_listener_thread.join(timeout=1.0)
        self.node.destroy_node()

    def get(self):
        try:
            t = self.buffer.lookup_transform('map', 'base_link', Time(),
                                             timeout=Duration(seconds=0.2))
        except Exception:
            return None
        tr = t.transform
        return tr.translation.x, tr.translation.y, yaw_from_quaternion(tr.rotation)

    def average(self, seconds):
        """เฉลี่ยตำแหน่งในช่วงเวลา (yaw เฉลี่ยแบบ vector เพื่อไม่พังที่รอยต่อ ±180)."""
        xs, ys, ss, cs = [], [], [], []
        end = time.time() + seconds
        while time.time() < end:
            p = self.get()
            if p:
                xs.append(p[0])
                ys.append(p[1])
                ss.append(math.sin(p[2]))
                cs.append(math.cos(p[2]))
            time.sleep(0.05)
        if not xs:
            return None
        return (statistics.mean(xs), statistics.mean(ys),
                math.atan2(statistics.mean(ss), statistics.mean(cs)))


def wait_until_ready(navigator, reader):
    """รอ Nav2 พร้อม โดย "ไม่" ส่ง initial pose ทับของผู้ใช้.

    BasicNavigator.waitUntilNav2Active() ใน Humble จะส่ง initial pose (0,0,0)
    ไปให้ AMCL ก่อนเช็กว่ามีตำแหน่งแล้วหรือยัง → ถ้าผู้ใช้ตั้ง pose ไว้จุดอื่น
    AMCL จะถูกรีเซ็ต จึงรอ node ให้ active เอง แล้วรอ TF map → base_link แทน
    """
    navigator._waitForNodeToActivate('amcl')
    navigator._waitForNodeToActivate('bt_navigator')
    warned = False
    while rclpy.ok() and reader.get() is None:
        if not warned:
            print('  รอ TF map → base_link ... กด "2D Pose Estimate" ใน RViz ก่อน', flush=True)
            warned = True
        time.sleep(0.5)


def measure(reader, target):
    """วัดตำแหน่งจริงหลังจอด เทียบกับเป้า → error แยกแกนตามทิศของเป้า."""
    pose = reader.average(MEASURE_TIME)
    if pose is None:
        return None
    x, y, yaw = pose
    tx, ty, tyaw = target
    dx, dy = x - tx, y - ty
    # หมุนเข้า frame ของเป้า: along = หน้า(+)/หลัง(-) ของเป้า, cross = ซ้าย(+)/ขวา(-)
    along = math.cos(tyaw) * dx + math.sin(tyaw) * dy
    cross = -math.sin(tyaw) * dx + math.cos(tyaw) * dy
    return {
        'x': x, 'y': y, 'yaw_deg': math.degrees(yaw),
        'pos_err': math.hypot(dx, dy), 'along': along, 'cross': cross,
        'yaw_err_deg': math.degrees(wrap(yaw - tyaw)),
    }


def summarize(rows, targets, args, started, outdir):
    lines = []
    out = lines.append
    out('=' * 78)
    out(f'สรุปผล nav_repeat_test   ({started:%Y-%m-%d %H:%M:%S})')
    out('=' * 78)
    for name, (tx, ty, tyaw) in targets.items():
        out(f'  จุด {name}: ({tx:+.3f}, {ty:+.3f}) yaw {math.degrees(tyaw):+.1f} deg')
    ok = [r for r in rows if r['result'] == 'SUCCEEDED']
    out(f'  goals ทั้งหมด {len(rows)} | สำเร็จ {len(ok)} | ไม่สำเร็จ {len(rows) - len(ok)}')
    out('-' * 78)
    out(f'  {"จุด":<4}{"n":>3}{"pos err เฉลี่ย":>15}{"max":>8}'
        f'{"along":>16}{"cross":>16}{"yaw err":>16}{"เวลา":>8}')
    out(f'  {"":<4}{"":>3}{"[m]":>15}{"[m]":>8}{"mean±std [m]":>16}'
        f'{"mean±std [m]":>16}{"mean±std [deg]":>16}{"[s]":>8}')
    for name in targets:
        rs = [r for r in ok if r['point'] == name and r['pos_err'] is not None]
        if not rs:
            out(f'  {name:<4}  ไม่มีข้อมูล')
            continue

        def ms(key):
            vals = [r[key] for r in rs]
            sd = statistics.pstdev(vals) if len(vals) > 1 else 0.0
            return f'{statistics.mean(vals):+.3f}±{sd:.3f}'

        def ms_deg(key):
            vals = [r[key] for r in rs]
            sd = statistics.pstdev(vals) if len(vals) > 1 else 0.0
            return f'{statistics.mean(vals):+.2f}±{sd:.2f}'

        out(f'  {name:<4}{len(rs):>3}{statistics.mean(r["pos_err"] for r in rs):>15.3f}'
            f'{max(r["pos_err"] for r in rs):>8.3f}{ms("along"):>16}{ms("cross"):>16}'
            f'{ms_deg("yaw_err_deg"):>16}{statistics.mean(r["duration"] for r in rs):>8.1f}')
    out('-' * 78)
    out('  อ่านค่า: mean = ความคลาดเคลื่อนเชิงระบบ (accuracy) | std = ความสม่ำเสมอ (repeatability)')
    out('           along +/- = จอดเลย/ก่อนถึงเป้า | cross +/- = เยื้องซ้าย/ขวาของเป้า')
    out(f'  goal tolerance ปัจจุบัน: xy {args.xy_tol:.2f} m, yaw {args.yaw_tol:.2f} rad '
        f'({math.degrees(args.yaw_tol):.1f} deg)')
    out('=' * 78)

    # เขียนไฟล์ "ก่อน" พิมพ์ขึ้นจอ (กันข้อมูลหายถ้าถูกขัดจังหวะ)
    os.makedirs(outdir, exist_ok=True)
    stem = os.path.join(outdir, f'nav_repeat_{started:%Y%m%d_%H%M%S}')
    with open(stem + '.txt', 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    if rows:
        with open(stem + '.csv', 'w', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
    print('\n' + '\n'.join(lines))
    print(f'\nบันทึกผลที่: {stem}.txt และ .csv', flush=True)


def cmd_here(navigator, reader):
    navigator._waitForNodeToActivate('amcl')
    pose = None
    for _ in range(20):
        pose = reader.average(MEASURE_TIME)
        if pose:
            break
    if pose is None:
        print('ไม่พบ TF map → base_link — ตั้ง 2D Pose Estimate ใน RViz ก่อน')
        return
    x, y, yaw = pose
    print(f'\nตำแหน่งปัจจุบัน (map): x {x:+.3f}  y {y:+.3f}  yaw {math.degrees(yaw):+.1f} deg')
    print(f'ใช้เป็น argument:  {x:.3f} {y:.3f} {math.degrees(yaw):.1f}')


def cmd_run(navigator, reader, args):
    targets = {
        'A': (args.a[0], args.a[1], math.radians(args.a[2])),
        'B': (args.b[0], args.b[1], math.radians(args.b[2])),
    }
    print('\nnav_repeat_test: A ↔ B')
    for name, (x, y, yaw) in targets.items():
        print(f'  {name}: ({x:+.3f}, {y:+.3f}) yaw {math.degrees(yaw):+.1f} deg')
    cycles_txt = 'ไม่จำกัด (Ctrl+C เพื่อหยุด)' if args.cycles == 0 else str(args.cycles)
    print(f'  จำนวนรอบ: {cycles_txt} | หยุดที่แต่ละจุด {args.wait:g} s\n')

    wait_until_ready(navigator, reader)
    print('  Nav2 พร้อม → เริ่มทดสอบ\n', flush=True)

    rows = []
    started = datetime.now()
    cycle = 0
    try:
        while rclpy.ok() and (args.cycles == 0 or cycle < args.cycles):
            cycle += 1
            for name in ('A', 'B'):
                target = targets[name]
                t0 = time.time()
                navigator.goToPose(make_pose(navigator, *target))
                while not navigator.isTaskComplete():
                    time.sleep(0.1)
                duration = time.time() - t0
                result = navigator.getResult()
                result_name = {TaskResult.SUCCEEDED: 'SUCCEEDED',
                               TaskResult.CANCELED: 'CANCELED',
                               TaskResult.FAILED: 'FAILED'}.get(result, 'UNKNOWN')

                m = None
                if result == TaskResult.SUCCEEDED:
                    time.sleep(SETTLE_TIME)              # ให้รถหยุดนิ่งก่อนวัด
                    m = measure(reader, target)
                row = {'cycle': cycle, 'point': name, 'result': result_name,
                       'duration': round(duration, 2),
                       'x': None, 'y': None, 'yaw_deg': None, 'pos_err': None,
                       'along': None, 'cross': None, 'yaw_err_deg': None}
                if m:
                    row.update({k: round(v, 4) for k, v in m.items()})
                rows.append(row)

                if m:
                    print(f'  รอบ {cycle:>3} → {name}: {result_name}  {duration:5.1f} s | '
                          f'err {m["pos_err"]:.3f} m (along {m["along"]:+.3f}, '
                          f'cross {m["cross"]:+.3f}) yaw {m["yaw_err_deg"]:+.2f} deg',
                          flush=True)
                else:
                    print(f'  รอบ {cycle:>3} → {name}: {result_name}  {duration:5.1f} s',
                          flush=True)
                if result != TaskResult.SUCCEEDED:
                    print('  ⚠ goal ไม่สำเร็จ → หยุดการทดสอบเพื่อความปลอดภัย')
                    raise KeyboardInterrupt

                # หยุดรอที่จุดให้ครบเวลา (นับรวมช่วง settle + วัดไปแล้ว)
                remain = args.wait - (SETTLE_TIME + MEASURE_TIME)
                if remain > 0:
                    time.sleep(remain)
    except (KeyboardInterrupt, ExternalShutdownException):
        print('\n  หยุดการทดสอบ → ยกเลิก goal ที่ค้างอยู่ (รถหยุด)', flush=True)
        try:
            navigator.cancelTask()
        except Exception:
            pass
    # Ctrl+C ครั้งแรก = หยุด → ไม่รับซ้ำระหว่างสรุปผล/เขียนไฟล์
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    summarize(rows, targets, args, started, args.outdir)


def main():
    ap = argparse.ArgumentParser(description='ทดสอบความแม่นยำการจอด Nav2: A ↔ B วนซ้ำ')
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('here', help='แสดงตำแหน่งปัจจุบันของรถ (ใช้หาพิกัดจุด)')
    run = sub.add_parser('run', help='วิ่ง A ↔ B วนซ้ำ')
    run.add_argument('--a', type=float, nargs=3, required=True, metavar=('X', 'Y', 'YAW_DEG'))
    run.add_argument('--b', type=float, nargs=3, required=True, metavar=('X', 'Y', 'YAW_DEG'))
    run.add_argument('--cycles', type=int, default=0, help='จำนวนรอบ (0 = ไม่จำกัด)')
    run.add_argument('--wait', type=float, default=3.0, help='หยุดที่แต่ละจุด (วินาที)')
    run.add_argument('--xy-tol', type=float, default=0.10,
                     help='ค่า xy_goal_tolerance ที่ใช้อยู่ (แสดงในรายงานเท่านั้น)')
    run.add_argument('--yaw-tol', type=float, default=0.10,
                     help='ค่า yaw_goal_tolerance ที่ใช้อยู่ (แสดงในรายงานเท่านั้น)')
    run.add_argument('--outdir', default=os.path.expanduser(
        '~/ros2_ws/src/agv_s2_bringup/test_results'))
    args, _ = ap.parse_known_args()

    # ปิด signal handler ของ rclpy: ปกติ Ctrl+C จะ shutdown context ทันที
    # → ส่ง cancelTask() ไม่ได้ → bt_navigator ยังพารถวิ่งไปที่เป้าต่อ (อันตราย)
    # ปิดแล้ว Ctrl+C = KeyboardInterrupt ธรรมดา → ยกเลิก goal ได้ก่อนปิด
    rclpy.init(signal_handler_options=SignalHandlerOptions.NO)
    navigator = BasicNavigator()
    reader = PoseReader()
    try:
        if args.cmd == 'here':
            cmd_here(navigator, reader)
        else:
            cmd_run(navigator, reader, args)
    finally:
        reader.destroy()
        navigator.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
