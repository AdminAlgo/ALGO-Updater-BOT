"""Telegram message delivery.

Posts alert text to Telegram chats via the Bot API `sendMessage` method, using
TELEGRAM_BOT_TOKEN. Stdlib only (urllib) — no extra dependencies.

Two modes:
  - live:     real HTTP POST to api.telegram.org.
  - dry_run:  prints what WOULD be sent and returns a simulated success, so the
              whole pipeline can be exercised before the bot is in the groups
              (or before real chat IDs exist).

A placeholder/empty bot token is rejected in live mode with a clear message, so
nobody accidentally "sends" against a fake token.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

TELEGRAM_API = "https://api.telegram.org"

# Tokens that obviously aren't real — refuse to "send" with these in live mode.
_PLACEHOLDER_HINTS = ("placeholder", "your-telegram-bot-token", "123456789:")


@dataclass(frozen=True)
class SendResult:
    ok: bool
    chat_id: str
    kind: str = ""
    error: str | None = None


def _looks_like_placeholder(token: str) -> bool:
    t = (token or "").strip().lower()
    return not t or any(h in t for h in _PLACEHOLDER_HINTS)


class TelegramSender:
    def __init__(
        self,
        bot_token: str,
        dry_run: bool = False,
        timeout: float = 15.0,
        inter_message_delay: float = 0.05,
    ) -> None:
        self._token = bot_token
        self.dry_run = dry_run
        self._timeout = timeout
        self._delay = inter_message_delay
        if not dry_run and _looks_like_placeholder(bot_token):
            raise ValueError(
                "TELEGRAM_BOT_TOKEN looks like a placeholder. Set a real token in "
                ".env, or run in dry-run mode to preview without sending."
            )

    # ------------------------------------------------------------------ #
    def _post(self, method: str, payload: dict) -> dict:
        url = f"{TELEGRAM_API}/bot{self._token}/{method}"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def send_message(self, chat_id: str, text: str, kind: str = "",
                     entities: list | None = None) -> SendResult:
        """Send one message. In dry-run, print and simulate success."""
        if self.dry_run:
            print(f"  [DRY-RUN] → chat {chat_id}" + (f" [{kind}]" if kind else ""))
            for ln in text.splitlines():
                print(f"           {ln}")
            return SendResult(ok=True, chat_id=chat_id, kind=kind)

        payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if entities:
            payload["entities"] = entities
        try:
            body = self._post("sendMessage", payload)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            return SendResult(False, chat_id, kind, f"HTTP {exc.code}: {detail}")
        except urllib.error.URLError as exc:
            return SendResult(False, chat_id, kind, f"network error: {exc.reason}")
        except json.JSONDecodeError:
            return SendResult(False, chat_id, kind, "invalid JSON response from Telegram")

        if not body.get("ok"):
            return SendResult(
                False, chat_id, kind,
                f"Telegram error {body.get('error_code')}: {body.get('description')}",
            )
        if self._delay:
            time.sleep(self._delay)
        return SendResult(ok=True, chat_id=chat_id, kind=kind)

    @staticmethod
    def _multipart(fields: dict, file_field: str, filename: str, file_bytes: bytes,
                   content_type: str = "image/png") -> tuple[str, bytes]:
        boundary = b"----AlgoELDBoundary7MA4YWxkTrZu0gW"
        parts: list[bytes] = []
        for k, v in fields.items():
            parts += [b"--" + boundary,
                      f'Content-Disposition: form-data; name="{k}"'.encode(),
                      b"", str(v).encode("utf-8")]
        parts += [b"--" + boundary,
                  f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"'.encode(),
                  f"Content-Type: {content_type}".encode(),
                  b"", file_bytes,
                  b"--" + boundary + b"--", b""]
        body = b"\r\n".join(parts)
        return f"multipart/form-data; boundary={boundary.decode()}", body

    def send_photo(self, chat_id: str, image_bytes: bytes, caption: str,
                   kind: str = "", caption_entities: list | None = None) -> SendResult:
        """Send a photo with a caption (caption max 1024 chars)."""
        if self.dry_run:
            print(f"  [DRY-RUN] → chat {chat_id} [PHOTO {len(image_bytes)}B]"
                  + (f" [{kind}]" if kind else ""))
            for ln in caption.splitlines():
                print(f"           {ln}")
            return SendResult(ok=True, chat_id=chat_id, kind=kind)

        fields = {"chat_id": chat_id, "caption": caption[:1024]}
        if caption_entities:
            fields["caption_entities"] = json.dumps(caption_entities)
        ctype, body = self._multipart(fields, "photo", "log.png", image_bytes)
        url = f"{TELEGRAM_API}/bot{self._token}/sendPhoto"
        req = urllib.request.Request(url, data=body, headers={"Content-Type": ctype}, method="POST")
        try:
            resp_body = urllib.request.urlopen(req, timeout=self._timeout).read().decode("utf-8")
            result = json.loads(resp_body)
        except urllib.error.HTTPError as exc:
            return SendResult(False, chat_id, kind, f"HTTP {exc.code}: {exc.read().decode('utf-8','replace')[:300]}")
        except (urllib.error.URLError, json.JSONDecodeError) as exc:
            return SendResult(False, chat_id, kind, f"photo send error: {exc}")
        if not result.get("ok"):
            return SendResult(False, chat_id, kind,
                              f"Telegram error {result.get('error_code')}: {result.get('description')}")
        if self._delay:
            time.sleep(self._delay)
        return SendResult(ok=True, chat_id=chat_id, kind=kind)

    def get_updates(self, offset: int = 0, timeout: int = 0) -> list[dict]:
        """Long-poll getUpdates for group registration. Returns update dicts.

        Only requests message / my_chat_member updates (enough to learn a
        group's id + title). Returns [] on error or in dry-run.
        """
        if self.dry_run:
            return []
        payload = {
            "offset": offset,
            "timeout": timeout,
            "allowed_updates": ["message", "my_chat_member"],
        }
        try:
            body = self._post("getUpdates", payload)
        except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError):
            return []
        return body.get("result", []) if body.get("ok") else []

    def send_alert(self, alert) -> SendResult:
        """Deliver a rules.Alert to its target chat (photo if it carries one).

        If the alert carries a user-id mention (driver has no @username), prepend
        the driver's name and attach a text_mention entity so they're pinged.
        """
        text = alert.text
        entities = None
        uid = getattr(alert, "mention_user_id", None)
        if uid:
            name = getattr(alert, "mention_name", None) or alert.driver_name
            text = f"{name}\n\n{text}"
            length = len(name.encode("utf-16-le")) // 2  # Telegram uses UTF-16 units
            entities = [{"type": "text_mention", "offset": 0, "length": length,
                         "user": {"id": int(uid)}}]

        image = getattr(alert, "image_png", None)
        if image:
            return self.send_photo(alert.chat_id, image, text, kind=alert.kind,
                                   caption_entities=entities)
        return self.send_message(alert.chat_id, text, kind=alert.kind, entities=entities)

    def send_all(self, alerts) -> list[SendResult]:
        return [self.send_alert(a) for a in alerts]

    # ------------------------------------------------------------------ #
    def check_token(self) -> SendResult:
        """Verify the token via getMe (no-op in dry-run)."""
        if self.dry_run:
            return SendResult(ok=True, chat_id="-", kind="getMe")
        try:
            body = self._post("getMe", {})
        except (urllib.error.HTTPError, urllib.error.URLError) as exc:
            return SendResult(False, "-", "getMe", str(exc))
        if not body.get("ok"):
            return SendResult(False, "-", "getMe", body.get("description"))
        uname = body.get("result", {}).get("username")
        return SendResult(ok=True, chat_id="-", kind=f"getMe:@{uname}")
