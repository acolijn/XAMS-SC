"""The three database sinks: audit, alarm state and flow periods.

All three had no tests at all. They are ordinary bus subscribers (§2.1) and
none of them is on the acquisition path, which is why they were easy to leave
out — and why leaving them out is worse than it looks. Each one exists so that
something survives the thing it records:

  * the audit trail outlives the person who made the write (§10 rule 5);
  * the alarm history outlives the alarm (§11);
  * a closed flow period outlives the reset that closed it (§7.5).

A sink that quietly stores nothing looks exactly like a sink with nothing to
store. So what is pinned here is mostly the refusals and the edge cases: the
record kept when its timestamp is unreadable, the repeat that must NOT be
stored, the failure that must stay loud.

The database is a stand-in (`doubles.FakePg`). These are tests of what each
sink decides to write, not of PostgreSQL.
"""

from __future__ import annotations

import json
import logging

import pytest

from doubles import RecordingBus
from xams_sc.model import iso, parse_iso, utcnow
from xams_sc.sinks.alarm_writer import AlarmWriter
from xams_sc.sinks.audit_writer import AuditWriter
from xams_sc.sinks.flow_writer import FlowPeriodWriter


def wired(cls, pg, *args, **kw):
    """A writer with its connection replaced and its subscription registered."""
    bus = RecordingBus()
    writer = cls(bus, "postgresql://nowhere", *args, **kw)
    writer._connect = lambda: pg
    writer.start()
    return writer, bus


# ===========================================================  the audit trail

class TestAuditWriter:
    def test_a_write_is_stored_with_its_fields(self, fake_pg):
        writer, _ = wired(AuditWriter, fake_pg)

        writer.handle(json.dumps({
            "t": "2026-09-21T09:00:00.000Z", "actor": "apc",
            "action": "vset", "target": "hv_cathode_vset",
            "old": "-4000.0", "new": "-4200.0", "result": "ok",
            "detail": "read back from the board"}))

        assert writer.written == 1
        (row,) = fake_pg.matching("INSERT INTO audit")
        assert row[1:] == ("apc", "vset", "hv_cathode_vset",
                           "-4000.0", "-4200.0", "ok",
                           "read back from the board")
        assert row[0].tzinfo is not None, "timestamps must stay aware"

    def test_a_refused_write_is_stored_too(self, fake_pg):
        """The module's own claim, and the reason it is worth a test.

        A log of only the successes answers "what did the system do" but not
        "what did somebody try to make it do", and the second question is the
        one asked after something has gone wrong.
        """
        writer, _ = wired(AuditWriter, fake_pg)

        writer.handle(json.dumps({
            "actor": "apc", "action": "vset", "target": "hv_cathode_vset",
            "new": "-9000.0", "result": "refused",
            "detail": "outside the permitted range"}))

        (row,) = fake_pg.matching("INSERT INTO audit")
        assert row[6] == "refused"
        assert writer.written == 1

    def test_a_missing_timestamp_is_stamped_on_arrival(self, fake_pg):
        writer, _ = wired(AuditWriter, fake_pg)
        before = utcnow()

        writer.handle(json.dumps({"actor": "apc", "action": "vset"}))

        (row,) = fake_pg.matching("INSERT INTO audit")
        assert before <= row[0] <= utcnow()

    def test_an_unreadable_timestamp_is_stamped_not_dropped(self, fake_pg, caplog):
        """Losing the fact that a write happened because its clock field was
        malformed would be the wrong trade, and the module says so."""
        writer, _ = wired(AuditWriter, fake_pg)

        with caplog.at_level(logging.WARNING):
            writer.handle(json.dumps({"t": "yesterday", "actor": "apc",
                                      "action": "vset"}))

        assert len(fake_pg.matching("INSERT INTO audit")) == 1
        assert "unreadable timestamp" in caplog.text

    def test_missing_fields_become_unknown_rather_than_null(self, fake_pg):
        writer, _ = wired(AuditWriter, fake_pg)

        writer.handle(json.dumps({"target": "hv_cathode_vset"}))

        (row,) = fake_pg.matching("INSERT INTO audit")
        assert row[1] == "unknown" and row[2] == "unknown" and row[6] == "unknown"

    def test_an_unparseable_record_is_warned_about_and_dropped(self, fake_pg, caplog):
        writer, _ = wired(AuditWriter, fake_pg)

        with caplog.at_level(logging.WARNING):
            writer.handle("{not json")

        assert fake_pg.calls == []
        assert "unparseable audit record" in caplog.text

    def test_a_database_failure_is_loud_and_does_not_raise(self, fake_pg, caplog):
        """This is the one sink whose failure means a write to an instrument
        happened with no durable record of it, so it logs at ERROR."""
        fake_pg.fail_with = RuntimeError("connection refused")
        writer, _ = wired(AuditWriter, fake_pg)

        with caplog.at_level(logging.ERROR):
            writer.handle(json.dumps({"actor": "apc", "action": "vset"}))

        assert writer.written == 0
        assert "COULD NOT STORE AN AUDIT RECORD" in caplog.text
        assert any(r.levelno >= logging.ERROR for r in caplog.records)

    def test_it_subscribes_to_the_audit_topic(self, fake_pg):
        writer, bus = wired(AuditWriter, fake_pg)

        assert bus.deliver("xams/audit", json.dumps(
            {"actor": "apc", "action": "vset"})) == 1
        assert writer.written == 1


