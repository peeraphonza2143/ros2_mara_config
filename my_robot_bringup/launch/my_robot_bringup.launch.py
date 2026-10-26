import os
from launch import LaunchDescription
from launch_ros.actions import Node
from ament_index_python.packages import get_package_share_directory


def generate_launch_description():
    pkg_dir = get_package_share_directory('my_robot_bringup')

    laser_filter_config = os.path.join(pkg_dir, 'config', 'laser_filter.yaml')

    return LaunchDescription([

        # Static TF: base_link → laser
        # x=0.27m  y=0  z=0.38m  yaw=3.14rad(≈180°)  pitch=0  roll=0
        Node(
            package='tf2_ros',
            executable='static_transform_publisher',
            name='base_link_to_laser',
            arguments=['0.27', '0', '0.38', '3.14', '0', '0',
                       'base_link', 'laser'],
        ),

        # Laser Filter: /scan → /scan_filtered
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
