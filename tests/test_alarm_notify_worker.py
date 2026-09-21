"""Notification is network I/O and leaves the engine on its own thread. §11.

**The engine used to telephone people while holding the lock that evaluates
measurements, on paho's network thread.** `send_email` opens a new SMTP
connection per recipient with a 20 s socket timeout, and the SMS client is
given no timeout at all, so one unreachable gateway stopped the engine from
evaluating anything - and stopped paho sending its keepalive, which dropped
the broker connection after 30 s. The alarm engine going deaf because it is
busy telephoning about the first alarm is precisely what this subsystem
exists to prevent.

It gets worse under load rather than better. Transitions are per channel with
no aggregation across them, so a CAEN link that dies takes 32 channels into
alarm at once: 32 messages, serially, each one blocking everything.

What these tests hold down is that the evaluating thread hands the message
over and carries on, that nothing is sent while the lock is held, that the
queue is bounded and says so when it overflows, and that a shutdown still
flushes what was queued. `test_alarms.py` drives an engine that was never
started and therefore sends inline - that is the other half of the contract
and it is asserted here too, because it is what keeps that file honest.
"""

import pathlib
import tempfile
import threading
import time

import pytest

from doubles import RecordingBus
from xams_sc.alarms.engine import NOTIFY_QUEUE_MAX, AlarmEngine
from xams_sc.config import load
from xams_sc.model import Measurement, Quality, utcnow

_ISOLATED = pathlib.Path(tempfile.gettempdir()) / "xams-tests-no-such-dir"

THRESHOLDS = {"pmain": {"hihi": {"value": 2.0, "severity": "major"}},
              "tt401": {"hihi": {"value": 2.0, "severity": "major"}}}


class BlockingNotifier:
    """A gateway that has accepted the connection and then says nothing."""

    def __init__(self):
        self.released = threading.Event()
        self.entered = threading.Event()
        self.sent = []

    def send(self, text, channels, rich=None):
        self.entered.set()
        self.released.wait(timeout=10)
        self.sent.append(text)
        return {"email": 1}


class CountingNotifier:
    def __init__(self):
        self.sent = []

    def send(self, text, channels, rich=None):
        self.sent.append(text)
        return {"email": 1}


def build(notifier, **defaults):
    cfg = load()
    cfg.alarms = {
        "defaults": {"hysteresis": 0.02, "min_repeat_minutes": 15,
                     "stale_after_seconds": 60, **defaults},
        "channels": THRESHOLDS,
        "staleness": {"severity": "major", "notify": ["email"]},
    }
    return AlarmEngine(RecordingBus(), cfg, notifier=notifier,
                       notify_state_path=_ISOLATED / "alarm_notify.json")


@pytest.fixture
def started():
    """An engine with its worker running, stopped again afterwards."""
    engines = []

    def make(notifier, **defaults):
        engine = build(notifier, **defaults)
        engine.start()
        engines.append(engine)
        return engine

    yield make
    for engine in engines:
        engine.stop()


