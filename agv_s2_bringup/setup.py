import os
from glob import glob
from setuptools import find_packages, setup

package_name = 'agv_s2_bringup'

setup(
    name=package_name,
    version='0.0.1',
    packages=find_packages(exclude=['test']),
    data_files=[
        ('share/ament_index/resource_index/packages',
            ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
        (os.path.join('share', package_name, 'launch'),
            glob('launch/*.launch.py')),
        (os.path.join('share', package_name, 'config'),
            glob('config/*.yaml')),
        (os.path.join('share', package_name, 'map'),
            glob('map/*')),
        (os.path.join('share', package_name, 'rviz'),
            glob('rviz/*.rviz')),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='mara01',
    maintainer_email='peeraphonza2143@gmail.com',
    description='Bringup for AGV with RPLiDAR S2 + WitMotion HWT901B IMU',
    license='Apache-2.0',
    entry_points={
        'console_scripts': [
            'wheel_odom_node = agv_s2_bringup.wheel_odom_node:main',
            'imu_config      = agv_s2_bringup.imu_config:main',
            'hwt901b_node    = agv_s2_bringup.hwt901b_node:main',
            'imu_test        = agv_s2_bringup.imu_test:main',
            'odom_compare    = agv_s2_bringup.odom_compare:main',
            'nav_repeat_test = agv_s2_bringup.nav_repeat_test:main',
        ],
    },
)
