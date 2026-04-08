"""
joy_bridge_node.py
────────────────────────────────────────────────────────────────────
ROS2 node: joy_bridge_node

Base station node. Reads Joy messages from the local Xbox controller
and serializes them into JSON envelopes for transmission over LoRa.

Data flow (base station)
────────────────────────
  joy_node (/joy_raw) → controller_node (/joy) → joy_bridge_node → /lora_tx_json
                                                                         ↓
                                                                   lora_sender_node
                                                                         ↓
                                                                      LoRa TX

Data flow (robot side)
──────────────────────
  LoRa RX → lora_receiver_node → /joy_raw → controller_node → /joy → state_manager

JSON Envelope
─────────────
{
    "msg_type": "joy",
    "sender":   "base",
    "ts":       <unix timestamp float>,
    "payload":  {
        "axes":    [<float>, ...],
        "buttons": [<int>, ...]
    }
}

ROS2 Parameters
───────────────
  sender_id   (string)  – default 'base'

Subscribed topics
─────────────────
  /joy   (sensor_msgs/Joy)  – local controller output from controller_node

Published topics
────────────────
  /lora_tx_json   (std_msgs/String)  – JSON envelope to lora_sender_node

Run
───
  ros2 run comm_protocol joy_bridge_node
"""

import json
import time

import rclpy
from rclpy.node import Node
from sensor_msgs.msg import Joy
from std_msgs.msg import String


class JoyBridgeNode(Node):

    def __init__(self):
        super().__init__('joy_bridge_node')

        # ── Parameters ──────────────────────────────────────────────
        self.declare_parameter('sender_id', 'base')
        self._sender_id = self.get_parameter('sender_id').get_parameter_value().string_value

        # ── Publishers ───────────────────────────────────────────────
        self._pub_tx = self.create_publisher(String, '/lora_tx_json', 10)

        # ── Subscriptions ────────────────────────────────────────────
        self._sub_joy = self.create_subscription(
            Joy, '/joy', self._on_joy, 10)

        self.get_logger().info(
            f'joy_bridge_node ready | sender={self._sender_id} '
            f'| subscribing to /joy → /lora_tx_json')

    # ── Callbacks ────────────────────────────────────────────────────

    def _on_joy(self, msg: Joy):
        """Serialize Joy message into a JSON envelope and publish to LoRa TX."""
        envelope = {
            'msg_type': 'joy',
            'sender':   self._sender_id,
            'ts':       time.time(),
            'payload':  {
                'axes':    list(msg.axes),
                'buttons': list(msg.buttons),
            },
        }

        tx_msg = String()
        tx_msg.data = json.dumps(envelope, separators=(',', ':'))
        self._pub_tx.publish(tx_msg)
        self.get_logger().debug(
            f'JOY TX axes={list(msg.axes)} buttons={list(msg.buttons)}')


# ── Entry point ──────────────────────────────────────────────────────

def main(args=None):
    rclpy.init(args=args)
    node = JoyBridgeNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()