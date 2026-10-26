# เก็บแผนที่ด้วย slam_toolbox + RPLiDAR S2 โดยเลือกแหล่ง odom ได้
#
#   ros2 launch agv_s2_bringup mapping_s2.launch.py                    # EKF (wheel + IMU)
#   ros2 launch agv_s2_bringup mapping_s2.launch.py use_ekf:=false     # baseline (wheel อย่างเดียว)
#   ros2 launch agv_s2_bringup mapping_s2.launch.py record:=true       # + บันทึก rosbag
#   ros2 launch agv_s2_bringup mapping_s2.launch.py rviz:=false        # ไม่เปิด RViz (เช่น รันผ่าน ssh)
#
# ขับด้วย teleop (terminal อื่น):
#   ros2 run teleop_twist_keyboard teleop_twist_keyboard --ros-args -p speed:=0.2 -p turn:=0.5
#
# บันทึกแผนที่ (ก่อนปิด launch):
#   ros2 run nav2_map_server map_saver_cli -f ~/ros2_ws/src/agv_s2_bringup/map/<ชื่อแผนที่>
#   ros2 service call /slam_toolbox/serialize_map slam_toolbox/srv/SerializePoseGraph \
#     "{filename: '$HOME/ros2_ws/src/agv_s2_bringup/map/<ชื่อแผนที่>'}"
#
# เล่น rosbag ซ้ำเพื่อจูน: /tf ใน bag มีทั้ง odom→base_link และ map→odom อยู่แล้ว
# ถ้าจะรัน EKF / SLAM ใหม่จาก bag ให้เล่นเฉพาะ raw topic เช่น
#   ros2 bag play <bag> --topics /scan /odom /imu/data /tf_static --clock
# (และตั้ง use_sim_time:=true ให้ node ที่รันใหม่)
import os
from datetime import datetime

from launch import LaunchDescription
from launch.actions import (DeclareLaunchArgument, ExecuteProcess,
                            IncludeLaunchDescription, OpaqueFunction)
from launch.conditions import IfCondition, UnlessCondition
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from ament_index_python.packages import get_package_share_directory

# topic ที่บันทึกลง rosbag: raw sensor (ไว้รัน EKF/SLAM ใหม่) + ผลลัพธ์ (ไว้เทียบ)
BAG_TOPICS = ['/scan', '/scan_filtered', '/odom', '/imu/data', '/odometry/filtered',
              '/joint_states', '/cmd_vel', '/tf', '/tf_static']


def make_bag_recorder(context):
    """สร้างชื่อ bag ตอน launch จริง เพื่อใส่โหมด (ekf/wheel) และเวลาในชื่อ."""
    if context.launch_configurations.get('record', 'false').lower() != 'true':
        return []
    use_ekf = context.launch_configurations.get('use_ekf', 'true').lower() == 'true'
    bag_root = os.path.expanduser(context.launch_configurations['bag_dir'])
    os.makedirs(bag_root, exist_ok=True)
    name = f'mapping_{"ekf" if use_ekf else "wheel"}_{datetime.now():%Y%m%d_%H%M%S}'
    return [ExecuteProcess(
        cmd=['ros2', 'bag', 'record', '-o', os.path.join(bag_root, name)] + BAG_TOPICS,
        output='screen',
    )]


def generate_launch_description():
    pkg_dir = get_package_share_directory('agv_s2_bringup')

    use_ekf = LaunchConfiguration('use_ekf')
    rviz = LaunchConfiguration('rviz')
    slam_params_file = LaunchConfiguration('slam_params_file')

    return LaunchDescription([

        # ── Launch arguments ─────────────────────────────────────────────
        DeclareLaunchArgument('use_ekf', default_value='true',
                              description='true = wheel + IMU ผ่าน EKF, '
                                          'false = wheel odom อย่างเดียว (baseline)'),
        DeclareLaunchArgument('rviz', default_value='true',
                              description='เปิด RViz พร้อม config สำหรับเก็บแผนที่'),
        DeclareLaunchArgument('record', default_value='false',
                              description='บันทึก rosbag ระหว่างเก็บแผนที่'),
        DeclareLaunchArgument('bag_dir', default_value='~/ros2_ws/bags',
                              description='โฟลเดอร์เก็บ rosbag'),
        DeclareLaunchArgument('slam_params_file',
                              default_value=os.path.join(
                                  pkg_dir, 'config', 'mapper_params_s2_factory.yaml'),
                              description='SLAM config (ใช้ไฟล์เดียวกันทั้ง 2 โหมดเพื่อเทียบผล)'),

        # ── Odom source: EKF หรือ wheel อย่างเดียว (เลือกได้ทีละแบบ) ──────
        # ekf_s2 จะเปิด sensors_s2 ให้เองพร้อม publish_tf:=false
        IncludeLaunchDescription(
            PythonLaunchDescriptionSource(
                os.path.join(pkg_dir, 'launch', 'ekf_s2.launch.py')),
            condition=IfCondition(use_ekf),
        ),
        # baseline: wheel odom เป็นผู้ publish TF odom → base_link เอง
        IncludeLaunchDescription(
            PythonLaunchDescriptionSource(
                os.path.join(pkg_dir, 'launch', 'sensors_s2.launch.py')),
            launch_arguments={'publish_tf': 'true'}.items(),
            condition=UnlessCondition(use_ekf),
        ),

        # ── SLAM: /scan_filtered + TF odom→base_link → /map + TF map→odom ─
        Node(
            package='slam_toolbox',
            executable='async_slam_toolbox_node',
            name='slam_toolbox',
            output='screen',
            parameters=[slam_params_file],
        ),

        # ── RViz ─────────────────────────────────────────────────────────
        Node(
            package='rviz2',
            executable='rviz2',
            name='rviz2',
            arguments=['-d', os.path.join(pkg_dir, 'rviz', 'mapping_s2.rviz')],
            condition=IfCondition(rviz),
        ),

        # ── rosbag (เมื่อ record:=true) ──────────────────────────────────
        OpaqueFunction(function=make_bag_recorder),

    ])
