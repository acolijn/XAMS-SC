-- XAMS slow control schema. See DESIGN.md §9.2.
--
-- Run on every install, on the lab PC and on a Nikhef VM alike:
--     psql -U postgres -d xams -f sql/schema.sql
--
-- This makes the install a procedure rather than something that was once
-- assembled by hand and can no longer be repeated (§12).
--
-- This database is a CACHE, not storage. The archive is the JSONL/Parquet
-- files. If it is lost, replay the history into a fresh one.

CREATE TABLE IF NOT EXISTS meas (
    t        timestamptz  NOT NULL,
    channel  text         NOT NULL,
    value    double precision,          -- mean over the log interval
    vmin     double precision,          -- optional, per channel (log_minmax)
    vmax     double precision,          -- optional, per channel
    raw      double precision,
    unit     text,
    quality  text         NOT NULL DEFAULT 'ok',
    src      text         NOT NULL DEFAULT 'xams'   -- 'labview' for imported history
);

CREATE INDEX IF NOT EXISTS meas_channel_t_idx ON meas (channel, t DESC);

-- One reading per channel per instant per source. This makes the store
-- IDEMPOTENT, which matters because duplicates arrive by a route that is not a
-- bug and cannot be designed away:
--
--   MQTT re-delivers RETAINED messages to every new subscriber. Each time the
--   sinks reconnect they receive the last value of every channel again, with
--   its ORIGINAL timestamp, and would insert it a second time. On 17 September
--   2026 a stray second sinks process turned that into 46,418 duplicate rows.
--
-- With this constraint the writer can use ON CONFLICT DO NOTHING and a replay
-- is harmless. Reprocessing an archive can then never inflate the history.
CREATE UNIQUE INDEX IF NOT EXISTS meas_unique_reading ON meas (t, channel, src);

-- Alarm state transitions, published on xams/alarm/<channel> and stored like
-- any other record so Grafana can show history without being in the alarm
-- path (§11).
CREATE TABLE IF NOT EXISTS alarm_events (
    t          timestamptz NOT NULL,
    channel    text        NOT NULL,
    state      text        NOT NULL,   -- ok | minor | major | stale
    threshold  text,                   -- lolo | low | high | hihi | staleness
    value      double precision,
    message    text
);

CREATE INDEX IF NOT EXISTS alarm_events_t_idx ON alarm_events (t DESC);

-- Flow integrator periods (§7.5). A reset CLOSES a period and opens a new
-- one; it never zeroes a counter, so the history of how much passed through
-- during each period survives.
CREATE TABLE IF NOT EXISTS flow_periods (
    id        bigserial PRIMARY KEY,
    channel   text        NOT NULL,
    start     timestamptz NOT NULL,
    stop      timestamptz,
    total_g   double precision NOT NULL DEFAULT 0,
    gaps_s    double precision NOT NULL DEFAULT 0,
    reset_by  text
);

-- Append-only audit of every write and every recipient change (§10, §4.4).
CREATE TABLE IF NOT EXISTS audit (
    t         timestamptz NOT NULL DEFAULT now(),
    actor     text        NOT NULL,
    action    text        NOT NULL,
    target    text,
    old_value text,
    new_value text,
    result    text,
    detail    text
);

CREATE INDEX IF NOT EXISTS audit_t_idx ON audit (t DESC);
