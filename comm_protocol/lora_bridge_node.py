# comm_protocol/lora_bridge_node.py
"""
lora_bridge_node.py
────────────────────────────────────────────────────────────────────
Combined TX + RX node for a single USB LoRa dongle per machine.

Use this instead of running lora_sender_node + lora_receiver_node
separately — they cannot share the same serial port.

Each machine runs one instance of this node pointed at its dongle.
It simultaneously:
  - Writes outbound JSON envelopes from /lora_tx_json to serial
  - Reads inbound JSON envelopes from serial and dispatches to topics

Parameters
──────────
  serial_port   (string)  – default '/dev/ttyUSB0'
  baud_rate     (int)     – default 115200
  sender_id     (string)  – 'base' or 'robot' (default: 'base')
"""

import json
import threading

import rclpy
from rclpy.node import Node
from std_msgs.msg import String, Bool
from geometry_msgs.msg import Twist, Vector3, PoseStamped
from sensor_msgs.msg import Joy

import serial
import serial.tools.list_ports

_WHITELIST = {
    'heartbeat':   ('/lora_rx_hb',   String),
    'cmd_vel':     ('/gait_command',  Twist),
    'robot_state': ('/estop',         Bool),
    'body_pose':   ('/body_pose',     Vector3),
    'joy':         ('/joy_raw',       Joy),
    'goal_pose':   ('/goal_pose',     PoseStamped),
}


class LoraBridgeNode(Node):

    def __init__(self):
        super().__init__('lora_bridge')

        self.declare_parameter('serial_port', '/dev/ttyUSB0')
        self.declare_parameter('baud_rate',   115200)
        self.declare_parameter('sender_id',   'base')

        port          = self.get_parameter('serial_port').get_parameter_value().string_value
        baud          = self.get_parameter('baud_rate').get_parameter_value().integer_value
        self._sid     = self.get_parameter('sender_id').get_parameter_value().string_value
        self._ser_lock = threading.Lock()

        # ── Serial ───────────────────────────────────────────────────
        self._ser        = None
        self._stop_event = threading.Event()
        try:
            self._ser = serial.Serial(port, baud, timeout=1.0)
            self.get_logger().info(f'Opened {port} @ {baud} baud')
        except serial.SerialException as e:
            self.get_logger().error(f'Failed to open {port}: {e}')
            ports = ', '.join(p.device for p in serial.tools.list_ports.comports()) or '(none)'
            self.get_logger().error(f'Available ports: {ports}')

        # ── RX publishers ─────────────────────────────────────────────
        self._pub_raw  = self.create_publisher(String, '/lora_rx', 10)
        self._publishers = {}
        for msg_type, (topic, ros_type) in _WHITELIST.items():
            self._publishers[msg_type] = self.create_publisher(ros_type, topic, 10)

        # ── TX subscription ──────────────────────────────────────────
        self._sub = self.create_subscription(
            String, '/lora_tx_json', self._on_tx_json, 10)

        # ── Read thread ───────────────────────────────────────────────
        if self._ser:
            self._read_thread = threading.Thread(target=self._read_loop, daemon=True)
            self._read_thread.start()

        self.get_logger().info(f'lora_bridge ({self._sid}) ready')

    # ── TX ────────────────────────────────────────────────────────────

    def _on_tx_json(self, msg: String):
        raw = msg.data.strip()
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            self.get_logger().warn(f'Dropping non-JSON: {e}')
            return
        if self._ser is None or not self._ser.is_open:
            self.get_logger().warn('Serial not open — dropping TX')
            return
        payload = (raw + '\n').encode('utf-8')
        with self._ser_lock:
            try:
                self._ser.write(payload)
                self.get_logger().info(f"TX [{parsed.get('msg_type','?')}]: {raw}")
            except serial.SerialException as e:
                self.get_logger().error(f'Serial write error: {e}')

    # ── RX ────────────────────────────────────────────────────────────

    def _read_loop(self):
        while not self._stop_event.is_set():
            try:
                with self._ser_lock:
                    line = self._ser.readline()
            except serial.SerialException as e:
                self.get_logger().error(f'Serial read error: {e}')
                break

            if not line:
                continue

            raw_text = line.decode('utf-8', errors='replace').strip()
            if not raw_text:
                continue

            # Publish raw for monitoring
            raw_msg = String()
            raw_msg.data = raw_text
            self._pub_raw.publish(raw_msg)

            try:
                envelope = json.loads(raw_text)
            except json.JSONDecodeError:
                self.get_logger().warn(f'Non-JSON RX: {raw_text[:80]}')
                continue

            msg_type = envelope.get('msg_type', '')
            payload  = envelope.get('payload', {})

            if msg_type not in self._publishers:
                self.get_logger().warn(f'Unknown msg_type: {msg_type}')
                continue

            ros_msg = self._build_message(msg_type, raw_text, payload)
            if ros_msg is not None:
                self._publishers[msg_type].publish(ros_msg)
                self.get_logger().info(f'RX dispatched [{msg_type}]')

    def _build_message(self, msg_type, raw_text, payload):
        try:
            if msg_type == 'heartbeat':
                m = String(); m.data = raw_text; return m
            elif msg_type == 'cmd_vel':
                m = Twist()
                lin = payload.get('linear', {}); ang = payload.get('angular', {})
                m.linear.x = float(lin.get('x', 0)); m.linear.y = float(lin.get('y', 0))
                m.angular.z = float(ang.get('z', 0)); return m
            elif msg_type == 'robot_state':
                m = Bool(); m.data = payload.get('state', '') == 'ESTOP'; return m
            elif msg_type == 'body_pose':
                m = Vector3()
                m.x = float(payload.get('roll', 0)); m.y = float(payload.get('pitch', 0))
                m.z = float(payload.get('yaw', 0)); return m
            elif msg_type == 'joy':
                m = Joy()
                m.header.stamp = self.get_clock().now().to_msg()
                m.axes    = [float(a) for a in payload.get('axes', [])]
                m.buttons = [int(b)   for b in payload.get('buttons', [])]
                return m
            elif msg_type == 'goal_pose':
                m = PoseStamped()
                m.header.stamp = self.get_clock().now().to_msg()
                m.header.frame_id = payload.get('frame_id', 'map')
                pos = payload.get('position', {}); ori = payload.get('orientation', {})
                m.pose.position.x = float(pos.get('x', 0)); m.pose.position.y = float(pos.get('y', 0))
                m.pose.position.z = float(pos.get('z', 0))
                m.pose.orientation.x = float(ori.get('x', 0)); m.pose.orientation.y = float(ori.get('y', 0))
                m.pose.orientation.z = float(ori.get('z', 0)); m.pose.orientation.w = float(ori.get('w', 1))
                return m
        except Exception as e:
            self.get_logger().error(f'Build error [{msg_type}]: {e}')
        return None

    def destroy_node(self):
        self._stop_event.set()
        if self._ser and self._ser.is_open:
            self._ser.close()
        super().destroy_node()


def main(args=None):
    rclpy.init(args=None)
    node = LoraBridgeNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()

if __name__ == '__main__':
    main()