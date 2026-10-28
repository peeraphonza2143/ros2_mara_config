"""
ทดสอบ sweep (หา/ปิดระบบหุ่นที่ค้าง) ด้วย process จริงที่ตั้งชื่อไฟล์ให้ตรงกับรายชื่อ — ไม่ต้องมี ROS

⚠ ความปลอดภัยของเทสต์: ทุกการหา/ปิดในไฟล์นี้ถูกจำกัดด้วย within=<pid ที่เทสต์เปิดเอง>
  เพื่อไม่ให้เทสต์ไปแตะระบบหุ่นจริงที่อาจรันอยู่บนเครื่องเดียวกัน (เช่น ekf_node / sllidar_node จริง)

รัน:  cd ~/ros2_ws/src/agv_web_app && python3 -m unittest discover -s test -v
"""
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from agv_web_app import sweep

SCRIPT = '''
import signal, sys, time
mode = sys.argv[1] if len(sys.argv) > 1 else "normal"
if mode in ("stubborn", "immortal"):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
if mode == "immortal":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
time.sleep(60)
'''


class SweepBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='sweep_')
        self.procs = []
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for p in self.procs:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                p.kill()
            except OSError:
                pass
            p.wait()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def spawn(self, name, *args, new_session=True, subdir=''):
        """รันสคริปต์ที่ตั้งชื่อไฟล์ = name (เหมือน node จริง: python3 <path>/<name> …)"""
        folder = os.path.join(self.tmp, subdir)
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, name)
        with open(path, 'w') as f:
            f.write(SCRIPT)
        p = subprocess.Popen([sys.executable, path, *args], start_new_session=new_session,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.procs.append(p)
        time.sleep(0.15)                      # ให้ exec เสร็จก่อนสแกน
        return p

    def mine(self):
        return [p.pid for p in self.procs]

    def find(self, **kw):
        return sweep.find_system_processes(within=self.mine(), **kw)

    def found(self, **kw):
        return {p.pid: p for p in self.find(**kw)}


class ClassifyTest(unittest.TestCase):

    def test_robot_system_processes_are_recognised(self):
        cases = {
            ('/opt/ros/humble/lib/slam_toolbox/async_slam_toolbox_node', '--ros-args'): 'async_slam_toolbox_node',
            ('/opt/ros/humble/lib/robot_localization/ekf_node',): 'ekf_node',
            ('/usr/bin/python3', '/w/install/agv_s2_bringup/lib/agv_s2_bringup/wheel_odom_node'): 'wheel_odom_node',
            ('/usr/bin/python3', '/w/install/agv_s2_bringup/lib/agv_s2_bringup/hwt901b_node'): 'hwt901b_node',
            ('/w/install/sllidar_ros2/lib/sllidar_ros2/sllidar_node',): 'sllidar_node',
            ('/opt/ros/humble/lib/laser_filters/scan_to_scan_filter_chain',): 'scan_to_scan_filter_chain',
            ('/opt/ros/humble/lib/nav2_amcl/amcl', '--ros-args'): 'amcl',
            ('/opt/ros/humble/lib/nav2_map_server/map_server',): 'map_server',
            ('/opt/ros/humble/lib/nav2_lifecycle_manager/lifecycle_manager',): 'lifecycle_manager',
            ('/usr/bin/python3', '/opt/ros/humble/bin/ros2', 'launch', 'agv_s2_bringup', 'nav_s2.launch.py'):
                'ros2 launch agv_s2_bringup',
            ('/opt/ros/humble/lib/tf2_ros/static_transform_publisher', '--ros-args', '-r',
             '__node:=base_link_to_imu'): 'static_transform_publisher base_link_to_imu',
        }
        for argv, why in cases.items():
            self.assertEqual(sweep.classify(list(argv)), why, argv)

    def test_other_things_are_left_alone(self):
        safe = [
            ['/usr/bin/python3', 'my_script.py', 'amcl'],                       # ชื่ออยู่ใน argument ไม่ใช่ไฟล์ที่รัน
            ['/home/u/amcl'],                                                   # amcl ที่ไม่ใช่ของ nav2
            ['/home/u/map_server', '--port', '80'],
            ['/usr/bin/python3', '/opt/ros/humble/bin/ros2', 'launch', 'other_pkg', 'x.launch.py'],
            ['/usr/bin/python3', '/opt/ros/humble/bin/ros2', 'run', 'agv_s2_bringup', 'imu_test'],   # run ไม่ใช่ launch
            ['/opt/ros/humble/lib/tf2_ros/static_transform_publisher', '--ros-args', '-r', '__node:=my_tf'],
            ['/opt/ros/humble/bin/rviz2'],
            ['/usr/bin/python3', '/opt/ros/humble/bin/ros2', 'run', 'teleop_twist_keyboard', 'teleop_twist_keyboard'],
            ['/usr/bin/python3', '/w/install/Drive_Motor/lib/Drive_Motor/drive_node'],     # ตัวขับมอเตอร์ ห้ามแตะ
            ['bash', '-c', 'ros2 launch agv_s2_bringup nav_s2.launch.py'],      # shell ที่มีข้อความนี้ใน argument
            [],
        ]
        for argv in safe:
            self.assertIsNone(sweep.classify(argv), argv)


class FindTest(SweepBase):

    def test_finds_matching_processes_only(self):
        slam = self.spawn('async_slam_toolbox_node')
        launch = self.spawn('ros2', 'launch', 'agv_s2_bringup', 'mapping_s2.launch.py')
        other = self.spawn('my_node')
        teleop = self.spawn('teleop_twist_keyboard')
        got = self.found()
        self.assertIn(slam.pid, got)
        self.assertIn(launch.pid, got)
        self.assertEqual(got[launch.pid].why, 'ros2 launch agv_s2_bringup')
        self.assertNotIn(other.pid, got)
        self.assertNotIn(teleop.pid, got)

    def test_nav2_names_need_a_nav2_folder(self):
        nav2 = self.spawn('amcl', subdir='opt/lib/nav2_amcl')
        stranger = self.spawn('amcl', subdir='home/me')
        got = self.found()
        self.assertIn(nav2.pid, got)
        self.assertNotIn(stranger.pid, got)

    def test_never_touches_own_process_group_or_the_web_app(self):
        same_group = self.spawn('async_slam_toolbox_node', new_session=False)    # กลุ่มเดียวกับเทสต์ = กลุ่มของเว็บแอป
        web = self.spawn('async_slam_toolbox_node', 'agv_web_app')               # มี agv_web_app ใน argv
        got = self.found()
        self.assertNotIn(same_group.pid, got)
        self.assertNotIn(web.pid, got)

    def test_other_users_processes_are_ignored(self):
        self.spawn('async_slam_toolbox_node')
        self.assertEqual(self.found(uid=os.getuid() + 1), {})

    def test_explicit_exclude_and_zombies(self):
        p = self.spawn('ekf_node')
        self.assertIn(p.pid, self.found())
        self.assertNotIn(p.pid, self.found(exclude=[p.pid]))
        os.kill(p.pid, signal.SIGKILL)
        time.sleep(0.2)                                       # ตายแล้วแต่ยังไม่ถูก wait() = zombie
        self.assertNotIn(p.pid, self.found())
        self.assertFalse(sweep.alive(p.pid))

    def test_within_limits_the_search(self):
        a = self.spawn('ekf_node')
        b = self.spawn('hwt901b_node')
        self.assertEqual(set(sweep.find_system_processes(within=[a.pid]) and
                             [p.pid for p in sweep.find_system_processes(within=[a.pid])]), {a.pid})
        self.assertEqual(sweep.find_system_processes(within=[]), [])
        self.assertIn(b.pid, self.found())


class StopTest(SweepBase):

    def stop(self, **kw):
        args = dict(sigint_wait=1.0, sigterm_wait=1.0, kill_wait=1.0, poll=0.02, find=self.find)
        args.update(kw)
        return sweep.stop_system_processes(**args)

    def test_polite_process_stops_on_sigint(self):
        p = self.spawn('async_slam_toolbox_node')
        stopped, remaining = self.stop()
        self.assertEqual((stopped, remaining), ([p.pid], []))
        self.assertEqual(p.wait(), -signal.SIGINT)

    def test_escalates_to_sigterm_then_sigkill(self):
        stubborn = self.spawn('ekf_node', 'stubborn')
        immortal = self.spawn('wheel_odom_node', 'immortal')
        stopped, remaining = self.stop()
        self.assertEqual(sorted(stopped), sorted([stubborn.pid, immortal.pid]))
        self.assertEqual(remaining, [])
        self.assertEqual(stubborn.wait(), -signal.SIGTERM)
        self.assertEqual(immortal.wait(), -signal.SIGKILL)

    def test_unrelated_process_survives(self):
        keep = self.spawn('my_node')
        self.spawn('hwt901b_node')
        self.stop()
        self.assertIsNone(keep.poll())

    def test_nothing_to_stop(self):
        self.assertEqual(self.stop(), ([], []))

    def test_unkillable_process_is_reported_as_remaining(self):
        p = self.spawn('sllidar_node')
        with mock.patch('agv_web_app.sweep.os.kill'):         # สัญญาณไม่ถึง = ปิดไม่ลง
            stopped, remaining = self.stop(sigint_wait=0.2, sigterm_wait=0.2, kill_wait=0.2)
        self.assertEqual(stopped, [])
        self.assertEqual([r.pid for r in remaining], [p.pid])

    def test_rescans_each_round(self):
        first = self.spawn('async_slam_toolbox_node', 'stubborn')
        calls = []

        def find():
            calls.append(1)
            return self.find()
        stopped, _ = self.stop(find=find, sigint_wait=0.3)
        self.assertIn(first.pid, stopped)
        self.assertGreaterEqual(len(calls), 3)               # รอบ SIGINT, รอบ SIGTERM, แล้วตรวจสุดท้าย


if __name__ == '__main__':
    unittest.main()
