from setuptools import find_packages, setup

package_name = 'comm_protocol'

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
    maintainer='rwelf',
    maintainer_email='rwelf@todo.todo',
    description='TODO: Package description',
    license='TODO: License declaration',
    extras_require={
        'test': [
            'pytest',
        ],
    },
    entry_points={
        'console_scripts': [
            'lora_sender        = comm_protocol.lora_sender_node:main',
            'lora_receiver      = comm_protocol.lora_receiver_node:main',
            'lora_bridge        = comm_protocol.lora_bridge_node:main',
            'heartbeat_node     = comm_protocol.heartbeat_node:main',
            'joy_bridge_node    = comm_protocol.joy_bridge_node:main',
            'goal_bridge_node   = comm_protocol.goal_bridge_node:main',
            'wifi_sender_node   = comm_protocol.wifi_sender_node:main',
            'wifi_receiver_node = comm_protocol.wifi_receiver_node:main',
            'eth_sender_node    = comm_protocol.eth_sender_node:main',
            'eth_receiver_node  = comm_protocol.eth_receiver_node:main',
        ],
    },
)