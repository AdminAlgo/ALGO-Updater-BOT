import types
import unittest
from unittest.mock import patch

from src.rules import Alert
from src.sender_queue import MAX_429_RETRIES, SendQueue
from src.telegram_sender import SendResult


class _FakeClock:
    """A controllable monotonic clock: sleep() advances it instantly instead
    of actually waiting, so rate-limit logic can be tested without delay."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _fake_time_module():
    clock = _FakeClock()
    mod = types.SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep)
    return mod, clock


def _alert(dedupe_key, chat_id="chat-1"):
    return Alert(
        kind="low_hours", audience="driver_group", chat_id=chat_id,
        company="Acme", driver_name="Jane", driver_id="d1",
        text="hello", dedupe_key=dedupe_key,
    )


class _FakeSender:
    """Returns a scripted sequence of SendResults per dedupe_key; repeats the
    last entry once the script for that key is exhausted."""

    def __init__(self, script=None):
        self.script = script or {}
        self.calls: list[str] = []

    def send_alert(self, alert):
        self.calls.append(alert.dedupe_key)
        seq = self.script.get(alert.dedupe_key)
        if not seq:
            return SendResult(ok=True, chat_id=str(alert.chat_id))
        idx = self.calls.count(alert.dedupe_key) - 1
        return seq[idx] if idx < len(seq) else seq[-1]


class SendQueueTests(unittest.TestCase):
    def _drain(self, queue):
        results = []
        stats = queue.drain(on_result=lambda a, r: results.append((a, r)))
        return stats, results

    def test_all_alerts_sent(self):
        sender = _FakeSender()
        mod, _clock = _fake_time_module()
        with patch("src.sender_queue.time", mod):
            queue = SendQueue(sender, global_per_second=1000, per_chat_per_minute=1000)
            queue.push(_alert("a"))
            queue.push(_alert("b"))
            queue.push(_alert("c"))
            stats, results = self._drain(queue)

        self.assertEqual(stats.detected, 3)
        self.assertEqual(stats.sent, 3)
        self.assertEqual(stats.failed, 0)
        self.assertTrue(all(r.ok for _, r in results))

    def test_dedupe_key_blocks_double_push(self):
        sender = _FakeSender()
        mod, _clock = _fake_time_module()
        with patch("src.sender_queue.time", mod):
            queue = SendQueue(sender)
            first = queue.push(_alert("dup"))
            second = queue.push(_alert("dup"))
            self.assertTrue(first)
            self.assertFalse(second)
            self.assertEqual(len(queue), 1)
            stats, _ = self._drain(queue)

        self.assertEqual(stats.sent, 1)
        self.assertEqual(sender.calls, ["dup"])

    def test_429_retries_then_succeeds(self):
        sender = _FakeSender({
            "a": [
                SendResult(ok=False, chat_id="chat-1", error="HTTP 429: ...", retry_after=1),
                SendResult(ok=True, chat_id="chat-1"),
            ],
        })
        mod, clock = _fake_time_module()
        with patch("src.sender_queue.time", mod):
            queue = SendQueue(sender, global_per_second=1000, per_chat_per_minute=1000)
            queue.push(_alert("a"))
            stats, results = self._drain(queue)

        self.assertEqual(stats.sent, 1)
        self.assertEqual(stats.failed, 0)
        self.assertEqual(sender.calls, ["a", "a"])
        self.assertTrue(results[0][1].ok)
        self.assertGreaterEqual(clock.now, 1.0)  # actually waited the retry_after

    def test_429_gives_up_after_max_retries(self):
        always_429 = SendResult(ok=False, chat_id="chat-1", error="HTTP 429: ...", retry_after=1)
        sender = _FakeSender({"a": [always_429]})
        mod, _clock = _fake_time_module()
        with patch("src.sender_queue.time", mod):
            queue = SendQueue(sender, global_per_second=1000, per_chat_per_minute=1000)
            queue.push(_alert("a"))
            stats, results = self._drain(queue)

        self.assertEqual(stats.sent, 0)
        self.assertEqual(stats.failed, 1)
        # 1 initial attempt + MAX_429_RETRIES retries.
        self.assertEqual(len(sender.calls), MAX_429_RETRIES + 1)
        self.assertFalse(results[0][1].ok)

    def test_per_chat_cap_throttles_a_burst(self):
        sender = _FakeSender()
        mod, clock = _fake_time_module()
        with patch("src.sender_queue.time", mod):
            queue = SendQueue(sender, global_per_second=1000, per_chat_per_minute=2)
            queue.push(_alert("a", chat_id="chat-1"))
            queue.push(_alert("b", chat_id="chat-1"))
            queue.push(_alert("c", chat_id="chat-1"))  # 3rd in the same minute
            stats, _ = self._drain(queue)

        self.assertEqual(stats.sent, 3)
        # The 3rd send had to wait out the per-chat/minute window.
        self.assertGreaterEqual(clock.now, 60.0)


if __name__ == "__main__":
    unittest.main()
