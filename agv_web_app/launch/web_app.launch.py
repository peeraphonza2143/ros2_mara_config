# เปิดเว็บแอป AGV (เฟส 1: หน้าสถานะ)
#
#   ros2 launch agv_web_app web_app.launch.py
#   ros2 launch agv_web_app web_app.launch.py port:=9000
#
# เปิดคู่กับ nav_s2.launch.py (หรือ ekf_s2) — เว็บแอปแค่อ่านสถานะ ไม่ได้เปิดเซนเซอร์เอง
import os

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue
from ament_index_python.packages import get_package_share_directory


def generate_launch_description():
    pkg_dir = get_package_share_directory('agv_web_app')

    return LaunchDescription([
        DeclareLaunchArgument(
            'port', default_value='8080',
            description='พอร์ตของเว็บเซิร์ฟเวอร์'),
        DeclareLaunchArgument(
            'config', default_value=os.path.join(pkg_dir, 'config', 'web_app.yaml'),
            description='ไฟล์ค่าตั้งต้นของเว็บแอป'),

        Node(
            package='agv_web_app',
            executable='web_server_node',
            name='web_server_node',
            output='screen',
            # launch argument เป็น string → ต้องบอกชนิดเมื่อส่งเป็น parameter
            parameters=[
                LaunchConfiguration('config'),
                {'port': ParameterValue(LaunchConfiguration('port'), value_type=int)},
            ],
            # ตอนกด Ctrl+C เว็บแอปต้องปิดระบบของโหมดที่เปิดไว้ก่อน (ใช้เวลาได้ถึง ~35 วินาที)
            # ค่าตั้งต้นของ ros2 launch รอแค่ 5 วินาทีแล้วใช้ SIGTERM/SIGKILL → ระบบจะค้างกำพร้า
            sigterm_timeout='60',
            sigkill_timeout='30',
        ),
    ])
