"""
ทดสอบจุดเริ่มต้นประจำแผนที่ (StartPoses) + การวางให้เองตอน Navigation เริ่ม (AutoStart) + สั่ง AMCL อัปเดตหลังวางตำแหน่ง
ใช้ของปลอมทั้งหมด — ไม่ต้องมี ROS

รัน:  cd ~/ros2_ws/src/agv_web_app && python3 -m unittest discover -s test -v
"""
import json
import os
import shutil
import tempfile
import unittest

from test_nav_pose import META, Clock, FakePublisher

from agv_web_app.nav_pose import REFRESH_GAP_S, REFRESH_TIMES, InitialPose, PoseError
from agv_web_app.start_pose import AutoStart, StartPoseError, StartPoses

VALID = lambda name: isinstance(name, str) and name.replace('_', '').replace('-', '').isalnum()    # noqa: E731


def running(since=1.0):
    return {'mode': 'NAVIGATION', 'state': 'RUNNING', 'target': None, 'since': since}


class StartPosesTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='sp_')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, 'nav', 'start_poses.json')
        self.poses = StartPoses(self.path, VALID)

    def test_roundtrip_and_per_map(self):
        self.assertIsNone(self.poses.get('lab'))
        saved = self.poses.set('lab', 1.0, 2.0, 0.5, META, now=123.0)
        self.assertEqual((saved['x'], saved['y'], saved['yaw'], saved['saved']), (1.0, 2.0, 0.5, 123.0))
        self.poses.set('hall', -1.0, 0.0, 3.0, META)
        self.assertEqual(self.poses.get('lab')['x'], 1.0)
        self.assertEqual(self.poses.get('hall')['x'], -1.0)
        self.assertIsNone(self.poses.get('other'))
        self.assertFalse(os.path.exists(self.path + '.tmp'))

    def test_validation_reuses_pose_rules(self):
        for args, code in (((99.0, 0.0, 0.0), 'nav.err.pose_outside'), (('a', 0.0, 0.0), 'nav.err.pose_invalid'),
                           ((float('nan'), 0.0, 0.0), 'nav.err.pose_invalid')):
            with self.assertRaises(StartPoseError) as cm:
                self.poses.set('lab', *args, META)
            self.assertEqual(cm.exception.code, code)
        with self.assertRaises(StartPoseError) as cm:
            self.poses.set('lab', 1.0, 1.0, 0.0, {'available': False})
        self.assertEqual(cm.exception.code, 'nav.err.no_map')
        self.assertIsNone(self.poses.get('lab'))

    def test_bad_names_refused(self):
        for bad in (None, '', '../x', 'a b', 5):
            with self.assertRaises(StartPoseError) as cm:
                self.poses.set(bad, 1.0, 1.0, 0.0, META)
            self.assertEqual(cm.exception.code, 'nav.err.startpose_nomap')
            self.assertIsNone(self.poses.get(bad))

    def test_names_in_a_hand_edited_file_are_validated_on_read(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, 'w') as f:
            json.dump({'a b': {'x': 1, 'y': 1, 'yaw': 0}, 'ok': {'x': 1, 'y': 1, 'yaw': 0}}, f)
        self.assertIsNone(self.poses.get('a b'))
        self.assertEqual(self.poses.get('ok')['x'], 1.0)

    def test_yaw_wrapped_and_clear(self):
        self.assertAlmostEqual(self.poses.set('lab', 1.0, 1.0, 7.0, META)['yaw'], 7.0 - 6.283185307179586)
        self.poses.clear('lab')
        self.poses.clear('lab')                                    # ซ้ำ/ไม่มี = ไม่เป็นไร
        self.assertIsNone(self.poses.get('lab'))

    def test_corrupt_file_is_empty_and_reported(self):
        os.makedirs(os.path.dirname(self.path))
        for content, reported in (('{not json', True), ('[1,2]', False), (json.dumps({'lab': {'x': 'a'}}), False),
                                  (json.dumps({'lab': 5}), False)):
            errors = []
            with open(self.path, 'w') as f:
                f.write(content)
            poses = StartPoses(self.path, VALID, on_error=errors.append)
            self.assertIsNone(poses.get('lab'), content)
            self.assertEqual(bool(errors), reported, content)
        poses.set('lab', 1.0, 1.0, 0.0, META)                      # ไฟล์เสียแล้วเขียนทับได้
        self.assertEqual(poses.get('lab')['x'], 1.0)


