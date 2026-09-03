"""Rate-limited delivery queue for Telegram alerts (FIX-7).

Detection (rules.py / evaluate_company) never sends inline — the scheduler
pushes every alert from one poll cycle onto a SendQueue, then drains it once.
Draining respects Telegram's rate limits (a global messages/second cap and a
per-chat messages/minute cap) and retries an HTTP 429 using the retry_after
Telegram tells us to wait, instead of dropping the message.

The queue is built fresh each poll cycle. Cycles never overlap (the scheduler
runs them strictly one after another — see scheduler.run_forever), so that
alone guarantees no alert is ever sent twice; push() also refuses a
dedupe_key that's already queued or sent, as a second line of defense.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass

log = logging.getLogger("eld_alert_bot")

# Give up retrying a 429 after this many attempts and defer to the next poll
# cycle instead — the alert isn't lost (state isn't committed on failure, so
# rules.py regenerates it next cycle), this just bounds how long one bad
# message can stall the whole cycle during an extended Telegram outage.
MAX_429_RETRIES = 5


@dataclass
class DrainStats:
    detected: int = 0
    sent: int = 0
    failed: int = 0
    drain_seconds: float = 0.0


class SendQueue:
    def __init__(self, sender, global_per_second: float = 25,
                per_chat_per_minute: int = 20) -> None:
        self._sender = sender
        self._min_gap = 1.0 / global_per_second if global_per_second > 0 else 0.0
        self._per_chat_per_minute = per_chat_per_minute
        self._queue: deque = deque()
        self._chat_sent_at: dict[str, deque] = {}
        self._queued_keys: set[str] = set()
        self._sent_keys: set[str] = set()

    def __len__(self) -> int:
        return len(self._queue)

    def push(self, alert) -> bool:
        """Queue one alert. Returns False (and skips it) if this exact
        dedupe_key has already been queued or sent this run."""
        key = alert.dedupe_key
        if key and (key in self._queued_keys or key in self._sent_keys):
            log.warning("skipping duplicate alert (dedupe_key=%s)", key)
            return False
        if key:
            self._queued_keys.add(key)
        self._queue.append(alert)
        return True

    def _wait_for_chat_budget(self, chat_id: str) -> None:
        """Block until sending to this chat won't exceed the per-chat/minute cap."""
        history = self._chat_sent_at.setdefault(chat_id, deque())
        while True:
            now = time.monotonic()
            while history and now - history[0] >= 60:
                history.popleft()
            if len(history) < self._per_chat_per_minute:
                return
            time.sleep(max(0.0, 60 - (now - history[0])))

    def drain(self, on_result=None) -> DrainStats:
        """Send every queued alert, respecting rate limits and retrying 429s.

        `on_result(alert, send_result)` runs once per alert after its final
        attempt (success, or giving up) — the caller commits de-dup state and
        records activity there, exactly as a direct send would.
        """
        stats = DrainStats(detected=len(self._queue))
        start = time.monotonic()
        last_sent = 0.0

        while self._queue:
            alert = self._queue.popleft()
            chat_id = str(alert.chat_id)
            self._wait_for_chat_budget(chat_id)

            gap = self._min_gap - (time.monotonic() - last_sent)
            if gap > 0:
                time.sleep(gap)

            result = self._sender.send_alert(alert)
            attempts = 0
            while not result.ok and result.retry_after and attempts < MAX_429_RETRIES:
                attempts += 1
                wait = min(max(result.retry_after, 1), 60)
                log.warning("Telegram 429 for chat %s — retry %d/%d in %ds",
                           chat_id, attempts, MAX_429_RETRIES, wait)
                time.sleep(wait)
                result = self._sender.send_alert(alert)
            if not result.ok and result.retry_after:
                log.error(
                    "Telegram 429 for chat %s persisted after %d retries — "
                    "deferring to next cycle", chat_id, MAX_429_RETRIES,
                )

            last_sent = time.monotonic()
            self._chat_sent_at[chat_id].append(last_sent)

            if alert.dedupe_key:
                self._queued_keys.discard(alert.dedupe_key)
            if result.ok:
                stats.sent += 1
                if alert.dedupe_key:
                    self._sent_keys.add(alert.dedupe_key)
            else:
                stats.failed += 1
            if on_result is not None:
                on_result(alert, result)

        stats.drain_seconds = time.monotonic() - start
        return stats
