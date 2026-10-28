"""
ros_params — ตัวส่งจริงของ nav_live: อ่าน/เขียนพารามิเตอร์ของ node อื่นผ่าน service มาตรฐานของ ROS
(<node>/get_parameters, <node>/set_parameters) ; ต้องใช้ rclpy จึงแยกไฟล์ออกจาก nav_live

เธรด: executor ของเว็บแอปหมุนอยู่ในเธรด ROS ส่วนคำขอเว็บมาจากเธรดงาน (run_in_executor)
  → ส่งคำขอด้วย call_async แล้วรอด้วย threading.Event ที่ callback ของ future ปลุก
    (ห้ามเรียก spin_until_future_complete จากเธรดงาน — จะไปแย่ง executor กับเธรด ROS)
client สร้างครั้งเดียวตอนเริ่ม node (รู้ชื่อ node ล่วงหน้า) ; ค้นพบ service ทีหลังตอน Nav2 เปิดได้เอง
"""
import math
import threading

from geometry_msgs.msg import PoseStamped, PoseWithCovarianceStamped
from nav2_msgs.action import FollowPath, NavigateToPose
from rcl_interfaces.msg import Parameter as ParameterMsg
from rcl_interfaces.msg import ParameterType, ParameterValue
from rcl_interfaces.srv import GetParameters, SetParameters
from rclpy.action import ActionClient
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from std_srvs.srv import Empty

from agv_web_app.nav_live import TransportError

WAIT_SERVICE_S = 1.0       # รอ service โผล่ใน graph (Nav2 ไม่รัน = ตอบเร็ว ไม่ค้าง)
CALL_TIMEOUT_S = 3.0


def _to_python(value):
    """ParameterValue → bool / float (ชนิดอื่นหรือยังไม่ได้ประกาศ = None)"""
    if value.type == ParameterType.PARAMETER_BOOL:
        return value.bool_value
    if value.type == ParameterType.PARAMETER_DOUBLE:
        return value.double_value
    if value.type == ParameterType.PARAMETER_INTEGER:
        return float(value.integer_value)
    return None


def _to_message(name, value):
    """ชนิดต้องตรงกับที่ Nav2 ประกาศ: bool → BOOL ; ตัวเลข → DOUBLE เสมอ (ส่ง int ไป node ที่ประกาศ double ถูกปฏิเสธ)"""
    msg = ParameterMsg()
    msg.name = name
    if isinstance(value, bool):
        msg.value = ParameterValue(type=ParameterType.PARAMETER_BOOL, bool_value=value)
    else:
        msg.value = ParameterValue(type=ParameterType.PARAMETER_DOUBLE, double_value=float(value))
    return msg


class RosParamTransport:

    def __init__(self, node, node_names):
        self._clients = {name: (node.create_client(GetParameters, f'{name}/get_parameters'),
                                node.create_client(SetParameters, f'{name}/set_parameters'))
                         for name in node_names}

    def _call(self, node_name, client, request):
        if not client.wait_for_service(timeout_sec=WAIT_SERVICE_S):
            raise TransportError(node_name, 'service not available')
        done = threading.Event()
        future = client.call_async(request)
        future.add_done_callback(lambda _f: done.set())
        if not done.wait(CALL_TIMEOUT_S):
            try:
                client.remove_pending_request(future)
            except Exception:                      # noqa: BLE001 — เก็บกวาดอย่างเดียว ไม่ให้บังข้อผิดพลาดจริง
                pass
            raise TransportError(node_name, 'timeout')
        if future.exception() is not None:
            raise TransportError(node_name, str(future.exception()))
        return future.result()

    def get(self, node, names):
        request = GetParameters.Request()
        request.names = list(names)
        response = self._call(node, self._clients[node][0], request)
        return {name: _to_python(value) for name, value in zip(names, response.values)}

    def set(self, node, values):
        request = SetParameters.Request()
        request.parameters = [_to_message(name, value) for name, value in values.items()]
        response = self._call(node, self._clients[node][1], request)
        return {name: (result.successful, result.reason) for name, result in zip(values, response.results)}


class RosPosePublisher:
    """ตัวส่ง initial pose จริง: publish PoseWithCovarianceStamped ที่ /initialpose (frame map) ให้ AMCL"""

    def __init__(self, node, topic='/initialpose'):
        self._node = node
        qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.VOLATILE)
        self._pub = node.create_publisher(PoseWithCovarianceStamped, topic, qos)

    def listeners(self):
        return self._pub.get_subscription_count()

    def publish(self, x, y, yaw, covariance):
        msg = PoseWithCovarianceStamped()
        msg.header.frame_id = 'map'
        msg.header.stamp = self._node.get_clock().now().to_msg()
        msg.pose.pose.position.x = x
        msg.pose.pose.position.y = y
        msg.pose.pose.orientation.z = math.sin(yaw / 2.0)
        msg.pose.pose.orientation.w = math.cos(yaw / 2.0)
        cov = [0.0] * 36
        cov[0], cov[7], cov[35] = covariance          # แถว/คอลัมน์ x, y และ yaw (index 0, 7, 35 ของเมทริกซ์ 6×6)
        msg.pose.covariance = cov
        self._pub.publish(msg)


# action_msgs/GoalStatus: SUCCEEDED=4, CANCELED=5 ; ที่เหลือ = failed
GOAL_RESULT = {4: 'succeeded', 5: 'canceled'}


def _seconds(duration):
    return duration.sec + duration.nanosec * 1e-9


