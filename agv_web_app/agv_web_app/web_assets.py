"""
web_assets — ประกอบไฟล์หน้าเว็บจากชิ้นส่วนต้นทาง (ไม่พึ่ง ROS → ทดสอบได้)

ต้นทางแยกเป็นไฟล์เล็กๆ เพื่อให้แก้ทีละหน้า/ทีละส่วนได้ แต่เบราว์เซอร์ยังได้ index.html และ app.js อย่างละไฟล์เหมือนเดิม
(พฤติกรรม/สถานะร่วม/WebSocket ไม่เปลี่ยน — app.js ยังเป็น closure เดียว) :
  web/index.html  มีบรรทัดจุดแทรกรูปแบบ  <!-- @include pages/xxx.html -->
  web/app.js      มีบรรทัดจุดแทรกรูปแบบ  // @include js/xxx.js
เซิร์ฟเวอร์แทนที่ "ทั้งบรรทัด" ของจุดแทรกด้วยเนื้อหาไฟล์นั้นตอนส่งให้เบราว์เซอร์

กติกา (ผิดข้อไหนโยน AssemblyError ไม่เงียบ — หน้าเว็บที่ขาดชิ้นส่วนอันตรายกว่าไม่ขึ้น) :
  - จุดแทรกต้องเป็นบรรทัดเดี่ยว ชี้ได้เฉพาะ pages/<ชื่อ>.html หรือ js/<ชื่อ>.js (ชื่อ A-Z a-z 0-9 _ -) ใต้โฟลเดอร์ web/
  - บรรทัดที่ขึ้นต้นเหมือนจุดแทรกแต่รูปแบบผิด (เช่น มีข้อความต่อท้าย) = ข้อผิดพลาด ไม่ถูกมองข้ามเงียบๆ
  - ชิ้นส่วนห้ามมีจุดแทรกซ้อน (ไม่ซ้อนชั้น)
  - ไฟล์ที่ไม่ได้ระบุไว้ (เช่น ../x, พาธสัมบูรณ์, symlink ที่ชี้ออกนอกโฟลเดอร์) ถูกปฏิเสธ
"""
import os
import re

TEMPLATES = ('index.html', 'app.js')
INCLUDE_RE = re.compile(
    r'^[ \t]*(?:<!--[ \t]*@include[ \t]+(\S+?)[ \t]*-->|//[ \t]*@include[ \t]+(\S+?))[ \t]*\r?\n', re.M)
# บรรทัดที่ "ตั้งใจเป็นจุดแทรก" (แม้พิมพ์ผิดรูปแบบ)
LOOSE_RE = re.compile(r'^[ \t]*(?:<!--|//)[ \t]*@include\b', re.M)
ALLOWED_RE = re.compile(r'^(?:pages/[A-Za-z0-9_-]+\.html|js/[A-Za-z0-9_-]+\.js)$')


class AssemblyError(Exception):
    pass


def _read(path):
    try:
        with open(path, encoding='utf-8') as f:
            return f.read()
    except (OSError, UnicodeDecodeError) as e:
        raise AssemblyError(f'อ่านไฟล์ไม่ได้: {path}: {e}')


def markers(text):
    """พาธของจุดแทรกทุกจุดในข้อความ (ตามลำดับ)"""
    return [m.group(1) or m.group(2) for m in INCLUDE_RE.finditer(text)]


def _well_formed(text):
    return len(LOOSE_RE.findall(text)) == len(markers(text))


def assemble(web_dir, name):
    """เนื้อหาของ web/<name> หลังแทนจุดแทรกทั้งหมด (ไฟล์ที่ไม่มีจุดแทรกคืนตามเดิม)"""
    root = os.path.realpath(web_dir)

    def piece(match):
        rel = match.group(1) or match.group(2)
        if not ALLOWED_RE.match(rel):
            raise AssemblyError(f'จุดแทรกไม่ถูกต้อง (ต้องเป็น pages/*.html หรือ js/*.js): {rel!r}')
        path = os.path.realpath(os.path.join(root, rel))
        if os.path.dirname(path) != os.path.join(root, rel.split('/')[0]):          # กัน symlink ที่ชี้ออกนอกโฟลเดอร์
            raise AssemblyError(f'ชิ้นส่วนอยู่นอกโฟลเดอร์ที่อนุญาต: {rel!r}')
        text = _read(path)
        if LOOSE_RE.search(text):
            raise AssemblyError(f'ชิ้นส่วนห้ามมีจุดแทรกซ้อน: {rel}')
        return text

    source = _read(os.path.join(root, name))
    if not _well_formed(source):                           # จุดแทรกที่พิมพ์ผิดรูปแบบ ต้องไม่ถูกมองข้ามเงียบๆ
        raise AssemblyError(f'พบจุดแทรกที่รูปแบบไม่ถูกต้องใน {name} (ต้องเป็นบรรทัดเดี่ยว เช่น // @include js/ชื่อ.js)')
    return INCLUDE_RE.sub(piece, source)


def check(web_dir):
    """ประกอบทุกไฟล์ต้นแบบหนึ่งรอบ (ไว้ให้ node ล้มทันทีตอนเริ่มถ้าชิ้นส่วนขาด)"""
    for name in TEMPLATES:
        assemble(web_dir, name)
