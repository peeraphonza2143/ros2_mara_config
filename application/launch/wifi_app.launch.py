import os
from launch import LaunchDescription
from launch.actions import ExecuteProcess
from launch_ros.actions import Node
from ament_index_python.packages import get_package_share_directory


def generate_launch_description():
    pkg_share = get_package_share_directory('application')
    web_dir = os.path.join(pkg_share, 'web')
    script_path = os.path.join(pkg_share, 'scripts', 'start_ap.sh')

    ssid = 'AMR_Robot'
    ip = '192.168.4.1'

    return LaunchDescription([
        # 1. Bring up the WiFi hotspot
        ExecuteProcess(
            cmd=['bash', script_path],
            output='screen'
        ),

        # 2. rosbridge websocket server (default port 9090) - lets the
        #    web page talk to ROS2 directly via roslibjs.
        Node(
            package='rosbridge_server',
            executable='rosbridge_websocket',
            name='rosbridge_websocket',
            output='screen'
        ),

        # 3. Serve the web control page with a plain Python HTTP server.
        ExecuteProcess(
            cmd=['python3', '-m', 'http.server', '8000', '--directory', web_dir],
            output='screen'
        ),

        # 4. Safety watchdog: web joystick -> /cmd_vel_web -> here -> /cmd_vel
        Node(
            package='application',
            executable='cmd_vel_watchdog',
            name='cmd_vel_watchdog',
            output='screen',
            parameters=[{
                'input_topic': '/cmd_vel_web',
                'output_topic': '/cmd_vel',
                'timeout_sec': 0.5,
                'check_rate_hz': 10.0,
                'max_linear_x': 0.8,
                'max_angular_z': 2.0,
            }]
        ),

        # 5. Onboard screen UI showing "WIFI CONTROL MODE" + live status.
        Node(
            package='application',
            executable='wifi_display_ui',
            name='wifi_display_ui',
            output='screen',
            parameters=[{
                'ssid': ssid,
                'ip': ip,
            }]
        ),

        # 6. Drive motor node - subscribes /cmd_vel and publishes odometry.
        #    Equivalent to: ros2 run Drive_Motor drive_odom
        Node(
            package='Drive_Motor',
            executable='drive_odom',
            name='drive_odom',
            output='screen'
        ),
    ])
