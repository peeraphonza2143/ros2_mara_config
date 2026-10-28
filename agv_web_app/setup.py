import os
from glob import glob
from setuptools import find_packages, setup

package_name = 'agv_web_app'

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
        (os.path.join('share', package_name, 'web'),
            glob('web/*.*')),
        (os.path.join('share', package_name, 'web', 'pages'),
            glob('web/pages/*.html')),
        (os.path.join('share', package_name, 'web', 'js'),
            glob('web/js/*.js')),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='mara01',
    maintainer_email='peeraphonza2143@gmail.com',
    description='Web app for operating the AGV from a phone/tablet over WiFi',
    license='Apache-2.0',
    entry_points={
        'console_scripts': [
            'web_server_node = agv_web_app.web_server_node:main',
        ],
    },
)
