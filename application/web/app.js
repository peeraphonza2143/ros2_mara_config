// Connect to rosbridge running on the mini PC (AP host IP)
const ros = new ROSLIB.Ros({ url: 'ws://192.168.4.1:9090' });

ros.on('connection', () => document.getElementById('status').innerText = 'Connected \u2705');
ros.on('error', (e) => document.getElementById('status').innerText = 'Error \u274C');
ros.on('close', () => document.getElementById('status').innerText = 'Disconnected \u26A0\uFE0F');

// NOTE: publishes to /cmd_vel_web, NOT /cmd_vel directly.
// The cmd_vel_watchdog ROS2 node consumes this topic, clamps values,
// and republishes to /cmd_vel - while also auto-stopping the robot if
// this topic goes silent (WiFi drop, tab close, etc).
const cmdVel = new ROSLIB.Topic({
  ros: ros,
  name: '/cmd_vel_web',
  messageType: 'geometry_msgs/msg/Twist'
});

const pad = document.getElementById('pad');
const stick = document.getElementById('stick');
const radius = 90;
let dragging = false;
let lastSendTime = 0;

function sendVel(x, y) {
  const linear = -y * 0.5;   // linear speed scale (m/s)
  const angular = -x * 1.5;  // angular speed scale (rad/s)
  const twist = new ROSLIB.Message({
    linear: { x: linear, y: 0, z: 0 },
    angular: { x: 0, y: 0, z: angular }
  });
  cmdVel.publish(twist);
}

function resetStick() {
  stick.style.top = '90px';
  stick.style.left = '90px';
  sendVel(0, 0); // stop immediately when the finger is released
}

function handleMove(clientX, clientY) {
  const rect = pad.getBoundingClientRect();
  let dx = clientX - rect.left - radius;
  let dy = clientY - rect.top - radius;
  const dist = Math.min(Math.hypot(dx, dy), radius);
  const angle = Math.atan2(dy, dx);
  dx = Math.cos(angle) * dist;
  dy = Math.sin(angle) * dist;

  stick.style.left = (90 + dx) + 'px';
  stick.style.top = (90 + dy) + 'px';

  sendVel(dx / radius, dy / radius);
  lastSendTime = Date.now();
}

pad.addEventListener('pointerdown', (e) => { dragging = true; handleMove(e.clientX, e.clientY); });
pad.addEventListener('pointermove', (e) => { if (dragging) handleMove(e.clientX, e.clientY); });
window.addEventListener('pointerup', () => { dragging = false; resetStick(); });

// Keep sending fresh commands while dragging so the ROS2 watchdog never
// times out during normal continuous use.
setInterval(() => {
  if (dragging && Date.now() - lastSendTime > 300) {
    sendVel(0, 0);
  }
}, 200);
