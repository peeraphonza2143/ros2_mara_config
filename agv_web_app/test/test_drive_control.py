"""
ทดสอบ DriveController (ตัวควบคุมขับด้วยมือ): เพดาน, ramp, deadman, หยุดทันที, ไม่ publish เมื่อไม่ได้ขับ

ใช้นาฬิกาปลอม (ส่งเวลาเข้าไปเอง) → ไม่ต้องรอจริง ไม่ต้องมี ROS/หุ่น
รัน:  cd ~/ros2_ws/src/agv_web_app && python3 -m unittest discover -s test -v
"""
import math
import random
import unittest

from agv_web_app.drive_control import DRIVE_MODES, DriveController, _slew, drive_allowed

DT = 0.05            # 20 Hz


def make(**kw):
    args = dict(max_linear=0.5, max_angular=1.0, default_linear=0.25, default_angular=0.6,
                linear_accel=0.6, angular_accel=1.5, linear_decel=1.5, angular_decel=3.0,
                heartbeat_timeout=0.3, release_hold=0.5)
    args.update(kw)
    return DriveController(**args)


def run(ctl, t0, seconds, lin=None, ang=0.0, **cmd):
    """จำลองเว็บส่งคำสั่งทุก tick (ถ้า lin ไม่ใช่ None) แล้วเก็บผล tick ; คืน (รายการผล, เวลาสุดท้าย)"""
    out, t = [], t0
    for _ in range(int(round(seconds / DT))):
        t += DT
        if lin is not None:
            ctl.command(lin, ang, now=t, **cmd)
        out.append(ctl.tick(now=t))
    return out, t


class LimitsTest(unittest.TestCase):

    def test_speed_is_fraction_times_user_max(self):
        c = make()
        c.command(1.0, 0.0, vmax=0.2, wmax=0.5, now=0.0)
        out, _ = run(c, 0.0, 2.0, 1.0, 0.0, vmax=0.2, wmax=0.5)
        self.assertAlmostEqual(out[-1][0], 0.2, places=6)
        c.command(0.5, -1.0, vmax=0.2, wmax=0.5, now=2.1)
        out, _ = run(c, 2.1, 2.0, 0.5, -1.0, vmax=0.2, wmax=0.5)
        self.assertAlmostEqual(out[-1][0], 0.1, places=6)
        self.assertAlmostEqual(out[-1][1], -0.5, places=6)

    def test_user_max_cannot_exceed_the_robot_cap(self):
        c = make()
        out, _ = run(c, 0.0, 3.0, 1.0, 1.0, vmax=99.0, wmax=99.0)
        self.assertAlmostEqual(out[-1][0], 0.5, places=6)               # เพดาน 0.5 m/s
        self.assertAlmostEqual(out[-1][1], 1.0, places=6)               # เพดาน 1.0 rad/s

    def test_fractions_beyond_one_are_clamped(self):
        c = make()
        out, _ = run(c, 0.0, 3.0, 7.0, -9.0)
        self.assertAlmostEqual(out[-1][0], 0.25, places=6)              # ค่าเริ่มต้น 0.25 × 1.0
        self.assertAlmostEqual(out[-1][1], -0.6, places=6)

    def test_negative_user_max_is_treated_as_zero(self):
        c = make()
        out, _ = run(c, 0.0, 1.0, 1.0, 1.0, vmax=-5.0, wmax=-5.0)
        self.assertTrue(all(o == (0.0, 0.0) or o is None for o in out))

    def test_defaults_used_when_user_max_missing(self):
        c = make(default_linear=0.3, default_angular=0.4)
        out, _ = run(c, 0.0, 3.0, 1.0, 1.0)
        self.assertEqual((round(out[-1][0], 6), round(out[-1][1], 6)), (0.3, 0.4))

    def test_defaults_are_capped_by_the_robot_limits(self):
        c = make(max_linear=0.2, default_linear=0.9)
        self.assertEqual(c.default_linear, 0.2)

    def test_invalid_values_are_rejected_and_stop_the_robot(self):
        for bad in (float('nan'), float('inf'), -float('inf'), 'abc', None, True, [1]):
            c = make()
            run(c, 0.0, 2.0, 1.0, 0.0)
            self.assertTrue(c.active)
            self.assertFalse(c.command(bad, 0.0, now=2.0), bad)
            self.assertEqual(c.state()['reason'], 'invalid')
            self.assertEqual(c.tick(now=2.05), (0.0, 0.0), bad)         # ไม่ปล่อยคำสั่งเก่าค้าง
            self.assertFalse(c.active)
        c = make()
        self.assertFalse(c.command(1.0, 0.0, vmax=float('nan'), now=0.0))
        self.assertFalse(c.command(1.0, 0.0, wmax='x', now=0.0))

    def test_hard_invariants_hold_for_random_input(self):
        rnd = random.Random(7)
        c = make(max_linear=0.4, max_angular=0.8)
        t = 0.0
        for _ in range(4000):
            t += DT
            if rnd.random() < 0.8:
                c.command(rnd.uniform(-5, 5), rnd.uniform(-5, 5), vmax=rnd.uniform(-1, 3),
                          wmax=rnd.uniform(-1, 3), now=t)
            if rnd.random() < 0.02:
                c.stop(now=t)
            out = c.tick(now=t)
            if out is None:
                continue
            self.assertLessEqual(abs(out[0]), 0.4 + 1e-9)
            self.assertLessEqual(abs(out[1]), 0.8 + 1e-9)
            self.assertTrue(math.isfinite(out[0]) and math.isfinite(out[1]))


