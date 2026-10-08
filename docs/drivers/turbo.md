# Turbo pump (HiPace 80)

Pfeiffer HiPace 80 with TC 110 electronics, in a HiCube 80 Eco pumping
station. Read by **listening** to the RS-485 link between the pump and the
station's DCU. Connected and checked on 8 October 2026.

**This driver never transmits** — not a command, not a query, not a line
break. The DCU is the master on that bus and polls the pump about twice a
second; a second master would collide with it, and a valid Pfeiffer telegram
from this PC is a command the pump obeys (parameter 010 switches the pumping
station). The port wrapper in `devices/turbo.py` has no `write` method at
all, and RTS/DTR are held low before the port opens.

---

## The hardware

| | |
|---|---|
| Tap | Pfeiffer M12 Y-piece in the pump ↔ DCU cable; M12 pigtail pins 1 (D+, brown), 3 (GND, blue), 4 (D−, black); pins 2 (+24 V) and 5 cut and insulated |
| Adapter | EXSYS EX-13009, FTDI `0403:6001`, USB serial `DIBIURQ3` (pyserial reports `DIBIURQ3A`), ~25 kΩ between T/R+ and T/R−, no termination |
| Serial | 9600 8N1 |
| Pump address | `001` |

The installation guide with the wiring is in the workspace,
`reference/HiPace80/Pfeiffer_HiPace80_RS485_sniffer_installation.pdf`.

---

## The channels

`phys` is the Pfeiffer parameter number.

| Channel | Param | | |
|---|---|---|---|
| `turbo_on` | 010 | pumping station on | `bool` |
| `turbo_at_speed` | 306 | set speed reached | `bool` |
| `turbo_accelerating` | 307 | accelerating | `bool` |
| `turbo_error` | 303 | error / warning number, 0 = none | `code`, not averaged; text in `turbo.log` |
| `turbo_overtemp_elec` | 304 | electronics over temperature | `bool` |
| `turbo_overtemp_pump` | 305 | pump over temperature | `bool` |
| `turbo_speed` | 309 | rotation speed | Hz |

A channel can only be added for a parameter in `PARAMS` in `turbo.py`; any
other number is refused at startup rather than decoded by guess.

---

## Only what the DCU asks for

Listening means the driver sees exactly what the DCU polls, and nothing else.
Checked on 8 October 2026:

| DCU setting | On the bus |
|---|---|
| always | 001, 002, 010, 300, 302, 303, 304, 305, 306, 307 |
| display on a pump parameter (e.g. 309) | that parameter as well |
| service line, DCU parameter **795**, on a pump parameter | that parameter as well |
| display on 340 (pressure) | nothing extra — 340 is the DCU's own |

**So the speed needs the DCU service line on 309** (795 = 309, set on
8 October 2026). Somebody who changes it takes `turbo_speed` off the bus; the
channel is then published as `quality=error` (and alarms), never left at its
last value, and `turbo.log` says which DCU setting to restore.

**The vacuum pressure is not on this bus.** The PKR gauge is plugged into the
DCU (X3), which reads it itself. It is read through the gauge's analog output
on the NI 9207 instead; see
`reference/HiPace80/PKR_gauge_analog_tap_guide.pdf` in the workspace.

---

## Identity

The pump cannot be asked who it is — that would be a transmission — and the
DCU never polls its name. Identity is therefore narrower than the `*IDN?` of
the other serial drivers ([§6.2](../DESIGN.md)):

1. the adapter is matched on VID/PID **and** USB serial — an FTDI VID/PID
   alone is shared by countless adapters;
2. the service starts only once a valid, checksummed reply from the
   configured pump address has been heard on it (`identify_s`, 5 s).

Every telegram is checked — field digits, length byte, checksum — because a
listener joining mid-stream sees fragments, and a fragment decoded as a
number is a plausible wrong value. Fragments are counted and dropped.

---

## On the P&I page

The pump's outline on the drawing is coloured by state and the speed is written
beside it; a card in the column says the state in words. See [the web UI
page](../operating/webui.md#the-turbo-on-the-drawing). The outline is a copy of
the drawing's own line work, made by `tools/build_mimic.py` (`SYMBOLS`), so a
revised drawing keeps the colouring.

---

## What a fault looks like from the outside

| Seen | Meaning |
|---|---|
| all `turbo_*` channels `error`, service `degraded` | no reply from the pump for `max_age_s` (5 s): DCU / pumping station unpowered, cable unplugged |
| only `turbo_speed` `error` | DCU display / service line no longer on 309 |
| service refuses to start, "no valid reply" | adapter present but no traffic: DCU off, or D+/D− swapped |
| service refuses to start, "no port with USB id" | adapter unplugged, or a different adapter (serial mismatch) |
| `turbo_error` non-zero | the pump reports an error or warning; the text (`Err…` / `Wrn…`) is in `turbo.log` |

---

## What it deliberately cannot do

Switch the pump, read its current, power or temperatures (unless the DCU
display shows one of them), or read the pressure. Any of these by query would
make this PC a second master on the DCU's bus.
