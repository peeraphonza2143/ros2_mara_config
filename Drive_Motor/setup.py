from setuptools import find_packages, setup

package_name = 'Drive_Motor'

setup(
    name=package_name,
    version='0.0.0',
    packages=find_packages(exclude=['test']),
    data_files=[
        ('share/ament_index/resource_index/packages',
            ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='mara01',
    maintainer_email='mara01@todo.todo',
    description='TODO: Package description',
    license='TODO: License declaration',
    extras_require={
        'test': [
            'pytest',
        ],
    },
    entry_points={
        'console_scripts': [
            'drive_node       = Drive_Motor.Drive_Motor:main',
            'drive_odom        = Drive_Motor.Drive_odom:main',
            'calib_radius     = Drive_Motor.calib_radius:main',
            'calib_separation = Drive_Motor.calib_separation:main',
        ],
    },
)
