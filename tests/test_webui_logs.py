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
from xams_sc.api import logview

from doubles import RecordingBus, StubDrift  # noqa: F401


LINE = "2026-09-18 13:27:0%d INFO     [caen] xams_sc.devices.caen: %s"


def pre(body: str) -> str:
    """Just the log block. The page around it says "seconds" and "first" in
    its own prose, which an index() over the whole document happily finds."""
    return body.split("<pre>", 1)[1].split("</pre>", 1)[0]


@pytest.fixture
def logs(tmp_path, monkeypatch):
    """A log folder of our own, in place of the repository's."""
    # Patched where it is READ. The log helpers moved out of app.py into
    # api/logview.py, and `log_files` resolves LOG_DIR in that module.
    monkeypatch.setattr(logview, "LOG_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def client(logs, monkeypatch):
    monkeypatch.setattr(app_module, "Bus", lambda **kw: RecordingBus())
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


class TestColouring:
    """The colours, which exist so the eye finds the one bad line in eighty
    (§8.1). The risk they bring is not cosmetic: colouring means putting
    markup around text that arrived from a serial port, so the escaping is
    what most of this class is about.
    """

    # The real format is `%(levelname)-8s` followed by a space, so the gap
    # before `[caen]` differs per level. Written out rather than patched into
    # LINE, so these read as the lines the handler actually writes.
    WARN = "2026-09-18 13:27:01 WARNING  [caen] xams_sc.devices.caen: %s"
    ERROR = "2026-09-18 13:27:01 ERROR    [caen] xams_sc.devices.caen: %s"

    def test_the_level_is_marked_with_its_own_class(self, client, logs):
        (logs / "caen.log").write_text(self.WARN % "link down",
                                       encoding="utf-8")

        body = pre(client.get("/logs?service=caen").text)

        assert '<span class="lg-warn">WARNING</span>' in body

    def test_an_error_colours_its_message_too(self, client, logs):
        """A level word four characters wide is easy to miss; the sentence
        beside it is not."""
        (logs / "caen.log").write_text(self.ERROR % "port closed",
                                       encoding="utf-8")

        body = pre(client.get("/logs?service=caen").text)

        assert '<span class="lg-error">port closed</span>' in body

    def test_an_ordinary_message_is_left_in_the_body_colour(self, client, logs):
        """If every line shouts, the one that matters stops standing out."""
        (logs / "caen.log").write_text(LINE % (1, "connected"),
                                       encoding="utf-8")

        body = pre(client.get("/logs?service=caen").text)

        assert "</span>: connected" in body

    def test_markup_in_a_log_line_is_escaped(self, client, logs):
        """A device reply is untrusted text. It reaches this page verbatim,
        and one <script> in a CAEN error string must not run here."""
        (logs / "caen.log").write_text(
            LINE % (1, "reply <script>alert(1)</script> & done"),
            encoding="utf-8")

        body = pre(client.get("/logs?service=caen").text)

        assert "<script>" not in body
        assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; done" in body

    def test_a_traceback_body_is_dimmed_as_one_block(self, client, logs):
        (logs / "caen.log").write_text("\n".join([
            self.ERROR % "read failed",
            "Traceback (most recent call last):",
        ]), encoding="utf-8")

        body = pre(client.get("/logs?service=caen").text)

        assert '<span class="lg-cont">Traceback (most recent call last):</span>' \
            in body

    def test_the_spacing_of_the_original_line_survives(self, client, logs):
        """Read side by side with the real file, the columns must still line
        up - the run of spaces after the level is captured, not assumed."""
        (logs / "caen.log").write_text(LINE % (1, "connected"),
                                       encoding="utf-8")

        body = pre(client.get("/logs?service=caen").text)

        assert '</span>     <span class="lg-svc">[caen]</span>' in body

    def test_a_line_that_is_not_a_record_is_still_shown(self, client, logs):
        """Anything the page cannot parse must still reach the reader. A log
        it silently drops lines from is worse than no log."""
        (logs / "caen.log").write_text("not a log record at all",
                                       encoding="utf-8")

        body = pre(client.get("/logs?service=caen").text)

        assert "not a log record at all" in body


class TestTheShellsOwnFormat:
    """`tools/backup.ps1` writes its log itself, in a shorter format than
    `setup_logging`: no milliseconds, no [service], no logger, and WARN where
    Python writes WARNING. Every line of it used to miss the record pattern,
    so backup.log - the one log where a failed nightly copy has to be
    obvious - came out uniformly grey."""

    # As `Log` in tools/backup.ps1 formats it: "{0} {1,-7} {2}".
    PS = "2026-09-18 09:43:52 %-7s %s"

    def test_the_level_is_coloured(self, client, logs):
        (logs / "backup.log").write_text(
            self.PS % ("INFO", "backup starting"), encoding="utf-8")

        body = pre(client.get("/logs?service=backup").text)

        assert '<span class="lg-info">INFO</span>' in body

    def test_warn_counts_as_a_warning(self, client, logs):
        """WARN and WARNING are one severity spelled two ways."""
        (logs / "backup.log").write_text(
            self.PS % ("WARN", "unreadable stamp; copying everything"),
            encoding="utf-8")

        body = pre(client.get("/logs?service=backup").text)

        assert '<span class="lg-warn">WARN</span>' in body

    def test_an_error_still_colours_its_message(self, client, logs):
        (logs / "backup.log").write_text(
            self.PS % ("ERROR", "robocopy exited 8"), encoding="utf-8")

        body = pre(client.get("/logs?service=backup").text)

        assert '<span class="lg-error">robocopy exited 8</span>' in body

    def test_no_service_or_logger_is_invented(self, client, logs):
        """There is nothing in the line to put there, and the rendered line
        must read the same as the file it came from."""
        (logs / "backup.log").write_text(
            self.PS % ("INFO", "106 file(s), 206,5 MB"), encoding="utf-8")

        body = pre(client.get("/logs?service=backup").text)

        assert "lg-svc" not in body and "lg-name" not in body
        assert "INFO</span>    106 file(s), 206,5 MB" in body

    def test_the_byte_order_mark_does_not_eat_the_first_line(self, client,
                                                             logs):
        """PowerShell 5.1 writes a BOM at the head of the file. Read as plain
        utf-8 it sits in front of the first timestamp, and that line - the
        'backup starting' line - loses its colours."""
        (logs / "backup.log").write_text(
            self.PS % ("INFO", "backup starting"), encoding="utf-8-sig")

        body = pre(client.get("/logs?service=backup").text)

        assert '<span class="lg-time">2026-09-18 09:43:52</span>' in body

    def test_a_python_record_is_not_matched_by_the_shorter_pattern(
            self, client, logs):
        """The long format is tried first, and the short one refuses
        milliseconds - a service log must keep its service and logger."""
        (logs / "caen.log").write_text(LINE % (1, "connected"),
                                       encoding="utf-8")

        body = pre(client.get("/logs?service=caen").text)

        assert '<span class="lg-svc">[caen]</span>' in body
