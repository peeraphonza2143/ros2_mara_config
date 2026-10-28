"""
ทดสอบตัวประกอบหน้าเว็บ (web_assets) + การส่ง index.html / app.js ที่ประกอบแล้วผ่านเซิร์ฟเวอร์

รัน:  cd ~/ros2_ws/src/agv_web_app && python3 -m unittest discover -s test -v
"""
import os
import re
import shutil
import tempfile
import unittest

from agv_web_app.web_assets import TEMPLATES, AssemblyError, assemble, check, markers

PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(PKG_ROOT, 'web')


def write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)


class AssembleRulesTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='wa_')
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_whole_marker_line_is_replaced_and_rest_is_untouched(self):
        write(self.tmp, 'pages/a.html', '<section>A</section>\n')
        write(self.tmp, 'js/b.js', 'var b = 1;\n')
        write(self.tmp, 'index.html',
              '<main>\n  <!-- @include pages/a.html -->\n  text @include pages/a.html inline\n</main>\n')
        write(self.tmp, 'app.js', '(function () {\n  // @include js/b.js\n  var c = 2;\n})();\n')
        self.assertEqual(assemble(self.tmp, 'index.html'),
                         '<main>\n<section>A</section>\n  text @include pages/a.html inline\n</main>\n')
        self.assertEqual(assemble(self.tmp, 'app.js'), '(function () {\nvar b = 1;\n  var c = 2;\n})();\n')

    def test_file_without_markers_is_returned_as_is(self):
        write(self.tmp, 'index.html', '<html>ok</html>')
        self.assertEqual(assemble(self.tmp, 'index.html'), '<html>ok</html>')

    def test_same_piece_may_be_used_twice(self):
        write(self.tmp, 'js/b.js', 'x;\n')
        write(self.tmp, 'app.js', '// @include js/b.js\n// @include js/b.js\n')
        self.assertEqual(assemble(self.tmp, 'app.js'), 'x;\nx;\n')

    def test_missing_piece_fails_loudly(self):
        write(self.tmp, 'index.html', '<!-- @include pages/missing.html -->\n')
        with self.assertRaises(AssemblyError):
            assemble(self.tmp, 'index.html')

    def test_missing_template_fails_loudly(self):
        with self.assertRaises(AssemblyError):
            assemble(self.tmp, 'index.html')

    def test_only_pages_html_and_js_js_are_allowed(self):
        write(self.tmp, 'secret.txt', 'nope')
        write(self.tmp, 'pages/x.js', 'wrong type')
        write(self.tmp, 'js/x.html', 'wrong type')
        write(os.path.dirname(self.tmp), 'outside_wa.js', 'outside')
        self.addCleanup(lambda: os.path.exists(os.path.join(os.path.dirname(self.tmp), 'outside_wa.js'))
                        and os.remove(os.path.join(os.path.dirname(self.tmp), 'outside_wa.js')))
        for bad in ('../outside_wa.js', 'js/../secret.txt', '/etc/passwd', 'secret.txt', 'pages/x.js', 'js/x.html',
                    'js/sub/x.js', 'pages/../index.html', 'js/.js', 'pages//x.html', 'js/x.js/..'):
            write(self.tmp, 'app.js', f'// @include {bad}\n')
            with self.assertRaises(AssemblyError, msg=bad):
                assemble(self.tmp, 'app.js')

    def test_malformed_marker_is_an_error_not_silently_ignored(self):
        write(self.tmp, 'js/a.js', 'a;\n')
        for bad in ('// @include js/a.js extra', '//@include js/a.js trailing words', '// @include',
                    '<!-- @include js/a.js', '// @include js/a.js -->', '<!-- @include js/a.js // x -->'):
            write(self.tmp, 'app.js', bad + '\n')
            with self.assertRaises(AssemblyError, msg=bad):
                assemble(self.tmp, 'app.js')
        write(self.tmp, 'app.js', 'var s = "// @include js/a.js";\n')                # อยู่กลางบรรทัด = ข้อความธรรมดา
        self.assertEqual(assemble(self.tmp, 'app.js'), 'var s = "// @include js/a.js";\n')

    def test_pieces_cannot_include_other_pieces(self):
        write(self.tmp, 'js/inner.js', 'y;\n')
        write(self.tmp, 'js/outer.js', '// @include js/inner.js\n')
        write(self.tmp, 'app.js', '// @include js/outer.js\n')
        with self.assertRaises(AssemblyError):
            assemble(self.tmp, 'app.js')

    def test_symlink_pointing_outside_is_rejected(self):
        outside = tempfile.mkdtemp(prefix='wa_out_')
        self.addCleanup(shutil.rmtree, outside, True)
        write(outside, 'evil.js', 'evil;\n')
        os.makedirs(os.path.join(self.tmp, 'js'))
        os.symlink(os.path.join(outside, 'evil.js'), os.path.join(self.tmp, 'js', 'evil.js'))
        write(self.tmp, 'app.js', '// @include js/evil.js\n')
        with self.assertRaises(AssemblyError):
            assemble(self.tmp, 'app.js')

    def test_check_assembles_both_templates(self):
        write(self.tmp, 'index.html', 'ok')
        with self.assertRaises(AssemblyError):          # app.js ไม่มี
            check(self.tmp)
        write(self.tmp, 'app.js', 'ok')
        check(self.tmp)


class RealWebDirTest(unittest.TestCase):
    """ชิ้นส่วนจริงของแอป"""

    @classmethod
    def setUpClass(cls):
        cls.html = assemble(WEB, 'index.html')
        cls.js = assemble(WEB, 'app.js')

    def test_check_passes(self):
        check(WEB)

    def test_no_marker_left_after_assembly(self):
        self.assertEqual(markers(self.html), [])
        self.assertEqual(markers(self.js), [])

    def test_every_page_is_present_exactly_once(self):
        views = re.findall(r'<section data-view="(\w+)"', self.html)
        self.assertEqual(sorted(views), ['home', 'mapping', 'nav', 'settings', 'waypoint'])

    def test_html_ids_are_unique(self):
        ids = re.findall(r'\sid="([^"]+)"', self.html)
        self.assertEqual(sorted(i for i in set(ids) if ids.count(i) > 1), [])

    def test_every_marker_in_templates_points_to_an_existing_piece_and_every_piece_is_used(self):
        used = set()
        for name in TEMPLATES:
            with open(os.path.join(WEB, name), encoding='utf-8') as f:
                used.update(markers(f.read()))
        on_disk = {f'{d}/{f}' for d in ('pages', 'js') for f in os.listdir(os.path.join(WEB, d))}
        self.assertEqual(used, on_disk, 'ชิ้นส่วนที่ไม่มีจุดแทรก (หรือจุดแทรกที่ไม่มีไฟล์)')

    def test_js_keeps_one_closure_and_strict_mode(self):
        self.assertTrue(self.js.startswith('//'))
        self.assertEqual(self.js.count("'use strict'"), 1)
        self.assertEqual(len(re.findall(r'^\(function \(\) \{$', self.js, re.M)), 1)
        self.assertTrue(self.js.rstrip().endswith('})();'))

    def test_pieces_are_installed_by_setup(self):
        with open(os.path.join(PKG_ROOT, 'setup.py'), encoding='utf-8') as f:
            setup = f.read()
        self.assertIn("'pages'", setup)
        self.assertIn("'js'", setup)


if __name__ == '__main__':
    unittest.main()
