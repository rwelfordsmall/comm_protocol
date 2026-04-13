"""
eth_receiver_node.py
────────────────────────────────────────────────────────────────────
ROS2 node: eth_receiver

Jetson-side TCP server for Ethernet transport. Listens for inbound
JSON envelope connections from eth_sender_node on the base station
and dispatches messages to the same ROS2 topics as lora_receiver_node
and wifi_receiver_node — the rest of the stack never knows which
transport was used.

Structurally identical to wifi_receiver_node — only the default port
and monitoring topic names differ so both can run simultaneously.

JSON Envelope Format (identical to LoRa / WiFi stack)
──────────────────────────────────────────────────────
{
    "msg_type": "heartbeat" | "cmd_vel" | "robot_state" | "body_pose"
              | "joy" | "goal_pose" | "test_ping",
    "sender":   "base" | "robot",
    "ts":       <unix timestamp float>,
    "payload":  { ... }
}

Dispatch table
──────────────
  heartbeat    → /lora_rx_hb    (std_msgs/String)
  cmd_vel      → /gait_command  (geometry_msgs/Twist)
  robot_state  → /estop         (std_msgs/Bool)
  body_pose    → /body_pose     (geometry_msgs/Vector3)
  joy          → /joy_raw       (sensor_msgs/Joy)
  goal_pose    → /goal_pose     (geometry_msgs/PoseStamped)
  test_ping    → /eth_rx_raw    (std_msgs/String)    ack JSON; also sent back over TCP

test_ping
─────────
  Handled like any other whitelisted type. The receiver builds a
  "test_ack" String message, publishes it to /eth_rx_raw for local
  monitoring, and writes the ack JSON back over the TCP connection so
  the base station sees it on /eth_rx. Available any time — no
  special mode required.

  Ack envelope:
  {
      "msg_type": "test_ack",
      "sender":   "robot",
      "ts":       <unix timestamp float>,
      "payload":  {
          "transport": "eth",
          "status":    "OK",
          "echo":      <original payload>
      }
  }

ROS2 Parameters
───────────────
  bind_host   (string)  – interface to bind  (default: '0.0.0.0')
  bind_port   (int)     – TCP port           (default: 5801)
  sender_id   (string)  – default 'robot'

Published topics
────────────────
  /eth_rx_raw   (std_msgs/String)  – raw JSON from base station (all types)
  (+ all dispatch topics above, shared with LoRa and WiFi receivers)

Run
───
  ros2 run comm_protocol eth_receiver_node
  ros2 run comm_protocol eth_receiver_node --ros-args -p bind_port:=5801
"""

import json
import socket
import threading
import time

import rclpy
from rclpy.node import Node
from std_msgs.msg import String, Bool
from geometry_msgs.msg import Twist, Vector3, PoseStamped
from sensor_msgs.msg import Joy

# ── Dispatch table ───────────────────────────────────────────────────────────
_WHITELIST = {
    'heartbeat':   ('/lora_rx_hb',   String),
    'cmd_vel':     ('/gait_command',  Twist),
    'robot_state': ('/estop',         Bool),
    'body_pose':   ('/body_pose',     Vector3),
    'joy':         ('/joy_raw',       Joy),
    'goal_pose':   ('/goal_pose',     PoseStamped),
    'test_ping':   ('/eth_rx_raw',    String),
}


