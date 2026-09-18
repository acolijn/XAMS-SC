# Writing a new driver

This is the page that decides whether the system outlives its author, so it
is a worked example rather than a description. Suppose a vacuum gauge arrives
on USB, speaking ASCII over a virtual COM port, and it should appear as
`pvac` in bar.

Read [How a driver works](index.md) first — the rules below are that page's
rules, applied.

---

## 1. Declare the channels

Nothing starts in Python. Add to `config/channels.yaml`:

```yaml
  - name: pvac
    device: vacgauge
    phys: "1"
    kind: voltage
    unit: bar
    offset: 0.0
    multiplier: 1.0
    description: turbo pump inlet pressure
```

and to `config/devices.yaml`, how the instrument is found and how it proves
it is the right one:

```yaml
vacgauge:
  match: {vid: "0403", pid: "6001", serial: "FT9ABCDE"}
  idn_contains: "TPG362"
  baud: 9600
```

`xams-ctl check` validates both without starting anything. A channel name is
permanent from the moment data is archived under it, so choose it as
carefully as you would a column name you cannot rename.

---

## 2. Subclass the service

`src/xams_sc/devices/vacgauge.py`. The two required methods, and nothing else
to begin with:

```python
"""Pfeiffer TPG 362 vacuum gauge. See DESIGN.md §7.x.

Read-only. Two things worth knowing before changing anything here: …
"""

from ..model import Measurement, Quality, utcnow
from ..scaling import apply
from ..service import BaseService


class VacGaugeService(BaseService):
    name = "vacgauge"

    def verify_identity(self) -> bool:
        """Ask the gauge who it is. False refuses to start (§6.2)."""
        if self.simulate:
            return True
        ...                       # resolve the port, send *IDN?, compare
        return True

    def read(self) -> list[Measurement]:
        """One acquisition cycle. Raise on failure — the loop backs off."""
        now = utcnow()
        out = []
        for ch in self.config.channels_for(self.name):
            raw = self._ask(ch.phys)          # may raise
            out.append(Measurement(
                t=now, channel=ch.name,
                value=apply(raw, ch.offset, ch.multiplier),
                unit=ch.unit, raw=raw, quality=Quality.OK))
        return out

    def close(self) -> None:
        """Release the port so LabVIEW can have it back."""
```

**Things the base class already does, so do not write them again:** the read
loop, the 1 s/10 s split, the averaging, the heartbeat, the state topic, the
single-instance lock, the exponential backoff, the `quality=error` publish on
failure, signal handling and the reload subscription.

**Four things the driver itself must get right:**

1. **Timestamp at the read**, never at publish. `utcnow()` at the top of
   `read()`, applied to every measurement in that cycle.
2. **Raise on failure.** Do not return the last good value, do not return an
   empty list — the loop needs the exception to mark the channels
   `quality=error` and back off.
3. **Publish the absence of a measurement as an absence.** If the instrument
   returns a sentinel or a rail value for "no sensor", check for it and
   publish `value=None, quality=ERROR` — see the RTD range check in
   [cDAQ](cdaq.md#what-a-module-fault-looks-like-from-the-outside).
4. **`channels_for(self.name)`**, never a hard-coded channel list. The map is
   configuration.

Implement `reconnect()` for anything on a serial port, and make it
**re-verify identity** rather than merely reopen — a reconnect after a USB
glitch is exactly where a swapped cable would slip through.

---

## 3. Give it a simulation mode

Not optional. Without it the driver can only be developed on the lab PC,
which is still running LabVIEW.

```python
    def read(self) -> list[Measurement]:
        if self.simulate:
            return self._read_simulated()
        ...
```

Simulated readings carry **`src="sim"`** and should wobble rather than sit
flat, so that nobody reads physics out of them. The provenance field exists
because synthetic values once reached the production database and could not
afterwards be told from measurements.

---

## 4. Register it

One line in `src/xams_sc/devices/__main__.py`:

```python
SERVICES = {
    ...
    "vacgauge": ("xams_sc.devices.vacgauge", "VacGaugeService"),
}
```

Then it runs:

```powershell
.\.venv\Scripts\python.exe -m xams_sc.devices vacgauge --simulate --log-level DEBUG
```

To have `xams-ctl` start and stop it with the rest, add its name to
`SERVICES` in `src/xams_sc/cli/xams_ctl.py` — **in dependency order**, since
services start in that order and stop in reverse.

---

## 5. Write the test

Tests here run **without hardware** and are aimed at the things that are easy
to get wrong and expensive to get wrong — not at line coverage. The cDAQ
tests are the model: excitation current, and the difference between a cold
sensor and no sensor.

`tests/test_vacgauge.py`:

```python
class RecordingBus:
    """Collects what the service publishes."""
    def __init__(self): self.measurements = []
    def publish_measurement(self, m): self.measurements.append(m)
    def publish_state(self, s, st): pass
    def publish_heartbeat(self, s): pass
    def subscribe(self, t, h): pass
    def connect(self): pass
    def disconnect(self): pass


class TestABadReadIsNotANumber:
    def test_a_sentinel_is_published_as_error(self, service):
        ...
        assert m.quality is Quality.ERROR
        assert m.value is None
```

Worth a test for each of: an unreadable instrument does not produce a
plausible number; identity mismatch refuses to start; every configured
channel is actually read; and any unit conversion that could be out by a
factor.

---

## 6. Document it

A page in `docs/drivers/`, added to the nav in `mkdocs.yml`. What belongs on
it, judging by the pages that have earned their keep:

- the wiring or connection facts that are **not** in the code — slot maps,
  serial settings, which inputs are unused;
- every route that was tried and **did not work**, so nobody retries it (see
  [UPS](ups.md#how-it-reads-and-why-this-way));
- what a fault looks like *from the outside*, as a table;
- what the driver deliberately **cannot** do.

---

## The checklist

| | |
|---|---|
| channels in `channels.yaml`, device in `devices.yaml` | `xams-ctl check` passes |
| `verify_identity()` refuses rather than guesses | |
| `read()` raises on failure and timestamps at the read | |
| an unreadable value is `None` + `quality=error` | never a plausible number |
| simulation mode, tagged `src="sim"` | |
| `close()` releases the hardware | LabVIEW can have it back |
| `reconnect()` re-verifies identity | serial devices |
| registered in `devices/__main__.py` | and in `xams-ctl` if it should autostart |
| tests that run without hardware | |
| a page in `docs/drivers/` | |

If the driver needs to **write** to its instrument, stop and read §10 and
§10a of [the design specification](../DESIGN.md) first. Every write path in
this system validates against `channels.yaml`, reads back before claiming
success, acknowledges on the bus and audits — including refusals — and none
of that is optional.
