from launch import LaunchDescription
from launch_ros.actions import Node


def generate_launch_description():
    return LaunchDescription([
        # 1. Drive motor node - subscribe /cmd_vel, publish odometry + TF.
        #    Equivalent to: ros2 run Drive_Motor drive_odom
        Node(
            package='Drive_Motor',
            executable='drive_odom',
            name='drive_odom',
            output='screen'
        ),

        # 2. หน้าจอควบคุม manual ด้วย PyQt6 - ปุ่ม/คีย์บอร์ด -> /cmd_vel
        Node(
            package='application',
            executable='pyqt_motor_control_ui',
            name='pyqt_motor_control_ui',
            output='screen',
            parameters=[{
                'speed_level': 3,  # 1-5, ดูค่าจริงต่อระดับได้ที่ SPEED_LEVELS ใน pyqt_motor_control_ui.py
            }]
        ),
    ])
