import sys
import os
import math
import time
import yaml
import rclpy
from nav2_simple_commander.robot_navigator import BasicNavigator, TaskResult
from geometry_msgs.msg import PoseStamped
from ament_index_python.packages import get_package_share_directory


def load_waypoints(file_path: str):
    """โหลด waypoints จากไฟล์ YAML → [(name, x, y, yaw), ...]"""
    with open(file_path) as f:
        data = yaml.safe_load(f)
    return [(wp['name'], float(wp['x']), float(wp['y']), float(wp['yaw']))
            for wp in data['waypoints']]


def make_pose(navigator, x: float, y: float, yaw: float) -> PoseStamped:
    pose = PoseStamped()
    pose.header.frame_id = 'map'
    pose.header.stamp = navigator.get_clock().now().to_msg()
    pose.pose.position.x = x
    pose.pose.position.y = y
    pose.pose.orientation.z = math.sin(yaw / 2.0)
    pose.pose.orientation.w = math.cos(yaw / 2.0)
    return pose


def main():
    # ── เลือกไฟล์ waypoints ──────────────────────────────────────────────
    # วิธีที่ 1 (default): อ่านจาก my_robot_bringup/config/waypoints.yaml
    # วิธีที่ 2 (override): ros2 run waypoint_sequence agv_waypoint_node /path/to/file.yaml
    user_args = rclpy.utilities.remove_ros_args(sys.argv[1:])
    if user_args:
        waypoints_file = user_args[0]
    else:
        pkg_dir = get_package_share_directory('my_robot_bringup')
        waypoints_file = os.path.join(pkg_dir, 'config', 'waypoints.yaml')

    if not os.path.isfile(waypoints_file):
        print(f'[ERROR] ไม่พบไฟล์ waypoints: {waypoints_file}')
        return

    rclpy.init()
    navigator = BasicNavigator()
    navigator.waitUntilNav2Active()

    waypoints = load_waypoints(waypoints_file)
    names = [w[0] for w in waypoints]

    print(f'โหลด waypoints จาก: {waypoints_file}')
    print(f'เส้นทาง: {" → ".join(names)} → วนซ้ำ')
    print('พฤติกรรม: หยุดรอ obstacle อัตโนมัติ ไม่หลีกหลบ\n')

    loop_count = 0

    while rclpy.ok():
        loop_count += 1

        # โหลด waypoints ใหม่ทุกรอบ → แก้ไขไฟล์ YAML แล้วมีผลรอบถัดไปทันที
        try:
            waypoints = load_waypoints(waypoints_file)
            names = [w[0] for w in waypoints]
        except Exception as e:
            print(f'[WARN] โหลด waypoints ล้มเหลว: {e} ใช้ค่าเดิม')

        goals = [make_pose(navigator, x, y, yaw) for _, x, y, yaw in waypoints]

        print(f'=== รอบที่ {loop_count}: {" → ".join(names)} ===')

        # ส่ง waypoints ทั้งหมดพร้อมกัน → Nav2 + RPP วางแผนและติดตามเส้นทาง
        navigator.goThroughPoses(goals)

        while not navigator.isTaskComplete():
            feedback = navigator.getFeedback()
            if feedback and feedback.number_of_poses_remaining is not None:
                print(f'  เหลืออีก {feedback.number_of_poses_remaining} waypoint(s)...')
            time.sleep(2.0)

        result = navigator.getResult()

        if result == TaskResult.SUCCEEDED:
            print(f'<<< รอบที่ {loop_count} สำเร็จ รีสตาร์ท loop...\n')
        elif result == TaskResult.CANCELED:
            print(f'<<< รอบที่ {loop_count} ถูกยกเลิก หยุดทำงาน')
            break
        else:
            # FAILED: เช่น obstacle บังนานเกิน timeout → เริ่มรอบใหม่
            print(f'<<< รอบที่ {loop_count} result={result} เริ่มรอบใหม่...\n')

        time.sleep(1.0)

    navigator.lifecycleShutdown()
    rclpy.shutdown()


if __name__ == '__main__':
    main()
