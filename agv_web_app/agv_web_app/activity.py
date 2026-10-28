"""
activity — "หุ่นกำลังทำอะไรอยู่" เป็นภาษาคน (คำนวณที่ backend ที่เดียว ให้ทุก client ใช้ร่วมกัน)

ไม่พึ่ง ROS → ทดสอบได้. ลำดับความสำคัญ (ข้อบนชนะข้อล่าง):
  error      ระบบผิดพลาด (เปลี่ยนโหมดไม่สำเร็จ)
  changing   กำลังเปิด/ปิดโหมด
  standby    พักเครื่อง — แอปเป็นคนคุมและไม่ได้เปิดระบบอะไรไว้
  not_ready  ระบบยังไม่พร้อม (ตามข้อมูล readiness)
  moving     กำลังเคลื่อนที่ (ความเร็วเกินเกณฑ์ภายใน MOVING_HOLD_S วินาทีล่าสุด)
  idle       หยุดนิ่ง

หมายเหตุ: "กำลังไปจุด X" และ "หยุดรอสิ่งกีดขวาง" ต้องรู้สถานะ goal ของ Nav2
(จะเพิ่มตอนทำ Navigation Mode)
"""

from agv_web_app.msg import msg

MOVING_HOLD_S = 1.5          # ถือว่ายัง "เคลื่อนที่" อีกกี่วินาทีหลังความเร็วหยุด (กันป้ายกะพริบ)

IDLE_DETAIL = {
    'NAVIGATION': 'act.idle.navigation',
    'MAPPING': 'act.idle.mapping',
    'MANUAL': 'act.idle.manual',
}


def compute_activity(mode, readiness, moving):
    """mode = ModeManager.snapshot() ; readiness = ผลของ _readiness() ; moving = bool

    คืน {'state', 'level': ok|warn|bad|off, 'text': msg, 'detail': msg|None}
    (ข้อความเป็น msg() ให้หน้าเว็บแปลภาษา)
    """
    state = mode.get('state')
    external = bool(mode.get('external'))

    if state == 'ERROR':
        return _a('error', 'bad', msg('act.error'), mode.get('message'))
    if state in ('STARTING', 'STOPPING'):
        return _a('changing', 'warn', msg('act.changing'), mode.get('message'))
    if mode.get('mode') == 'STANDBY' and not external:
        return _a('standby', 'off', msg('act.standby'), msg('act.standby.detail'))
    if not readiness.get('ready'):
        return _a('not_ready', 'warn', msg('act.not_ready'), readiness.get('hint'))
    if moving:
        return _a('moving', 'ok', msg('act.moving'), None)
    return _a('idle', 'ok', msg('act.idle'),
              msg(IDLE_DETAIL.get(mode.get('mode'), 'act.idle.default')))


def _a(state, level, text, detail):
    return {'state': state, 'level': level, 'text': text, 'detail': detail}
