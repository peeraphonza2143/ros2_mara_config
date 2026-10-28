"""
รัน test/ui_harness.js (logic ของหน้าเว็บ) ด้วย gjs เป็นส่วนหนึ่งของชุดเทสต์ Python

ไม่มี gjs บนเครื่อง → ข้าม  (ติดตั้ง: sudo apt install gjs)
"""
import os
import shutil
import subprocess
import unittest

PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(PKG_ROOT, 'test', 'ui_harness.js')


@unittest.skipIf(shutil.which('gjs') is None, 'ไม่พบ gjs — ข้ามเทสต์หน้าเว็บ')
class UiLogicTest(unittest.TestCase):

    def test_ui_harness_passes(self):
        result = subprocess.run(['gjs', '-m', HARNESS, PKG_ROOT], capture_output=True,
                                text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('0 failed', result.stdout)


if __name__ == '__main__':
    unittest.main()