# =========================================================  the alarm history

def alarm(state="major", threshold="high", value=2.1, **kw):
    return json.dumps({"state": state, "threshold": threshold,
                       "value": value, **kw})


class TestAlarmWriter:
    def test_a_transition_is_recorded(self, fake_pg):
        writer, _ = wired(AlarmWriter, fake_pg)

        writer.handle("pmain", alarm())

        (row,) = fake_pg.matching("INSERT INTO alarm_events")
        assert row[1:] == ("pmain", "major", "high", 2.1,
                           "pmain major on high")
        assert writer.written == 1

    def test_the_same_state_again_is_not_recorded(self, fake_pg):
        """The engine publishes retained state and a reconnect re-delivers it.
        Storing that again fills the table with rows saying the same thing."""
        writer, _ = wired(AlarmWriter, fake_pg)

        writer.handle("pmain", alarm())
        writer.handle("pmain", alarm())
        writer.handle("pmain", alarm())

        assert writer.written == 1

    def test_acknowledging_is_a_transition(self, fake_pg):
        writer, _ = wired(AlarmWriter, fake_pg)

        writer.handle("pmain", alarm())
        writer.handle("pmain", alarm(acknowledged=True))

        assert writer.written == 2
        assert "(acknowledged)" in fake_pg.rows[-1][-1]

    def test_returning_to_ok_is_a_transition(self, fake_pg):
        writer, _ = wired(AlarmWriter, fake_pg)

        writer.handle("pmain", alarm())
        writer.handle("pmain", alarm(state="ok", threshold=None))

        assert writer.written == 2
        assert fake_pg.rows[-1][2] == "ok"
        assert fake_pg.rows[-1][-1] == "pmain ok", "no threshold, no ' on '"

    def test_a_cleared_retained_topic_forgets_the_channel(self, fake_pg):
        """An empty payload is how MQTT deletes a topic, not a corrupt message.
        The next genuine alarm must read as a fresh transition, not as a
        repeat of the state that was cleared."""
        writer, _ = wired(AlarmWriter, fake_pg)
        writer.handle("pmain", alarm())

        writer.handle("pmain", "")
        writer.handle("pmain", alarm())

        assert writer.written == 2

    def test_the_since_field_is_used_as_the_timestamp(self, fake_pg):
        writer, _ = wired(AlarmWriter, fake_pg)
        when = utcnow()

        writer.handle("pmain", alarm(since=iso(when)))

        # Through `iso` and back, not `when` itself: the one timestamp format
        # is millisecond precision, so a round trip is lossy by design and
        # asserting on the original would be asserting on the wrong thing.
        assert fake_pg.rows[0][0] == parse_iso(iso(when))

    def test_an_unreadable_since_falls_back_to_now(self, fake_pg):
        writer, _ = wired(AlarmWriter, fake_pg)
        before = utcnow()

        writer.handle("pmain", alarm(since="the day before"))

        assert before <= fake_pg.rows[0][0] <= utcnow()

    def test_an_unparseable_payload_is_warned_about(self, fake_pg, caplog):
        writer, _ = wired(AlarmWriter, fake_pg)

        with caplog.at_level(logging.WARNING):
            writer.handle("pmain", "{nonsense")

        assert fake_pg.calls == []
        assert "unparseable alarm payload" in caplog.text

    def test_a_database_failure_does_not_interfere_with_the_alarm(self, fake_pg, caplog):
        fake_pg.fail_with = RuntimeError("connection refused")
        writer, _ = wired(AlarmWriter, fake_pg)

        with caplog.at_level(logging.WARNING):
            writer.handle("pmain", alarm())

        assert writer.written == 0
        assert "could not record alarm" in caplog.text

    def test_it_takes_the_channel_from_the_topic(self, fake_pg):
        writer, bus = wired(AlarmWriter, fake_pg)

        assert bus.deliver("xams/alarm/t_cryostat", alarm()) == 1
        assert fake_pg.rows[0][1] == "t_cryostat"


# ==========================================================  the flow periods

def period(total_g=4213.8, gaps_s=0.0, by="apc"):
    return json.dumps({"channel": "fm101_total",
                       "start": "2026-08-01T09:14:00.000Z",
                       "stop": "2026-09-16T11:02:00.000Z",
                       "total_g": total_g, "gaps_s": gaps_s, "reset_by": by})


