from setuptools import find_packages, setup

package_name = 'waypoint_sequence'

setup(
    name=package_name,
    version='0.0.1',
    packages=find_packages(exclude=['test']),
    data_files=[
        ('share/ament_index/resource_index/packages',
            ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='mara01',
    maintainer_email='peeraphonza2143@gmail.com',
    description='Sequential waypoint navigation A → B → C using Nav2',
    license='Apache-2.0',
    entry_points={
        'console_scripts': [
            'waypoint_sequence_node = waypoint_sequence.waypoint_sequence_node:main',
            'agv_waypoint_node      = waypoint_sequence.agv_waypoint_node:main',
            'path_recorder         = waypoint_sequence.path_recorder:main',
            'waypoint_editor       = waypoint_sequence.waypoint_editor:main',
            'map_cleaner           = waypoint_sequence.map_cleaner:main',
        ],
    },
)
