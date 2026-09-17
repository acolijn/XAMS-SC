# Configuration files

Everything the system knows about the hardware is in `config/`, in git.
**Adding a sensor touches no Python.**

| File | What it defines | How it is changed |
|---|---|---|
| `channels.yaml` | every channel: address, scaling, unit, write range | edit, commit, `xams-ctl reload` |
| `devices.yaml` | how each instrument is found and identified | edit, commit, `xams-ctl reload` |
| `alarms.yaml` | thresholds and severity routing | edit, commit, `xams-ctl reload` |
| `recipients.yaml` | who gets notified | the web UI, or by hand — no restart |
| `secrets.yaml` | credentials | by hand; **never committed** |

Three conventions that must not be changed:

- **Scaling is `value = (raw - offset) * multiplier`**, in that order. It
  matches LabVIEW exactly. Changing it breaks every historical comparison.
- **HV values are stored signed.** The supplies report unsigned magnitudes with
  polarity separate; the sign is applied in software. Changing this later
  silently inverts history.
- **Channel names are permanent.** They are the identity of a measurement in
  the MQTT topic, the archive, the database and the UI.

## Generated references

The current contents of two of these files are published as part of this
manual, straight from the YAML: [the channel table](../reference/channels.md)
and [the alarm table](../reference/alarms.md). They are regenerated at every
build, so they cannot drift.

!!! warning "Not written yet"
    Wanted: the field-by-field meaning of each file, and why `recipients.yaml` is the one that is edited from the UI. The generated [channel](../reference/channels.md) and [alarm](../reference/alarms.md) tables show the current contents.
