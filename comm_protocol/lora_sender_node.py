"""
lora_sender_node.py
────────────────────────────────────────────────────────────────────
ROS2 node: lora_sender

Reads JSON envelope messages from /lora_tx_json (std_msgs/String)
and writes them as newline-terminated strings to the USB LoRa dongle.

JSON Envelope Format
────────────────────
{
    "msg_type": "heartbeat" | "cmd_vel" | "robot_state" | "body_pose",
    "sender":   "base" | "robot",
    "ts":       <unix timestamp float>,
    "payload":  { ... }
}

The node validates that incoming strings are well-formed JSON before
transmitting. Malformed messages are logged and dropped.

ROS2 Parameters
───────────────
  serial_port   (string)  – default '/dev/ttyUSB0'
  baud_rate     (int)     – default 115200
  sender_id     (string)  – default 'base'  (used for log context only)

Subscribed topics
─────────────────
  /lora_tx_json   (std_msgs/String)  – JSON envelope to transmit

Run
───
  ros2 run comm_protocol lora_sender
  ros2 run comm_protocol lora_sender --ros-args -p serial_port:=/dev/ttyUSB1
"""

import json

import rclpy
from rclpy.node import Node
from std_msgs.msg import String

import serial
import serial.tools.list_ports


class LoraSenderNode(Node):

    def __init__(self):
        super().__init__('lora_sender')

        # ── Parameters ──────────────────────────────────────────────
        self.declare_parameter('serial_port', '/dev/ttyUSB0')
        self.declare_parameter('baud_rate',   115200)
        self.declare_parameter('sender_id',   'base')

        port      = self.get_parameter('serial_port').get_parameter_value().string_value
        baud      = self.get_parameter('baud_rate').get_parameter_value().integer_value
        self._sid = self.get_parameter('sender_id').get_parameter_value().string_value

        # ── Serial port ─────────────────────────────────────────────
        self._ser = None
        try:
            self._ser = serial.Serial(port, baud, timeout=1.0)
            self.get_logger().info(f'Opened serial port {port} @ {baud} baud')
        except serial.SerialException as e:
            self.get_logger().error(f'Failed to open {port}: {e}')
            self.get_logger().error('Available ports: ' + self._list_ports())

        # ── ROS subscription ─────────────────────────────────────────
        self._sub = self.create_subscription(
            String, '/lora_tx_json', self._on_tx_json, 10)

        self.get_logger().info(
            f'lora_sender ({self._sid}) ready — subscribing to /lora_tx_json')

    # ── Callbacks ────────────────────────────────────────────────────

    def _on_tx_json(self, msg: String):
        """Validate JSON envelope, then write to serial."""
        raw = msg.data.strip()

        # Validate JSON before transmitting
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            self.get_logger().warn(f'Dropping non-JSON message: {e}')
            return

        msg_type = parsed.get('msg_type', 'unknown')

        if self._ser is None or not self._ser.is_open:
            self.get_logger().warn(
                f'Serial port not open — dropping [{msg_type}]')
            return

        payload = (raw + '\n').encode('utf-8')
        try:
            self._ser.write(payload)
            self.get_logger().info(f'TX → LoRa [{msg_type}]: {raw}')
        except serial.SerialException as e:
            self.get_logger().error(f'Serial write error: {e}')

    # ── Helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _list_ports() -> str:
        ports = serial.tools.list_ports.comports()
        return ', '.join(p.device for p in ports) if ports else '(none found)'

    def destroy_node(self):
        if self._ser and self._ser.is_open:
            self._ser.close()
            self.get_logger().info('Serial port closed.')
        super().destroy_node()


# ── Entry point ──────────────────────────────────────────────────────

def main(args=None):
    rclpy.init(args=args)
    node = LoraSenderNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()