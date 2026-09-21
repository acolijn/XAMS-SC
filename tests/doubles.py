"""Stand-ins for the bus, the drift watcher and a database connection.

Kept out of `conftest.py` so a test can import one directly when it needs to
build its own — `from doubles import RecordingBus` — rather than being forced
through a fixture. The fixtures in `conftest.py` are thin wrappers over these.

Every one of these mirrors the object it replaces in the places that matter. A
double that lags the real object passes tests the real object would fail, and
the suite then certifies behaviour nothing has.
"""

from __future__ import annotations

import json

from paho.mqtt.client import topic_matches_sub


# Where the web UI is served, and therefore the only Host and Origin it
# accepts. `create_app()` defaults to the same pair.
UI_ORIGIN = "http://127.0.0.1:8000"


def ui_client(app):
    """A TestClient that reaches the app the way a BROWSER does.

    `TestClient(app)` alone calls itself `testserver` and sends no `Origin`,
    and the app refuses both: an unknown Host is DNS rebinding and a POST
    with no same-site Origin is CSRF. Tests that built their own client that
    way would all be testing the refusal instead of the page.

    In one place because there is one answer: a test file that spelled the
    origin out itself would go stale the day the port moves.
    """
    from fastapi.testclient import TestClient

    return TestClient(app, base_url=UI_ORIGIN,
                      headers={"origin": UI_ORIGIN})


class RecordingBus:
    """Stands in for `Bus`: records instead of publishing.

    Deliberately mirrors the real bus in the two places it matters:

    * `handlers` is a LIST, not a dict keyed by topic. Three consumers
      legitimately subscribe to `xams/meas/#`, and a dict lets the second
      silently displace the first — the exact bug the real `Bus` once had. A
      double that reproduces the defect under test proves nothing.
    * `deliver()` dispatches to EVERY matching handler, using the same
      wildcard matcher paho uses, rather than to one looked up by pattern.
    """

    def __init__(self):
        self.published: list[tuple[str, str]] = []
        self.handlers: list[tuple[str, object]] = []
        self.measurements: list = []
        self.states: list = []
        self.heartbeats: list[str] = []
        # When set, called with every published command, so a test can play
        # the service at the other end and feed an ack back the way a running
        # one would.
        self.on_publish = None
        # The CLI checks `connected` before it will send anything, and the
        # real Bus sets it from the broker's CONNACK. True from the start
        # here: a test that wants the broker down sets it False.
        self.connected = True
        self.connect_called = False
        self.disconnected = False

    # ------------------------------------------------------------- publishing

    def publish_raw(self, topic: str, payload: str, retain: bool = False) -> None:
        self.published.append((topic, payload))
        if self.on_publish is not None:
            self.on_publish(topic, payload)

    def publish_measurement(self, m) -> None:
        self.measurements.append(m)

    def publish_state(self, service, state) -> None:
        self.states.append((service, state))

    def publish_heartbeat(self, service) -> None:
        self.heartbeats.append(service)

    # ------------------------------------------------------------ subscribing

    def subscribe(self, topic: str, handler) -> None:
        self.handlers.append((topic, handler))

    def deliver(self, topic: str, payload: str) -> int:
        """Deliver a message to every handler whose pattern matches.

        Returns how many were called, so a test can assert that a message it
        thought it was sending actually reached somebody. A silent zero is how
        a test ends up asserting nothing at all.
        """
        called = 0
        for pattern, handler in self.handlers:
            if topic_matches_sub(pattern, topic):
                handler(topic, payload)
                called += 1
        return called

    def ack(self, topic: str, payload: dict) -> int:
        """Feed an acknowledgement back, as the owning service would."""
        return self.deliver(topic, json.dumps(payload))

    # ------------------------------------------------------------- lifecycle

    def connect(self) -> None:
        # Deliberately does NOT set `connected`. The real Bus sets it from the
        # broker's CONNACK, which may never arrive — `connect_async` does not
        # block and a missing broker is a warning, not a failure. A double
        # that connected synchronously made "the broker is down" untestable,
        # because the code under test would see a live connection either way.
        self.connect_called = True

    def disconnect(self) -> None:
        self.disconnected = True

    # ------------------------------------------------------------ convenience

    def published_on(self, topic: str) -> list[str]:
        """Every payload published on exactly `topic`, in order."""
        return [p for t, p in self.published if t == topic]

    def json_on(self, topic: str) -> list[dict]:
        """The same, parsed. Most command and audit payloads are JSON."""
        return [json.loads(p) for p in self.published_on(topic)]

    def last_json(self, topic: str) -> dict | None:
        """The most recent JSON payload on `topic`, or None if there was none.

        None rather than raising: "nothing was published" is a thing tests
        assert on, and it should read as a value, not as an error.
        """
        parsed = self.json_on(topic)
        return parsed[-1] if parsed else None

    def measured(self, channel: str):
        """The most recent measurement published for `channel`, or None."""
        for m in reversed(self.measurements):
            if m.channel == channel:
                return m
        return None


class StubDrift:
    """The Grafana dashboard-drift check, stubbed out.

    Without this the web UI tests reach across to Grafana over HTTP, which
    makes them fail on any machine where Grafana is merely stopped — and the
    thing they are testing has nothing to do with dashboards.
    """

    def get(self):
        return {"state": "ok", "dashboards": [], "detail": "stubbed"}


class FakePg:
    """A psycopg connection that records instead of connecting.

    Every sink is the same shape — take a payload, execute one or more
    statements — so what a test needs to see is which statement ran with which
    values, in what order. `calls` keeps that; `rows` is the parameters alone,
    for the common case of asserting on one INSERT.

    It is its own cursor. psycopg's real cursor is a separate object, but no
    sink here depends on that, and one object keeps the assertions short.
    """

    def __init__(self, rowcount: int = 1, fetch: list | None = None):
        self.calls: list[tuple[str, tuple]] = []
        self.closed = False
        self.rowcount = rowcount
        # Queued `fetchone` results, consumed in order; None once exhausted,
        # which is what psycopg returns for a query that matched nothing.
        self._fetch = list(fetch or [])
        # When set, every execute raises it — the database being down.
        self.fail_with: Exception | None = None

    # -- psycopg's connection/cursor protocol, minimally -------------------
    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        if self.fail_with is not None:
            raise self.fail_with
        self.calls.append((sql, tuple(params) if params is not None else ()))

    def executemany(self, sql, rows):
        if self.fail_with is not None:
            raise self.fail_with
        for r in rows:
            self.calls.append((sql, tuple(r)))

    def fetchone(self):
        return self._fetch.pop(0) if self._fetch else None

    def close(self):
        self.closed = True

    # -- assertions --------------------------------------------------------
    @property
    def rows(self) -> list[tuple]:
        """The parameters of every statement, in order."""
        return [params for _, params in self.calls]

    def matching(self, fragment: str) -> list[tuple]:
        """Parameters of the statements whose SQL contains `fragment`.

        Lets a test say "the INSERT into audit" without pasting the statement.
        """
        return [params for sql, params in self.calls if fragment in sql]