class RampTest(unittest.TestCase):

    def test_acceleration_never_exceeds_the_limit(self):
        c = make()
        out, _ = run(c, 0.0, 2.0, 1.0, 1.0)
        vs = [0.0] + [o[0] for o in out]
        ws = [0.0] + [o[1] for o in out]
        for a, b in zip(vs, vs[1:]):
            self.assertLessEqual(abs(b - a), 0.6 * DT + 1e-9)
        for a, b in zip(ws, ws[1:]):
            self.assertLessEqual(abs(b - a), 1.5 * DT + 1e-9)
        self.assertLess(out[0][0], 0.25)                                 # ไม่กระโดดไปที่เป้าหมายทันที

    def test_reaches_target_in_expected_time(self):
        c = make()                                                       # 0.25 m/s ด้วย 0.6 m/s² ≈ 0.42 วินาที
        out, _ = run(c, 0.0, 0.3, 1.0, 0.0)
        self.assertLess(out[-1][0], 0.25)
        out, _ = run(c, 0.3, 0.3, 1.0, 0.0)
        self.assertAlmostEqual(out[-1][0], 0.25, places=6)

    def test_release_ramps_down_with_the_deceleration_limit(self):
        c = make()
        _, t = run(c, 0.0, 2.0, 1.0, 0.0)
        out, t = run(c, t, 1.0, 0.0, 0.0)                                # ปล่อยจอย (ส่งศูนย์)
        vs = [0.25] + [o[0] for o in out if o is not None]
        for a, b in zip(vs, vs[1:]):
            self.assertLessEqual(abs(b - a), 1.5 * DT + 1e-9)
        self.assertEqual([o for o in out if o is not None][-1], (0.0, 0.0))
        self.assertEqual(c.state()['reason'], 'released')

    def test_reversal_passes_through_zero_not_jump(self):
        c = make()
        _, t = run(c, 0.0, 2.0, 1.0, 0.0)
        out, _ = run(c, t, 1.5, -1.0, 0.0)
        vs = [0.25] + [o[0] for o in out]
        for a, b in zip(vs, vs[1:]):
            self.assertLessEqual(abs(b - a), 1.5 * DT + 1e-9)
        self.assertTrue(any(abs(v) < 1e-9 for v in vs[1:5]) or min(vs) < 0)  # ผ่านศูนย์ไปทางลบ
        self.assertAlmostEqual(out[-1][0], -0.25, places=6)

    def test_slew_helper(self):
        self.assertEqual(_slew(0.0, 1.0, 1.0, 5.0, 0.1), 0.1)            # เร่ง
        self.assertEqual(_slew(1.0, 0.0, 1.0, 5.0, 0.1), 0.5)            # เบรกแรงกว่า
        self.assertEqual(_slew(1.0, -1.0, 1.0, 5.0, 0.1), 0.5)           # กลับทิศ: เบรกก่อน
        self.assertEqual(_slew(0.95, 1.0, 1.0, 5.0, 0.1), 1.0)           # ไม่เลยเป้า
        self.assertEqual(_slew(0.5, 0.5, 1.0, 5.0, 0.1), 0.5)


class DeadmanTest(unittest.TestCase):

    def test_keeps_driving_while_commands_keep_coming(self):
        c = make()
        out, _ = run(c, 0.0, 3.0, 1.0, 0.0)
        self.assertTrue(all(o is not None and o[0] > 0 for o in out))
        self.assertAlmostEqual(out[-1][0], 0.25, places=6)

    def test_commands_stop_coming_robot_decelerates_to_zero(self):
        c = make()
        _, t = run(c, 0.0, 2.0, 1.0, 0.0)
        out, t = run(c, t, 1.0, None)                                     # ไม่มีคำสั่งใหม่ (WiFi หลุด)
        self.assertEqual(c.state()['reason'], 'timeout')
        vs = [0.25] + [o[0] for o in out]
        for a, b in zip(vs, vs[1:]):
            self.assertLessEqual(abs(b - a), 1.5 * DT + 1e-9)             # ลดลงอย่างนุ่มนวล ไม่ใช่กระชาก
        self.assertEqual(out[-1] if out[-1] is not None else (0.0, 0.0), (0.0, 0.0))

    def test_holds_speed_until_the_heartbeat_timeout_then_drops(self):
        c = make(heartbeat_timeout=0.3)
        _, t = run(c, 0.0, 2.0, 1.0, 0.0)
        out, _ = run(c, t, 0.25, None)                                    # 0.25 วินาทีไม่มีคำสั่ง: ยังอยู่ในกรอบเวลา
        self.assertTrue(all(abs(o[0] - 0.25) < 1e-9 for o in out))
        out, _ = run(c, t + 0.25, 0.3, None)
        self.assertLess(out[-1][0], 0.25)                                 # เกิน 0.3 วินาที → เริ่มลด

    def test_time_gap_larger_than_timeout_stops_even_with_one_old_command(self):
        c = make()
        c.command(1.0, 0.0, now=0.0)
        run(c, 0.0, 1.0, 1.0, 0.0)
        out = c.tick(now=10.0)                                            # โปรแกรมค้าง 10 วินาที แล้วกลับมา
        self.assertLess(out[0], 0.25)
        self.assertEqual(c.state()['reason'], 'timeout')

    def test_new_command_after_timeout_drives_again(self):
        c = make()
        _, t = run(c, 0.0, 1.0, 1.0, 0.0)
        _, t = run(c, t, 1.5, None)
        out, _ = run(c, t, 1.0, 1.0, 0.0)
        self.assertAlmostEqual(out[-1][0], 0.25, places=6)


