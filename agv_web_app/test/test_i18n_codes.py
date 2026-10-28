"""
ตรวจว่า "ทุกรหัสข้อความที่ backend ส่งให้หน้าเว็บ" มีคำแปลครบทั้ง EN และ TH และพารามิเตอร์ตรงกัน

backend ส่ง msg('code', ชื่อ=ค่า) → หน้าเว็บแปลด้วยแม่แบบใน web/i18n.js ("... {ชื่อ} ...")
เทสต์นี้อ่านโค้ด Python ด้วย ast แล้วเทียบกับพจนานุกรมใน i18n.js (ไม่ต้องรัน JavaScript)

รัน:  cd ~/ros2_ws/src/agv_web_app && python3 -m unittest discover -s test -v
"""
import ast
import glob
import os
import re
import unittest

PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CODE_RE = re.compile(r'(mode|act|ready|api|auth|route|find)\.[a-z0-9_.]+')
ENTRY_RE = re.compile(r'^\s*"([\w.]+)":\s*"((?:[^"\\]|\\.)*)"', re.M)
PLACEHOLDER_RE = re.compile(r'\{(\w+)\}')


def load_dictionaries():
    with open(os.path.join(PKG_ROOT, 'web', 'i18n.js'), encoding='utf-8') as f:
        source = f.read()
    en_part, th_part = source.split('\n    th: {', 1)
    en_part = en_part.split('en: {', 1)[1]
    th_part = th_part.split('\n  };', 1)[0]
    return dict(ENTRY_RE.findall(en_part)), dict(ENTRY_RE.findall(th_part))


def backend_sources():
    for path in sorted(glob.glob(os.path.join(PKG_ROOT, 'agv_web_app', '*.py'))):
        with open(path, encoding='utf-8') as f:
            yield os.path.basename(path), ast.parse(f.read())


def string_constants(node):
    """ค่าคงที่ str ทั้งหมดใต้ node (รวมสองแขนของ x if c else y)"""
    return [n.value for n in ast.walk(node)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]


class I18nCodesTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.en, cls.th = load_dictionaries()

    def test_dictionaries_parsed(self):
        self.assertGreater(len(self.en), 100)
        self.assertEqual(sorted(self.en), sorted(self.th), 'en/th ต้องมี key ชุดเดียวกัน')

    def test_placeholders_match_between_languages(self):
        def names(table, key):
            return sorted(PLACEHOLDER_RE.findall(table[key]))

        bad = [k for k in self.en if names(self.en, k) != names(self.th, k)]
        self.assertEqual(bad, [], 'พารามิเตอร์ {x} ของ en/th ไม่ตรงกัน')

    def test_every_backend_code_is_translated_in_both_languages(self):
        codes = {}
        for name, tree in backend_sources():
            for value in string_constants(tree):
                if CODE_RE.fullmatch(value):
                    codes.setdefault(value, name)
        self.assertGreater(len(codes), 30, 'ไม่พบรหัสข้อความใน backend — regex เปลี่ยน?')
        missing = {c: f for c, f in codes.items() if c not in self.en or c not in self.th}
        self.assertEqual(missing, {}, 'รหัสที่ backend ส่งแต่ไม่มีคำแปล (รหัส: ไฟล์)')

    def test_msg_call_params_match_the_template_placeholders(self):
        checked = 0
        problems = []
        for name, tree in backend_sources():
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id == 'msg' and node.args):
                    continue
                passed = sorted(k.arg for k in node.keywords if k.arg)
                for code in string_constants(node.args[0]):
                    if code not in self.en:
                        continue                    # ถูกจับโดยเทสต์ก่อนหน้าแล้ว
                    for lang, table in (('en', self.en), ('th', self.th)):
                        wanted = sorted(set(PLACEHOLDER_RE.findall(table[code])))
                        if sorted(set(passed)) != wanted:
                            problems.append(
                                f'{name}: msg({code!r}) ส่ง {passed} แต่แม่แบบ {lang} ใช้ {wanted}')
                    checked += 1
        self.assertGreater(checked, 25)
        self.assertEqual(problems, [])

    def test_codes_from_activity_idle_detail_table_are_translated(self):
        from agv_web_app.activity import IDLE_DETAIL
        for code in list(IDLE_DETAIL.values()) + ['act.idle.default']:
            self.assertIn(code, self.en)
            self.assertIn(code, self.th)

    def test_no_thai_sentences_left_in_user_facing_backend_values(self):
        # ข้อความที่ผู้ใช้เห็นต้องมาจากรหัส ไม่ใช่ประโยคไทยตายตัวใน backend (docstring ไม่นับ)
        thai = re.compile(r'[฀-๿]')
        offenders = []
        for name, tree in backend_sources():
            docstrings = {id(n.body[0].value) for n in ast.walk(tree)
                          if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                          and n.body and isinstance(n.body[0], ast.Expr)
                          and isinstance(getattr(n.body[0], 'value', None), ast.Constant)}
            # ข้อความที่ส่งเข้า _on_error(...) ไปลง log ของ node = สำหรับผู้พัฒนา ไม่ใช่ UI (เหมือน get_logger)
            # (ข้อความใน raise Error(...) ก็เป็นของผู้พัฒนาเช่นกัน — ถูกจับแล้วส่งต่อเข้า _on_error)
            dev_logs = {id(c) for n in ast.walk(tree)
                        if (isinstance(n, ast.Call) and getattr(n.func, 'attr', None) in ('_on_error', '_on_event'))
                        or isinstance(n, ast.Raise)
                        for c in ast.walk(n)}
            for node in ast.walk(tree):
                if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                        and thai.search(node.value) and id(node) not in docstrings
                        and id(node) not in dev_logs):
                    offenders.append(f'{name}:{node.lineno}: {node.value[:40]!r}')
        # log ของ node (get_logger) ยังเป็นไทยได้ — เป็นข้อความสำหรับผู้พัฒนา ไม่ใช่ UI
        offenders = [o for o in offenders if not o.startswith('web_server_node.py')]
        self.assertEqual(offenders, [])


if __name__ == '__main__':
    unittest.main()
