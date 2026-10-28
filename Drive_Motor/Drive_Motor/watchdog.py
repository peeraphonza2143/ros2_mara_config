"""Watchdog ของคำสั่ง /cmd_vel (ไม่พึ่ง ROS/ฮาร์ดแวร์ ทดสอบได้).

ปัญหาที่แก้: ตัวขับมอเตอร์สั่งความเร็วค้างไว้ที่คำสั่งล่าสุดตลอดไป
ถ้าผู้ส่ง /cmd_vel หยุดส่ง (WiFi หลุด / โปรแกรมค้าง / ถูกปิดกลางคัน)
มอเตอร์จะวิ่งต่อด้วยความเร็วเดิม

ถ้าตั้ง cmd_timeout > 0 และไม่มีคำสั่งใหม่เกินเวลานั้น ให้สั่งหยุด
timeout = 0 → ปิด (พฤติกรรมเดิมทุกอย่าง)
"""


class CmdWatchdog:

    def __init__(self, timeout=0.0):
        self.timeout = max(0.0, float(timeout))
        self._last = None      # เวลา (วินาที) ที่ได้รับคำสั่งล่าสุด ; None = ยังไม่เคยได้รับ
        self._tripped = False  # สั่งหยุดไปแล้วหลังคำสั่งล่าสุด

    @property
    def enabled(self):
        return self.timeout > 0.0

    def feed(self, now):
        """ได้รับคำสั่งใหม่: นับเวลาใหม่ และปลดสถานะหยุดเพราะ watchdog."""
        self._last = now
        self._tripped = False

    def age(self, now):
        return None if self._last is None else now - self._last

    def expired(self, now):
        """ควรสั่งหยุดตอนนี้ไหม (ถ้า True และสั่งหยุดสำเร็จ ให้เรียก mark_tripped)."""
        if not self.enabled or self._last is None or self._tripped:
            return False
        return (now - self._last) > self.timeout

    def mark_tripped(self):
        """สั่งหยุดสำเร็จแล้ว: ไม่สั่งซ้ำจนกว่าจะได้รับคำสั่งใหม่.

        ถ้าสั่งไม่สำเร็จ อย่าเรียก ระบบจะลองใหม่รอบถัดไป
        """
        self._tripped = True