class TestFlowPeriodWriter:
    def test_it_closes_the_open_period_then_opens_the_next(self, fake_pg):
        writer, _ = wired(FlowPeriodWriter, fake_pg)

        writer.handle(period())

        order = [sql.split()[0] for sql, _ in fake_pg.calls]
        assert order == ["UPDATE", "INSERT", "INSERT"], \
            "close before open, or the new period is the one that gets closed"
        assert writer.written == 1

    def test_the_total_carries_its_gaps(self, fake_pg):
        """A period that ran with an outage in it is an underestimate, and the
        total must carry that evidence rather than requiring somebody to
        reconstruct it from service logs months later."""
        writer, _ = wired(FlowPeriodWriter, fake_pg)

        writer.handle(period(total_g=118.4, gaps_s=7200.0))

        (closed,) = fake_pg.matching("UPDATE flow_periods")
        assert closed[1] == 118.4 and closed[2] == 7200.0
        assert closed[3] == "apc"

    def test_the_reset_is_audited_with_the_gaps_in_the_detail(self, fake_pg):
        writer, _ = wired(FlowPeriodWriter, fake_pg)

        writer.handle(period(total_g=118.4, gaps_s=7200.0))

        (audit,) = fake_pg.matching("INSERT INTO audit")
        assert audit[1] == "apc" and audit[2] == "fm101_total"
        assert "118.400" in audit[4] and "7200s of gaps" in audit[4]

    def test_a_first_ever_reset_still_opens_a_period(self, fake_pg):
        """No period open means the UPDATE affects no rows. The INSERT must
        still run, or there is nothing for the next reset to close."""
        fake_pg.rowcount = 0
        writer, _ = wired(FlowPeriodWriter, fake_pg)

        writer.handle(period())

        assert len(fake_pg.matching("INSERT INTO flow_periods")) == 1
        assert writer.written == 1

    def test_an_unparseable_period_is_warned_about(self, fake_pg, caplog):
        writer, _ = wired(FlowPeriodWriter, fake_pg)

        with caplog.at_level(logging.WARNING):
            writer.handle("{nonsense")

        assert fake_pg.calls == []
        assert "unparseable flow period" in caplog.text

    def test_a_database_failure_does_not_unmake_the_reset(self, fake_pg, caplog):
        """The integrator's own state file is the authority and the reset has
        already happened. Failing here must not make it look as though it
        did not."""
        fake_pg.fail_with = RuntimeError("connection refused")
        writer, _ = wired(FlowPeriodWriter, fake_pg)

        with caplog.at_level(logging.ERROR):
            writer.handle(period())

        assert writer.written == 0
        assert "could not record the closed flow period" in caplog.text

    def test_ensure_open_period_opens_one_when_none_is_open(self, fake_pg):
        writer, _ = wired(FlowPeriodWriter, fake_pg)

        writer.ensure_open_period("fm101_total", utcnow())

        assert len(fake_pg.matching("INSERT INTO flow_periods")) == 1

    def test_ensure_open_period_leaves_an_existing_one_alone(self, fake_pg):
        fake_pg._fetch = [(1,)]
        writer, _ = wired(FlowPeriodWriter, fake_pg)

        writer.ensure_open_period("fm101_total", utcnow())

        assert fake_pg.matching("INSERT INTO flow_periods") == []

    def test_it_subscribes_to_the_flow_period_topic(self, fake_pg):
        writer, bus = wired(FlowPeriodWriter, fake_pg)

        assert bus.deliver("xams/flow/period", period()) == 1
        assert writer.written == 1


# ======================================================  what all three share

# The three sinks are the same shape, and the shape is the contract: hold one
# connection, drop it when a write fails so the next one reconnects, and close
# cleanly whether or not there was ever a connection to close. Asserted once,
# for all of them, rather than three times in three dialects.
WRITERS = [
    pytest.param(AuditWriter, lambda w: w.handle(json.dumps(
        {"actor": "apc", "action": "vset"})), id="audit"),
    pytest.param(AlarmWriter, lambda w: w.handle("pmain", alarm()), id="alarm"),
    pytest.param(FlowPeriodWriter, lambda w: w.handle(period()), id="flow"),
]


@pytest.mark.parametrize("cls, write", WRITERS)
class TestEverySinkShares:
    def test_a_failed_write_drops_the_connection_so_the_next_reconnects(
            self, cls, write, fake_pg):
        """Holding a dead handle means every later write fails too, and the
        outage outlives the thing that caused it."""
        writer, _ = wired(cls, fake_pg)
        writer._conn = fake_pg
        fake_pg.fail_with = RuntimeError("connection refused")

        write(writer)

        assert fake_pg.closed, "the dead connection was not closed"
        assert writer._conn is None, "a dead handle was kept"

    def test_close_closes_the_connection(self, cls, write, fake_pg):
        writer, _ = wired(cls, fake_pg)
        writer._conn = fake_pg

        writer.close()

        assert fake_pg.closed

    def test_close_without_a_connection_is_harmless(self, cls, write, fake_pg):
        writer, _ = wired(cls, fake_pg)

        writer.close()          # never connected; must not raise

    def test_close_survives_a_connection_that_raises(self, cls, write, fake_pg):
        """Shutdown must not be the thing that fails loudest."""
        class Hostile:
            closed = False

            def close(self):
                raise RuntimeError("broken pipe")

        writer, _ = wired(cls, fake_pg)
        writer._conn = Hostile()

        writer.close()          # must not raise
