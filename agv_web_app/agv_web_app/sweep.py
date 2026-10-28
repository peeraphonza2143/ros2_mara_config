"""
sweep — หาและปิด process ของ "ระบบหุ่น" ที่ค้างอยู่ (ไม่มีใครดูแลแล้ว) สำหรับปุ่ม Reset all

เหตุที่ต้องมี: ถ้าเว็บแอปตายกลางคัน (SIGKILL / ไฟดับ / ถูกขัดจังหวะตอนปิด) ระบบที่มันเปิดไว้ (ros2 launch ของแต่ละโหมด)
จะยังรันต่อโดยไม่มีใครรู้จัก — กลุ่ม process ถูกเปิดแยก (start_new_session) เพื่อไม่ให้ Ctrl+C ของเว็บฆ่าโดยไม่ตั้งใจ

หลักการเลือก (แคบและชัดเจน ไม่ใช่ "ฆ่าทุกอย่างที่เกี่ยวกับ ROS"):
  - เฉพาะ process ของ "ผู้ใช้คนเดียวกัน" ; ไม่แตะตัวเอง / บรรพบุรุษ / กลุ่มเดียวกับตัวเอง / อะไรที่มี 'agv_web_app'
  - จับจาก "ชื่อไฟล์ที่รัน" (argv[0] หรือสคริปต์ที่ python รัน) ตามรายชื่อด้านล่าง — ไม่ใช่ข้อความที่อยู่ใน argument
  - ปิดแบบสุภาพก่อน: SIGINT → SIGTERM → SIGKILL (สแกนใหม่ทุกรอบ เพราะ launch ที่ปิดแล้วอาจปล่อย/ปิดลูกเอง)

ไม่พึ่ง ROS → ทดสอบด้วย process จริงที่ตั้งชื่อให้ตรงได้ (ดู test/test_sweep.py)
"""
import os
import re
import signal
import time

# ชื่อไฟล์ที่รันได้ของระบบหุ่น (ดู agv_s2_bringup/launch/*.launch.py)
SYSTEM_EXECUTABLES = frozenset({
    'async_slam_toolbox_node', 'sync_slam_toolbox_node',     # slam_toolbox (Mapping)
    'ekf_node',                                              # robot_localization (EKF)
    'wheel_odom_node', 'hwt901b_node', 'sllidar_node',       # เซนเซอร์: ล้อ / IMU / LiDAR
    'scan_to_scan_filter_chain',                             # laser_filters
})
# ชื่อทั่วไปของ Nav2 — นับเฉพาะเมื่อไฟล์อยู่ในโฟลเดอร์ nav2_* (กันชื่อ "amcl"/"map_server" ของอย่างอื่น)
NAV2_EXECUTABLES = frozenset({
    'amcl', 'map_server', 'controller_server', 'planner_server', 'behavior_server', 'bt_navigator',
    'waypoint_follower', 'lifecycle_manager', 'smoother_server', 'velocity_smoother', 'collision_monitor',
})
# ชื่อ node (ใน --ros-args -r __node:=ชื่อ) ของ static_transform_publisher ที่ sensors_s2 เปิด
SYSTEM_NODE_NAMES = frozenset({'base_link_to_laser', 'base_link_to_imu'})
LAUNCH_PACKAGES = ('agv_s2_bringup',)                       # ros2 launch <แพ็กเกจเหล่านี้> …
NODE_NAME_RE = re.compile(r'__node:=([^\s]+)')


class Proc:
    __slots__ = ('pid', 'argv', 'why')

    def __init__(self, pid, argv, why):
        self.pid, self.argv, self.why = pid, argv, why

    def __repr__(self):
        return f'Proc({self.pid}, {self.why})'


def _read(path):
    try:
        with open(path, 'rb') as f:
            return f.read()
    except OSError:
        return None


def _state(proc_root, pid):
    """ตัวอักษรสถานะใน /proc/<pid>/stat (Z = zombie) ; None = ไม่มีแล้ว"""
    raw = _read(f'{proc_root}/{pid}/stat')
    if raw is None:
        return None
    try:
        return raw.decode(errors='replace').rsplit(')', 1)[1].split()[0]
    except IndexError:
        return None


def alive(pid, proc_root='/proc'):
    state = _state(proc_root, pid)
    return state is not None and state != 'Z'


def _ancestors(proc_root, pid):
    out = set()
    while pid > 1 and pid not in out:
        out.add(pid)
        raw = _read(f'{proc_root}/{pid}/stat')
        if raw is None:
            break
        try:
            pid = int(raw.decode(errors='replace').rsplit(')', 1)[1].split()[1])
        except (IndexError, ValueError):
            break
    return out


