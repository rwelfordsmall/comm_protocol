"""
lora_sender_node.py
────────────────────────────────────────────────────────────────────
ROS2 node: lora_sender

Current behaviour (Phase 1 – proof of concept):
  • Publishes a counter string to /lora_tx on a configurable timer.
  • Reads from /lora_tx and writes each message out to the LoRa dongle
    over the USB serial port.

Future (Phase 2 – topic bridge):
  • Swap the timer publisher for a subscription to an external topic
    (e.g. /cmd_vel serialised to JSON), keeping the serial-write logic
    identical.

ROS2 Parameters
───────────────
  serial_port   (string)  – default '/dev/ttyUSB0'
  baud_rate     (int)     – default 9600   (must match dongle config)
  timer_period  (float)   – default 1.0 s  (how often to send)

Run
───
  ros2 run lora_bridge lora_sender
  ros2 run lora_bridge lora_sender --ros-args -p serial_port:=/dev/ttyUSB1 -p timer_period:=0.5
"""

import rclpy
from rclpy.node import Node
from std_msgs.msg import String

import serial
import serial.tools.list_ports


class LoraSenderNode(Node):

    def __init__(self):
        super().__init__('lora_sender')

        # ── Parameters ──────────────────────────────────────────────
        self.declare_parameter('serial_port',  '/dev/ttyUSB0')
        self.declare_parameter('baud_rate',    115200)
        self.declare_parameter('timer_period', 1.0)

        port   = self.get_parameter('serial_port').get_parameter_value().string_value
        baud   = self.get_parameter('baud_rate').get_parameter_value().integer_value
        period = self.get_parameter('timer_period').get_parameter_value().double_value

        # ── Serial port ─────────────────────────────────────────────
        self._ser = None
        try:
            self._ser = serial.Serial(port, baud, timeout=1.0)
            self.get_logger().info(f'Opened serial port {port} @ {baud} baud')
        except serial.SerialException as e:
            self.get_logger().error(f'Failed to open serial port {port}: {e}')
            self.get_logger().error('Available ports: ' + self._list_ports())
            # Node will keep running so you can introspect topics;
            # messages will simply be dropped until the port is fixed.

        # ── ROS interfaces ──────────────────────────────────────────
        # Internal publisher – timer fires and publishes here
        self._pub = self.create_publisher(String, '/lora_tx', 10)

        # Subscriber watches /lora_tx and forwards to serial
        self._sub = self.create_subscription(
            String, '/lora_tx', self._on_lora_tx, 10)

        # Timer drives the proof-of-concept transmissions
        self._counter = 0
        self._timer = self.create_timer(period, self._timer_cb)

        self.get_logger().info(
            f'lora_sender ready — publishing to /lora_tx every {period}s')

    # ── Callbacks ────────────────────────────────────────────────────

    def _timer_cb(self):
        """Phase 1: publish a simple counter string."""
        msg = String()
        msg.data = f'Hello from sender | count={self._counter}'
        self._counter += 1
        self._pub.publish(msg)
        self.get_logger().debug(f'Published: {msg.data}')

    def _on_lora_tx(self, msg: String):
        """Write whatever arrives on /lora_tx out to the LoRa dongle."""
        payload = (msg.data + '\n').encode('utf-8')

        if self._ser is None or not self._ser.is_open:
            self.get_logger().warn(
                'Serial port not open — dropping message: ' + msg.data)
            return

        try:
            self._ser.write(payload)
            self.get_logger().info(f'TX → LoRa: {msg.data}')
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