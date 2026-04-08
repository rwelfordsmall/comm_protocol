"""
goal_bridge_node.py
────────────────────────────────────────────────────────────────────
ROS2 node: goal_bridge_node

Base station node. Subscribes to /goal_pose (geometry_msgs/PoseStamped)
and serializes it into a JSON envelope for transmission over LoRa.

The goal pose can be set from RViz on the base station using the
"Nav2 Goal" tool, exactly as it would be on the robot directly.
On the robot side, lora_receiver_node reconstructs the PoseStamped
and publishes it to /goal_pose, where Nav2's bt_navigator picks it up.

Data flow (base station)
────────────────────────
  RViz "Nav2 Goal" → /goal_pose → goal_bridge_node → /lora_tx_json
                                                           ↓
                                                     lora_sender_node
                                                           ↓
                                                        LoRa TX

Data flow (robot side)
──────────────────────
  LoRa RX → lora_receiver_node → /goal_pose → Nav2 bt_navigator
                                                     ↓
                                               autonomous_bridge_node
                                                     ↓
                                               /gait_command → gait_node

Prerequisites (robot side)
──────────────────────────
  - Robot must be in AUTONOMOUS state (Y button on controller)
  - Nav2 stack must be running (autonomous:=true in dog_launch.py)
  - autonomous_bridge_node must be active

JSON Envelope
─────────────
{
    "msg_type":  "goal_pose",
    "sender":    "base",
    "ts":        <unix timestamp float>,
    "payload":   {
        "frame_id":    "map",
        "position":    { "x": <float>, "y": <float>, "z": <float> },
        "orientation": { "x": <float>, "y": <float>, "z": <float>, "w": <float> }
    }
}

ROS2 Parameters
───────────────
  sender_id   (string)  – default 'base'

Subscribed topics
─────────────────
  /goal_pose   (geometry_msgs/PoseStamped)  – goal from RViz Nav2 Goal tool

Published topics
────────────────
  /lora_tx_json   (std_msgs/String)  – JSON envelope to lora_sender_node

Run
───
  ros2 run comm_protocol goal_bridge_node
"""

import json
import time

import rclpy
from rclpy.node import Node
from geometry_msgs.msg import PoseStamped
from std_msgs.msg import String


class GoalBridgeNode(Node):

    def __init__(self):
        super().__init__('goal_bridge_node')

        # ── Parameters ──────────────────────────────────────────────
        self.declare_parameter('sender_id', 'base')
        self._sender_id = self.get_parameter('sender_id').get_parameter_value().string_value

        # ── Publishers ───────────────────────────────────────────────
        self._pub_tx = self.create_publisher(String, '/lora_tx_json', 10)

        # ── Subscriptions ────────────────────────────────────────────
        self._sub_goal = self.create_subscription(
            PoseStamped, '/goal_pose', self._on_goal_pose, 10)

        self.get_logger().info(
            f'goal_bridge_node ready | sender={self._sender_id} '
            f'| subscribing to /goal_pose → /lora_tx_json')

    # ── Callbacks ────────────────────────────────────────────────────

    def _on_goal_pose(self, msg: PoseStamped):
        """Serialize PoseStamped into a JSON envelope and publish to LoRa TX."""
        envelope = {
            'msg_type': 'goal_pose',
            'sender':   self._sender_id,
            'ts':       time.time(),
            'payload':  {
                'frame_id': msg.header.frame_id,
                'position': {
                    'x': msg.pose.position.x,
                    'y': msg.pose.position.y,
                    'z': msg.pose.position.z,
                },
                'orientation': {
                    'x': msg.pose.orientation.x,
                    'y': msg.pose.orientation.y,
                    'z': msg.pose.orientation.z,
                    'w': msg.pose.orientation.w,
                },
            },
        }

        tx_msg = String()
        tx_msg.data = json.dumps(envelope, separators=(',', ':'))
        self._pub_tx.publish(tx_msg)

        pos = msg.pose.position
        self.get_logger().info(
            f'GOAL TX → LoRa | frame={msg.header.frame_id} '
            f'pos=({pos.x:.2f}, {pos.y:.2f}, {pos.z:.2f})')


# ── Entry point ──────────────────────────────────────────────────────

def main(args=None):
    rclpy.init(args=args)
    node = GoalBridgeNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()