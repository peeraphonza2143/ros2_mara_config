import os
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue
from ament_index_python.packages import get_package_share_directory


def generate_launch_description():
    pkg_dir = get_package_share_directory('agv_s2_bringup')
    sllidar_dir = get_package_share_directory('sllidar_ros2')

    laser_filter_config = os.path.join(pkg_dir, 'config', 'laser_filter_s2.yaml')

    lidar_port = LaunchConfiguration('lidar_port')
    imu_port = LaunchConfiguration('imu_port')
    publish_tf = LaunchConfiguration('publish_tf')
    wheel_separation = LaunchConfiguration('wheel_separation')
    laser_x = LaunchConfiguration('laser_x')
    laser_y = LaunchConfiguration('laser_y')
    laser_z = LaunchConfiguration('laser_z')
    laser_yaw = LaunchConfiguration('laser_yaw')
    imu_x = LaunchConfiguration('imu_x')
    imu_y = LaunchConfiguration('imu_y')
    imu_z = LaunchConfiguration('imu_z')
    imu_yaw = LaunchConfiguration('imu_yaw')

    return LaunchDescription([

        # ── Launch arguments (แก้ได้ตอนรันโดยไม่ต้อง build ใหม่) ──────────
        # /dev/rplidar_s2 มาจาก udev rule (จับคู่ด้วย serial ของ CP2102N บน S2)
        DeclareLaunchArgument('lidar_port', default_value='/dev/rplidar_s2',
                              description='Serial port ของ RPLiDAR S2 (udev symlink)'),
        # /dev/imu มาจาก udev rule (CH340 ไม่มี serial → ผูกกับช่อง USB 1-6)
        DeclareLaunchArgument('imu_port', default_value='/dev/imu',
                              description='Serial port ของ IMU HWT901B (udev symlink)'),
        DeclareLaunchArgument('publish_tf', default_value='true',
                              description='true = wheel odom publish TF odom→base_link, '
                                          'false = ให้ EKF publish แทน'),
        # effective track width (calibrate ด้วย odom_compare rotate → ค่า "อ้างอิง gyro/EKF")
        # 2026-10-25: ทวนเข็ม 0.446, ตามเข็ม 0.453 (ล้อลื่นไม่เท่ากันแต่ละทิศ) → ใช้ค่าเฉลี่ย
        # EKF ไม่ได้รับผลจากค่านี้ (fuse แค่ vx จากล้อ, yaw มาจาก gyro)
        DeclareLaunchArgument('wheel_separation', default_value='0.450',
                              description='ระยะห่างล้อซ้าย-ขวา (m) ที่ใช้คำนวณ odom'),

        # ตำแหน่งติดตั้ง S2 เทียบกับ base_link (วัดจริง: x 30 cm, z 36.3 cm)
        # laser_yaw = pi: ด้านหน้าของ S2 หันไปท้ายรถ (ทดสอบจริง: yaw 0 ทำให้ scan กลับหน้า-หลัง)
        DeclareLaunchArgument('laser_x', default_value='0.30'),
        DeclareLaunchArgument('laser_y', default_value='0.0'),
        DeclareLaunchArgument('laser_z', default_value='0.363'),
        DeclareLaunchArgument('laser_yaw', default_value='3.14159'),

        # ตำแหน่งติดตั้ง IMU เทียบกับ base_link (วัดจริง: z 27.2 cm)
        # IMU +Y ชี้ซ้าย, +Z ชี้ขึ้น → +X = Y×Z ชี้หน้า → แกนตรงกับ base_link (yaw = 0)
        DeclareLaunchArgument('imu_x', default_value='0.0'),
        DeclareLaunchArgument('imu_y', default_value='0.0'),
        DeclareLaunchArgument('imu_z', default_value='0.272'),
        DeclareLaunchArgument('imu_yaw', default_value='0.0'),

        # ── Wheel odometry + motor driver ────────────────────────────────
        Node(
            package='agv_s2_bringup',
            executable='wheel_odom_node',
            name='zlac_odom_s2_node',
            output='screen',
            parameters=[{
                'publish_tf': ParameterValue(publish_tf, value_type=bool),
                'wheel_separation': ParameterValue(wheel_separation, value_type=float),
            }],
        ),

        # ── RPLiDAR S2 (ใช้ launch ของ sllidar_ros2 เดิม, baud 1000000) ──
        IncludeLaunchDescription(
            PythonLaunchDescriptionSource(
                os.path.join(sllidar_dir, 'launch', 'sllidar_s2_launch.py')),
            launch_arguments={
                'serial_port': lidar_port,
                'frame_id': 'laser',
            }.items(),
        ),

        # ── IMU HWT901B → /imu/data ──────────────────────────────────────
        Node(
            package='agv_s2_bringup',
            executable='hwt901b_node',
            name='hwt901b_node',
            output='screen',
            parameters=[{
                'port': imu_port,
                'frame_id': 'imu_link',
            }],
        ),

        # ── Static TF: base_link → laser ─────────────────────────────────
        # ใช้ argument แบบมีชื่อกำกับ (แบบไล่ตำแหน่ง deprecated และสับสน yaw/roll ง่าย)
        Node(
            package='tf2_ros',
            executable='static_transform_publisher',
            name='base_link_to_laser',
            arguments=['--x', laser_x, '--y', laser_y, '--z', laser_z,
                       '--yaw', laser_yaw, '--pitch', '0', '--roll', '0',
                       '--frame-id', 'base_link', '--child-frame-id', 'laser'],
        ),

        # ── Static TF: base_link → imu_link ──────────────────────────────
        Node(
            package='tf2_ros',
            executable='static_transform_publisher',
            name='base_link_to_imu',
            arguments=['--x', imu_x, '--y', imu_y, '--z', imu_z,
                       '--yaw', imu_yaw, '--pitch', '0', '--roll', '0',
                       '--frame-id', 'base_link', '--child-frame-id', 'imu_link'],
        ),

        # ── Laser Filter: /scan → /scan_filtered ─────────────────────────
        Node(
            package='laser_filters',
            executable='scan_to_scan_filter_chain',
            name='laser_filter',
            parameters=[laser_filter_config],
            remappings=[
                ('scan',          '/scan'),
                ('scan_filtered', '/scan_filtered'),
            ],
        ),

    ])
