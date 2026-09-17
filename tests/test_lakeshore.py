"""Lake Shore 335 parsing. No hardware required.

Responses recorded from the instrument on 17 September 2026, firmware 1.2.
"""

import pytest

from xams_sc.devices.lakeshore import LakeShore, describe_reading_status


class FakePort:
    """Stands in for pyserial, replaying a fixed answer."""

    def __init__(self, answer: str | bytes):
        self.answer = answer.encode() if isinstance(answer, str) else answer
        self.written = []

    def reset_input_buffer(self):
        pass

    def write(self, data):
        self.written.append(data)

    def read_until(self, *_a, **_kw):
        return self.answer


def device_with(answer):
    d = LakeShore("COM_TEST")
    d._serial = FakePort(answer)
    return d


class TestIdentity:
    def test_recorded_idn_response(self):
        """The real reply. The third field is instrument/option-card serial,
        and with no option card fitted the second half is '#######'."""
        d = device_with("LSCI,MODEL335,335A12T/#######,1.2\r\n")
        assert d.identity() == ("MODEL335", "335A12T")

    def test_option_card_serial_is_not_part_of_the_identity(self):
        d = device_with("LSCI,MODEL335,335A12T/9988776,1.2\r\n")
        assert d.identity() == ("MODEL335", "335A12T")

    def test_serial_matches_the_usb_descriptor(self):
        """The USB descriptor reports 335A12T. The two must agree, or the
        instrument is verified on one count and not the other."""
        d = device_with("LSCI,MODEL335,335A12T/#######,1.2\r\n")
        _, serial_num = d.identity()
        assert serial_num == "335A12T"

    @pytest.mark.parametrize("answer", [
        "", "   \r\n", "LSCI\r\n", "LSCI,MODEL335\r\n",
        "LSCI,,335A12T,1.2\r\n",        # empty model
        "LSCI,MODEL335,/#######,1.2\r\n",  # empty instrument serial
        "garbage\r\n",
    ])
    def test_malformed_idn_returns_none(self, answer):
        """Unidentified, never "probably the right one" (§6.2 rule 3)."""
        assert device_with(answer).identity() is None


class TestReadings:
    def test_celsius_reading(self):
        assert device_with("-89.998\r\n").number("CRDG? A") == pytest.approx(-89.998)

    def test_kelvin_and_celsius_describe_the_same_temperature(self):
        """Recorded together: CRDG? A = -89.998, KRDG? A = +183.15.

        This is what settles the unit question. 183.15 K is -90.00 C, so the
        channel that ranges to -90 is Celsius — and there is no negative
        Kelvin, which is how the mislabelling was spotted in the imported
        LabVIEW history.
        """
        celsius = device_with("-89.998\r\n").number("CRDG? A")
        kelvin = device_with("+183.15\r\n").number("KRDG? A")
        assert kelvin - 273.15 == pytest.approx(celsius, abs=0.01)

    def test_heater_percentage(self):
        assert device_with("+051.1\r\n").number("HTR? 1") == pytest.approx(51.1)

    def test_pid_query_takes_the_first_field(self):
        d = device_with("+0100.0,+0020.0,+000.0\r\n")
        assert d.number("PID? 1") == pytest.approx(100.0)

    def test_recorded_pid_matches_the_design(self):
        """§7.3 records P = 100, I = 20, D = 0. The instrument agrees."""
        text = device_with("+0100.0,+0020.0,+000.0\r\n").query("PID? 1")
        p, i, dterm = [float(x) for x in text.split(",")]
        assert (p, i, dterm) == (100.0, 20.0, 0.0)

    @pytest.mark.parametrize("answer", ["", "   ", "abc\r\n"])
    def test_unparseable_reading_is_none(self, answer):
        assert device_with(answer).number("CRDG? A") is None


class TestReadingStatus:
    def test_zero_is_a_good_reading(self):
        assert describe_reading_status(0) == "ok"

    def test_recorded_good_status(self):
        # RDGST? A returned '000' on both inputs.
        assert describe_reading_status(int("000")) == "ok"

    def test_invalid_reading_flagged(self):
        assert "invalid reading" in describe_reading_status(1 << 0)

    def test_underrange_flagged(self):
        assert "temp underrange" in describe_reading_status(1 << 4)

    def test_a_flagged_reading_is_not_a_temperature(self):
        """Any non-zero status means the number must not be stored as data:
        a disconnected sensor reporting zero is the frozen-plausible-value
        failure principle 4 forbids."""
        for bit in (0, 4, 5, 6, 7):
            assert describe_reading_status(1 << bit) != "ok"


class TestReadOnly:
    """Milestone 5 is read-only. Setpoint control is milestone 8, and only
    after §10's open decision about the heater shut-off is resolved.

    This inspects the command strings the module actually sends, rather than
    grepping the source — an earlier version of this test searched for "PID "
    and matched the phrase "the PID settings" in a docstring, which is the
    kind of test that fails for the wrong reason and passes for the wrong
    reason too.
    """

    @staticmethod
    def _commands_sent():
        """Every literal passed to .query() or .number() in the module."""
        import ast
        import inspect

        from xams_sc.devices import lakeshore

        tree = ast.parse(inspect.getsource(lakeshore))
        found = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr not in ("query", "number"):
                continue
            if not node.args:
                continue
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                found.append(arg.value)
            elif isinstance(arg, ast.JoinedStr):       # an f-string
                literal = "".join(v.value for v in arg.values
                                  if isinstance(v, ast.Constant))
                found.append(literal)
        return found

    def test_the_module_sends_some_commands(self):
        # Guard against the inspection silently finding nothing, which would
        # make the real test below vacuously pass.
        assert self._commands_sent(), "no commands found — inspection is broken"

    def test_every_command_is_a_query(self):
        for command in self._commands_sent():
            assert "?" in command, (
                f"lakeshore.py sends {command!r}, which is not a query. "
                f"Only queries belong here until milestone 8.")


