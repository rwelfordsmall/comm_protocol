"""
lora_receiver_node.py
────────────────────────────────────────────────────────────────────
ROS2 node: lora_receiver

Reads newline-terminated JSON envelopes from the USB LoRa dongle and
dispatches them to the appropriate ROS2 topics based on msg_type.

JSON Envelope Format
────────────────────
{
    "msg_type": "heartbeat" | "cmd_vel" | "robot_state" | "body_pose" | "joy" | "goal_pose",
    "sender":   "base" | "robot",
    "ts":       <unix timestamp float>,
    "payload":  { ... }
}

Dispatch table (_WHITELIST)
───────────────────────────
  heartbeat    → /lora_rx_hb    (std_msgs/String)              raw JSON for heartbeat_node
  cmd_vel      → /gait_command  (geometry_msgs/Twist)
  robot_state  → /estop         (std_msgs/Bool)                True if payload state == 'ESTOP'
  body_pose    → /body_pose     (geometry_msgs/Vector3)
  joy          → /joy_raw       (sensor_msgs/Joy)              Xbox controller from base station
  goal_pose    → /goal_pose     (geometry_msgs/PoseStamped)    Nav2 goal from base station RViz

Notes
─────
  robot_state / ESTOP:
    Inbound robot_state envelopes map directly to /estop (Bool). state_manager
    subscribes to /estop as its authoritative external ESTOP input, avoiding
    the race condition of publishing to /robot_state which state_manager only
    publishes to (never subscribes to).

  joy dispatch:
    Inbound Joy messages land on /joy_raw so they flow through the existing
    controller_node watchdog before reaching state_manager. This preserves
    the safety timeout behaviour identically to a locally connected controller.

  goal_pose dispatch:
    Inbound goal poses land on /goal_pose — the same topic RViz's "Nav2 Goal"
    tool publishes to. Nav2 bt_navigator picks this up directly. The robot must
    be in AUTONOMOUS state with Nav2 running for goals to execute.

ROS2 Parameters
───────────────
  serial_port   (string)  – default '/dev/ttyUSB1'
  baud_rate     (int)     – default 115200

Published topics
────────────────
  /lora_rx          (std_msgs/String)              – raw RX line (debug/monitoring)
  /lora_rx_hb       (std_msgs/String)              – heartbeat JSON for heartbeat_node
  /gait_command     (geometry_msgs/Twist)          – velocity commands from base station
  /estop            (std_msgs/Bool)                – True when ESTOP received over LoRa
  /body_pose        (geometry_msgs/Vector3)        – body orientation commands
  /joy_raw          (sensor_msgs/Joy)              – controller input from base station
  /goal_pose        (geometry_msgs/PoseStamped)    – Nav2 goal from base station

Run
───
  ros2 run comm_protocol lora_receiver
  ros2 run comm_protocol lora_receiver --ros-args -p serial_port:=/dev/ttyUSB0
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


# Whitelist: maps msg_type → (topic, ros_msg_type)
# Publishers are created dynamically in __init__ from this table.
_WHITELIST = {
    'heartbeat':   ('/lora_rx_hb',   String),
    'cmd_vel':     ('/gait_command',  Twist),
    'robot_state': ('/estop',         Bool),
    'body_pose':   ('/body_pose',     Vector3),
    'joy':         ('/joy_raw',       Joy),
    'goal_pose':   ('/goal_pose',     PoseStamped),
}


class LoraReceiverNode(Node):

    def __init__(self):
        super().__init__('lora_receiver')

        # ── Parameters ───────────────────────────────────────────────
        self.declare_parameter('serial_port', '/dev/ttyUSB1')
        self.declare_parameter('baud_rate',   115200)

        port = self.get_parameter('serial_port').get_parameter_value().string_value
        baud = self.get_parameter('baud_rate').get_parameter_value().integer_value

        # ── Publishers ───────────────────────────────────────────────
        # Raw line — always published for monitoring/debugging
        self._pub_raw = self.create_publisher(String, '/lora_rx', 10)

        # Whitelist publishers keyed by msg_type
        self._lora_pubs = {}
        for msg_type, (topic, ros_type) in _WHITELIST.items():
            self._lora_pubs[msg_type] = self.create_publisher(ros_type, topic, 10)
            self.get_logger().info(f'Whitelisted [{msg_type}] → {topic}')

        # ── Serial ───────────────────────────────────────────────────
        self._ser         = None
        self._read_thread = None
        self._stop_event  = threading.Event()

        try:
            self._ser = serial.Serial(port, baud, timeout=1.0)
            self.get_logger().info(f'Opened serial port {port} @ {baud} baud')
            self._start_read_thread()
        except serial.SerialException as e:
            self.get_logger().error(f'Failed to open serial port {port}: {e}')
            self.get_logger().error('Available ports: ' + self._list_ports())

        self.get_logger().info('lora_receiver ready')

    # ── Read thread ──────────────────────────────────────────────────

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
                self.get_logger().debug(f'RX <- LoRa: {text}')

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

        publisher = self._lora_pubs[msg_type]
        payload   = envelope.get('payload', {})

        try:
            ros_msg = self._build_ros_msg(msg_type, payload, raw_text)
        except (KeyError, TypeError, ValueError) as e:
            self.get_logger().error(
                f'Failed to build ROS msg for [{msg_type}]: {e} | payload={payload}')
            return

        publisher.publish(ros_msg)
        self.get_logger().debug(
            f'Dispatched [{msg_type}] → {_WHITELIST[msg_type][0]}')

    # ── Message builders ─────────────────────────────────────────────

    def _build_ros_msg(self, msg_type: str, payload: dict, raw_text: str):
        """Construct the appropriate ROS2 message from the JSON payload."""

        if msg_type == 'heartbeat':
            # Forward raw JSON string to heartbeat_node for watchdog update
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
            # Map ESTOP state string → Bool on /estop (state_manager's subscribed input)
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
            self.get_logger().info(
                f'GOAL RX | frame={msg.header.frame_id} '
                f'pos=({msg.pose.position.x:.2f}, '
                f'{msg.pose.position.y:.2f}, '
                f'{msg.pose.position.z:.2f})')
            return msg

        raise ValueError(f'Message type undefined, no builder: "{msg_type}"')

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