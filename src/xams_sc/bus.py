"""MQTT publish/subscribe. See DESIGN.md §2.1, §3, §6.4.

Every service publishes here; nothing writes to a database directly. The bus
is what keeps acquisition, scaling, storage and alarming from entangling —
it makes the wrong thing awkward to do.

Topics (§3):

    xams/meas/<channel>             measurement, JSON, retained
    xams/status/<service>/heartbeat ISO-8601 UTC, retained
    xams/status/<service>/state     starting | running | degraded | stopped
    xams/alarm/<channel>            alarm state changes
    xams/cmd/<device>/<action>      control requests
    xams/ack/<device>/<action>      command acknowledgements

The broker is a shared dependency, so an outage must cost a visible gap and
never a silent one. This client buffers while disconnected and republishes in
order when the broker returns; if the buffer overflows, the loss is counted
and reported rather than passing unnoticed.
"""

from __future__ import annotations

import collections
import logging
import threading
from typing import Callable

import paho.mqtt.client as mqtt

from .model import Measurement, ServiceState, iso, utcnow

log = logging.getLogger(__name__)

TOPIC_MEAS = "xams/meas"
TOPIC_STATUS = "xams/status"
TOPIC_ALARM = "xams/alarm"
TOPIC_CMD = "xams/cmd"
TOPIC_ACK = "xams/ack"
TOPIC_RELOAD = "xams/cmd/all/reload"
TOPIC_FLOW_RESET = "xams/cmd/derived/flow_reset"
TOPIC_FLOW_PERIOD = "xams/flow/period"

# The Lake Shore control path (§10). These are the first topics in the system
# that cause an instrument to do something, as against reporting what it has
# done, which is why every one of them has an acknowledgement beside it.
TOPIC_LS_SETPOINT = "xams/cmd/lakeshore/setpoint"
TOPIC_LS_RANGE = "xams/cmd/lakeshore/range"
ACK_LS_SETPOINT = "xams/ack/lakeshore/setpoint"
ACK_LS_RANGE = "xams/ack/lakeshore/range"

# Every write, published for a sink to store (§2.1, §10 rule 5). Publishing it
# rather than writing it here means an audit record survives the database
# being down: it is in the JSONL archive either way.
TOPIC_AUDIT = "xams/audit"