class TestHeaterPower:
    """Percent of full scale to watts (§ the derive block in channels.yaml).

    Quadratic, so it cannot be an offset and a multiplier. The percentage
    stays the instrument's own number and this is derived from it.
    """

    def test_recorded_working_point(self):
        """51.1% was the real output on 17 September 2026."""
        from xams_sc.scaling import heater_power
        assert heater_power(51.1, 45.4, 31.2) == pytest.approx(17.25, abs=0.01)

    def test_zero_percent_is_zero_watts(self):
        from xams_sc.scaling import heater_power
        assert heater_power(0.0, 45.4, 31.2) == 0.0

    def test_full_scale(self):
        from xams_sc.scaling import heater_power
        assert heater_power(100.0, 45.4, 31.2) == pytest.approx(66.06, abs=0.01)

    def test_it_is_quadratic_not_linear(self):
        """Doubling the percentage quadruples the power. A linear scaling
        would give twice — which is why this cannot be expressed as a
        multiplier."""
        from xams_sc.scaling import heater_power
        at_25 = heater_power(25.0, 45.4, 31.2)
        at_50 = heater_power(50.0, 45.4, 31.2)
        assert at_50 == pytest.approx(4 * at_25)

    def test_transform_is_registered_by_name(self):
        from xams_sc.scaling import TRANSFORMS
        assert "heater_power" in TRANSFORMS

    def test_config_declares_the_derived_channels(self):
        from xams_sc.config import load
        cfg = load()
        for name, source in (("ls_heater_1_w", "ls_heater_1"),
                             ("ls_heater_2_w", "ls_heater_2")):
            ch = cfg.channels[name]
            assert ch.unit == "W"
            assert ch.derive["from"] == source
            assert ch.derive["transform"] == "heater_power"
            assert ch.derive["full_scale_v"] == 45.4
            assert ch.derive["resistance_ohm"] == 31.2

    def test_the_percentage_channel_is_kept(self):
        """The instrument's own number must survive alongside the derived
        one — everything else can be recomputed from it."""
        from xams_sc.config import load
        cfg = load()
        assert cfg.channels["ls_heater_1"].unit == "percent"
        assert cfg.channels["ls_heater_1"].derive is None

    def test_an_unknown_transform_is_refused(self, tmp_path):
        import textwrap

        from xams_sc.config import ConfigError, load
        (tmp_path / "devices.yaml").write_text("lakeshore: {}\n", encoding="utf-8")
        (tmp_path / "alarms.yaml").write_text("defaults: {}\nchannels: {}\n",
                                              encoding="utf-8")
        (tmp_path / "channels.yaml").write_text(textwrap.dedent("""
            channels:
              - {name: x_w, device: lakeshore, phys: "1", kind: power, unit: W,
                 derive: {from: x, transform: no_such_transform}}
        """), encoding="utf-8")
        with pytest.raises(ConfigError, match="unknown transform"):
            load(tmp_path)


class TestDerivedQuality:
    def test_a_failed_source_gives_no_wattage(self):
        """If the percentage could not be read, the wattage is unknown — not
        zero. A derived value inherits its source's quality."""
        from xams_sc.config import load
        from xams_sc.devices.lakeshore import LakeShoreService
        from xams_sc.model import Measurement, Quality, utcnow

        class NullBus:
            connected = True
            def publish_measurement(self, m): pass
            def publish_state(self, s, st): pass
            def publish_heartbeat(self, s): pass
            def subscribe(self, t, h): pass
            def connect(self): pass
            def disconnect(self): pass

        svc = LakeShoreService(load(), NullBus(), simulate=True)
        now = utcnow()
        failed = [Measurement(t=now, channel="ls_heater_1", value=None,
                              unit="percent", quality=Quality.ERROR)]
        derived = svc._derive_from(failed, now)
        watts = [m for m in derived if m.channel == "ls_heater_1_w"]
        assert len(watts) == 1
        assert watts[0].value is None
        assert watts[0].quality is Quality.ERROR

    def test_derived_shares_its_source_timestamp(self):
        """A phantom lag between a value and the thing it was computed from
        is the kind of artefact that gets mistaken for physics."""
        from xams_sc.config import load
        from xams_sc.devices.lakeshore import LakeShoreService
        from xams_sc.model import Measurement, Quality, utcnow

        class NullBus:
            connected = True
            def publish_measurement(self, m): pass
            def publish_state(self, s, st): pass
            def publish_heartbeat(self, s): pass
            def subscribe(self, t, h): pass
            def connect(self): pass
            def disconnect(self): pass

        svc = LakeShoreService(load(), NullBus(), simulate=True)
        now = utcnow()
        good = [Measurement(t=now, channel="ls_heater_1", value=51.1,
                            unit="percent", quality=Quality.OK)]
        derived = svc._derive_from(good, now)
        watts = next(m for m in derived if m.channel == "ls_heater_1_w")
        assert watts.t == now
        assert watts.value == pytest.approx(17.25, abs=0.01)
