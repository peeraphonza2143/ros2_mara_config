"""
auth — PIN ผู้ดูแล + session (ไม่พึ่ง ROS/tornado → ทดสอบได้ด้วยนาฬิกาปลอม)

เป้าหมาย: กันคนเผลอกด/คนในเครือข่ายสั่งเปลี่ยนโหมด ไม่ใช่ความปลอดภัยระดับเครือข่าย
(หน้าเว็บเป็น HTTP — PIN ถูกดักได้ถ้าเครือข่ายไม่เข้ารหัส จึงต้องใช้ WiFi แบบ WPA2)

กติกา
  - admin_pin ว่าง = ปิดการล็อกอินทั้งหมด (ไม่มีรหัสเริ่มต้นที่เดาได้)
  - token ของ session เป็นเลขสุ่ม (secrets) เก็บฝั่งเซิร์ฟเวอร์; ไม่ใช้งานนานเกินกำหนดจึงหมดอายุ
  - เทียบ PIN แบบ constant-time (hmac.compare_digest)
  - ผิดครบ max_fails ครั้ง → ล็อกเครื่องนั้นชั่วคราว และล็อกนานขึ้นเป็นทวีคูณถ้าผิดต่อเนื่อง
"""
import hmac
import math
import secrets
import threading
import time
from collections import OrderedDict, namedtuple

MAX_PIN_LEN = 64

# error: None | 'disabled' | 'locked' | 'bad_pin'
LoginResult = namedtuple('LoginResult', 'ok token error retry_after')


class AuthManager:

    def __init__(self, pin, session_minutes=30.0, max_fails=5, lockout_seconds=30.0,
                 max_lockout_seconds=600.0, max_sessions=20, clock=time.monotonic):
        self._pin = (pin or '').encode('utf-8')
        self._ttl = float(session_minutes) * 60.0
        self._max_fails = int(max_fails)
        self._lockout = float(lockout_seconds)
        self._max_lockout = float(max_lockout_seconds)
        self._max_sessions = int(max_sessions)
        self._clock = clock
        self._lock = threading.Lock()
        self._sessions = OrderedDict()      # token -> เวลาที่ใช้งานล่าสุด (เรียงเก่า→ใหม่)
        self._clients = {}                  # client -> {'fails', 'level', 'until'}

    @property
    def enabled(self):
        return bool(self._pin)

    def login(self, attempt, client):
        """คืน LoginResult; client = ตัวระบุเครื่อง (เช่น IP) ที่ใช้นับครั้งที่ผิด"""
        if not self.enabled:
            return LoginResult(False, None, 'disabled', 0)
        now = self._clock()
        with self._lock:
            rec = self._clients.setdefault(client, {'fails': 0, 'level': 0, 'until': 0.0})
            if now < rec['until']:
                return LoginResult(False, None, 'locked', math.ceil(rec['until'] - now))

            if self._pin_matches(attempt):
                self._clients.pop(client, None)
                return LoginResult(True, self._new_session(now), None, 0)

            rec['fails'] += 1
            if rec['fails'] >= self._max_fails:
                rec['level'] += 1
                wait = min(self._lockout * 2 ** (rec['level'] - 1), self._max_lockout)
                rec['fails'] = 0
                rec['until'] = now + wait
                return LoginResult(False, None, 'locked', math.ceil(wait))
            return LoginResult(False, None, 'bad_pin', 0)

    def validate(self, token, touch=True):
        """token ถูกต้องและยังไม่หมดอายุหรือไม่

        touch=True  : นับเป็นการใช้งาน → ต่ออายุ (หมดอายุเมื่อ "ไม่ใช้งาน" นานเกินกำหนด)
        touch=False : แค่ดูสถานะ ไม่ต่ออายุ — ใช้กับการที่หน้าเว็บถามเป็นระยะ ไม่งั้นการถามเอง
                      จะทำให้เซสชันไม่มีวันหมดอายุ
        """
        if not self.enabled or not isinstance(token, str):
            return False
        now = self._clock()
        with self._lock:
            self._prune(now)
            if token not in self._sessions:
                return False
            if touch:
                self._sessions[token] = now
                self._sessions.move_to_end(token)
            return True

    def logout(self, token):
        with self._lock:
            self._sessions.pop(token, None)

    # ── ภายใน (เรียกใต้ lock เท่านั้น) ─────────────────────────────────
    def _pin_matches(self, attempt):
        if not isinstance(attempt, str) or len(attempt) > MAX_PIN_LEN:
            return False
        return hmac.compare_digest(attempt.encode('utf-8'), self._pin)

    def _new_session(self, now):
        self._prune(now)
        while len(self._sessions) >= self._max_sessions:
            self._sessions.popitem(last=False)          # ทิ้งอันเก่าสุด
        token = secrets.token_urlsafe(32)
        self._sessions[token] = now
        return token

    def _prune(self, now):
        expired = [t for t, last in self._sessions.items() if now - last > self._ttl]
        for token in expired:
            del self._sessions[token]
