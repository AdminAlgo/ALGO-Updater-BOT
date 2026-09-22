"""Telegram message delivery.

Posts alert text to Telegram chats via the Bot API `sendMessage` method, using
TELEGRAM_BOT_TOKEN. Stdlib only (urllib) — no extra dependencies.

Three modes:
  - live:       real HTTP POST to api.telegram.org.
  - dry_run:    prints what WOULD be sent and reports the send as SUPPRESSED —
                never as a success, because a caller that believes a suppressed
                alert was delivered commits its de-dup state and the alert is
                then never sent for real (that is what silenced staging).
  - dry_run + allow_chats:  the staging split. Chats in `allow_chats` get REAL
                messages; every other chat is suppressed as above. This is what
                lets a test bot prove itself end-to-end in groups you created
                for it, while it stays physically unable to message a driver.

A placeholder/empty bot token is rejected whenever a real send is possible
(live, or dry_run with a non-empty allow_chats), so nobody accidentally "sends"
against a fake token.
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
    retry_after: int | None = None  # seconds Telegram told us to wait (429 only)
    # True when SAFE_MODE deliberately withheld this message. Distinct from both
    # ok (nothing was delivered) and a plain failure (nothing is wrong, and
    # retrying will not help). Callers MUST NOT commit de-dup state for these.
    suppressed: bool = False


def _looks_like_placeholder(token: str) -> bool:
    t = (token or "").strip().lower()
    return not t or any(h in t for h in _PLACEHOLDER_HINTS)


def _parse_retry_after(status_code: int, raw_body: str) -> int | None:
    """Telegram's 429 body carries retry_after under `parameters` — parsed
    from the FULL body, not the 300-char truncated copy kept for logging."""
    if status_code != 429:
        return None
    try:
        body = json.loads(raw_body)
    except json.JSONDecodeError:
        return None
    try:
        return int((body.get("parameters") or {}).get("retry_after"))
    except (TypeError, ValueError):
        return None


class TelegramSender:
    def __init__(
        self,
        bot_token: str,
        dry_run: bool = False,
        timeout: float = 15.0,
        inter_message_delay: float = 0.05,
        allow_chats=None,
    ) -> None:
        """`allow_chats` only means anything under dry_run: those chat ids (and
        only those) still receive real messages. It is the staging escape hatch
        — see the module docstring."""
        self._token = bot_token
        self.dry_run = dry_run
        self._timeout = timeout
        self._delay = inter_message_delay
        self.allow_chats = frozenset(
            str(c).strip() for c in (allow_chats or ()) if str(c).strip()
        )
        # A real send is possible unless we are suppressing every single chat,
        # so that is exactly when a fake token has to be refused.
        if (not dry_run or self.allow_chats) and _looks_like_placeholder(bot_token):
            raise ValueError(
                "TELEGRAM_BOT_TOKEN looks like a placeholder. Set a real token in "
                ".env, or run in dry-run mode to preview without sending."
            )

    # ------------------------------------------------------------------ #
    def _suppressed(self, chat_id) -> bool:
        """True when SAFE_MODE must withhold this chat's message."""
        return self.dry_run and str(chat_id) not in self.allow_chats

    def _suppress_result(self, chat_id, kind: str, what: str) -> SendResult:
        # Wording stays mode-neutral: this same path serves `--notify --dry-run`
        # previews, where naming SAFE_MODE would be simply wrong.
        reason = ("withheld: chat is not in the allow list"
                  if self.allow_chats else "withheld: sending is disabled")
        print(f"  [NOT SENT] → chat {chat_id}"
              + (f" [{kind}]" if kind else "") + (f" {what}" if what else ""))
        return SendResult(ok=False, chat_id=chat_id, kind=kind, error=reason,
                          suppressed=True)

    # ------------------------------------------------------------------ #
    def _post(self, method: str, payload: dict, read_timeout: float | None = None) -> dict:
        url = f"{TELEGRAM_API}/bot{self._token}/{method}"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=read_timeout or self._timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def send_message(self, chat_id: str, text: str, kind: str = "",
                     entities: list | None = None) -> SendResult:
        """Send one message. Suppressed chats are printed, never delivered, and
        reported with ok=False + suppressed=True."""
        if self._suppressed(chat_id):
            res = self._suppress_result(chat_id, kind, "")
            for ln in text.splitlines():
                print(f"           {ln}")
            return res

        payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if entities:
            payload["entities"] = entities
        try:
            body = self._post("sendMessage", payload)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            return SendResult(False, chat_id, kind, f"HTTP {exc.code}: {raw[:300]}",
                              retry_after=_parse_retry_after(exc.code, raw))
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
        if self._suppressed(chat_id):
            res = self._suppress_result(chat_id, kind, f"[PHOTO {len(image_bytes)}B]")
            for ln in caption.splitlines():
                print(f"           {ln}")
            return res

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
            raw = exc.read().decode("utf-8", "replace")
            return SendResult(False, chat_id, kind, f"HTTP {exc.code}: {raw[:300]}",
                              retry_after=_parse_retry_after(exc.code, raw))
        except (urllib.error.URLError, json.JSONDecodeError) as exc:
            return SendResult(False, chat_id, kind, f"photo send error: {exc}")
        if not result.get("ok"):
            return SendResult(False, chat_id, kind,
                              f"Telegram error {result.get('error_code')}: {result.get('description')}")
        if self._delay:
            time.sleep(self._delay)
        return SendResult(ok=True, chat_id=chat_id, kind=kind)

    def set_my_commands(self, commands: list[tuple[str, str]]) -> bool:
        """Register the slash-command menu shown in Telegram's compose box.

        `commands` is [(name_without_slash, description), ...]. Best-effort: a
        failure is logged by the caller, not raised.
        """
        if self.dry_run:
            return True
        payload = {"commands": [{"command": c, "description": d} for c, d in commands]}
        try:
            body = self._post("setMyCommands", payload)
        except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError, TimeoutError):
            return False
        return bool(body.get("ok"))

    def get_updates(self, offset: int = 0, timeout: int = 0) -> list[dict]:
        """Long-poll getUpdates for commands + group registration. Returns update
        dicts. Only requests message / my_chat_member updates. Returns [] on
        error or in dry-run.

        With ``timeout`` > 0 Telegram holds the connection open that many seconds
        waiting for an update, so the HTTP read timeout is extended past it.
        """
        if self.dry_run:
            return []
        payload = {
            "offset": offset,
            "timeout": timeout,
            "allowed_updates": ["message", "my_chat_member"],
        }
        read_timeout = (timeout + self._timeout) if timeout else self._timeout
        try:
            body = self._post("getUpdates", payload, read_timeout=read_timeout)
        except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError, TimeoutError):
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
        """Verify the token via getMe.

        Skipped only when nothing can ever be sent (dry-run with no allowlist) —
        with an allowlist the token is about to be used for real, so a bad one
        must fail at boot rather than at the first alert.
        """
        if self.dry_run and not self.allow_chats:
            return SendResult(ok=True, chat_id="-", kind="getMe")
        try:
            body = self._post("getMe", {})
        except (urllib.error.HTTPError, urllib.error.URLError) as exc:
            return SendResult(False, "-", "getMe", str(exc))
        if not body.get("ok"):
            return SendResult(False, "-", "getMe", body.get("description"))
        uname = body.get("result", {}).get("username")
        return SendResult(ok=True, chat_id="-", kind=f"getMe:@{uname}")
