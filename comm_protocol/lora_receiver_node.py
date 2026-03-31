"""
lora_receiver_node.py
────────────────────────────────────────────────────────────────────
ROS2 node: lora_receiver

Reads newline-terminated strings from the USB LoRa dongle over serial
and publishes each one to /lora_rx (std_msgs/String).

The node uses a background thread for blocking serial reads so the
ROS2 executor is never blocked.

ROS2 Parameters
───────────────
  serial_port  (string)  – default '/dev/ttyUSB0'
  baud_rate    (int)     – default 9600   (must match dongle config)

Run
───
  ros2 run lora_bridge lora_receiver
  ros2 run lora_bridge lora_receiver --ros-args -p serial_port:=/dev/ttyUSB1

Monitor
───────
  ros2 topic echo /lora_rx
"""

import threading

import rclpy
from rclpy.node import Node
from std_msgs.msg import String

import serial
import serial.tools.list_ports


class LoraReceiverNode(Node):

    def __init__(self):
        super().__init__('lora_receiver')

        # ── Parameters ──────────────────────────────────────────────
        self.declare_parameter('serial_port', '/dev/ttyUSB0')
        self.declare_parameter('baud_rate',   115200)

        port = self.get_parameter('serial_port').get_parameter_value().string_value
        baud = self.get_parameter('baud_rate').get_parameter_value().integer_value

        # ── ROS publisher ────────────────────────────────────────────
        self._pub = self.create_publisher(String, '/lora_rx', 10)

        # ── Serial port ─────────────────────────────────────────────
        self._ser = None
        self._read_thread = None
        self._stop_event = threading.Event()

        try:
            self._ser = serial.Serial(port, baud, timeout=1.0)
            self.get_logger().info(f'Opened serial port {port} @ {baud} baud')
            self._start_read_thread()
        except serial.SerialException as e:
            self.get_logger().error(f'Failed to open serial port {port}: {e}')
            self.get_logger().error('Available ports: ' + self._list_ports())

        self.get_logger().info('lora_receiver ready — publishing to /lora_rx')

    # ── Serial read thread ────────────────────────────────────────────

    def _start_read_thread(self):
        self._read_thread = threading.Thread(
            target=self._read_loop, daemon=True)
        self._read_thread.start()

    def _read_loop(self):
        """
        Blocking readline loop running in a background thread.
        Publishes each complete line to /lora_rx.
        """
        while not self._stop_event.is_set():
            if self._ser is None or not self._ser.is_open:
                break
            try:
                raw = self._ser.readline()   # blocks up to timeout=1.0 s
                if not raw:
                    continue                 # timeout — loop again

                text = raw.decode('utf-8', errors='replace').strip()
                if not text:
                    continue

                msg = String()
                msg.data = text
                self._pub.publish(msg)
                self.get_logger().info(f'RX ← LoRa: {text}')

            except serial.SerialException as e:
                self.get_logger().error(f'Serial read error: {e}')
                break
            except Exception as e:
                self.get_logger().error(f'Unexpected error in read loop: {e}')
                break

        self.get_logger().info('Serial read thread exiting.')

    # ── Helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _list_ports() -> str:
        ports = serial.tools.list_ports.comports()
        return ', '.join(p.device for p in ports) if ports else '(none found)'

    def destroy_node(self):
        self._stop_event.set()
        if self._ser and self._ser.is_open:
            self._ser.close()
            self.get_logger().info('Serial port closed.')
        if self._read_thread:
            self._read_thread.join(timeout=2.0)
        super().destroy_node()


# ── Entry point ──────────────────────────────────────────────────────

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