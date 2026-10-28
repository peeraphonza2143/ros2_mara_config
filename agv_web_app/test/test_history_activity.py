"""
ทดสอบ SpeedHistory (ประวัติความเร็ว) และ compute_activity (สถานะหุ่นภาษาคน)

รัน:  cd ~/ros2_ws/src/agv_web_app && python3 -m unittest discover -s test -v
"""
import unittest

from agv_web_app.activity import compute_activity
from agv_web_app.history import MAX_WINDOW_S, SpeedHistory
from agv_web_app.msg import msg

READY = {'ready': True, 'hint': None}
NOT_READY = {'ready': False, 'hint': msg('ready.loc.none.hint')}


def mode(m='NAVIGATION', state='RUNNING', message=None, external=()):
    return {'mode': m, 'state': state, 'message': message, 'external': list(external)}


class SpeedHistoryTest(unittest.TestCase):

    def test_window_filters_old_samples_and_times_are_relative_to_now(self):
        h = SpeedHistory()
        for i in range(10):
            h.add(1000.0 + i, 0.1 * i, 0.0)
        s = h.series(window_s=4.0, now=1009.0)
        self.assertEqual(s['t'], [-4.0, -3.0, -2.0, -1.0, 0.0])
        self.assertEqual(s['v'], [0.5, 0.6, 0.7, 0.8, 0.9])
        self.assertTrue(all(t <= 0 for t in s['t']))

    def test_none_samples_are_kept_as_gaps(self):
        h = SpeedHistory()
        h.add(1.0, 0.2, 0.1)
        h.add(2.0, None, None)
        h.add(3.0, 0.3, -0.1)
        s = h.series(window_s=10, now=3.0)
        self.assertEqual(s['v'], [0.2, None, 0.3])
        self.assertEqual(s['w'], [0.1, None, -0.1])

    def test_ring_buffer_drops_oldest(self):
        h = SpeedHistory(maxlen=3)
        for i in range(5):
            h.add(float(i), float(i), 0.0)
        self.assertEqual(h.series(window_s=100, now=4.0)['v'], [2.0, 3.0, 4.0])

    def test_window_is_clamped(self):
        h = SpeedHistory()
        self.assertEqual(h.series(window_s=99999, now=0.0)['window'], MAX_WINDOW_S)
        self.assertEqual(h.series(window_s=-5, now=0.0)['window'], 1.0)

    def test_empty_history(self):
        s = SpeedHistory().series(now=10.0)
        self.assertEqual((s['t'], s['v'], s['w']), ([], [], []))


class ActivityTest(unittest.TestCase):

    def state(self, m, readiness=READY, moving=False):
        return compute_activity(m, readiness, moving)

    def test_error_has_top_priority(self):
        failure = msg('mode.err.timeout', mode='NAVIGATION', seconds=60, log='/x.log')
        a = self.state(mode(state='ERROR', message=failure), NOT_READY, moving=True)
        self.assertEqual((a['state'], a['level'], a['text']['code']), ('error', 'bad', 'act.error'))
        self.assertEqual(a['detail'], failure)             # ส่งต่อสาเหตุจริงให้หน้าเว็บแปล

    def test_changing_mode(self):
        a = self.state(mode(state='STARTING', message=msg('mode.starting', mode='NAVIGATION')),
                       NOT_READY)
        self.assertEqual((a['state'], a['level']), ('changing', 'warn'))
        self.assertEqual(self.state(mode(state='STOPPING'))['state'], 'changing')

    def test_standby_managed_by_app(self):
        a = self.state(mode('STANDBY'), NOT_READY)
        self.assertEqual((a['state'], a['level']), ('standby', 'off'))

    def test_standby_with_external_system_is_judged_by_readiness(self):
        # รันนอกแอป: ไม่ใช่ "พักเครื่อง" ต้องดูความพร้อมจริงของระบบที่รันอยู่
        a = self.state(mode('STANDBY', external=['bt_navigator']), READY)
        self.assertEqual(a['state'], 'idle')
        self.assertEqual(self.state(mode('STANDBY', external=['ekf_filter_node']),
                                    NOT_READY)['state'], 'not_ready')

    def test_not_ready_shows_hint(self):
        a = self.state(mode('NAVIGATION'), NOT_READY)
        self.assertEqual((a['state'], a['detail']), ('not_ready', NOT_READY['hint']))

    def test_moving_and_idle(self):
        self.assertEqual(self.state(mode(), moving=True)['state'], 'moving')
        idle = self.state(mode(), moving=False)
        self.assertEqual((idle['state'], idle['level'], idle['detail']['code']),
                         ('idle', 'ok', 'act.idle.navigation'))

    def test_idle_detail_depends_on_mode(self):
        self.assertEqual(self.state(mode('MAPPING'))['detail']['code'], 'act.idle.mapping')
        self.assertEqual(self.state(mode('MANUAL'))['detail']['code'], 'act.idle.manual')
        self.assertEqual(self.state(mode('NAVIGATION', external=['x']))['detail']['code'],
                         'act.idle.navigation')

    def test_not_ready_beats_moving(self):
        # ไม่พร้อม + ล้อหมุน (เช่นเข็นด้วยมือ) → ยังบอก "ไม่พร้อม" ไม่ใช่ "กำลังเคลื่อนที่"
        self.assertEqual(self.state(mode(), NOT_READY, moving=True)['state'], 'not_ready')


if __name__ == '__main__':
    unittest.main()


class MsgTest(unittest.TestCase):

    def test_param_named_code_does_not_collide(self):
        # เคยพลาด: msg('x', code=3) ชนกับอาร์กิวเมนต์แรกชื่อ code → TypeError ตอนระบบตายเอง
        self.assertEqual(msg('x', code=3), {'code': 'x', 'params': {'code': 3}})

    def test_is_json_serialisable(self):
        import json
        json.dumps(msg('mode.err.leftover', nodes=['a', 'b'], n=1))
