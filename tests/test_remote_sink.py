"""The second writer, to the Nikhef VM, and the replay that backs it. See §12.

The VM is a copy for the group; the lab PC must not depend on it. What is
pinned here is that the remote block is read with its TLS settings, that a
VM in trouble is a warning that names what to replay and never a `degraded`
lab PC, and that a replay reads the archive as the writer wrote it and adds
only what is missing.
"""

from __future__ import annotations

import importlib.util
import json
import logging
from datetime import date

import pytest

from doubles import FakePg
from xams_sc.config import ROOT
from xams_sc.sinks.__main__ import RemoteHealth, dsn_from_secrets


def load_replay():
    spec = importlib.util.spec_from_file_location(
        "replay_jsonl", ROOT / "tools" / "replay_jsonl.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def secrets(tmp_path):
    path = tmp_path / "secrets.yaml"
    path.write_text(
        "postgres:\n"
        "  host: 127.0.0.1\n  database: xams\n  user: xams\n  password: pw\n"
        "postgres_remote:\n"
        "  host: plotit-xams.nikhef.nl\n  database: xams\n"
        "  user: xams_writer\n  password: wpw\n"
        "  sslmode: verify-full\n  sslrootcert: config/plotit-xams-ca.crt\n",
        encoding="utf-8")
    return path


class TestSecrets:
    def test_the_local_block_is_unchanged(self, secrets):
        dsn = dsn_from_secrets(path=secrets)
        assert dsn == "host=127.0.0.1 port=5432 dbname=xams user=xams password=pw"

    def test_the_remote_block_carries_tls(self, secrets):
        dsn = dsn_from_secrets("postgres_remote", path=secrets)
        assert "host=plotit-xams.nikhef.nl" in dsn
        assert "user=xams_writer" in dsn
        assert "sslmode=verify-full" in dsn

    def test_a_relative_certificate_is_taken_from_the_repository(self, secrets):
        """The service does not necessarily start in the repository, and the
        lab PC's path has a space in it."""
        dsn = dsn_from_secrets("postgres_remote", path=secrets)
        cert = ROOT / "config" / "plotit-xams-ca.crt"
        assert f"sslrootcert='{cert.as_posix()}'" in dsn

    def test_no_backslash_reaches_libpq(self, secrets):
        """Inside a quoted DSN value libpq treats a backslash as an escape,
        so a Windows path C:\\Users\\... reached it as C:Users... and the
        certificate "did not exist" — which is what happened on the lab PC."""
        from pathlib import PureWindowsPath

        cert = PureWindowsPath(r"C:\Users\localadmin\XAMS SC\config\ca.crt")
        assert "\\" not in cert.as_posix()
        dsn = dsn_from_secrets("postgres_remote", path=secrets)
        assert "\\" not in dsn

    def test_no_remote_block_means_no_remote_writer(self, tmp_path):
        """The normal state until the VM exists."""
        path = tmp_path / "secrets.yaml"
        path.write_text("postgres:\n  database: xams\n", encoding="utf-8")
        assert dsn_from_secrets("postgres_remote", path=path) is None


class StubPg:
    def __init__(self):
        self.dropped = 0
        self.pending = 0

    @property
    def stats(self):
        return {"written": 0, "pending": self.pending, "dropped": self.dropped}


class TestRemoteHealth:
    def test_quiet_when_all_is_well(self, caplog):
        with caplog.at_level(logging.INFO):
            RemoteHealth(StubPg()).check()
        assert caplog.records == []

    def test_discarded_rows_are_a_warning_that_names_the_replay(self, caplog):
        """Not an ERROR: the rows are in the archive. But the message must
        say how to get them onto the VM."""
        pg = StubPg()
        health = RemoteHealth(pg)
        pg.dropped = 12
        with caplog.at_level(logging.INFO):
            health.check()

        [rec] = caplog.records
        assert rec.levelno == logging.WARNING
        assert "replay_jsonl.py --since" in rec.getMessage()

    def test_reported_on_change_not_every_cycle(self, caplog):
        pg = StubPg()
        health = RemoteHealth(pg)
        pg.dropped = 5
        health.check()
        caplog.clear()
        with caplog.at_level(logging.INFO):
            health.check()
        assert caplog.records == []

    def test_it_never_touches_the_service_state(self, bus):
        """RemoteHealth has no bus at all, so a VM outage cannot turn the
        lab PC's status page amber. Pinned so nobody gives it one."""
        pg = StubPg()
        pg.dropped = 1000
        pg.pending = 300_000
        RemoteHealth(pg).check()
        assert bus.states == []

    def test_a_large_backlog_warns_once_and_says_when_it_drains(self, caplog):
        pg = StubPg()
        health = RemoteHealth(pg, backlog_warn=100)
        pg.pending = 500
        with caplog.at_level(logging.INFO):
            health.check()
            health.check()
            pg.pending = 0
            health.check()
        assert [r.levelno for r in caplog.records] == [logging.WARNING, logging.INFO]


# ---------------------------------------------------------------- replay

def write_day(directory, day, lines):
    path = directory / f"{day}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


HEADER = json.dumps({"t": "2026-09-20T00:00:00Z", "meta": {"config": "abc"}})
READING = json.dumps({"t": "2026-09-20T10:00:00Z", "ch": "pmain", "v": 1.5,
                      "u": "bar", "q": "ok"})
IMPORTED = json.dumps({"t": "2026-09-20T10:00:02Z", "ch": "tt201", "v": -95.0,
                       "u": "C", "q": "ok", "src": "labview"})


class CopyPg(FakePg):
    """FakePg plus the two things a replay uses that a sink does not."""

    def __init__(self, added=0):
        super().__init__(rowcount=added)
        self.copied: list[tuple] = []
        self.commits = 0

    def copy(self, sql):
        pg = self

        class _Copy:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def write_row(self, row):
                pg.copied.append(tuple(row))

        self.calls.append((sql, ()))
        return _Copy()

    def commit(self):
        self.commits += 1


class TestReplay:
    def test_files_are_chosen_by_date_and_oldest_first(self, tmp_path):
        replay = load_replay()
        for day in ("2026-09-19", "2026-09-21", "2026-09-20", "2026-09-22"):
            write_day(tmp_path, day, [HEADER])
        (tmp_path / "notes.jsonl").write_text("", encoding="utf-8")

        files = replay.select_files(tmp_path, date(2026, 9, 20), date(2026, 9, 21))
        assert [f.stem for f in files] == ["2026-09-20", "2026-09-21"]

    def test_header_and_a_torn_last_line_are_skipped(self, tmp_path):
        """A power cut mid-write leaves half a line; it must not stop a
        replay of a month."""
        replay = load_replay()
        path = write_day(tmp_path, "2026-09-20", [HEADER, READING, '{"t": "2026-09'])
        stats = {"read": 0, "unparseable": 0}

        rows = list(replay.rows_from(path, stats))

        assert stats == {"read": 1, "unparseable": 1}
        assert rows[0][1:] == ("pmain", 1.5, None, "bar", "ok", "xams")

    def test_provenance_survives_the_replay(self, tmp_path):
        """Imported LabVIEW rows must stay `labview` on the VM, or they are
        indistinguishable from readings this system took."""
        replay = load_replay()
        path = write_day(tmp_path, "2026-09-20", [HEADER, IMPORTED])
        rows = list(replay.rows_from(path, {"read": 0, "unparseable": 0}))
        assert rows[0][-1] == "labview"

    def test_a_day_is_staged_then_merged_without_duplicates(self, tmp_path):
        replay = load_replay()
        path = write_day(tmp_path, "2026-09-20", [HEADER, READING, IMPORTED])
        pg = CopyPg(added=2)
        stats = {"read": 0, "unparseable": 0}

        added = replay.replay_file(pg, path, stats)

        assert added == 2
        assert len(pg.copied) == 2
        sql = [s for s, _ in pg.calls]
        assert "TEMP TABLE" in sql[0]
        assert sql[1].startswith("COPY replay_stage")
        assert "ON CONFLICT (t, channel, src) DO NOTHING" in sql[2]
        assert pg.commits == 1
