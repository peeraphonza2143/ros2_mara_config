import rclpy
import math
import time
from nav2_simple_commander.robot_navigator import BasicNavigator
from geometry_msgs.msg import PoseStamped


# ── กำหนด Waypoints ที่นี่ (x เมตร, y เมตร, yaw เรเดียน) ──────────────
#    อ่านพิกัดจาก RViz: คลิก "Publish Point" แล้วดูค่าใน /clicked_point
WAYPOINTS = [
    ('A', 1.48375, -0.626756,0.00236524), #-3.31337,0.979509,0.999846
    ('B', -10.5422, -3.45442,0.706509),   # 1.57 rad ≈ 90 องศา #0.79847, 1.19823,9.0799e-07
    ('C', -24.142,-2.06642,0.99997), #-1.2289, -1.07362,0.630321
]


def make_pose(navigator, x, y, yaw):
    pose = PoseStamped()
    pose.header.frame_id = 'map'
    pose.header.stamp = navigator.get_clock().now().to_msg()
    pose.pose.position.x = x
    pose.pose.position.y = y
    pose.pose.orientation.z = math.sin(yaw / 2.0)
    pose.pose.orientation.w = math.cos(yaw / 2.0)
    return pose


def main():
    rclpy.init()
    navigator = BasicNavigator()

    # รอให้ Nav2 และ AMCL พร้อมก่อนเริ่มเดิน
    navigator.waitUntilNav2Active()
    print('Nav2 พร้อมแล้ว เริ่มวิ่ง waypoint sequence...')

    # ── วนซ้ำ A → B → C → A → ... ──────────────────────────────────────
    while rclpy.ok():
        for name, x, y, yaw in WAYPOINTS:
            goal = make_pose(navigator, x, y, yaw)

            print(f'>>> กำลังเดินทางไป waypoint {name}  ({x}, {y})')
            navigator.goToPose(goal)

            # รอจนกว่าจะถึงเป้าหมาย (หรือ fail)
            while not navigator.isTaskComplete():
                pass

            result = navigator.getResult()
            print(f'<<< ถึง {name} แล้ว  |  result: {result}\n')
            time.sleep(1.0)

    navigator.lifecycleShutdown()
    rclpy.shutdown()


if __name__ == '__main__':
    main()
