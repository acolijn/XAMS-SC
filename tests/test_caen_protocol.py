"""CAEN protocol parsing, against recorded responses. See DESIGN.md §13.

Every string below was captured from the real supplies on 17 September 2026 —
serial 19198 (firmware 1.08) and serial 79 (firmware 1.04). The two run
different firmware, so both are represented: a divergence should fail here
rather than in the lab.

The malformed cases matter more than the well-formed ones. §7.2 requires that
"a malformed or absent reply is a read error, never a silently substituted
value", and a parser that returns 0.0 for a truncated response would put a
plausible zero on an electrode monitor.
"""

import pytest

from xams_sc.devices.caen import STAT_BITS, _decode, describe_status
from xams_sc.scaling import apply_sign


class TestDecode:
    """Recorded replies from both units."""

    @pytest.mark.parametrize("response,expected", [
        ("#BD:00,CMD:OK,VAL:DT1470ET", "DT1470ET"),
        ("#BD:00,CMD:OK,VAL:19198", "19198"),       # firmware 1.08 unit
        ("#BD:00,CMD:OK,VAL:79", "79"),             # firmware 1.04 unit
        ("#BD:00,CMD:OK,VAL:1.08", "1.08"),
        ("#BD:00,CMD:OK,VAL:4", "4"),
        ("#BD:00,CMD:OK,VAL:0700.0", "0700.0"),
        ("#BD:00,CMD:OK,VAL:0000.0", "0000.0"),
        ("#BD:00,CMD:OK,VAL:000.000", "000.000"),
        ("#BD:00,CMD:OK,VAL:01024", "01024"),
        ("#BD:00,CMD:OK,VAL:-", "-"),               # POL, negative
        ("#BD:00,CMD:OK,VAL:+", "+"),               # POL, positive
    ])
    def test_well_formed(self, response, expected):
        assert _decode(response) == expected

    @pytest.mark.parametrize("response", [
        "",                                  # nothing came back
        "   ",
        "#BD:00,CMD:ERR",                    # the board rejected the command
        "#BD:00,CMD:OK",                     # OK but no value
        "#BD:00,CMD:OK,VAL:",                # empty value
        "BD:00,CMD:OK,VAL:19198",            # missing leading #
        "#BD:00,CMD:OK,VAL",                 # truncated mid-key
        "#BD:00,CM",                         # truncated early
        "garbage",
        "\x00\xff\x13",                      # line noise
    ])
    def test_malformed_returns_none(self, response):
        """None, never a substituted value. This is the whole point."""
        assert _decode(response) is None

    def test_trailing_whitespace_and_newlines(self):
        assert _decode("#BD:00,CMD:OK,VAL:19198\r\n") == "19198"
        assert _decode("  #BD:00,CMD:OK,VAL:79  ") == "79"

    def test_a_truncated_number_is_not_silently_zero(self):
        # The dangerous failure: something that looks parseable but is not.
        assert _decode("#BD:00,CMD:ERR,VAL:0000.0") is None


class TestStatusWord:
    def test_all_channels_off_reads_1024(self):
        """Every channel on both units read 01024 with outputs off."""
        assert describe_status(1024) == "DISABLED"

    def test_zero_is_off(self):
        assert describe_status(0) == "OFF"

    def test_on_and_ramping(self):
        assert describe_status(0b011) == "ON,RAMP_UP"

    def test_trip_is_reported(self):
        assert "TRIP" in describe_status(1 << 7)

    def test_interlock_is_reported(self):
        assert "INTERLOCK" in describe_status(1 << 12)

    def test_every_bit_has_a_name(self):
        for bit in range(14):
            assert bit in STAT_BITS, f"status bit {bit} unnamed"

    def test_combined_flags(self):
        word = (1 << 0) | (1 << 7) | (1 << 10)
        described = describe_status(word)
        assert "ON" in described and "TRIP" in described and "DISABLED" in described


