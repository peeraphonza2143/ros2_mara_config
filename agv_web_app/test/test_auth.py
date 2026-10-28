"""
ทดสอบ AuthManager ด้วยนาฬิกาปลอม (ไม่ต้องรอเวลาจริง)

รัน:  cd ~/ros2_ws/src/agv_web_app && python3 -m unittest discover -s test -v
"""
import unittest

from agv_web_app.auth import AuthManager


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class AuthManagerTest(unittest.TestCase):

    def setUp(self):
        self.clock = FakeClock()
        self.auth = AuthManager('4321', session_minutes=30, max_fails=5,
                                lockout_seconds=30, max_sessions=3, clock=self.clock)

    def test_empty_pin_disables_everything(self):
        auth = AuthManager('', clock=self.clock)
        self.assertFalse(auth.enabled)
        res = auth.login('', 'a')
        self.assertEqual((res.ok, res.error), (False, 'disabled'))
        self.assertFalse(auth.validate('anything'))

    def test_correct_pin_gives_working_token(self):
        res = self.auth.login('4321', 'a')
        self.assertTrue(res.ok)
        self.assertTrue(self.auth.validate(res.token))

    def test_wrong_pin_rejected(self):
        res = self.auth.login('0000', 'a')
        self.assertEqual((res.ok, res.error), (False, 'bad_pin'))
        self.assertIsNone(res.token)

    def test_non_string_and_oversized_pin_rejected(self):
        self.assertEqual(self.auth.login(4321, 'a').error, 'bad_pin')
        self.assertEqual(self.auth.login(None, 'a').error, 'bad_pin')
        self.assertEqual(self.auth.login('4' * 5000, 'a').error, 'bad_pin')

    def test_unknown_token_rejected(self):
        self.assertFalse(self.auth.validate('nope'))
        self.assertFalse(self.auth.validate(None))

    def test_tokens_are_unique_and_long(self):
        tokens = {self.auth.login('4321', f'c{i}').token for i in range(3)}
        self.assertEqual(len(tokens), 3)
        self.assertTrue(all(len(t) >= 32 for t in tokens))

    # ── ล็อกเมื่อผิดซ้ำ ────────────────────────────────────────────────
    def test_lockout_after_max_fails(self):
        for _ in range(4):
            self.assertEqual(self.auth.login('x', 'a').error, 'bad_pin')
        res = self.auth.login('x', 'a')                      # ครั้งที่ 5
        self.assertEqual((res.error, res.retry_after), ('locked', 30))
        # ระหว่างล็อก แม้ใส่ PIN ถูกก็ไม่ผ่าน
        self.assertEqual(self.auth.login('4321', 'a').error, 'locked')

    def test_lockout_expires_and_other_clients_unaffected(self):
        for _ in range(5):
            self.auth.login('x', 'a')
        self.assertTrue(self.auth.login('4321', 'b').ok)     # เครื่องอื่นใช้ได้
        self.clock.advance(31)
        self.assertTrue(self.auth.login('4321', 'a').ok)

    def test_repeated_lockouts_get_longer(self):
        for _ in range(5):
            self.auth.login('x', 'a')
        self.clock.advance(31)
        waits = []
        for _ in range(5):
            res = self.auth.login('x', 'a')
        waits.append(res.retry_after)
        self.assertEqual(waits, [60])                        # ครั้งที่ 2 = 2 เท่า

    def test_success_resets_failure_count(self):
        for _ in range(4):
            self.auth.login('x', 'a')
        self.assertTrue(self.auth.login('4321', 'a').ok)
        for _ in range(4):                                   # นับใหม่จาก 0 ไม่ล็อกที่ 5 สะสม
            self.assertEqual(self.auth.login('x', 'a').error, 'bad_pin')

    # ── อายุ session ───────────────────────────────────────────────────
    def test_session_expires_when_idle(self):
        token = self.auth.login('4321', 'a').token
        self.clock.advance(30 * 60 + 1)
        self.assertFalse(self.auth.validate(token))

    def test_activity_extends_session(self):
        token = self.auth.login('4321', 'a').token
        for _ in range(4):                                   # ใช้ทุก 20 นาที → ไม่หมดอายุ
            self.clock.advance(20 * 60)
            self.assertTrue(self.auth.validate(token))

    def test_peek_does_not_extend_session(self):
        # การ "ถามสถานะ" (touch=False) ต้องไม่ต่ออายุ ไม่งั้นเซสชันไม่มีวันหมดอายุ
        token = self.auth.login('4321', 'a').token
        self.clock.advance(20 * 60)
        self.assertTrue(self.auth.validate(token, touch=False))    # ยังไม่หมดอายุ (20 < 30)
        self.clock.advance(11 * 60)                                 # รวม 31 นาทีนับจากล็อกอิน
        self.assertFalse(self.auth.validate(token, touch=False))    # ถามที่นาทีที่ 20 ไม่ต่ออายุ

    def test_logout_invalidates_token(self):
        token = self.auth.login('4321', 'a').token
        self.auth.logout(token)
        self.assertFalse(self.auth.validate(token))

    def test_session_count_is_capped_oldest_dropped(self):
        tokens = []
        for i in range(4):                                   # max_sessions = 3
            tokens.append(self.auth.login('4321', f'c{i}').token)
            self.clock.advance(1)
        self.assertFalse(self.auth.validate(tokens[0]))
        self.assertTrue(all(self.auth.validate(t) for t in tokens[1:]))


if __name__ == '__main__':
    unittest.main()
