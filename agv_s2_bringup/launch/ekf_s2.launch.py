import os
from launch import LaunchDescription
from launch.actions import IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch_ros.actions import Node
from ament_index_python.packages import get_package_share_directory


def generate_launch_description():
    pkg_dir = get_package_share_directory('agv_s2_bringup')
    ekf_config = os.path.join(pkg_dir, 'config', 'ekf.yaml')

    return LaunchDescription([

        # ── Sensors (wheel odom + S2 + IMU + TF) ─────────────────────────
        # บังคับ publish_tf:=false เสมอ → EKF เป็นผู้ publish TF odom → base_link คนเดียว
        # (ถ้ามี 2 node publish TF คู่เดียวกัน รถใน RViz จะกระตุกไปมา)
        # argument อื่นของ sensors_s2 ยัง override ได้จาก command line เช่น laser_yaw:=...
        IncludeLaunchDescription(
            PythonLaunchDescriptionSource(
                os.path.join(pkg_dir, 'launch', 'sensors_s2.launch.py')),
            launch_arguments={'publish_tf': 'false'}.items(),
        ),

        # ── EKF: /odom + /imu/data → /odometry/filtered + TF ─────────────
        Node(
            package='robot_localization',
            executable='ekf_node',
            name='ekf_filter_node',      # ต้องตรงกับชื่อบนสุดใน ekf.yaml
            output='screen',
            parameters=[ekf_config],
        ),

    ])
