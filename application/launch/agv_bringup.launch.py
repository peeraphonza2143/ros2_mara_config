"""
agv_bringup.launch.py — เปิดระบบ AGV ทั้งหมดด้วยคำสั่งเดียว

แทนการเปิด 4 terminal:
  1. ros2 run Drive_Motor drive_odom
  2. ros2 launch sllidar_ros2 sllidar_c1_launch.py
  3. ros2 launch my_robot_bringup my_robot_bringup.launch.py
  4. ros2 launch my_robot_bringup agv_nav.launch.py

ลำดับการเปิด:
  t = 0 s          : motor (/odom + TF odom->base_link), lidar (/scan),
                     static TF + laser filter (/scan_filtered)
  t = nav_delay s  : Nav2 (map_server, amcl, controller, planner, ...)
  Nav2 ต้องรอให้ /odom, TF และ /scan_filtered พร้อมก่อน จึงหน่วงเวลาไว้

ตัวอย่าง:
  ros2 launch application agv_bringup.launch.py
  ros2 launch application agv_bringup.launch.py map_file:=/path/to/map.yaml nav_delay:=8.0
"""

import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription, TimerAction
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    bringup_dir = get_package_share_directory('my_robot_bringup')
    lidar_dir = get_package_share_directory('sllidar_ros2')

    map_file = LaunchConfiguration('map_file')
    nav2_params = LaunchConfiguration('nav2_params')
    lidar_port = LaunchConfiguration('lidar_port')
    nav_delay = LaunchConfiguration('nav_delay')

    declare_args = [
        DeclareLaunchArgument(
            'map_file',
            default_value=os.path.join(bringup_dir, 'map', 'my_map_agv3.yaml'),
            description='Path to map yaml file'),
        DeclareLaunchArgument(
            'nav2_params',
            default_value=os.path.join(bringup_dir, 'config', 'nav2_params_agv.yaml'),
            description='Path to nav2 params file (AGV profile)'),
        DeclareLaunchArgument(
            'lidar_port',
            default_value='/dev/rplidar',
            description='Serial port of RPLiDAR C1'),
        DeclareLaunchArgument(
            'nav_delay',
            default_value='5.0',
            description='Seconds to wait before starting Nav2'),
    ]

    # 1. Motor driver: /cmd_vel -> ZLAC8015D, publish /odom + TF odom->base_link
    drive_odom = Node(
        package='Drive_Motor',
        executable='drive_odom',
        name='drive_odom',
        output='screen',
    )

    # 2. Lidar: publish /scan (frame 'laser')
    lidar = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(lidar_dir, 'launch', 'sllidar_c1_launch.py')),
        launch_arguments={'serial_port': lidar_port}.items(),
    )

    # 3. Static TF base_link->laser + laser filter /scan -> /scan_filtered
    robot_bringup = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(bringup_dir, 'launch', 'my_robot_bringup.launch.py')),
    )

    # 4. Nav2 (AGV profile) — หน่วงเวลาให้ข้อมูลจากข้อ 1-3 พร้อมก่อน
    agv_nav = TimerAction(
        period=nav_delay,
        actions=[
            IncludeLaunchDescription(
                PythonLaunchDescriptionSource(
                    os.path.join(bringup_dir, 'launch', 'agv_nav.launch.py')),
                launch_arguments={
                    'map_file': map_file,
                    'nav2_params': nav2_params,
                }.items(),
            ),
        ],
    )

    return LaunchDescription(declare_args + [
        drive_odom,
        lidar,
        robot_bringup,
        agv_nav,
    ])
