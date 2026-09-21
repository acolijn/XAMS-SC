# UPS

APC unit, read over USB HID. A power event is one of the few things that can
end a run, so mains loss goes to SMS at once rather than waiting for the
battery to run down.

Read-only, and the smallest service in the system — three channels. It earns
its place on consequence, not on volume.

---

## The three channels

| Channel | | |
|---|---|---|
| `ups_on_battery` | `1` while running on battery, `0` on line power | the one that matters |
| `ups_battery_pct` | charge, percent | how long you have |
| `ups_runtime_min` | the UPS's own estimate, minutes | treat with care |

`ups_on_battery` is the alarm. The other two answer "how long have I got" once
it has fired, and both are shown on the [System health
page](../operating/webui.md).

### How much to trust the runtime estimate

It is the UPS's own number, computed from the present load and the battery's
state of charge. It is **honest but not a promise**: it assumes the load stays
as it is, and it degrades as the battery ages — an old battery reports
optimistically until the moment it does not.

Use it to decide *what to do next*, not to plan a shutdown to the minute. The
observed value on the lab PC on 17 September 2026 was 1579 s, about 26
minutes, at 100% charge.

---

## How it reads, and why this way

Three routes were tried on 17 September 2026, and two of them do not work.
They are recorded here so nobody retries them.

| Route | |
|---|---|
| `Win32_Battery` and the WMI battery classes | **no instances.** PowerChute has claimed the HID and Windows does not register it as a system battery |
| `GetSystemPowerStatus` | answers, but reports `BatteryFlag = 128` — "no system battery". Its `ACLineStatus` therefore describes the **wall socket**, not the UPS, and would read "on line power" while the UPS ran on battery |
| **the HID feature reports directly** | works — and works *alongside* PowerChute |

The second one is the interesting failure. It returns a plausible answer to a
question that sounds right, and that answer is wrong in exactly the situation
the channel exists for.

> A channel that cannot detect the thing it monitors is worse than no
> channel.

So it was rejected rather than used as an approximation.

**HID input is shareable on Windows**, so reading the feature reports does not
take the device away from PowerChute Personal Edition, which keeps doing its
safe-shutdown job. The two coexist.

---

## The usages

Standard **USB HID Power Device / Battery System** usages, not APC
extensions, so this should survive a UPS replacement:

| Usage | |
|---|---|
| `0x008500D0` | AC present |
| `0x008500D1` | battery present |
| `0x00850045` | discharging |
| `0x00850044` | charging |
| `0x00850066` | remaining capacity |
| `0x00850068` | runtime to empty |
| `0x00840065` | needs replacement |
| `0x00840069` | overload |

A usage the unit does not report is simply absent from the result, and the
caller decides whether its channel is therefore unknown — rather than
substituting a zero.

The device is matched on VID/PID and serial from
[`devices.yaml`](../software/config.md), like every other instrument. It
reports itself as *Legacy Communication Card FW:LCC 03.1 / ID=5004*.

---

## What `ups_on_battery` means

`1` means the UPS is supplying the load from its battery — the mains has gone
or is out of tolerance. It does **not** mean the battery is low, and it does
not mean anything is about to shut down.

On the System health page it is rendered as `ON BATTERY` or `line power` rather
than as a number, because `1` and `0` are a poor way to convey the one fact
somebody needs at a glance.

**It is derived from two independent signals, and either one is enough.**
`ACPresent` drops immediately when the mains goes; `Discharging` follows a
moment later. Waiting for both would delay the alarm by the lag between them,
so the channel is true as soon as *either* says so. A transition in either
direction is logged — `UPS IS ON BATTERY - mains power has been lost`.

`needs_replacement` and `overload` are read from the device but are not
published as channels today. Worth adding when somebody wants them: they are
already in the usage table above, so it is a channel in `channels.yaml` and
three lines here.
