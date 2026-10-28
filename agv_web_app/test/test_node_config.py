"""
โหลด config/web_app.yaml "ตัวจริง" เข้า StatusNode "ตัวจริง" (regression test)

เคยพลาด: YAML เขียน `session_minutes: 30` (int) แต่โค้ดประกาศเป็น 30.0 (double)
→ ROS ปฏิเสธ node ล้มตอนเริ่ม; ทดสอบด้วยพารามิเตอร์จาก command line ไม่เจอบั๊กนี้

ต้อง source ROS ก่อนรัน (ถ้าไม่มี rclpy จะข้ามเทสต์พวกนี้)
ใช้ ROS_DOMAIN_ID=77 ในเทสต์ เพื่อไม่ให้ node ทดสอบไปปนกับระบบหุ่นจริง
"""
import os
import unittest

import yaml

try:
    import rclpy
except ImportError:      # ไม่ได้ source ROS
    rclpy = None

PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(PKG_ROOT, 'config', 'web_app.yaml')


@unittest.skipIf(rclpy is None, 'ต้อง source ROS ก่อน (ไม่พบ rclpy)')
class NodeConfigTest(unittest.TestCase):

    def setUp(self):
        old = os.environ.get('ROS_DOMAIN_ID')
        os.environ['ROS_DOMAIN_ID'] = '77'
        self.addCleanup(self._restore_domain, old)

    @staticmethod
    def _restore_domain(old):
        if old is None:
            os.environ.pop('ROS_DOMAIN_ID', None)
        else:
            os.environ['ROS_DOMAIN_ID'] = old

    def make_node(self, *overrides):
        """สร้าง StatusNode โดยโหลด YAML จริง แล้วทับด้วย -p ... ตามที่ขอ"""
        from agv_web_app.web_server_node import StatusNode
        args = ['--ros-args', '--params-file', CONFIG]
        for item in overrides:
            args += ['-p', item]
        rclpy.init(args=args)
        self.addCleanup(rclpy.try_shutdown)
        node = StatusNode()
        self.addCleanup(node.destroy_node)
        return node

    def test_shipped_yaml_loads_without_error(self):
        node = self.make_node()
        self.assertEqual(node.port, 8080)
        # ไม่ตรวจว่า PIN ว่างหรือไม่: ผู้ใช้ตั้ง PIN ของตัวเองในไฟล์นี้ได้ (ดูเทสต์ถัดไปสำหรับกรณีว่าง)
        self.assertIsInstance(node.auth.enabled, bool)
        self.assertEqual(node.auth._ttl, 30 * 60)         # session_minutes: 30 (int ใน YAML)

    def test_empty_admin_pin_disables_admin_functions(self):
        node = self.make_node("admin_pin:=''")
        self.assertFalse(node.auth.enabled)              # admin_pin ว่าง = ปิดฟังก์ชันสั่งงาน

    def test_every_yaml_key_is_declared_by_the_node(self):
        # ROS เมินพารามิเตอร์ที่ไม่ได้ประกาศแบบเงียบ ๆ → key พิมพ์ผิดใน YAML จะไม่มีผลโดยไม่แจ้ง
        with open(CONFIG) as f:
            keys = yaml.safe_load(f)['web_server_node']['ros__parameters'].keys()
        node = self.make_node()
        undeclared = [k for k in keys if not node.has_parameter(k)]
        self.assertEqual(undeclared, [], 'key ใน YAML ที่ node ไม่รู้จัก')

    def test_integer_values_accepted_for_numeric_params(self):
        node = self.make_node('session_minutes:=45', 'push_hz:=5', 'mode_sigint_timeout:=20')
        self.assertEqual(node.push_hz, 5.0)
        self.assertEqual(node.auth._ttl, 45 * 60)
        self.assertEqual(node.modes._sigint_timeout, 20.0)

    def test_non_numeric_value_falls_back_to_default(self):
        node = self.make_node('topic_timeout:=abc')
        self.assertEqual(node.timeout, 1.0)

    def test_numeric_pin_disables_admin_functions(self):
        node = self.make_node('admin_pin:=1234')         # ไม่มี quote → ROS อ่านเป็น int
        self.assertFalse(node.auth.enabled)

    def test_quoted_pin_is_kept_exactly_including_leading_zeros(self):
        node = self.make_node("admin_pin:='0042'")
        self.assertTrue(node.auth.enabled)
        self.assertTrue(node.auth.login('0042', 'x').ok)
        self.assertFalse(node.auth.login('42', 'y').ok)


if __name__ == '__main__':
    unittest.main()
