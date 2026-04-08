"""
heartbeat_node.py
────────────────────────────────────────────────────────────────────
ROS2 node: heartbeat_node

Implements a two-way heartbeat system over the LoRa link.

Behaviour
─────────
  TX side (every 1 second):
    Publishes a JSON heartbeat envelope to /lora_tx_json.
    The lora_sender_node picks this up and writes it to the dongle.

  RX side (watchdog):
    Subscribes to /lora_rx_hb (std_msgs/String carrying raw JSON).
    The lora_receiver_node dispatches inbound heartbeats here.
    If no heartbeat is received within `timeout_sec` (default 30 s),
    the node publishes True to /estop to trigger the state_manager
    emergency stop.

  Recovery:
    Once ESTOP is fired, the node continues monitoring. When a
    heartbeat is received again it logs the recovery but does NOT
    automatically clear the ESTOP — that requires a manual START
    button press on the controller (matching state_manager behaviour).

JSON Heartbeat Envelope
───────────────────────
{
    "msg_type": "heartbeat",
    "sender":   "base" | "robot",
    "ts":       <unix timestamp float>,
    "payload":  { "seq": <int> }
}

ROS2 Parameters
───────────────
  sender_id      (string)  – 'base' or 'robot' (default: 'base')
  heartbeat_hz   (float)   – TX rate in Hz        (default: 1.0)
  timeout_sec    (float)   – RX watchdog timeout   (default: 30.0)

Subscribed topics
─────────────────
  /lora_rx_hb   (std_msgs/String)  – inbound heartbeat JSON from receiver

Published topics
────────────────
  /lora_tx_json  (std_msgs/String)  – outbound JSON envelope to sender
  /estop         (std_msgs/Bool)    – True on watchdog timeout → state_manager ESTOP

Run
───
  ros2 run comm_protocol heartbeat_node
  ros2 run comm_protocol heartbeat_node --ros-args -p sender_id:=robot
"""

import json
import time

import rclpy
from rclpy.node import Node
from std_msgs.msg import String, Bool


class HeartbeatNode(Node):

    def __init__(self):
        super().__init__('heartbeat_node')

        # ── Parameters ──────────────────────────────────────────────
        self.declare_parameter('sender_id',    'base')
        self.declare_parameter('heartbeat_hz', 1.0)
        self.declare_parameter('timeout_sec',  30.0)

        self._sender_id   = self.get_parameter('sender_id').get_parameter_value().string_value
        hb_hz             = self.get_parameter('heartbeat_hz').get_parameter_value().double_value
        self._timeout_sec = self.get_parameter('timeout_sec').get_parameter_value().double_value

        # ── State ────────────────────────────────────────────────────
        self._tx_seq        = 0
        self._last_rx_time  = time.monotonic()  # init to now — no ESTOP on startup
        self._estop_fired   = False
        self._link_was_lost = False             # track for recovery logging

        # ── Publishers ───────────────────────────────────────────────
        self._pub_tx    = self.create_publisher(String, '/lora_tx_json', 10)
        self._pub_estop = self.create_publisher(Bool,   '/estop',        10)

        # ── Subscriptions ────────────────────────────────────────────
        self._sub_hb = self.create_subscription(
            String, '/lora_rx_hb', self._on_heartbeat_rx, 10)

        # ── Timers ───────────────────────────────────────────────────
        self._tx_timer       = self.create_timer(1.0 / hb_hz, self._tx_heartbeat)
        self._watchdog_timer = self.create_timer(1.0,          self._check_watchdog)

        self.get_logger().info(
            f'heartbeat_node ready | sender={self._sender_id} '
            f'| tx={hb_hz:.1f} Hz | timeout={self._timeout_sec:.0f}s')

    # ── TX: publish outbound heartbeat ───────────────────────────────

    def _tx_heartbeat(self):
        envelope = {
            'msg_type': 'heartbeat',
            'sender':   self._sender_id,
            'ts':       time.time(),
            'payload':  {'seq': self._tx_seq},
        }
        self._tx_seq += 1

        msg = String()
        msg.data = json.dumps(envelope, separators=(',', ':'))
        self._pub_tx.publish(msg)
        self.get_logger().debug(f'HB TX seq={self._tx_seq - 1}')

    # ── RX: update watchdog timestamp ────────────────────────────────

    def _on_heartbeat_rx(self, msg: String):
        """Called by lora_receiver_node when a heartbeat envelope arrives."""
        try:
            envelope = json.loads(msg.data)
        except json.JSONDecodeError as e:
            self.get_logger().warn(f'Malformed heartbeat JSON: {e}')
            return

        sender = envelope.get('sender', 'unknown')
        seq    = envelope.get('payload', {}).get('seq', '?')
        ts     = envelope.get('ts', 0.0)

        self._last_rx_time = time.monotonic()

        if self._link_was_lost:
            self.get_logger().info(
                f'LoRa link RESTORED — received heartbeat from [{sender}] '
                f'seq={seq}. ESTOP must be cleared manually via controller.')
            self._link_was_lost = False
            self._estop_fired   = False  # re-arm watchdog for next dropout

        self.get_logger().debug(f'HB RX from [{sender}] seq={seq} ts={ts:.3f}')

    # ── Watchdog: check timeout every second ─────────────────────────

    def _check_watchdog(self):
        elapsed = time.monotonic() - self._last_rx_time

        if not self._estop_fired:
            if elapsed >= 20.0:
                self.get_logger().warn(
                    f'LoRa heartbeat silent for {elapsed:.1f}s '
                    f'(ESTOP in {self._timeout_sec - elapsed:.1f}s)')
            elif elapsed >= 10.0:
                self.get_logger().warn(
                    f'LoRa heartbeat silent for {elapsed:.1f}s')

        if elapsed >= self._timeout_sec and not self._estop_fired:
            self._fire_estop(elapsed)

    def _fire_estop(self, elapsed: float):
        """Publish True to /estop and flag so we only fire once per dropout."""
        self._estop_fired   = True
        self._link_was_lost = True

        self.get_logger().error(
            f'LoRa heartbeat lost for {elapsed:.1f}s — firing ESTOP!')

        estop_msg = Bool()
        estop_msg.data = True
        self._pub_estop.publish(estop_msg)

        # Also propagate ESTOP over LoRa in case the other side is still alive
        envelope = {
            'msg_type': 'robot_state',
            'sender':   self._sender_id,
            'ts':       time.time(),
            'payload':  {'state': 'ESTOP'},
        }
        tx_msg = String()
        tx_msg.data = json.dumps(envelope, separators=(',', ':'))
        self._pub_tx.publish(tx_msg)


# ── Entry point ──────────────────────────────────────────────────────

def main(args=None):
    rclpy.init(args=args)
    node = HeartbeatNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()