class TestSignConvention:
    """The supplies report unsigned magnitudes with POL separate (§7.2).

    Recorded VSET values, 17 September 2026:
      hv_1: 700.0, 1000.0, 500.0, 600.0     all POL '-'
      hv_2: 2250.0, 1750.0 POL '-'; 4200.0, 600.0 POL '+'
    """

    @pytest.mark.parametrize("magnitude,sign,expected", [
        (700.0, -1, -700.0),      # PMT bottom
        (1000.0, -1, -1000.0),    # PMT top
        (2250.0, -1, -2250.0),    # cathode
        (1750.0, -1, -1750.0),    # gate
        (4200.0, 1, 4200.0),      # anode
        (600.0, 1, 600.0),        # NaI
        (0.0, -1, 0.0),           # off
    ])
    def test_recorded_setpoints(self, magnitude, sign, expected):
        assert apply_sign(magnitude, sign) == pytest.approx(expected)

    def test_cathode_and_anode_end_up_opposite(self):
        """The reason the convention exists: these must not plot together
        as if both were positive."""
        cathode = apply_sign(2250.0, -1)
        anode = apply_sign(4200.0, 1)
        assert cathode < 0 < anode


class TestOnlyVsetIsEverWritten:
    """This class used to assert that `CMD:SET` appeared nowhere at all.

    That was correct while the driver was read-only, and stopped being correct
    on 18 September 2026 when §10a's write path was added. It is narrowed
    rather than deleted, because the part worth keeping was never "no writes"
    — it was **which** writes, and the answer is still almost none.
    """

    # Exactly three parameters may be written, and the list is short on
    # purpose. Widening it is a decision, and this test is where that decision
    # has to be made explicitly rather than arrived at.
    WRITABLE = ("PAR:VSET", "PAR:{par}")   # {par} is the ON/OFF formatter

    def test_only_the_allowed_parameters_are_written(self):
        import inspect

        from xams_sc.devices import caen
        written = [line.strip() for line in inspect.getsource(caen).splitlines()
                   if "CMD:SET" in line]

        assert written, "no write path found; §10a should have one"
        for line in written:
            assert any(p in line for p in self.WRITABLE), (
                "caen.py writes a parameter outside the allowed set: %s\n"
                "MAXV, RUP, RDW, TRIP and ISET are protection settings and "
                "live on the instrument (§10 rule 2)." % line)

    def test_protection_settings_are_never_written(self):
        """§10 rule 2. These stay configured on the instrument, and that is
        what keeps this software out of the protection path."""
        import inspect

        from xams_sc.devices import caen
        for line in inspect.getsource(caen).splitlines():
            if "CMD:SET" not in line:
                continue
            for forbidden in ("MAXV", "RUP", "RDW", "TRIP", "ISET", "BDCTR"):
                assert forbidden not in line, (
                    "caen.py writes %s: %s" % (forbidden, line.strip()))

    def test_the_enable_switch_stays_out_of_reach(self):
        """`ON`/`OFF` energise a channel that is already enabled. Nothing here
        can clear the `DISABLED` bit — that is the front-panel switch, and it
        is the last gate between a bug and an electrode (§10a).

        There is no CAEN command that clears `DISABLED`, so this asserts the
        distinction is still *described* correctly: if somebody ever wires the
        enable to a command, the docstring will have to change first.
        """
        import inspect

        from xams_sc.devices import caen
        doc = inspect.getdoc(caen) or ""

        assert "enable switch" in doc.lower()
        assert "hand operation" in doc.lower()

    def test_command_builder_only_emits_mon(self):
        from xams_sc.devices.caen import CaenChannelReader
        import inspect
        source = inspect.getsource(CaenChannelReader._command)
        assert "CMD:MON" in source
        assert "CMD:SET" not in source


