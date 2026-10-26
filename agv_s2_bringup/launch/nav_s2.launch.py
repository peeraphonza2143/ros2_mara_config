# Navigation ด้วย Nav2 (AMCL + RPP) บนชุด RPLiDAR S2 + IMU + EKF
#
#   ros2 launch agv_s2_bringup nav_s2.launch.py
#   ros2 launch agv_s2_bringup nav_s2.launch.py map_file:=/path/to/map.yaml
#   ros2 launch agv_s2_bringup nav_s2.launch.py rviz:=false
#
# หลังเปิด: ใน RViz กด "2D Pose Estimate" วางตำแหน่ง/ทิศเริ่มต้นของรถให้ตรงแผนที่ก่อน
# (set_initial_pose: false → AMCL ไม่รู้ตำแหน่งจนกว่าจะตั้งให้) แล้วจึงกด "Nav2 Goal"
#
# โครงสร้าง TF:  map ─(amcl)→ odom ─(ekf)→ base_link ─(static)→ laser / imu_link
import os

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.conditions import IfCondition
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from ament_index_python.packages import get_package_share_directory

# ลำดับนี้ = ลำดับที่ lifecycle_manager จะ configure → activate
NAV2_NODES = [
    'map_server',
    'amcl',
    'controller_server',
    'planner_server',
    'behavior_server',
    'bt_navigator',
    'waypoint_follower',
]


def generate_launch_description():
    pkg_dir = get_package_share_directory('agv_s2_bringup')

    nav2_params = LaunchConfiguration('nav2_params')
    map_file = LaunchConfiguration('map_file')
    rviz = LaunchConfiguration('rviz')

    def nav2_node(package, executable, name, **kwargs):
        return Node(package=package, executable=executable, name=name,
                    output='screen', parameters=[nav2_params], **kwargs)

    return LaunchDescription([

        # ── Launch arguments ─────────────────────────────────────────────
        DeclareLaunchArgument(
            'nav2_params',
            default_value=os.path.join(pkg_dir, 'config', 'nav2_params_s2.yaml'),
            description='Nav2 params (ชุด S2)'),
        DeclareLaunchArgument(
            'map_file',
            default_value=os.path.join(pkg_dir, 'map', 'factory_ekf.yaml'),
            description='แผนที่ที่เก็บด้วย mapping_s2.launch.py'),
        DeclareLaunchArgument('rviz', default_value='true',
                              description='เปิด RViz พร้อม config สำหรับ navigation'),

        # ── Sensors + EKF (EKF เป็นผู้ publish TF odom → base_link) ──────
        IncludeLaunchDescription(
            PythonLaunchDescriptionSource(
                os.path.join(pkg_dir, 'launch', 'ekf_s2.launch.py')),
        ),

        # ── Nav2 (ชุดเดียวกับ my_robot_bringup/agv_nav.launch.py) ───────
        Node(package='nav2_map_server', executable='map_server', name='map_server',
             output='screen', parameters=[nav2_params, {'yaml_filename': map_file}]),
        nav2_node('nav2_amcl', 'amcl', 'amcl'),
        nav2_node('nav2_controller', 'controller_server', 'controller_server',
                  remappings=[('cmd_vel', '/cmd_vel')]),
        nav2_node('nav2_planner', 'planner_server', 'planner_server'),
        nav2_node('nav2_behaviors', 'behavior_server', 'behavior_server'),
        nav2_node('nav2_bt_navigator', 'bt_navigator', 'bt_navigator'),
        nav2_node('nav2_waypoint_follower', 'waypoint_follower', 'waypoint_follower'),

        Node(
            package='nav2_lifecycle_manager',
            executable='lifecycle_manager',
            name='lifecycle_manager_navigation',
            output='screen',
            parameters=[{
                'use_sim_time': False,
                'autostart': True,
                'node_names': NAV2_NODES,
            }],
        ),

        # ── RViz ─────────────────────────────────────────────────────────
        Node(
            package='rviz2',
            executable='rviz2',
            name='rviz2',
            arguments=['-d', os.path.join(pkg_dir, 'rviz', 'nav_s2.rviz')],
            condition=IfCondition(rviz),
        ),

    ])
