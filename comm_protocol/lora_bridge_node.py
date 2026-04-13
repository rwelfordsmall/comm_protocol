# comm_protocol/lora_bridge_node.py
"""
Combined TX + RX node for a single USB LoRa dongle per machine.

Changes from original:
- Uses buffered byte reads instead of readline() so RX works even if the radio
  delivers partial chunks or awkward line breaks.
- Publishes actual received test_ping/test_ack JSON to /lora_rx for monitoring.
- Sends test_ack only once, back over serial, when a test_ping is received.
- Adds better debug logging for raw serial input and oversized/invalid buffers.
- Avoids holding the serial lock during long blocking reads.
"""

import json
import threading
import time

import rclpy
from rclpy.node import Node
from std_msgs.msg import String, Bool
from geometry_msgs.msg import Twist, Vector3, PoseStamped
from sensor_msgs.msg import Joy

import serial
import serial.tools.list_ports


_WHITELIST = {
    'heartbeat': ('/lora_rx_hb', String),
    'cmd_vel': ('/gait_command', Twist),
    'robot_state': ('/estop', Bool),
    'body_pose': ('/body_pose', Vector3),
    'joy': ('/joy_raw', Joy),
    'goal_pose': ('/goal_pose', PoseStamped),
    'test_ping': ('/lora_rx', String),
    'test_ack': ('/lora_rx', String),
}

_TX_WHITELIST = {
    'heartbeat',
    'cmd_vel',
    'robot_state',
    'body_pose',
    'joy',
    'goal_pose',
    'test_ping',
    'test_ack',
}


