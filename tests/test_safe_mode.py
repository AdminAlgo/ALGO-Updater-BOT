"""SAFE_MODE must withhold alerts without ever pretending it delivered them.

The bug these cover: a dry-run send used to return ok=True, so the scheduler
committed the alert's de-dup state and reported it as sent. Staging therefore
looked healthy while delivering nothing, and the alert was consumed — turning
SAFE_MODE off later would not resend it.
"""

import unittest

from src.sender_queue import SendQueue
from src.telegram_sender import TelegramSender

REAL_TOKEN = "123456780:AAaaBBbbCCccDDddEEeeFFffGGgg"


class _Alert:
    """Minimal stand-in for rules.Alert (what SendQueue/scheduler touch)."""

    def __init__(self, chat_id, dedupe_key="k1"):
        self.chat_id = chat_id
        self.dedupe_key = dedupe_key
        self.kind = "low_hours"
        self.driver_name = "Jane Driver"
        self.driver_id = "d1"
        self.company = "Acme"
        self.audience = "DRIVER_GROUP"
        self.text = "2 hours left"
        self.extra_chat_ids = ()
        self.committed = 0

    def commit(self, state, now):
        self.committed += 1


class SuppressionTests(unittest.TestCase):
    def test_safe_mode_send_is_not_a_success(self):
        sender = TelegramSender("placeholder-token", dry_run=True)
        res = sender.send_message("-100123", "hello", kind="low_hours")
        self.assertFalse(res.ok)
        self.assertTrue(res.suppressed)
        self.assertIn("withheld", res.error)

    def test_suppressed_send_has_no_retry_after(self):
        # Otherwise SendQueue would treat a withheld message as a 429 and sit
        # in its retry loop for every alert, every cycle.
        sender = TelegramSender("placeholder-token", dry_run=True)
        self.assertIsNone(sender.send_message("-100123", "hi").retry_after)

    def test_allowlisted_chat_is_not_suppressed(self):
        sender = TelegramSender(REAL_TOKEN, dry_run=True, allow_chats=["-100777"])
        self.assertFalse(sender._suppressed("-100777"))
        self.assertFalse(sender._suppressed(-100777))  # int ids match too
        self.assertTrue(sender._suppressed("-100123"))

    def test_blank_entries_in_the_allowlist_are_ignored(self):
        sender = TelegramSender(REAL_TOKEN, dry_run=True,
                                allow_chats=["-100777", "", "  ", "-100888"])
        self.assertEqual(sender.allow_chats, frozenset({"-100777", "-100888"}))

    def test_allowlist_with_a_placeholder_token_is_refused(self):
        # An allowlist means real sends are about to happen, so a fake token
        # has to fail at construction rather than at the first alert.
        with self.assertRaises(ValueError):
            TelegramSender("placeholder-token", dry_run=True, allow_chats=["-100777"])

    def test_no_allowlist_still_tolerates_a_placeholder_token(self):
        TelegramSender("placeholder-token", dry_run=True)  # must not raise

    def test_check_token_is_skipped_only_when_nothing_can_send(self):
        muted = TelegramSender("placeholder-token", dry_run=True)
        self.assertTrue(muted.check_token().ok)  # no network call


class QueueAccountingTests(unittest.TestCase):
    def _drain(self, chat_id, allow=()):
        sender = TelegramSender(REAL_TOKEN, dry_run=True, allow_chats=allow)
        queue = SendQueue(sender, global_per_second=1000, per_chat_per_minute=1000)
        alert = _Alert(chat_id)
        queue.push(alert)
        results = []
        stats = queue.drain(on_result=lambda a, r: results.append(r))
        return stats, alert, results[0]

    def test_withheld_alert_counts_as_suppressed_not_sent_or_failed(self):
        stats, _, _ = self._drain("-100123", allow=["-100777"])
        self.assertEqual((stats.sent, stats.failed, stats.suppressed), (0, 0, 1))
        self.assertEqual(stats.detected, 1)

    def test_withheld_alert_is_not_marked_sent_for_the_run(self):
        # _sent_keys is the queue's "already delivered" memory; a withheld
        # alert must stay eligible so it goes out once the chat is allowed.
        sender = TelegramSender(REAL_TOKEN, dry_run=True)
        queue = SendQueue(sender, global_per_second=1000, per_chat_per_minute=1000)
        queue.push(_Alert("-100123"))
        queue.drain()
        self.assertNotIn("k1", queue._sent_keys)


class SchedulerCycleTests(unittest.TestCase):
    """End-to-end through the real run_cycle: a withheld alert must stay
    pending, so the SAME alert is still detected on the next cycle."""

    def _fixtures(self):
        from src.config import Company, Config, LowHoursThresholds, Secrets
        from src.eld.base import (ConnectionState, DriverSnapshot, HosTimers,
                                  SnapshotResult)
        from src.registry import GroupRegistry
        from src.state import AlertState

        snap = DriverSnapshot(
            driver_id="d1", name="Jane Driver", username=None,
            duty_status_code="DS_D",  # Driving => alertable
            hos=HosTimers(drive_seconds=60 * 60, shift_seconds=4 * 3600,
                          break_seconds=4 * 3600, cycle_seconds=40 * 3600),
            connection=ConnectionState(has_vehicle=False),
        )
        company = Company(name="Acme", provider="factor",
                          driver_group_chat_id="-100123", monitor_all_drivers=True)
        config = Config(
            poll_interval_seconds=120, team_group_chat_id=None,
            low_hours_thresholds_minutes=LowHoursThresholds(
                driver_group=[120], team_group=[]),
            disconnect_realert_minutes=60, disconnect_stale_minutes=30,
            shift_limit_hours=14,
            connection_required_statuses=["Driving", "On Duty", "Yard Move"],
            companies=[company],
            secrets=Secrets(factor_api_base_url="https://x",
                            leader_api_base_url="https://x",
                            telegram_bot_token=REAL_TOKEN),
            attach_log_image=False, disconnect_alerts_enabled=False,
            on_duty_alert_hours=0,
        )
        registry = GroupRegistry()
        registry.register("d1", "-100123", "Jane's group", "manual", "Jane Driver")
        return config, registry, AlertState(), SnapshotResult(snapshots=[snap])

    def _run(self, sender, monkey_result):
        from src import scheduler

        config, registry, state, result = self._fixtures()
        original = scheduler.build_provider
        scheduler.build_provider = lambda company, secrets: type(
            "P", (), {"fetch_snapshots": lambda self, drivers, all_active=False: monkey_result}
        )()
        try:
            first = scheduler.run_cycle(config, sender, state, registry=registry,
                                        discover=False)
            second = scheduler.run_cycle(config, sender, state, registry=registry,
                                         discover=False)
        finally:
            scheduler.build_provider = original
        return first, second

    def test_withheld_alert_is_re_detected_next_cycle(self):
        sender = TelegramSender(REAL_TOKEN, dry_run=True)  # nothing allowlisted
        first, second = self._run(sender, self._fixtures()[3])

        self.assertEqual(first.alerts, 1)
        self.assertEqual((first.sent, first.failed, first.suppressed), (0, 0, 1))
        # The whole point: not consumed. A real send would have de-duped it away.
        self.assertEqual(second.alerts, 1)
        self.assertEqual(second.suppressed, 1)

    def test_coverage_counts_the_linked_driver(self):
        sender = TelegramSender(REAL_TOKEN, dry_run=True)
        first, _ = self._run(sender, self._fixtures()[3])
        self.assertEqual(first.monitored, 1)
        self.assertEqual(first.unlinked, [])


if __name__ == "__main__":
    unittest.main()
