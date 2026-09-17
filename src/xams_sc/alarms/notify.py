"""Alarm notification. See DESIGN.md §11.

A thin interface — `send_sms`, `send_email` — and the alarm engine knows
nothing else, so what sits underneath is replaceable without touching alarm
logic.

REUSING THE EXISTING SMS PATH. The LabVIEW system calls a Python script in
`SC_software\\send_sms_python\\`, which sends through MessageBird. That script
works: the numbers are right, the gateway accepts it, the message format
arrives, the costs are known. Rebuilding it means rediscovering all of that, so
the same library and the same sender id are used here.

§11 asks three things to be checked before adopting it. Checked 17 September
2026:

  * **Python 2 or 3?** Python 3. It uses `print()` as a function throughout.
  * **Are phone numbers hardcoded?** No — the script takes the number as
    `sys.argv[1]`. Numbers live in `recipients.yaml` here.
  * **Does it write a status file only LabVIEW reads?** No.

**THE API KEY WAS HARDCODED IN THE SCRIPT**, in plaintext, on the Desktop, in
three separate copies. It is read from `config/secrets.yaml` here and must
never enter the repository: a key committed to git remains in the history after
deletion, and private repositories are still cloned, shared and backed up
(§11). That key has sat on a desktop for years and is a good candidate for
rotation.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

log = logging.getLogger(__name__)


class Notifier:
    """Routes an alarm message to the enabled recipients.

    Every send is best-effort and failures are logged rather than raised: a
    gateway being down must not stop the engine evaluating the next reading.
    """

    def __init__(self, secrets: dict, recipients_provider):
        self.secrets = secrets or {}
        # A callable, not a list: recipients.yaml is edited from the web UI and
        # applied without a restart (§4.4), so the current list must be fetched
        # at send time rather than captured at construction.
        self._recipients = recipients_provider
        self._sms_client = None
        self._warned_no_sms = False
        self._warned_no_email = False

    # ------------------------------------------------------------------- SMS

    def _client(self):
        if self._sms_client is not None:
            return self._sms_client
        config = self.secrets.get("sms") or {}
        key = config.get("api_key")
        if not key:
            if not self._warned_no_sms:
                log.warning("no SMS api_key in secrets.yaml; SMS disabled")
                self._warned_no_sms = True
            return None
        try:
            import messagebird
        except ImportError:
            if not self._warned_no_sms:
                log.warning("the messagebird package is not installed; SMS disabled")
                self._warned_no_sms = True
            return None
        self._sms_client = messagebird.Client(key)
        return self._sms_client

    def send_sms(self, number: str, text: str) -> bool:
        client = self._client()
        if client is None:
            return False
        sender = (self.secrets.get("sms") or {}).get("sender", "XAMS")
        try:
            client.message_create(sender, number, text, {"reference": "xams-sc"})
            log.info("SMS sent to %s", _mask(number))
            return True
        except Exception as exc:
            log.error("SMS to %s failed: %s", _mask(number), exc)
            return False

    # ----------------------------------------------------------------- email

    def send_email(self, address: str, subject: str, body: str) -> bool:
        config = self.secrets.get("email") or {}
        host = config.get("smtp_host")
        if not host:
            if not self._warned_no_email:
                log.warning("no smtp_host in secrets.yaml; email disabled")
                self._warned_no_email = True
            return False

        message = EmailMessage()
        message["From"] = config.get("from_address", "xams-sc@localhost")
        message["To"] = address
        message["Subject"] = subject
        message.set_content(body)

        try:
            with smtplib.SMTP(host, int(config.get("smtp_port", 587)), timeout=20) as smtp:
                if config.get("username"):
                    smtp.starttls()
                    smtp.login(config["username"], config.get("password", ""))
                smtp.send_message(message)
            log.info("email sent to %s", address)
            return True
        except Exception as exc:
            log.error("email to %s failed: %s", address, exc)
            return False

    # ------------------------------------------------------------- dispatch

    def send(self, text: str, channels: list[str]) -> dict[str, int]:
        """Send one alarm message over the requested channels.

        Returns a count of successful deliveries per channel, so a caller can
        tell "nobody was told" from "everybody was told".
        """
        people = [r for r in (self._recipients() or []) if r.get("enabled")]
        if not people:
            # An empty list is a warning: alarms reach nobody (§4.4).
            log.error("ALARM NOT DELIVERED: no enabled recipients. %s", text)
            return {}

        sent = {"sms": 0, "email": 0, "sound": 0}
        for person in people:
            if "sms" in channels and person.get("phone"):
                sent["sms"] += bool(self.send_sms(person["phone"], text))
            if "email" in channels and person.get("email"):
                sent["email"] += bool(self.send_email(
                    person["email"], "XAMS slow control alarm", text))
        if "sound" in channels:
            sent["sound"] += bool(play_sound())
        return sent


def play_sound() -> bool:
    """Audible alert on the lab PC. Best effort; never fatal."""
    try:
        import winsound
        winsound.MessageBeep(winsound.MB_ICONHAND)
        return True
    except Exception:
        return False


def _mask(number: str) -> str:
    """Log the last few digits only. A full number in a log file is still a
    personal detail, and logs get pasted into issues and emails."""
    number = str(number)
    return ("*" * max(0, len(number) - 4)) + number[-4:] if number else "?"
