"""
msg — ข้อความที่ผู้ใช้เห็น ส่งเป็น "รหัส + พารามิเตอร์" แทนประโยคสำเร็จรูป

backend ไม่รู้ภาษา: ส่ง {'code': 'mode.err.timeout', 'params': {...}} แล้วหน้าเว็บ (web/i18n.js)
แปลเป็นภาษาที่ผู้ใช้เลือก (EN / TH) — เพิ่มภาษาใหม่ได้โดยแก้ไฟล์เดียว
ทุก code ที่ใช้ต้องมีคำแปลครบทุกภาษาใน i18n.js (มีเทสต์ test_i18n_codes.py คอยตรวจ)
"""


def msg(code, /, **params):
    """สร้างข้อความ (dict ที่เป็น JSON ได้) ; params เป็น str / ตัวเลข / list ของ str

    code เป็น positional-only → ตั้งชื่อ param ว่า code ก็ไม่ชนกัน
    """
    return {'code': code, 'params': params}