class StopTest(unittest.TestCase):

    def test_stop_is_immediate_zero_without_ramp(self):
        c = make()
        _, t = run(c, 0.0, 2.0, 1.0, 1.0)
        c.stop('stop', now=t)
        self.assertEqual(c.tick(now=t + DT), (0.0, 0.0))
        self.assertFalse(c.active)
        self.assertEqual(c.state()['reason'], 'stop')

    def test_zeros_are_sent_for_a_short_time_then_publishing_stops(self):
        c = make(release_hold=0.5)
        _, t = run(c, 0.0, 1.0, 1.0, 0.0)
        c.stop(now=t)
        out, _ = run(c, t, 1.0, None)
        zeros = [o for o in out if o == (0.0, 0.0)]
        nones = [o for o in out if o is None]
        self.assertEqual(len(zeros) + len(nones), len(out))
        self.assertGreaterEqual(len(zeros), 8)                            # ~0.5 วินาที = ~10 รอบที่ 20 Hz
        self.assertLessEqual(len(zeros), 12)
        self.assertEqual(out[-1], None)                                   # หลังจากนั้นเงียบ

    def test_idle_controller_never_publishes(self):
        c = make()
        for i in range(100):
            self.assertIsNone(c.tick(now=i * DT))

    def test_zero_command_from_idle_does_not_start_publishing(self):
        c = make()
        c.command(0.0, 0.0, now=0.0)
        self.assertIsNone(c.tick(now=DT))

    def test_after_release_publishes_zeros_then_goes_silent(self):
        c = make(release_hold=0.5)
        _, t = run(c, 0.0, 1.0, 1.0, 0.0)
        out, t = run(c, t, 1.5, 0.0, 0.0)
        self.assertEqual(out[-1] if out[-1] is not None else None, None)
        self.assertTrue(any(o == (0.0, 0.0) for o in out))
        nones_at_end = out[-5:]
        self.assertTrue(all(o is None for o in nones_at_end))

    def test_command_during_hold_resumes_driving(self):
        c = make()
        _, t = run(c, 0.0, 1.0, 1.0, 0.0)
        c.stop(now=t)
        c.tick(now=t + DT)
        out, _ = run(c, t + DT, 1.0, 1.0, 0.0)
        self.assertAlmostEqual(out[-1][0], 0.25, places=6)

    def test_negative_zero_is_normalised(self):
        c = make()
        _, t = run(c, 0.0, 1.0, 1.0, -1.0)
        out, _ = run(c, t, 2.0, 0.0, 0.0)
        zeros = [v for o in out if o is not None for v in o if v == 0]
        self.assertTrue(zeros)
        for v in zeros:
            self.assertEqual(math.copysign(1.0, v), 1.0)            # ศูนย์ต้องเป็น +0.0 ไม่ใช่ -0.0

    def test_state_and_limits_are_json_friendly(self):
        import json
        c = make()
        run(c, 0.0, 1.0, 1.0, 0.0)
        json.dumps(c.state())
        self.assertEqual(c.limits()['max_linear'], 0.5)


class AllowedTest(unittest.TestCase):

    def snap(self, mode, state='RUNNING', **kw):
        return dict({'mode': mode, 'state': state, 'external': [], 'leftover': None}, **kw)

    def test_only_mapping_and_manual_when_running_cleanly(self):
        self.assertEqual(DRIVE_MODES, ('MAPPING', 'MANUAL'))
        self.assertTrue(drive_allowed(self.snap('MAPPING')))
        self.assertTrue(drive_allowed(self.snap('MANUAL')))
        self.assertFalse(drive_allowed(self.snap('NAVIGATION')))          # Nav2 สั่ง /cmd_vel อยู่
        self.assertFalse(drive_allowed(self.snap('STANDBY')))
        for state in ('STARTING', 'STOPPING', 'ERROR'):
            self.assertFalse(drive_allowed(self.snap('MAPPING', state)), state)
        self.assertFalse(drive_allowed(self.snap('MAPPING', external=['bt_navigator'])))
        self.assertFalse(drive_allowed(self.snap('MAPPING', leftover={'mode': 'NAVIGATION'})))
        self.assertFalse(drive_allowed({}))


if __name__ == '__main__':
    unittest.main()
