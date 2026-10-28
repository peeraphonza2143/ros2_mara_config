"""ทดสอบ CmdWatchdog และพฤติกรรมของ node เมื่อ cmd_timeout ถูกเปิด/ปิด (ไม่ต้องมีมอเตอร์จริง).

ตัวที่สองใช้ Controller ปลอมแทน ZLAC8015D และรันใน ROS_DOMAIN_ID=77 (ข้ามถ้าไม่มี rclpy/zlac8015d)
"""
import os
import time
import unittest

from Drive_Motor.watchdog import CmdWatchdog

try:
    import rclpy
    from geometry_msgs.msg import Twist
    import Drive_Motor.Drive_Motor as drive
except ImportError:
    rclpy = None


class CmdWatchdogTest(unittest.TestCase):

    def test_disabled_by_default_never_expires(self):
        w = CmdWatchdog()
        self.assertFalse(w.enabled)
        w.feed(0.0)
        self.assertFalse(w.expired(1e9))
        self.assertFalse(CmdWatchdog(-1).enabled)

    def test_never_expires_before_first_command(self):
        w = CmdWatchdog(0.5)
        self.assertFalse(w.expired(100.0))
        self.assertIsNone(w.age(100.0))

    def test_expires_after_timeout_only(self):
        w = CmdWatchdog(0.5)
        w.feed(10.0)
        self.assertFalse(w.expired(10.4))
        self.assertFalse(w.expired(10.5))          # เท่ากับ timeout ยังไม่หมด
        self.assertTrue(w.expired(10.51))
        self.assertAlmostEqual(w.age(10.6), 0.6)

    def test_trips_once_then_rearms_on_new_command(self):
        w = CmdWatchdog(0.5)
        w.feed(0.0)
        self.assertTrue(w.expired(1.0))
        w.mark_tripped()
        self.assertFalse(w.expired(2.0))           # ไม่สั่งซ้ำ
        w.feed(3.0)
        self.assertFalse(w.expired(3.2))
        self.assertTrue(w.expired(3.6))

    def test_retries_until_marked_tripped(self):
        w = CmdWatchdog(0.5)
        w.feed(0.0)
        self.assertTrue(w.expired(1.0))
        self.assertTrue(w.expired(1.1))            # สั่งหยุดไม่สำเร็จ (ไม่ mark) → ลองใหม่


class FakeController:
    instances = []

    def __init__(self, port=None):
        self.rpm_calls = []
        self.fail = False
        FakeController.instances.append(self)

    def disable_motor(self):
        pass

    def enable_motor(self):
        pass

    def set_accel_time(self, *a):
        pass

    def set_decel_time(self, *a):
        pass

    def set_mode(self, *a):
        pass

    def get_rpm(self):
        return 0.0, 0.0

    def set_rpm(self, left, right):
        if self.fail:
            raise OSError('RS485 down')
        self.rpm_calls.append((left, right))


@unittest.skipIf(rclpy is None, 'ต้อง source ROS และมี zlac8015d')
class NodeWatchdogTest(unittest.TestCase):

    def setUp(self):
        self._old = os.environ.get('ROS_DOMAIN_ID')
        os.environ['ROS_DOMAIN_ID'] = '77'
        self._ctrl = drive.Controller
        drive.Controller = FakeController
        FakeController.instances.clear()
        self.addCleanup(self.restore)

    def restore(self):
        drive.Controller = self._ctrl
        if self._old is None:
            os.environ.pop('ROS_DOMAIN_ID', None)
        else:
            os.environ['ROS_DOMAIN_ID'] = self._old

    def make(self, timeout):
        args = ['--ros-args', '-p', f'cmd_timeout:={timeout}']
        rclpy.init(args=args)
        self.addCleanup(rclpy.try_shutdown)
        node = drive.ZlacDriverNode()
        self.addCleanup(node.destroy_node)
        return node, FakeController.instances[-1]

    @staticmethod
    def twist(v, w=0.0):
        m = Twist()
        m.linear.x, m.angular.z = v, w
        return m

    def spin_for(self, node, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            rclpy.spin_once(node, timeout_sec=0.02)

    def test_default_keeps_old_behaviour_motors_keep_last_command(self):
        node, motors = self.make(0.0)
        node.cmd_callback(self.twist(0.3))
        self.spin_for(node, 0.6)
        self.assertEqual(len(motors.rpm_calls), 1)       # ไม่มีการสั่งหยุดเอง
        self.assertNotEqual(motors.rpm_calls[-1], (0, 0))

    def test_stops_motors_after_timeout_once(self):
        node, motors = self.make(0.3)
        node.cmd_callback(self.twist(0.3))
        self.assertNotEqual(motors.rpm_calls[-1], (0, 0))
        self.spin_for(node, 0.15)
        self.assertEqual(len(motors.rpm_calls), 1)       # ยังไม่หมดเวลา
        self.spin_for(node, 0.5)
        self.assertEqual(motors.rpm_calls[-1], (0, 0))
        self.assertEqual(len(motors.rpm_calls), 2)
        self.spin_for(node, 0.4)
        self.assertEqual(len(motors.rpm_calls), 2)       # ไม่สั่งซ้ำรัวๆ

    def test_continuous_commands_keep_the_robot_moving(self):
        node, motors = self.make(0.3)
        end = time.monotonic() + 0.9
        while time.monotonic() < end:
            node.cmd_callback(self.twist(0.2))
            self.spin_for(node, 0.05)
        self.assertTrue(all(c != (0, 0) for c in motors.rpm_calls))

    def test_new_command_after_stop_moves_again_and_arms_again(self):
        node, motors = self.make(0.2)
        node.cmd_callback(self.twist(0.3))
        self.spin_for(node, 0.5)
        self.assertEqual(motors.rpm_calls[-1], (0, 0))
        node.cmd_callback(self.twist(0.3))
        self.assertNotEqual(motors.rpm_calls[-1], (0, 0))
        self.spin_for(node, 0.5)
        self.assertEqual(motors.rpm_calls[-1], (0, 0))

    def test_retries_when_rs485_fails_during_stop(self):
        node, motors = self.make(0.2)
        node.cmd_callback(self.twist(0.3))
        motors.fail = True
        self.spin_for(node, 0.5)                          # ถึงเวลาหยุดแต่สั่งไม่ได้
        self.assertEqual(len(motors.rpm_calls), 1)
        motors.fail = False
        self.spin_for(node, 0.2)
        self.assertEqual(motors.rpm_calls[-1], (0, 0))    # RS485 กลับมา → หยุดได้

    def test_no_command_ever_means_no_action(self):
        node, motors = self.make(0.2)
        self.spin_for(node, 0.5)
        self.assertEqual(motors.rpm_calls, [])


if __name__ == '__main__':
    unittest.main()