class RosGoalClient:
    """ตัวส่ง goal จริง: action client ของ Nav2 ตามสัญญาของ nav_goal.NavGoal
      - send()      : NavigateToPose ที่ /navigate_to_pose (ผ่าน planner)
      - send_line() : FollowPath ที่ /follow_path ของ controller_server — ส่งเส้นตรงที่สร้างเองให้ตัวควบคุมตามตรงๆ ข้าม planner
                      controller_id / goal_checker_id ต้องตรงชื่อ plugin ใน nav2 params (FollowPath / general_goal_checker)
    งานค้างได้ทีละหนึ่ง (ตัวนับ token เดียวกัน) → cancel() ยกเลิกงานล่าสุดไม่ว่าชนิดไหน
    send*() เรียกจากเธรดงานของเว็บ ; callback ทั้งหมดมาจากเธรด ROS (executor)
    → ส่งต่อให้ listener ซึ่งมี lock ของตัวเอง"""

    def __init__(self, node, action='navigate_to_pose', line_action='follow_path',
                 controller_id='FollowPath', goal_checker_id='general_goal_checker'):
        self._node = node
        self._client = ActionClient(node, NavigateToPose, action)
        self._line_client = ActionClient(node, FollowPath, line_action)
        self._controller_id, self._goal_checker_id = controller_id, goal_checker_id
        self._lock = threading.Lock()
        self._token = 0
        self._handle = None
        self._cancel_pending = False

    def ready(self):
        return self._client.server_is_ready()

    def line_ready(self):
        return self._line_client.server_is_ready()

    def send(self, x, y, yaw, listener):
        goal = NavigateToPose.Goal()
        goal.pose.header.frame_id = 'map'
        goal.pose.header.stamp = self._node.get_clock().now().to_msg()
        goal.pose.pose.position.x = x
        goal.pose.pose.position.y = y
        goal.pose.pose.orientation.z = math.sin(yaw / 2.0)
        goal.pose.pose.orientation.w = math.cos(yaw / 2.0)
        self._submit(self._client, goal, listener, self._feedback)

    def send_line(self, points, listener):
        """points = [(x, y, yaw), ...] ในเฟรม map (เส้นตรงที่ NavGoal สร้าง)"""
        goal = FollowPath.Goal()
        stamp = self._node.get_clock().now().to_msg()
        goal.path.header.frame_id = 'map'
        goal.path.header.stamp = stamp
        for x, y, yaw in points:
            pose = PoseStamped()
            pose.header.frame_id = 'map'
            pose.header.stamp = stamp
            pose.pose.position.x = float(x)
            pose.pose.position.y = float(y)
            pose.pose.orientation.z = math.sin(yaw / 2.0)
            pose.pose.orientation.w = math.cos(yaw / 2.0)
            goal.path.poses.append(pose)
        goal.controller_id = self._controller_id
        goal.goal_checker_id = self._goal_checker_id
        self._submit(self._line_client, goal, listener, self._line_feedback)

    def _submit(self, client, goal, listener, feedback):
        with self._lock:
            self._token += 1
            token = self._token
            self._handle, self._cancel_pending = None, False
        future = client.send_goal_async(goal, feedback_callback=lambda fb: feedback(token, fb, listener))
        future.add_done_callback(lambda f: self._accepted(token, f, listener))

    def cancel(self):
        with self._lock:
            handle = self._handle
            self._cancel_pending = handle is None            # ยังไม่ถูกรับ → ยกเลิกทันทีที่ถูกรับ
        if handle is not None:
            handle.cancel_goal_async()

    def _line_feedback(self, token, message, listener):
        if token != self._token:
            return
        listener.on_feedback({'distance_remaining': float(message.feedback.distance_to_goal),
                              'eta': None, 'recoveries': 0, 'elapsed': None})

    def _feedback(self, token, message, listener):
        if token != self._token:
            return
        fb = message.feedback
        listener.on_feedback({'distance_remaining': float(fb.distance_remaining),
                              'eta': _seconds(fb.estimated_time_remaining),
                              'recoveries': int(fb.number_of_recoveries),
                              'elapsed': _seconds(fb.navigation_time)})

    def _accepted(self, token, future, listener):
        try:
            handle = future.result()
        except Exception:                                    # noqa: BLE001 — ถูกปฏิเสธ/ติดต่อไม่ได้ = ไม่รับ
            listener.on_accepted(False)
            return
        if not handle.accepted:
            listener.on_accepted(False)
            return
        with self._lock:
            stale = token != self._token
            if not stale:
                self._handle = handle
            cancel_now = stale or self._cancel_pending
        if cancel_now:
            handle.cancel_goal_async()                       # goal เก่า/ถูกสั่งยกเลิกก่อนรับ ห้ามปล่อยให้วิ่ง
        if not stale:
            listener.on_accepted(True)
            handle.get_result_async().add_done_callback(lambda f: self._result(token, f, listener))

    def _result(self, token, future, listener):
        if token != self._token:
            return
        try:
            status = future.result().status
        except Exception:                                    # noqa: BLE001
            status = None
        listener.on_result(GOAL_RESULT.get(status, 'failed'))


class RosNomotionUpdate:
    """เรียก service /request_nomotion_update ของ AMCL (std_srvs/Empty) : ให้ AMCL อัปเดตด้วยสแกนปัจจุบันทันที
    เรียกจากเธรดงานของเว็บ ; คืน True/False (False = ไม่มี service/หมดเวลา/ผิดพลาด)"""

    def __init__(self, node, name='/request_nomotion_update'):
        self._client = node.create_client(Empty, name)

    def __call__(self):
        if not self._client.wait_for_service(timeout_sec=WAIT_SERVICE_S):
            return False
        done = threading.Event()
        future = self._client.call_async(Empty.Request())
        future.add_done_callback(lambda _f: done.set())
        if not done.wait(CALL_TIMEOUT_S):
            try:
                self._client.remove_pending_request(future)
            except Exception:                      # noqa: BLE001 — เก็บกวาดอย่างเดียว
                pass
            return False
        return future.exception() is None