class TestTalk:
    """`_talk` itself, which until 20 September 2026 had no test at all.

    Everything else in this file tests the parsing of a reply that has
    already arrived. `_talk` is what decides WHICH reply that is — and since
    a DT1470ET answer carries no `PAR` and no `CH`, that decision rests
    entirely on the exchange staying in step. It is the most load-bearing
    function in the driver and it was the least covered.
    """

    class FakePort:
        """Just enough serial.Serial, with a settable backlog."""

        def __init__(self, replies, waiting=0, raises_in_waiting=False):
            self.replies = list(replies)
            self.written = []
            self.flushed = 0
            self._waiting = waiting
            self._raises = raises_in_waiting

        @property
        def in_waiting(self):
            if self._raises:
                raise OSError("port is going away")
            return self._waiting

        def reset_input_buffer(self):
            self.flushed += 1

        def write(self, data):
            self.written.append(data)

        def read_until(self, *_a, **_kw):
            return self.replies.pop(0) if self.replies else b""

        def read(self, *_a, **_kw):
            return self.replies.pop(0) if self.replies else b""

    def reader(self, port):
        from xams_sc.devices.caen import CaenChannelReader
        r = CaenChannelReader("hv_1", "COM9", 0, 9600, "DT1470ET", "19198")
        r._serial = port
        return r

    def test_returns_the_reply(self, monkeypatch):
        monkeypatch.setattr("time.sleep", lambda _s: None)
        port = self.FakePort([b"#BD:00,CMD:OK,VAL:0700.0\r\n"])
        assert _decode(self.reader(port)._talk("$BD:0,CMD:MON,PAR:VSET,CH:0")) \
            == "0700.0"
        assert port.written == [b"$BD:0,CMD:MON,PAR:VSET,CH:0\r\n"]

    def test_a_backlog_never_costs_the_reading(self, monkeypatch):
        """THE POINT OF THE DIAGNOSTIC: it observes, it does not interfere.

        Bytes already waiting mean the stream has slipped, and that is worth
        a warning — but the exchange underneath must still return its value.
        """
        monkeypatch.setattr("time.sleep", lambda _s: None)
        port = self.FakePort([b"#BD:00,CMD:OK,VAL:0135.0\r\n"], waiting=26)
        r = self.reader(port)
        assert _decode(r._talk("$BD:0,CMD:MON,PAR:VSET,CH:0")) == "0135.0"
        assert r._stale_events == 1

    def test_in_waiting_may_fail_without_losing_the_reading(self, monkeypatch):
        """A diagnostic that can break acquisition is worse than none.

        `in_waiting` talks to the driver and can raise on a port that is on
        its way out — which is precisely when the reading still matters.
        """
        monkeypatch.setattr("time.sleep", lambda _s: None)
        port = self.FakePort([b"#BD:00,CMD:OK,VAL:0700.0\r\n"],
                             raises_in_waiting=True)
        r = self.reader(port)
        assert _decode(r._talk("$BD:0,CMD:MON,PAR:VSET,CH:0")) == "0700.0"
        assert r._stale_events == 0

    def test_a_silent_port_is_a_read_failure(self, monkeypatch):
        """Never a substituted value (§7.2) — after the one short retry."""
        monkeypatch.setattr("time.sleep", lambda _s: None)
        port = self.FakePort([])
        assert _decode(self.reader(port)._talk("$BD:0,CMD:MON,PAR:VSET,CH:0")) \
            is None

    def test_the_retry_is_used_when_the_first_read_is_empty(self, monkeypatch):
        monkeypatch.setattr("time.sleep", lambda _s: None)
        port = self.FakePort([b"", b"#BD:00,CMD:OK,VAL:0200.0\r\n"])
        assert _decode(self.reader(port)._talk("$BD:0,CMD:MON,PAR:VSET,CH:0")) \
            == "0200.0"

    def test_the_stale_warning_is_rate_limited(self, monkeypatch):
        """A link that has slipped does it many times a second."""
        monkeypatch.setattr("time.sleep", lambda _s: None)
        warnings = []
        monkeypatch.setattr("xams_sc.devices.caen.log.warning",
                            lambda *a, **kw: warnings.append(a))
        port = self.FakePort([b"#BD:00,CMD:OK,VAL:1\r\n"] * 5, waiting=26)
        r = self.reader(port)
        for _ in range(5):
            r._talk("$BD:0,CMD:MON,PAR:VSET,CH:0")
        assert r._stale_events == 5
        assert len(warnings) == 1
