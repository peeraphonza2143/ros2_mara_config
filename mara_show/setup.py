from setuptools import find_packages, setup

package_name = 'mara_show'

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
    description='เดินหน้า-ถอยหลังวนซ้ำ 1 เมตร พร้อมหยุดทันทีเมื่อ LiDAR เจอ object หน้า/หลังในระยะ 0.5 เมตร',
    license='Apache-2.0',
    entry_points={
        'console_scripts': [
            'shuttle_node      = mara_show.shuttle_node:main',
            'lidar_safety_node = mara_show.lidar_safety_node:main',
        ],
    },
)
