import json
import threading

import rclpy
from rclpy.node import Node
from std_msgs.msg import String
from geometry_msgs.msg import Twist, Vector3

import serial
import serial.tools.list_ports


# Whitelist:
# Maps msg_type → (topic, publisher_attr_name)
# Publishers are created dynamically in __init__ from this table.
_WHITELIST = {
    'heartbeat':   ('/lora_rx_hb',     String),
    'cmd_vel':     ('/gait_command',    Twist),
    'robot_state': ('/robot_state',     String),
    'body_pose':   ('/body_pose',       Vector3),
}


class LoraReceiverNode(Node):

    def __init__(self):
        super().__init__('lora_receiver')

        # Parameters, baselined to USB1 and 115200 baude rate (usb dongle configured to it, initially 9600)
        self.declare_parameter('serial_port', '/dev/ttyUSB1')
        self.declare_parameter('baud_rate',   115200)

        # If commandline args
        port = self.get_parameter('serial_port').get_parameter_value().string_value
        baud = self.get_parameter('baud_rate').get_parameter_value().integer_value

        # The Publisher
        # Allows for debugging/monitoring topics — all received lines come through
        self._pub_raw = self.create_publisher(String, '/lora_rx', 10)

        # Whitelist publishers keyed by msg_type
        self._publishers = {}
        for msg_type, (topic, ros_type) in _WHITELIST.items():
            self._publishers[msg_type] = self.create_publisher(ros_type, topic, 10)
            self.get_logger().info(f'Whitelisted [{msg_type}] → {topic}')

        # Serial port stuff
        self._ser = None
        self._read_thread = None
        self._stop_event = threading.Event()

        try:
            self._ser = serial.Serial(port, baud, timeout=1.0)
            self.get_logger().info(f'Opened serial port {port} @ {baud} baud')
            self._start_read_thread()
        except serial.SerialException as e:
            # Debugging info
            self.get_logger().error(f'Failed to open serial port {port}: {e}')
            self.get_logger().error('Available ports: ' + self._list_ports())

        self.get_logger().info('lora_receiver ready')


    # Read thread 
    def _start_read_thread(self):
        self._read_thread = threading.Thread(
            target=self._read_loop, daemon=True)
        self._read_thread.start()

    def _read_loop(self):
        """Blocking readline loop. Parses JSON and dispatches per whitelist."""
        while not self._stop_event.is_set():
            if self._ser is None or not self._ser.is_open:
                break
            try:
                raw = self._ser.readline()
                if not raw:
                    continue  # serial timeout, loop again

                text = raw.decode('utf-8', errors='replace').strip()
                if not text:
                    continue

                # Always publish raw line for monitoring
                raw_msg = String()
                raw_msg.data = text
                self._pub_raw.publish(raw_msg)
                self.get_logger().info(f'RX <- LoRa: {text}')

                # Attempt JSON parse
                try:
                    envelope = json.loads(text)
                except json.JSONDecodeError as e:
                    self.get_logger().warn(
                        f'Non-JSON line received (dropping): {e} | line={text!r}')
                    continue

                self._dispatch(envelope, text)

            except serial.SerialException as e:
                self.get_logger().error(f'Serial read error: {e}')
                break
            except Exception as e:
                self.get_logger().error(f'Unexpected error in read loop: {e}')
                break

        self.get_logger().info('Serial read thread exiting.')

    # ── Dispatch ─────────────────────────────────────────────────────

    def _dispatch(self, envelope: dict, raw_text: str):
        """Route a parsed JSON envelope to the appropriate ROS2 topic."""
        msg_type = envelope.get('msg_type')

        if msg_type not in _WHITELIST:
            self.get_logger().warn(
                f'Unknown msg_type "{msg_type}" — dropping. '
                f'Whitelisted types: {list(_WHITELIST.keys())}')
            return

        publisher = self._publishers[msg_type]
        payload   = envelope.get('payload', {})

        try:
            ros_msg = self._build_ros_msg(msg_type, payload, raw_text)
        except (KeyError, TypeError, ValueError) as e:
            self.get_logger().error(
                f'Failed to build ROS msg for [{msg_type}]: {e} | payload={payload}')
            return

        publisher.publish(ros_msg)
        self.get_logger().info(
            f'Dispatched [{msg_type}] → {_WHITELIST[msg_type][0]}')

    # Builds 
    def _build_ros_msg(self, msg_type: str, payload: dict, raw_text: str):
        """Construct the appropriate ROS2 message from the JSON payload."""

        if msg_type == 'heartbeat':
            # Forward raw JSON string to heartbeat_node for timestamp inspection
            msg = String()
            msg.data = raw_text
            return msg

        elif msg_type == 'cmd_vel':
            msg = Twist()
            lin = payload.get('linear', {})
            ang = payload.get('angular', {})
            msg.linear.x  = float(lin.get('x', 0.0))
            msg.linear.y  = float(lin.get('y', 0.0))
            msg.linear.z  = float(lin.get('z', 0.0))
            msg.angular.x = float(ang.get('x', 0.0))
            msg.angular.y = float(ang.get('y', 0.0))
            msg.angular.z = float(ang.get('z', 0.0))
            return msg

        elif msg_type == 'robot_state':
            msg = String()
            msg.data = str(payload.get('state', ''))
            return msg

        elif msg_type == 'body_pose':
            msg = Vector3()
            msg.x = float(payload.get('roll',  0.0))
            msg.y = float(payload.get('pitch', 0.0))
            msg.z = float(payload.get('yaw',   0.0))
            return msg

        raise ValueError(f'Message type undefined, no builder: "{msg_type}"')

    # Helpers

    @staticmethod
    # Specifically to find port in case can't connect, just to help user debug
    def _list_ports() -> str:
        ports = serial.tools.list_ports.comports()
        return ', '.join(p.device for p in ports) if ports else '(none found)'

    # Ensures a clean shutdown
    def destroy_node(self):
        self._stop_event.set()
        if self._ser and self._ser.is_open:
            self._ser.close()
            self.get_logger().info('Serial port closed.')
        if self._read_thread:
            self._read_thread.join(timeout=2.0)
        super().destroy_node()


# Entry point
def main(args=None):
    rclpy.init(args=args)
    node = LoraReceiverNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()