def alarm(channel="pmain", value=9.9):
    return Measurement(t=utcnow(), channel=channel, value=value, unit="bar",
                       quality=Quality.OK)


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class TestTheEvaluatingThreadIsNotHeldUp:
    def test_on_measurement_returns_while_the_gateway_hangs(self, started):
        """THE BUG. This call used to sit in smtplib for 20 s per recipient."""
        notifier = BlockingNotifier()
        engine = started(notifier)

        start = time.monotonic()
        engine.on_measurement(alarm())
        elapsed = time.monotonic() - start

        assert notifier.entered.wait(timeout=5), "nothing was ever sent"
        assert elapsed < 1.0, (
            "on_measurement took %.1f s while the gateway hung" % elapsed)
        notifier.released.set()

    def test_the_lock_is_not_held_while_sending(self, started):
        """A second channel must still be evaluated mid-send.

        This is the half that matters on a cascade: 32 channels go into alarm
        together, and the 31st must not wait for the gateway to answer about
        the first.
        """
        notifier = BlockingNotifier()
        engine = started(notifier)
        engine.on_measurement(alarm("pmain"))
        assert notifier.entered.wait(timeout=5)

        start = time.monotonic()
        engine.on_measurement(alarm("tt401"))
        elapsed = time.monotonic() - start

        assert elapsed < 1.0, "the second channel waited for the first's SMS"
        # `active()` takes the lock itself, so reaching this at all is the
        # assertion: under the old code it would sit here until the gateway
        # answered about pmain.
        assert {"pmain", "tt401"} <= {s.channel for s in engine.active()}
        notifier.released.set()

    def test_a_raising_gateway_does_not_kill_the_worker(self, started):
        class Angry:
            def __init__(self):
                self.calls = 0

            def send(self, text, channels, rich=None):
                self.calls += 1
                raise RuntimeError("no route to host")

        notifier = Angry()
        engine = started(notifier)
        engine.on_measurement(alarm("pmain"))
        engine.on_measurement(alarm("tt401"))

        assert wait_for(lambda: notifier.calls == 2), (
            "the worker died on the first failure")


class TestTheQueueIsBounded:
    def test_it_drops_loudly_rather_than_growing(self, started, caplog):
        """A wedged gateway must not grow this without limit.

        The drop is an error in the log, in the same words as an empty
        recipient list: both mean somebody who should have been told was not.
        """
        notifier = BlockingNotifier()
        engine = started(notifier, min_repeat_minutes=0)
        assert engine._outbox is not None

        # One message is taken by the worker and blocks there; the rest fill
        # the queue exactly to its bound.
        for _ in range(NOTIFY_QUEUE_MAX + 40):
            engine._dispatch("pmain", "flood", ["email"], None)

        assert engine._outbox.qsize() <= NOTIFY_QUEUE_MAX
        assert "ALARM NOT DELIVERED" in caplog.text
        notifier.released.set()

    def test_dispatch_never_blocks_the_caller(self, started):
        notifier = BlockingNotifier()
        engine = started(notifier)
        for _ in range(NOTIFY_QUEUE_MAX + 5):
            engine._dispatch("pmain", "flood", ["email"], None)

        start = time.monotonic()
        engine._dispatch("pmain", "one more", ["email"], None)
        assert time.monotonic() - start < 1.0
        notifier.released.set()


class TestItStillGetsSent:
    def test_the_message_arrives_by_way_of_the_worker(self, started):
        notifier = CountingNotifier()
        engine = started(notifier)
        engine.on_measurement(alarm())
        assert wait_for(lambda: len(notifier.sent) == 1)
        assert "pmain" in notifier.sent[0]

    def test_the_order_alarms_went_off_in_is_kept(self, started):
        notifier = CountingNotifier()
        engine = started(notifier)
        engine.on_measurement(alarm("pmain"))
        engine.on_measurement(alarm("tt401"))
        assert wait_for(lambda: len(notifier.sent) == 2)
        assert "pmain" in notifier.sent[0] and "tt401" in notifier.sent[1]

    def test_stopping_flushes_what_was_queued(self):
        """A shutdown must not swallow the alarm that prompted it."""
        notifier = CountingNotifier()
        engine = build(notifier)
        engine.start()
        engine.on_measurement(alarm())
        engine.stop()
        assert len(notifier.sent) == 1


class TestAnEngineThatWasNeverStarted:
    def test_sends_on_the_calling_thread(self):
        """No worker means no other thread to hand it to.

        This is how the unit tests in test_alarms.py drive the engine, and
        asserting it here is what stops that file quietly testing nothing if
        the dispatch ever changes.
        """
        notifier = CountingNotifier()
        engine = build(notifier)
        assert engine._outbox is None

        engine.on_measurement(alarm())

        assert len(notifier.sent) == 1, "nothing was sent without a worker"