def classify(argv):
    """argv ของ process นี้เป็นระบบหุ่นไหม → เหตุผลสั้นๆ หรือ None"""
    if not argv:
        return None
    exe = os.path.basename(argv[0])
    script = argv[0]
    if exe.startswith('python') and len(argv) > 1:           # สคริปต์ Python (เช่น node ของ agv_s2_bringup, ros2)
        script = argv[1]
        exe = os.path.basename(script)
    if exe in SYSTEM_EXECUTABLES:
        return exe
    if exe in NAV2_EXECUTABLES and '/nav2_' in script:
        return exe
    if exe == 'ros2' and 'launch' in argv[1:3] and any(p in argv for p in LAUNCH_PACKAGES):
        return 'ros2 launch ' + next(p for p in LAUNCH_PACKAGES if p in argv)
    if exe == 'static_transform_publisher':
        names = set(NODE_NAME_RE.findall(' '.join(argv)))
        if names & SYSTEM_NODE_NAMES:
            return 'static_transform_publisher ' + sorted(names & SYSTEM_NODE_NAMES)[0]
    return None


def find_system_processes(proc_root='/proc', uid=None, exclude=(), within=None):
    """รายการ Proc ของระบบหุ่นที่ยังรันอยู่ (ของผู้ใช้ uid เท่านั้น)

    within = จำกัดให้พิจารณาเฉพาะ pid เหล่านี้ (ใช้ในเทสต์ — กันไม่ให้เทสต์ไปแตะระบบหุ่นจริงของเครื่อง)"""
    uid = os.getuid() if uid is None else uid
    me = os.getpid()
    skip = _ancestors(proc_root, me) | set(exclude)
    only = None if within is None else set(within)
    try:
        my_pgid = os.getpgid(0)
    except OSError:
        my_pgid = None
    found = []
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return found
    for name in entries:
        if not name.isdigit():
            continue
        pid = int(name)
        if pid in skip or (only is not None and pid not in only):
            continue
        try:
            if os.stat(f'{proc_root}/{pid}').st_uid != uid:
                continue
        except OSError:
            continue
        raw = _read(f'{proc_root}/{pid}/cmdline')
        if not raw:
            continue
        argv = [a.decode(errors='replace') for a in raw.split(b'\0') if a]
        if any('agv_web_app' in a for a in argv):             # ตัวเว็บแอปเอง / launch ของมัน
            continue
        why = classify(argv)
        if why is None or not alive(pid, proc_root):
            continue
        if my_pgid is not None:
            try:
                if os.getpgid(pid) == my_pgid:                # กลุ่มเดียวกับเรา = อย่าแตะ
                    continue
            except OSError:
                continue
        found.append(Proc(pid, argv, why))
    return found


def stop_system_processes(find=find_system_processes, sigint_wait=15.0, sigterm_wait=8.0, kill_wait=3.0,
                          poll=0.1, proc_root='/proc'):
    """ปิดทุก process ที่ find() เจอ แบบ SIGINT → SIGTERM → SIGKILL ; คืน (stopped_pids, remaining[Proc])"""
    seen = {}
    for sig, wait in ((signal.SIGINT, sigint_wait), (signal.SIGTERM, sigterm_wait), (signal.SIGKILL, kill_wait)):
        procs = find()                                        # สแกนใหม่ทุกรอบ
        if not procs:
            break
        for p in procs:
            seen[p.pid] = p
            try:
                os.kill(p.pid, sig)
            except (ProcessLookupError, PermissionError):
                pass
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if not any(alive(p.pid, proc_root) for p in procs):
                break
            time.sleep(poll)
    remaining = find()
    stopped = sorted(pid for pid in seen if not alive(pid, proc_root))
    return stopped, remaining


def group_processes(pgid, proc_root='/proc', uid=None):
    """สมาชิกของ process group นี้ที่เป็นระบบหุ่น (ใช้ตรวจว่าบันทึกกลุ่มค้างเป็นของเราจริง ไม่ใช่ pid ซ้ำ)"""
    uid = os.getuid() if uid is None else uid
    out = []
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return out
    for name in entries:
        if not name.isdigit():
            continue
        pid = int(name)
        try:
            if os.stat(f'{proc_root}/{pid}').st_uid != uid or os.getpgid(pid) != pgid:
                continue
        except OSError:
            continue
        raw = _read(f'{proc_root}/{pid}/cmdline')
        argv = [a.decode(errors='replace') for a in (raw or b'').split(b'\0') if a]
        if classify(argv) is not None and alive(pid, proc_root):
            out.append(pid)
    return out
