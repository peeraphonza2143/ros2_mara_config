import os
from glob import glob
from setuptools import setup

package_name = 'application'

setup(
    name=package_name,
    version='0.0.1',
    packages=[package_name],
    data_files=[
        ('share/ament_index/resource_index/packages',
            ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
        (os.path.join('share', package_name, 'launch'), glob('launch/*.launch.py')),
        (os.path.join('share', package_name, 'scripts'), glob('scripts/*.sh')),
        (os.path.join('share', package_name, 'web'), glob('web/*')),
        (os.path.join('share', package_name, 'config'), glob('config/*.yaml')),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='your_name',
    maintainer_email='you@example.com',
    description='WiFi manual control application for AMR robot',
    license='Apache-2.0',
    tests_require=['pytest'],
    entry_points={
        'console_scripts': [
            'cmd_vel_watchdog = application.cmd_vel_watchdog:main',
            'wifi_display_ui = application.display_ui:main',
            'pyqt_motor_control_ui = application.pyqt_motor_control_ui:main',
            'agv_control_app = application.agv_control_app:main',
        ],
    },
)