class LoraBridgeNode(Node):
    def __init__(self):
        super().__init__('lora_bridge')

        self.declare_parameter('serial_port', '/dev/ttyUSB0')
        self.declare_parameter('baud_rate', 115200)
        self.declare_parameter('sender_id', 'base')
        self.declare_parameter('serial_timeout', 0.1)
        self.declare_parameter('max_line_bytes', 4096)
        self.declare_parameter('debug_raw_serial', False)

        port = self.get_parameter('serial_port').get_parameter_value().string_value
        baud = self.get_parameter('baud_rate').get_parameter_value().integer_value
        self._sid = self.get_parameter('sender_id').get_parameter_value().string_value
        serial_timeout = (
            self.get_parameter('serial_timeout').get_parameter_value().double_value
        )
        self._max_line_bytes = (
            self.get_parameter('max_line_bytes').get_parameter_value().integer_value
        )
        self._debug_raw_serial = (
            self.get_parameter('debug_raw_serial').get_parameter_value().bool_value
        )

        self._ser_lock = threading.Lock()
        self._ser = None
        self._stop_event = threading.Event()
        self._rx_buffer = bytearray()

        try:
            self._ser = serial.Serial(port, baud, timeout=serial_timeout)
            self.get_logger().info(f'Opened {port} @ {baud} baud')
        except serial.SerialException as e:
            self.get_logger().error(f'Failed to open {port}: {e}')
            ports = ', '.join(p.device for p in serial.tools.list_ports.comports()) or '(none)'
            self.get_logger().error(f'Available ports: {ports}')

        # Raw monitor publisher
        self._pub_raw = self.create_publisher(String, '/lora_rx', 10)

        # Typed dispatch publishers
        self._dispatch_pubs = {}
        for msg_type, (topic, ros_type) in _WHITELIST.items():
            if topic == '/lora_rx':
                # Reuse raw publisher for test_ping / test_ack monitor traffic
                self._dispatch_pubs[msg_type] = self._pub_raw
            else:
                self._dispatch_pubs[msg_type] = self.create_publisher(ros_type, topic, 10)

        self._sub = self.create_subscription(
            String,
            '/lora_tx_json',
            self._on_tx_json,
            10
        )

        if self._ser:
            self._read_thread = threading.Thread(
                target=self._read_loop,
                daemon=True,
            )
            self._read_thread.start()

        self.get_logger().info(f'lora_bridge ({self._sid}) ready')

    # ------------------------------------------------------------------
    # TX
    # ------------------------------------------------------------------
    def _on_tx_json(self, msg: String):
        raw = msg.data.strip()
        if not raw:
            self.get_logger().warn('Dropping empty TX message')
            return

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            self.get_logger().warn(f'Dropping non-JSON TX: {e}')
            return

        msg_type = parsed.get('msg_type', 'unknown')
        if msg_type not in _TX_WHITELIST:
            self.get_logger().warn(f'Dropping unknown TX msg_type "{msg_type}"')
            return

        if self._ser is None or not self._ser.is_open:
            self.get_logger().warn(f'Serial not open — dropping TX [{msg_type}]')
            return

        payload = (raw + '\n').encode('utf-8')

        with self._ser_lock:
            try:
                self._ser.write(payload)
                self._ser.flush()
            except serial.SerialException as e:
                self.get_logger().error(f'Serial write error: {e}')
                return

        self.get_logger().info(f'TX [{msg_type}]: {raw}')

    # ------------------------------------------------------------------
    # RX
    # ------------------------------------------------------------------
    def _read_loop(self):
        while not self._stop_event.is_set():
            if self._ser is None or not self._ser.is_open:
                time.sleep(0.1)
                continue

            try:
                # Only hold lock while touching serial object
                with self._ser_lock:
                    waiting = self._ser.in_waiting
                    chunk = self._ser.read(waiting or 1)
            except serial.SerialException as e:
                self.get_logger().error(f'Serial read error: {e}')
                break

            if not chunk:
                continue

            if self._debug_raw_serial:
                self.get_logger().info(f'RAW RX CHUNK: {chunk!r}')

            self._rx_buffer.extend(chunk)

            # Prevent runaway buffer growth if framing is broken
            if len(self._rx_buffer) > self._max_line_bytes * 4:
                preview = bytes(self._rx_buffer[:160]).decode('utf-8', errors='replace')
                self.get_logger().warn(
                    f'RX buffer oversized ({len(self._rx_buffer)} bytes); '
                    f'clearing buffer. Preview: {preview!r}'
                )
                self._rx_buffer.clear()
                continue

            self._drain_rx_buffer()

    def _drain_rx_buffer(self):
        while True:
            nl_idx = self._rx_buffer.find(b'\n')
            if nl_idx < 0:
                return

            frame = bytes(self._rx_buffer[:nl_idx])
            del self._rx_buffer[:nl_idx + 1]

            raw_text = frame.decode('utf-8', errors='replace').strip()
            if not raw_text:
                continue

            if len(raw_text.encode('utf-8', errors='replace')) > self._max_line_bytes:
                self.get_logger().warn(
                    f'Dropping oversized RX frame ({len(raw_text)} chars)'
                )
                continue

            self._handle_rx_line(raw_text)

    def _handle_rx_line(self, raw_text: str):
        # Always publish raw line for monitoring
        raw_msg = String()
        raw_msg.data = raw_text
        self._pub_raw.publish(raw_msg)

        try:
            envelope = json.loads(raw_text)
        except json.JSONDecodeError as e:
            self.get_logger().warn(f'Non-JSON RX: {e}: {raw_text[:120]}')
            return

        if envelope.get('sender') == self._sid:
            self.get_logger().debug(
                f'Dropping self-echo [{envelope.get("msg_type", "unknown")}]'
            )
            return

        msg_type = envelope.get('msg_type', '')
        payload = envelope.get('payload', {})

        if msg_type not in _WHITELIST:
            self.get_logger().warn(f'Unknown RX msg_type "{msg_type}" — dropping')
            return

        ros_msg = self._build_message(msg_type, raw_text, payload)
        if ros_msg is not None:
            self._dispatch_pubs[msg_type].publish(ros_msg)
            self.get_logger().info(f'RX dispatched [{msg_type}]')

        if msg_type == 'test_ping':
            self._send_test_ack(payload)

    # ------------------------------------------------------------------
    # Message builders
    # ------------------------------------------------------------------
    def _build_message(self, msg_type, raw_text, payload):
        try:
            if msg_type == 'heartbeat':
                m = String()
                m.data = raw_text
                return m

            if msg_type == 'cmd_vel':
                m = Twist()
                lin = payload.get('linear', {})
                ang = payload.get('angular', {})
                m.linear.x = float(lin.get('x', 0.0))
                m.linear.y = float(lin.get('y', 0.0))
                m.linear.z = float(lin.get('z', 0.0))
                m.angular.x = float(ang.get('x', 0.0))
                m.angular.y = float(ang.get('y', 0.0))
                m.angular.z = float(ang.get('z', 0.0))
                return m

            if msg_type == 'robot_state':
                m = Bool()
                m.data = payload.get('state', '') == 'ESTOP'
                return m

            if msg_type == 'body_pose':
                m = Vector3()
                m.x = float(payload.get('roll', 0.0))
                m.y = float(payload.get('pitch', 0.0))
                m.z = float(payload.get('yaw', 0.0))
                return m

            if msg_type == 'joy':
                m = Joy()
                m.header.stamp = self.get_clock().now().to_msg()
                m.axes = [float(a) for a in payload.get('axes', [])]
                m.buttons = [int(b) for b in payload.get('buttons', [])]
                return m

            if msg_type == 'goal_pose':
                m = PoseStamped()
                m.header.stamp = self.get_clock().now().to_msg()
                m.header.frame_id = payload.get('frame_id', 'map')

                pos = payload.get('position', {})
                ori = payload.get('orientation', {})

                m.pose.position.x = float(pos.get('x', 0.0))
                m.pose.position.y = float(pos.get('y', 0.0))
                m.pose.position.z = float(pos.get('z', 0.0))

                m.pose.orientation.x = float(ori.get('x', 0.0))
                m.pose.orientation.y = float(ori.get('y', 0.0))
                m.pose.orientation.z = float(ori.get('z', 0.0))
                m.pose.orientation.w = float(ori.get('w', 1.0))
                return m

            if msg_type in ('test_ping', 'test_ack'):
                # Publish the actual raw JSON received for monitoring
                m = String()
                m.data = raw_text
                return m

        except Exception as e:
            self.get_logger().error(f'Build error [{msg_type}]: {e}')
            return None

        return None

    # ------------------------------------------------------------------
    # test_ack
    # ------------------------------------------------------------------
    def _send_test_ack(self, original_payload: dict):
        ack = {
            'msg_type': 'test_ack',
            'sender': self._sid,
            'ts': time.time(),
            'payload': {
                'transport': 'lora',
                'status': 'OK',
                'echo': original_payload,
            }
        }

        raw = json.dumps(ack)

        if self._ser is None or not self._ser.is_open:
            self.get_logger().warn('Serial not open — cannot send test_ack')
            return

        with self._ser_lock:
            try:
                self._ser.write((raw + '\n').encode('utf-8'))
                self._ser.flush()
            except serial.SerialException as e:
                self.get_logger().error(f'Serial write error (test_ack): {e}')
                return

        self.get_logger().info(f'test_ping received -> test_ack sent: {raw}')

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------
    def destroy_node(self):
        self._stop_event.set()

        try:
            if hasattr(self, '_read_thread') and self._read_thread.is_alive():
                self._read_thread.join(timeout=1.0)
        except Exception:
            pass

        if self._ser and self._ser.is_open:
            try:
                self._ser.close()
            except Exception:
                pass

        super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
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