"""
eth_sender_node.py
────────────────────────────────────────────────────────────────────
ROS2 node: eth_sender

Base-station side. Reads JSON envelope messages from /eth_tx_json
(std_msgs/String) and forwards them over a persistent TCP connection
to the matching eth_receiver_node running on the Jetson.

Structurally identical to wifi_sender_node — only the default port
and topic names differ so both transports can run simultaneously.

JSON Envelope Format (same as LoRa / WiFi stack)
─────────────────────────────────────────────────
{
    "msg_type": "heartbeat" | "cmd_vel" | "robot_state" | "body_pose"
              | "joy" | "goal_pose" | "test_ping",
    "sender":   "base" | "robot",
    "ts":       <unix timestamp float>,
    "payload":  { ... }
}

test_ping
─────────
  Send a "test_ping" envelope at any time to verify the Ethernet link
  end to end. eth_receiver_node handles it like any other whitelisted
  type — builds a "test_ack" and sends it back over TCP so it arrives
  here on /eth_rx. No special mode required.

ROS2 Parameters
───────────────
  robot_host      (string)  – Jetson hostname/IP   (default: 'quaddog.local')
  robot_port      (int)     – TCP port              (default: 5801)
  sender_id       (string)  – default 'base'
  reconnect_sec   (float)   – reconnect interval    (default: 5.0)

Subscribed topics
─────────────────
  /eth_tx_json      (std_msgs/String)  – JSON envelopes to transmit
  /active_transport (std_msgs/String)  – 'lora'|'wifi'|'eth' selector

Published topics
────────────────
  /eth_rx   (std_msgs/String)  – inbound data from Jetson (acks, robot state)

Run
───
  ros2 run comm_protocol eth_sender_node
  ros2 run comm_protocol eth_sender_node --ros-args \\
      -p robot_host:=192.168.1.50 -p robot_port:=5801
"""

import json
import socket
import threading

import rclpy
from rclpy.node import Node
from std_msgs.msg import String

# ── Whitelist ─────────────────────────────────────────────────────────────────
_WHITELIST = {
    'heartbeat', 'cmd_vel', 'robot_state',
    'body_pose', 'joy', 'goal_pose',
    'test_ping',
}


class EthSenderNode(Node):

    def __init__(self):
        super().__init__('eth_sender')

        # ── Parameters ───────────────────────────────────────────────
        self.declare_parameter('robot_host',    'quaddog.local')
        self.declare_parameter('robot_port',    5801)
        self.declare_parameter('sender_id',     'base')
        self.declare_parameter('reconnect_sec', 5.0)

        self._host      = self.get_parameter('robot_host').get_parameter_value().string_value
        self._port      = self.get_parameter('robot_port').get_parameter_value().integer_value
        self._sid       = self.get_parameter('sender_id').get_parameter_value().string_value
        self._retry_sec = self.get_parameter('reconnect_sec').get_parameter_value().double_value

        # ── State ────────────────────────────────────────────────────
        self._sock       = None
        self._sock_lock  = threading.Lock()
        self._active     = True
        self._stop_event = threading.Event()

        # ── Publishers ───────────────────────────────────────────────
        self._pub_rx = self.create_publisher(String, '/eth_rx', 10)

        # ── Subscriptions ────────────────────────────────────────────
        self._sub_tx = self.create_subscription(
            String, '/eth_tx_json', self._on_tx_json, 10)
        self._sub_transport = self.create_subscription(
            String, '/active_transport', self._on_transport, 10)

        # ── Connection thread ────────────────────────────────────────
        self._conn_thread = threading.Thread(
            target=self._connection_loop, daemon=True)
        self._conn_thread.start()

        self.get_logger().info(
            f'eth_sender ({self._sid}) ready → {self._host}:{self._port}')

    # ── Transport selector ───────────────────────────────────────────

    def _on_transport(self, msg: String):
        self._active = (msg.data.strip().lower() == 'eth')
        self.get_logger().info(
            f'Transport selector → {msg.data} | eth_sender '
            f'{"ACTIVE" if self._active else "STANDBY"}')

    # ── TX callback ──────────────────────────────────────────────────

    def _on_tx_json(self, msg: String):
        if not self._active:
            return

        raw = msg.data.strip()

        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError as e:
            self.get_logger().warn(f'Dropping non-JSON message: {e}')
            return

        msg_type = envelope.get('msg_type', 'unknown')

        if msg_type not in _WHITELIST:
            self.get_logger().warn(f'Dropping unknown msg_type "{msg_type}"')
            return

        with self._sock_lock:
            if self._sock is None:
                self.get_logger().warn(
                    f'Ethernet not connected — dropping [{msg_type}]')
                return
            try:
                self._sock.sendall((raw + '\n').encode('utf-8'))
                self.get_logger().info(f'TX → Ethernet [{msg_type}]: {raw}')
            except OSError as e:
                self.get_logger().error(f'Ethernet send error: {e}')
                self._close_socket()

    # ── Connection management ────────────────────────────────────────

    def _connection_loop(self):
        while not self._stop_event.is_set():
            self.get_logger().info(
                f'Connecting to Jetson (Ethernet) at {self._host}:{self._port} …')
            try:
                s = socket.create_connection(
                    (self._host, self._port), timeout=5.0)
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                with self._sock_lock:
                    self._sock = s
                self.get_logger().info(
                    f'Ethernet connected to {self._host}:{self._port}')
                self._recv_loop(s)
            except OSError as e:
                self.get_logger().warn(
                    f'Ethernet connection failed: {e} — retry in {self._retry_sec}s')
            finally:
                self._close_socket()
            self._stop_event.wait(self._retry_sec)

    def _recv_loop(self, sock: socket.socket):
        buf = b''
        while not self._stop_event.is_set():
            try:
                chunk = sock.recv(4096)
                if not chunk:
                    self.get_logger().warn('Ethernet connection closed by remote.')
                    break
                buf += chunk
                while b'\n' in buf:
                    line, buf = buf.split(b'\n', 1)
                    text = line.decode('utf-8', errors='replace').strip()
                    if text:
                        rx_msg = String()
                        rx_msg.data = text
                        self._pub_rx.publish(rx_msg)
                        self.get_logger().debug(f'RX ← Ethernet: {text}')
            except OSError:
                break

    def _close_socket(self):
        with self._sock_lock:
            if self._sock:
                try:
                    self._sock.close()
                except OSError:
                    pass
                self._sock = None

    # ── Cleanup ──────────────────────────────────────────────────────

    def destroy_node(self):
        self._stop_event.set()
        self._close_socket()
        super().destroy_node()


# ── Entry point ──────────────────────────────────────────────────────────────

def main(args=None):
    rclpy.init(args=args)
    node = EthSenderNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()