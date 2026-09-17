# Architecture

Every device service reads its instrument and publishes to a local MQTT
broker. Nothing writes to a database directly. Storage, alarming and the UI
are all subscribers, so any of them can be added or replaced without touching
a driver.

**The files are the truth; the database is an index over them.** If PostgreSQL
is lost, replay the archive into a new one. Nothing in any driver changes.

!!! warning "Not written yet"
    The full argument is in [the design specification](../DESIGN.md) §2–§4. This page should become the short version of it — the one a new student reads first.
