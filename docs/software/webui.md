# Web UI

**How to use it is in [The web interface](../operating/webui.md).** This page is
how it is built.

Server-rendered HTML, a meta refresh, no JavaScript framework and no build step
— so that in three years a student can change it without installing a toolchain.
Binds to `127.0.0.1`, never `0.0.0.0`.

Pages: Overview (`/`), Channels (`/status`), P&I mimic (`/mimic`), High voltage
(`/hv`), Logs (`/logs`). This manual is served by the same process, at
`/manual`, so it is present on the lab PC whether or not the building network
is.

It renders from the **retained MQTT topics, never the database** (§8.1). The
database can be down and every page still answers.

**Writes go through the bus, not through the web process.** A control form posts
to FastAPI, which publishes on `xams/cmd/...` and waits for the matching
`xams/ack/...`; the service that owns the serial port does the validating, the
writing, the read-back and the auditing. The web layer therefore holds no copy
of a permitted range and no instrument handle — a command from this UI and one
from the CLI get identical treatment. POST then redirect, so a refresh cannot
repeat a write.

!!! warning "Not written yet"
    Wanted: the mimic build step (`tools/build_mimic.py`) and its tag-drift
    check, and the shape of `/api/state`.
