"""getUpdates must never fail in silence.

A bot that answers some commands and ignores others, with nothing in the log,
was two processes polling one token: Telegram replies 409 CONFLICT to whichever
one it decides to starve, and this returned [] without a word. The whole point
of these tests is that the log names the cause.
"""

import io
import unittest
import urllib.error
from unittest import mock

from src.telegram_sender import TelegramSender

TOKEN = "123456780:AAaaBBbbCCccDDddEEeeFFffGGgg"


def _http_error(code: str | int, body: bytes):
    return urllib.error.HTTPError(
        "https://api.telegram.org", int(code), "err", {}, io.BytesIO(body))


class GetUpdatesErrorTests(unittest.TestCase):
    def setUp(self):
        self.sender = TelegramSender(TOKEN, dry_run=False)

    def test_409_names_the_second_poller(self):
        err = _http_error(409, b'{"description":"terminated by other getUpdates"}')
        with mock.patch.object(TelegramSender, "_post", side_effect=err), \
                self.assertLogs("eld_alert_bot", level="ERROR") as caught:
            self.assertEqual(self.sender.get_updates(0), [])
        self.assertIn("409 CONFLICT", caught.output[0])
        self.assertIn("same bot token", caught.output[0].lower())

    def test_other_http_errors_are_still_reported(self):
        err = _http_error(502, b"bad gateway")
        with mock.patch.object(TelegramSender, "_post", side_effect=err), \
                self.assertLogs("eld_alert_bot", level="WARNING") as caught:
            self.assertEqual(self.sender.get_updates(0), [])
        self.assertIn("502", caught.output[0])

    def test_a_refusal_in_the_body_is_reported(self):
        with mock.patch.object(TelegramSender, "_post",
                               return_value={"ok": False, "description": "Unauthorized"}), \
                self.assertLogs("eld_alert_bot", level="WARNING") as caught:
            self.assertEqual(self.sender.get_updates(0), [])
        self.assertIn("Unauthorized", caught.output[0])

    def test_a_good_poll_stays_quiet(self):
        with mock.patch.object(TelegramSender, "_post",
                               return_value={"ok": True, "result": [{"update_id": 7}]}), \
                self.assertNoLogs("eld_alert_bot", level="WARNING"):
            self.assertEqual(self.sender.get_updates(0), [{"update_id": 7}])


if __name__ == "__main__":
    unittest.main()
