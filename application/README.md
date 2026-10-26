# application (AMR WiFi Manual Control)

ROS2 (Humble) package that turns a mini PC on an AMR into a self-contained
WiFi control hotspot: run one launch file and a phone can connect and drive
the robot manually from a web page, with a safety watchdog and an onboard
status screen.

## Package structure

```
application/
├── application/
│   ├── __init__.py
│   ├── cmd_vel_watchdog.py      # Safety watchdog node (/cmd_vel_web -> /cmd_vel)
│   └── display_ui.py            # PyQt6 fullscreen status display node
├── launch/
│   └── wifi_app.launch.py       # Brings up AP + rosbridge + web server + watchdog + UI
├── scripts/
│   └── start_ap.sh              # Creates the WiFi hotspot via nmcli
├── web/
│   ├── index.html               # Joystick control page
│   ├── app.js                   # Joystick logic + rosbridge connection
│   └── roslib.min.js            # NOT included - see "Required manual step" below
├── resource/
│   └── application               # ament resource marker (empty file)
├── package.xml
└── setup.py
```

## Required manual step: get roslib.min.js

`roslib.min.js` is the third-party roslibjs library and is not included in
this package. Since the robot's WiFi hotspot has no internet access, you
must download it once (on a machine with internet) and copy it in:

```bash
# on a machine with internet access
wget https://cdn.jsdelivr.net/npm/roslib@1/build/roslib.min.js -O roslib.min.js
```

Then place the file at `application/web/roslib.min.js` before building the
package (or copy it into the install/share directory after building).

## Install dependencies

```bash
sudo apt update
sudo apt install ros-humble-rosbridge-suite network-manager python3-pyqt6
```

## Build

```bash
cd ~/ros2_ws/src
# copy this "application" folder in here
cd ~/ros2_ws
colcon build --packages-select application
source install/setup.bash
```

## Configure before first run

Edit `scripts/start_ap.sh` to set your WiFi interface name, SSID, and
password:

```bash
IFACE="wlan0"
SSID="AMR_Robot"
PASSWORD="amr12345"
```

Make sure `wlan0` matches an adapter that supports AP mode
(`iw list` -> look for "AP" under Supported interface modes).

## Run

```bash
ros2 launch application wifi_app.launch.py
```

This will:
1. Start the WiFi hotspot (`start_ap.sh`)
2. Start `rosbridge_websocket` (port 9090)
3. Serve the web control page on port 8000
4. Start the `cmd_vel_watchdog` safety node
5. Start the `wifi_display_ui` fullscreen status window

On the phone:
1. Connect to WiFi SSID `AMR_Robot`
2. Open a browser to `http://192.168.4.1:8000`
3. Drag the joystick to drive the robot

## Notes

- `wifi_display_ui` requires a running X11/Wayland desktop session on the
  mini PC (set up auto-login so a session is ready before this launches).
- The watchdog stops the robot automatically if no command is received for
  `timeout_sec` (default 0.5s) - protects against WiFi drops, browser
  freezes, or a closed tab.
- Tune `max_linear_x` / `max_angular_z` in `wifi_app.launch.py` to match
  your robot's safe speed limits.