class Bus:
    """A thin wrapper over paho-mqtt with an outage buffer.

    Bound to loopback by default and deliberately so: commands travel over
    this bus, so anything that can reach the broker can set a high voltage
    (§8). A broker on 0.0.0.0 is an unauthenticated control interface.
    """

    def __init__(
        self,
        client_id: str,
        host: str = "127.0.0.1",
        port: int = 1883,
        buffer_seconds: float = 300.0,
        publish_interval_s: float = 1.0,
    ):
        self.client_id = client_id
        self.host = host
        self.port = port

        # Bounded buffer: roughly `buffer_seconds` worth of publishes. Bounded
        # because an unbounded queue turns a broker outage into a memory leak,
        # which is a worse failure than the one it was meant to survive.
        depth = max(64, int(buffer_seconds / max(publish_interval_s, 0.01)) * 4)
        self._buffer: collections.deque[tuple[str, str, bool]] = collections.deque(maxlen=depth)
        self._buffer_depth = depth
        self._dropped = 0
        self._lock = threading.Lock()
        self._connected = threading.Event()

        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id=client_id, clean_session=True
        )
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        # A LIST, not a dict keyed by topic. Several consumers legitimately
        # subscribe to the same pattern - the JSONL archive, the PostgreSQL
        # writer and the alarm engine all want xams/meas/#. Keying by topic
        # means the second subscriber silently replaces the first, which is
        # exactly the opposite of what the bus exists to provide (§2.1).
        self._handlers: list[tuple[str, Callable[[str, str], None]]] = []
        self._client.on_message = self._on_message

    # ---------------------------------------------------------------- lifecycle

    def connect(self) -> None:
        """Start the network loop. Does not block on the broker being up.

        A service must keep reading hardware whether or not the broker is
        reachable (§6.4), so connection failure here is a warning, not a fatal.
        """
        self._client.will_set(
            f"{TOPIC_STATUS}/{self.client_id}/state",
            ServiceState.STOPPED.value,
            retain=True,
        )
        try:
            self._client.connect_async(self.host, self.port, keepalive=30)
        except Exception as exc:  # pragma: no cover - network dependent
            log.warning("broker %s:%s not reachable yet: %s", self.host, self.port, exc)
        self._client.loop_start()

    def disconnect(self) -> None:
        try:
            self._client.loop_stop()
            self._client.disconnect()
        except Exception:
            pass

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    # ---------------------------------------------------------------- callbacks

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code == 0:
            log.info("connected to broker %s:%s", self.host, self.port)
            self._connected.set()
            for topic in {pattern for pattern, _ in self._handlers}:
                client.subscribe(topic)
            self._flush()
        else:
            log.warning("broker refused connection: %s", reason_code)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        self._connected.clear()
        log.warning("disconnected from broker (%s); buffering", reason_code)

    def _on_message(self, client, userdata, msg):
        payload = msg.payload.decode("utf-8", errors="replace")
        for pattern, handler in self._handlers:
            if mqtt.topic_matches_sub(pattern, msg.topic):
                try:
                    handler(msg.topic, payload)
                except Exception:
                    # A failing subscriber must not take down the loop, and a
                    # swallowed exception must not be invisible.
                    log.exception("handler for %s raised on topic %s", pattern, msg.topic)

    # ---------------------------------------------------------------- publish

    def _publish(self, topic: str, payload: str, retain: bool = False) -> None:
        if self._connected.is_set():
            info = self._client.publish(topic, payload, qos=0, retain=retain)
            if info.rc == mqtt.MQTT_ERR_SUCCESS:
                return
            log.debug("publish to %s failed (rc=%s); buffering", topic, info.rc)

        with self._lock:
            if len(self._buffer) == self._buffer_depth:
                self._dropped += 1
            self._buffer.append((topic, payload, retain))

    def _flush(self) -> None:
        """Republish the buffer, in order, once the broker returns."""
        with self._lock:
            pending, self._buffer = list(self._buffer), collections.deque(
                maxlen=self._buffer_depth
            )
            dropped, self._dropped = self._dropped, 0

        if dropped:
            # The gap is recorded explicitly rather than passing silently.
            log.error(
                "broker outage overflowed the buffer: %d messages lost", dropped
            )
        if pending:
            log.info("republishing %d buffered messages", len(pending))
        for topic, payload, retain in pending:
            self._client.publish(topic, payload, qos=0, retain=retain)

    @property
    def dropped(self) -> int:
        """Messages lost to buffer overflow since the last flush."""
        return self._dropped

    # ---------------------------------------------------------------- public API

    def publish_measurement(self, m: Measurement) -> None:
        self._publish(f"{TOPIC_MEAS}/{m.channel}", m.to_json(), retain=True)

    def publish_heartbeat(self, service: str) -> None:
        self._publish(f"{TOPIC_STATUS}/{service}/heartbeat", iso(utcnow()), retain=True)

    def publish_state(self, service: str, state: ServiceState) -> None:
        self._publish(f"{TOPIC_STATUS}/{service}/state", state.value, retain=True)

    def publish_raw(self, topic: str, payload: str, retain: bool = False) -> None:
        self._publish(topic, payload, retain=retain)

    def subscribe(self, topic: str, handler: Callable[[str, str], None]) -> None:
        """Register a handler. Safe to call before connect().

        Every handler registered for a matching pattern is called, so adding a
        consumer never displaces one that was already there.
        """
        self._handlers.append((topic, handler))
        if self._connected.is_set():
            self._client.subscribe(topic)

    @property
    def subscriber_count(self) -> int:
        return len(self._handlers)
