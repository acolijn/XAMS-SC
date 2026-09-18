"""The Logs tab. See DESIGN.md §8.1.

It is the second place anybody looks when a service is unhappy — after
`xams-ctl status` — so what it must never do is look like silence when the
service has been talking. Three ways it could:

  * read a `logs\\` folder that nothing writes to, and list nothing
  * show the tail of a file that has just rotated, which is nearly empty
    while 10 MB of history sits next to it
  * look current when it is a snapshot from twenty minutes ago

And one thing it must not do: read a file because the URL asked it to.
"""

import pytest
from fastapi.testclient import TestClient

from xams_sc.api import app as app_module

from test_webui_flow_reset import FakeBus, StubDrift  # noqa: F401


LINE = "2026-09-18 13:27:0%d INFO     [caen] xams_sc.devices.caen: %s"


def pre(body: str) -> str:
    """Just the log block. The page around it says "seconds" and "first" in
    its own prose, which an index() over the whole document happily finds."""
    return body.split("<pre>", 1)[1].split("</pre>", 1)[0]


@pytest.fixture
def logs(tmp_path, monkeypatch):
    """A log folder of our own, in place of the repository's."""
    monkeypatch.setattr(app_module, "LOG_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def client(logs, monkeypatch):
    monkeypatch.setattr(app_module, "Bus", lambda **kw: FakeBus())
    monkeypatch.setattr(app_module, "DriftWatcher", StubDrift)
    return TestClient(app_module.create_app())


class TestWhatItShows:
    def test_the_newest_line_is_at_the_top(self, client, logs):
        (logs / "caen.log").write_text(
            "\n".join([LINE % (1, "first"), LINE % (2, "second"),
                       LINE % (3, "third")]), encoding="utf-8")

        body = pre(client.get("/logs?service=caen").text)

        assert body.index("third") < body.index("second") < body.index("first")

    def test_a_traceback_is_not_printed_inside_out(self, client, logs):
        """The record somebody came to read is the one that must stay
        readable. Reversing line by line would put the exception first and
        the frames after it, in reverse."""
        (logs / "caen.log").write_text("\n".join([
            LINE % (1, "an earlier line"),
            LINE % (2, "read failed"),
            "Traceback (most recent call last):",
            '  File "caen.py", line 10, in read',
            "SerialException: port closed",
        ]), encoding="utf-8")

        body = pre(client.get("/logs?service=caen").text)

        assert body.index("Traceback") < body.index("caen.py") \
            < body.index("SerialException")
        assert body.index("read failed") < body.index("an earlier line")

    def test_it_says_when_it_was_read(self, client, logs):
        """Every other page refreshes itself, so the habit the UI teaches is
        that what you see is current. This one does not, and must say so."""
        (logs / "caen.log").write_text(LINE % (1, "hello"), encoding="utf-8")

        body = client.get("/logs?service=caen").text

        assert "Read at" in body
        assert "does not refresh itself" in body


class TestRotation:
    def test_rotated_files_are_offered(self, client, logs):
        (logs / "caen.log").write_text(LINE % (1, "now"), encoding="utf-8")
        (logs / "caen.log.1").write_text(LINE % (2, "before"), encoding="utf-8")

        body = client.get("/logs?service=caen").text

        assert "older=1" in body

    def test_a_rotated_file_can_be_read(self, client, logs):
        (logs / "caen.log").write_text(LINE % (1, "now"), encoding="utf-8")
        (logs / "caen.log.1").write_text(LINE % (2, "yesterday"),
                                         encoding="utf-8")

        body = client.get("/logs?service=caen&older=1").text

        assert "yesterday" in body

    def test_a_rotation_that_does_not_exist_says_so(self, client, logs):
        (logs / "caen.log").write_text(LINE % (1, "now"), encoding="utf-8")

        body = client.get("/logs?service=caen&older=4").text

        assert "does not exist" in body

    def test_only_the_files_the_handler_writes(self, client, logs):
        """`setup_logging` keeps 5. A sixth is not a log this system wrote."""
        (logs / "caen.log").write_text(LINE % (1, "now"), encoding="utf-8")
        (logs / "caen.log.9").write_text("secret", encoding="utf-8")

        body = client.get("/logs?service=caen&older=9").text

        assert "secret" not in body


class TestItReadsOnlyItsOwnLogs:
    @pytest.mark.parametrize("service", [
        "../secrets",
        "../../etc/passwd",
        "..\\..\\config\\secrets",
        "C:/Windows/System32/config",
    ])
    def test_a_path_from_the_url_selects_nothing(self, client, logs, service,
                                                 tmp_path):
        """The name is looked up in the whitelist, never interpolated into a
        path. Loopback binding is a second line of defence, not the first."""
        (tmp_path.parent / "secrets.log").write_text("token: hunter2",
                                                     encoding="utf-8")

        body = client.get("/logs", params={"service": service}).text

        assert "hunter2" not in body
        assert "no such log" in body

    def test_the_listed_names_are_the_ones_that_work(self, client, logs):
        (logs / "caen.log").write_text(LINE % (1, "hello"), encoding="utf-8")
        (logs / "alarms.log").write_text(LINE % (2, "quiet"), encoding="utf-8")

        body = client.get("/logs?service=caen").text

        assert "service=alarms" in body and "service=caen" in body


class TestWhenThereIsNothingToShow:
    def test_a_missing_log_folder_is_not_a_crash(self, client, logs):
        for path in logs.iterdir():
            path.unlink()
        logs.rmdir()

        r = client.get("/logs?service=caen")

        assert r.status_code == 200
        assert "no such log" in r.text