class RefreshTest(unittest.TestCase):

    def make(self, results):
        self.calls = []
        it = iter(results)

        def refresher():
            self.calls.append(1)
            return next(it, True)
        self.pub, self.clock = FakePublisher(), Clock()
        self.events = []
        return InitialPose(self.pub, on_event=self.events.append, sleep=self.clock.sleep, clock=self.clock,
                           refresher=refresher)

    def test_set_requests_nomotion_updates_after_publishing(self):
        pose = self.make([True, True])
        pose.set(1.0, 2.0, 0.5, META)
        self.assertEqual((len(self.pub.sent), len(self.calls)), (3, REFRESH_TIMES))
        self.assertGreaterEqual(self.clock.sleeps.count(REFRESH_GAP_S), REFRESH_TIMES)

    def test_refresher_failure_does_not_fail_the_pose(self):
        pose = self.make([False])
        result = pose.set(1.0, 2.0, 0.5, META)
        self.assertEqual((result['x'], len(self.pub.sent), len(self.calls)), (1.0, 3, 1))
        self.assertTrue(any('unavailable' in e for e in self.events))

    def test_invalid_pose_never_refreshes(self):
        pose = self.make([])
        with self.assertRaises(PoseError):
            pose.set(99.0, 0.0, 0.0, META)
        self.assertEqual(self.calls, [])

    def test_refresh_button(self):
        pose = self.make([True])
        pose.refresh()
        self.assertEqual(len(self.calls), 1)
        pose = self.make([False])
        with self.assertRaises(PoseError) as cm:
            pose.refresh()
        self.assertEqual(cm.exception.code, 'nav.err.refresh_failed')

    def test_no_refresher_configured(self):
        pose = InitialPose(FakePublisher(), sleep=Clock().sleep)
        with self.assertRaises(PoseError):
            pose.refresh()
        self.assertEqual(pose.set(1.0, 1.0, 0.0, META)['x'], 1.0)           # ไม่มี refresher = ทำงานเหมือนเดิม


class FakeInitial:
    def __init__(self, errors=()):
        self.errors, self.sent = list(errors), []

    def set(self, x, y, yaw, meta):
        if self.errors:
            raise PoseError(self.errors.pop(0))
        self.sent.append((x, y, yaw, bool(meta)))


class AutoStartTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='as_')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.poses = StartPoses(os.path.join(self.tmp, 'sp.json'), VALID)
        self.clock = Clock()
        self.name = 'lab'
        self.events = []

    def make(self, initial, deadline=45.0):
        return AutoStart(self.poses, initial, lambda: META, lambda: self.name, on_event=self.events.append,
                         sleep=self.clock.sleep, clock=self.clock, deadline=deadline)

    def run_tick(self, auto, mode):
        auto.tick(mode)
        if auto.thread:
            auto.thread.join(5)

    def test_applies_saved_pose_once_per_run(self):
        self.poses.set('lab', 1.0, 2.0, 0.5, META)
        initial = FakeInitial()
        auto = self.make(initial)
        self.run_tick(auto, running(1.0))
        self.assertEqual(initial.sent, [(1.0, 2.0, 0.5, True)])
        self.assertEqual(auto.status(), {'state': 'applied', 'error': None, 'name': 'lab'})
        for _ in range(5):
            self.run_tick(auto, running(1.0))                    # รอบเดิม = ไม่ส่งซ้ำ (ไม่ทับตำแหน่งที่ผู้ใช้แก้เอง)
        self.assertEqual(len(initial.sent), 1)
        self.run_tick(auto, running(2.0))                        # รอบใหม่ (Nav restart) = ส่งอีกครั้ง
        self.assertEqual(len(initial.sent), 2)

    def test_no_saved_pose_or_unnamed_map(self):
        initial = FakeInitial()
        auto = self.make(initial)
        self.run_tick(auto, running())
        self.assertEqual((initial.sent, auto.status()['state']), ([], 'none'))
        self.poses.set('lab', 1.0, 1.0, 0.0, META)
        self.name = None                                         # แผนที่ default/config ไม่มีชื่อ
        auto2 = self.make(initial)
        self.run_tick(auto2, running())
        self.assertEqual((initial.sent, auto2.status()['state']), ([], 'none'))

    def test_only_when_navigation_is_settled(self):
        self.poses.set('lab', 1.0, 1.0, 0.0, META)
        initial = FakeInitial()
        auto = self.make(initial)
        for mode in ({'mode': 'NAVIGATION', 'state': 'STARTING', 'target': None, 'since': 1.0},
                     {'mode': 'NAVIGATION', 'state': 'RUNNING', 'target': 'STANDBY', 'since': 1.0},
                     {'mode': 'MAPPING', 'state': 'RUNNING', 'target': None, 'since': 1.0},
                     {'mode': 'STANDBY', 'state': 'RUNNING', 'target': None, 'since': 1.0}):
            self.run_tick(auto, mode)
        self.assertEqual((initial.sent, auto.status()['state']), ([], 'idle'))

    def test_retries_until_amcl_and_map_are_ready(self):
        self.poses.set('lab', 1.0, 1.0, 0.0, META)
        initial = FakeInitial(['nav.err.no_map', 'nav.err.no_listener', 'nav.err.no_listener'])
        auto = self.make(initial)
        self.run_tick(auto, running())
        self.assertEqual((len(initial.sent), auto.status()['state']), (1, 'applied'))
        self.assertEqual(len(self.clock.sleeps), 3)

    def test_gives_up_after_deadline_and_reports_reason(self):
        self.poses.set('lab', 1.0, 1.0, 0.0, META)
        initial = FakeInitial(['nav.err.no_listener'] * 200)
        auto = self.make(initial, deadline=5.0)
        self.run_tick(auto, running())
        self.assertEqual(initial.sent, [])
        self.assertEqual(auto.status(), {'state': 'failed', 'error': 'nav.err.no_listener', 'name': 'lab'})

    def test_hard_errors_do_not_retry(self):
        self.poses.set('lab', 1.0, 1.0, 0.0, META)
        initial = FakeInitial(['nav.err.pose_outside', 'nav.err.pose_outside'])
        auto = self.make(initial)
        self.run_tick(auto, running())
        self.assertEqual((auto.status()['state'], auto.status()['error'], len(initial.errors)),
                         ('failed', 'nav.err.pose_outside', 1))

    def test_leaving_navigation_resets_and_stops_retrying(self):
        self.poses.set('lab', 1.0, 1.0, 0.0, META)
        initial = FakeInitial(['nav.err.no_listener'] * 3)
        auto = self.make(initial)
        seen = []
        original = self.clock.sleep

        def sleep(seconds):
            original(seconds)
            if len(self.clock.sleeps) == 1:
                auto.tick({'mode': 'STANDBY', 'state': 'RUNNING', 'target': None, 'since': 9.0})   # Nav ถูกปิดระหว่างรอ
            seen.append(1)
        auto._sleep = sleep
        self.run_tick(auto, running())
        self.assertEqual((initial.sent, auto.status()['state']), ([], 'idle'))
        self.assertEqual(len(initial.errors), 2)                 # ลองแค่ครั้งแรกแล้วหยุด

    def test_nav_stopping_while_applying_does_not_mark_applied(self):
        self.poses.set('lab', 1.0, 1.0, 0.0, META)
        auto = self.make(None)

        class StopsNavWhileSetting:
            def set(_self, x, y, yaw, meta):
                auto.tick({'mode': 'STANDBY', 'state': 'RUNNING', 'target': None, 'since': 9.0})
        auto._initial = StopsNavWhileSetting()
        self.run_tick(auto, running())
        self.assertEqual(auto.status()['state'], 'idle')

    def test_manual_note(self):
        auto = self.make(FakeInitial())
        auto.note_manual('lab')
        self.assertEqual(auto.status(), {'state': 'applied', 'error': None, 'name': 'lab'})


if __name__ == '__main__':
    unittest.main()
