"""Batching, backlog and recovery in the PostgreSQL writer. See §9.3, §12.

The archive is the truth and this is an index over it, so a database outage
is meant to cost query convenience and never data. It did cost data, in a way
that only appeared during recovery.

`flush` writes one batch of `batch_size`. The pump called it once per
`flush_interval_s`, which caps the write rate at 500 rows per 10 s — fifty a
second against a steady load of about five, so the margin looked generous.
After an outage it was not: a full backlog of `max_pending` needed over half
an hour to clear, and for every minute of that the queue sat at its cap
DISCARDING new readings. The recovery was slower than the failure and lost
data of its own.

What is pinned here is that a backlog drains in one cycle, that a drain stops
rather than spinning when the database is down, and that shutdown still
writes what it holds.
"""

from __future__ import annotations

import json
import threading

import pytest

from doubles import RecordingBus
from xams_sc.model import Measurement, Quality, utcnow


@pytest.fixture
def writer(fake_pg):
    from xams_sc.sinks.pg_writer import PgWriter

    w = PgWriter(RecordingBus(), "postgresql://nowhere", batch_size=500,
                 flush_interval_s=10.0, max_pending=100_000)
    w._connect = lambda: fake_pg
    # Also set directly: `close` and the failure path both act on `_conn`, and
    # a fixture that only patches `_connect` leaves them with nothing to act
    # on — so the test would pass while asserting about nothing.
    w._conn = fake_pg
    return w


def reading(channel="pmain", value=1.0):
    return Measurement(t=utcnow(), channel=channel, value=value,
                       unit="bar", quality=Quality.OK)


def fill(writer, n, channel="pmain"):
    for i in range(n):
        writer.add(reading(channel, float(i)))


class TestDraining:
    def test_a_backlog_clears_in_one_cycle(self, writer, fake_pg):
        """The whole point. 10 000 rows used to need 20 cycles — 200 seconds
        — and now go in one."""
        fill(writer, 10_000)

        written = writer.drain(budget_s=60.0)

        assert written == 10_000
        assert writer.stats["pending"] == 0

    def test_one_flush_still_writes_only_one_batch(self, writer, fake_pg):
        """`drain` loops; `flush` did not change."""
        fill(writer, 10_000)

        assert writer.flush() == 500
        assert writer.stats["pending"] == 9_500

    def test_an_empty_queue_costs_one_call(self, writer, fake_pg):
        """Steady state must not pay for the loop."""
        assert writer.drain() == 0
        assert fake_pg.calls == []

    def test_the_budget_bounds_a_long_drain(self, writer, fake_pg):
        """Whatever is left goes in the next cycle. A drain that ran to
        completion regardless would starve the stop signal and monopolise the
        database."""
        fill(writer, 100_000)

        written = writer.drain(budget_s=0.0)

        assert written == 500, "the budget did not bound the drain"
        assert writer.stats["pending"] == 99_500

    def test_a_dead_database_breaks_the_loop_rather_than_spinning(
            self, writer, fake_pg):
        """Retrying a dead connection in a tight loop turns an outage into a
        busy wait."""
        fill(writer, 10_000)
        fake_pg.fail_with = RuntimeError("connection refused")

        written = writer.drain(budget_s=60.0)

        assert written == 0
        assert writer.stats["pending"] == 10_000, "the batch was not put back"
        assert len(fake_pg.calls) == 0

    def test_nothing_is_lost_when_a_write_fails_midway(self, writer, fake_pg):
        fill(writer, 2_000)
        writer.flush()                      # 500 written
        fake_pg.fail_with = RuntimeError("connection refused")

        writer.drain(budget_s=60.0)

        assert writer.stats["pending"] == 1_500
        assert writer.stats["written"] == 500


class TestTheBacklogCap:
    def test_rows_beyond_max_pending_are_counted_not_silently_dropped(
            self, fake_pg):
        from xams_sc.sinks.pg_writer import PgWriter

        w = PgWriter(RecordingBus(), "postgresql://nowhere", max_pending=10)
        w._connect = lambda: fake_pg
        fill(w, 25)

        assert w.stats["pending"] == 10
        assert w.stats["dropped"] == 15, "the loss must be countable"

    def test_a_drained_queue_accepts_rows_again(self, writer, fake_pg):
        """The recovery this whole change is about: once the backlog is
        written, new readings stop being discarded."""
        writer.max_pending = 10
        fill(writer, 25)
        assert writer.stats["dropped"] == 15

        writer.drain(budget_s=60.0)
        fill(writer, 5)

        assert writer.stats["pending"] == 5
        assert writer.stats["dropped"] == 15, "dropped more after draining"


class TestShutdown:
    def test_close_writes_what_it_still_holds(self, writer, fake_pg):
        """`respect_stop=False`: the stop flag is what brought us here, and a
        drain that honoured it would write nothing on the way out."""
        fill(writer, 3_000)

        writer.close()

        assert writer.stats["pending"] == 0
        assert writer.stats["written"] == 3_000
        assert fake_pg.closed

    def test_close_on_a_dead_database_does_not_hang(self, writer, fake_pg):
        fill(writer, 3_000)
        fake_pg.fail_with = RuntimeError("connection refused")

        done = threading.Event()
        threading.Thread(target=lambda: (writer.close(), done.set()),
                         daemon=True).start()

        assert done.wait(5), "close() did not return promptly"
        assert writer.stats["pending"] == 3_000, "data was discarded on the way out"


class TestTheSubscription:
    def test_a_measurement_on_the_bus_is_queued(self, writer):
        writer.start()
        try:
            writer.bus.deliver("xams/meas/pmain", reading().to_json())
        finally:
            writer.close()

        assert writer.stats["written"] == 1

    def test_a_malformed_payload_does_not_take_down_the_subscriber(self, writer):
        writer.start()
        try:
            writer.bus.deliver("xams/meas/pmain", "{not json")
            writer.bus.deliver("xams/meas/pmain", json.dumps({"nonsense": True}))
        finally:
            writer.close()

        assert writer.stats["pending"] == 0
