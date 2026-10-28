"""
ระบบปลอมสำหรับทดสอบ ModeManager (แทน ros2 launch จริง)

ใช้: fake_system.py <statefile> <ชื่อ node> <พฤติกรรม> <หน่วงก่อนขึ้น(วินาที)>
  "node" ปลอม = ชื่อที่ถูกเขียนลง statefile (test ใช้ไฟล์นี้เป็น ROS graph จำลอง)

พฤติกรรม
  normal       ขึ้นปกติ ปิดได้ด้วย SIGINT/SIGTERM แล้วลบชื่อตัวเองออก
  stubborn     เมิน SIGINT (ต้องใช้ SIGTERM)
  immortal     เมินทั้ง SIGINT และ SIGTERM (ต้องใช้ SIGKILL — ลบชื่อตัวเองไม่ได้)
  never_ready  ไม่ลงทะเบียนชื่อเลย (จำลองระบบเปิดไม่ขึ้น)
  crash        ขึ้นแล้วตายเองหลัง ~1 วินาที (exit code 3)
  child        เหมือน normal แต่สร้าง process ลูก (เขียน pid ไว้ที่ <statefile>.child)
  lingering    ปิดได้ปกติ แต่ชื่อยังค้างใน "graph" อีก ~0.8 วินาทีหลัง process จบ
               (จำลอง ROS graph ที่อัปเดตช้ากว่า process จริง)
"""
import fcntl
import os
import signal
import subprocess
import sys
import time


def update(path, name, add):
    with open(path + '.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            with open(path) as f:
                names = f.read().split()
        except FileNotFoundError:
            names = []
        names = [n for n in names if n != name]
        if add:
            names.append(name)
        with open(path, 'w') as f:
            f.write(' '.join(names))


def main():
    path, name, behavior, delay = sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4])

    if behavior == 'remove':
        # ตัวช่วยของ lingering: ลบชื่อออกจาก graph หลังหน่วงเวลา (อยู่นอก process group ของระบบ)
        time.sleep(delay)
        update(path, name, False)
        return

    def bye(*_):
        if behavior == 'lingering':
            subprocess.Popen([sys.executable, os.path.abspath(__file__), path, name,
                              'remove', '0.8'], start_new_session=True)
        else:
            update(path, name, False)
        sys.exit(0)

    if behavior != 'immortal':
        signal.signal(signal.SIGTERM, bye)
    else:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if behavior in ('stubborn', 'immortal'):
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    else:
        signal.signal(signal.SIGINT, bye)

    if behavior == 'child':
        child = subprocess.Popen(['sleep', '60'])
        with open(path + '.child', 'w') as f:
            f.write(str(child.pid))

    time.sleep(delay)
    if behavior == 'never_ready':
        time.sleep(60)
        return
    update(path, name, True)
    if behavior == 'crash':
        time.sleep(1.0)
        update(path, name, False)
        os._exit(3)
    time.sleep(60)


if __name__ == '__main__':
    main()