class EthReceiverNode(Node):

    def __init__(self):
        super().__init__('eth_receiver')

        # ── Parameters ───────────────────────────────────────────────
        self.declare_parameter('bind_host', '0.0.0.0')
        self.declare_parameter('bind_port', 5801)
        self.declare_parameter('sender_id', 'robot')

        self._host = self.get_parameter('bind_host').get_parameter_value().string_value
        self._port = self.get_parameter('bind_port').get_parameter_value().integer_value
        self._sid  = self.get_parameter('sender_id').get_parameter_value().string_value

        self._stop_event = threading.Event()

        # ── Publishers ───────────────────────────────────────────────
        self._pub_raw = self.create_publisher(String, '/eth_rx_raw', 10)
        self._publishers = {}
        for msg_type, (topic, ros_type) in _WHITELIST.items():
            if msg_type == 'test_ping':
                self._publishers[msg_type] = self._pub_raw
            else:
                self._publishers[msg_type] = self.create_publisher(ros_type, topic, 10)

        # ── Start TCP server ─────────────────────────────────────────
        self._server_thread = threading.Thread(
            target=self._server_loop, daemon=True)
        self._server_thread.start()

        self.get_logger().info(
            f'eth_receiver ({self._sid}) listening on {self._host}:{self._port}')

    # ── TCP server ───────────────────────────────────────────────────

    def _server_loop(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind((self._host, self._port))
            srv.listen(1)
            srv.settimeout(1.0)
            self.get_logger().info(
                f'Ethernet TCP server bound to {self._host}:{self._port}')

            while not self._stop_event.is_set():
                try:
                    conn, addr = srv.accept()
                except socket.timeout:
                    continue
                self.get_logger().info(f'Ethernet connection from {addr}')
                threading.Thread(
                    target=self._client_loop,
                    args=(conn, addr),
                    daemon=True).start()

    def _client_loop(self, conn: socket.socket, addr):
        buf = b''
        try:
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            while not self._stop_event.is_set():
                chunk = conn.recv(4096)
                if not chunk:
                    self.get_logger().info(f'Ethernet client {addr} disconnected.')
                    break
                buf += chunk
                while b'\n' in buf:
                    line, buf = buf.split(b'\n', 1)
                    text = line.decode('utf-8', errors='replace').strip()
                    if not text:
                        continue

                    raw_msg = String()
                    raw_msg.data = text
                    self._pub_raw.publish(raw_msg)
                    self.get_logger().debug(f'RX ← Ethernet [{addr}]: {text}')

                    try:
                        envelope = json.loads(text)
                    except json.JSONDecodeError as e:
                        self.get_logger().warn(
                            f'Non-JSON from Ethernet (dropping): {e}')
                        continue

                    self._dispatch(envelope, text, conn)

        except OSError as e:
            self.get_logger().warn(f'Ethernet client {addr} error: {e}')
        finally:
            conn.close()

    # ── Dispatch ─────────────────────────────────────────────────────

    def _dispatch(self, envelope: dict, raw_text: str, conn: socket.socket):
        msg_type = envelope.get('msg_type')

        if msg_type not in _WHITELIST:
            self.get_logger().warn(
                f'Unknown msg_type "{msg_type}" — dropping. '
                f'Whitelisted: {list(_WHITELIST.keys())}')
            return

        payload = envelope.get('payload', {})
        try:
            ros_msg = self._build_ros_msg(msg_type, payload, raw_text)
        except (KeyError, TypeError, ValueError) as e:
            self.get_logger().error(
                f'Failed to build ROS msg for [{msg_type}]: {e}')
            return

        self._publishers[msg_type].publish(ros_msg)
        self.get_logger().debug(
            f'Dispatched [{msg_type}] → {_WHITELIST[msg_type][0]}')

        # test_ping: also send ack back over TCP to the base station
        if msg_type == 'test_ping':
            self._send_test_ack(payload, conn)

    # ── Message builders ─────────────────────────────────────────────

    def _build_ros_msg(self, msg_type: str, payload: dict, raw_text: str):
        if msg_type == 'heartbeat':
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
            state = str(payload.get('state', ''))
            msg = Bool()
            msg.data = (state == 'ESTOP')
            return msg

        elif msg_type == 'body_pose':
            msg = Vector3()
            msg.x = float(payload.get('roll',  0.0))
            msg.y = float(payload.get('pitch', 0.0))
            msg.z = float(payload.get('yaw',   0.0))
            return msg

        elif msg_type == 'joy':
            msg = Joy()
            msg.header.stamp = self.get_clock().now().to_msg()
            msg.axes    = [float(a) for a in payload.get('axes',    [])]
            msg.buttons = [int(b)   for b in payload.get('buttons', [])]
            return msg

        elif msg_type == 'goal_pose':
            msg = PoseStamped()
            msg.header.stamp    = self.get_clock().now().to_msg()
            msg.header.frame_id = payload.get('frame_id', 'map')
            pos = payload.get('position', {})
            ori = payload.get('orientation', {})
            msg.pose.position.x    = float(pos.get('x', 0.0))
            msg.pose.position.y    = float(pos.get('y', 0.0))
            msg.pose.position.z    = float(pos.get('z', 0.0))
            msg.pose.orientation.x = float(ori.get('x', 0.0))
            msg.pose.orientation.y = float(ori.get('y', 0.0))
            msg.pose.orientation.z = float(ori.get('z', 0.0))
            msg.pose.orientation.w = float(ori.get('w', 1.0))
            return msg

        elif msg_type == 'test_ping':
            ack = {
                'msg_type': 'test_ack',
                'sender':   'robot',
                'ts':       time.time(),
                'payload':  {
                    'transport': 'eth',
                    'status':    'OK',
                    'echo':      payload,
                }
            }
            msg = String()
            msg.data = json.dumps(ack)
            return msg

        raise ValueError(f'No builder for msg_type: "{msg_type}"')

    # ── Test ack ─────────────────────────────────────────────────────

    def _send_test_ack(self, original_payload: dict, conn: socket.socket):
        ack = {
            'msg_type': 'test_ack',
            'sender':   'robot',
            'ts':       time.time(),
            'payload':  {
                'transport': 'eth',
                'status':    'OK',
                'echo':      original_payload,
            }
        }
        try:
            conn.sendall((json.dumps(ack) + '\n').encode('utf-8'))
            self.get_logger().info('test_ping received → test_ack sent')
        except OSError as e:
            self.get_logger().error(f'Failed to send test_ack: {e}')

    # ── Cleanup ──────────────────────────────────────────────────────

    def destroy_node(self):
        self._stop_event.set()
        super().destroy_node()


# ── Entry point ──────────────────────────────────────────────────────────────

def main(args=None):
    rclpy.init(args=args)
    node = EthReceiverNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()