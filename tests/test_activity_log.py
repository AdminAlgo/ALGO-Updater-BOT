import tempfile
import unittest
from pathlib import Path

from src.activity_log import MAX_ENTRIES, ActivityEntry, ActivityLog


def _entry(i: int) -> ActivityEntry:
    return ActivityEntry(
        ts=f"2026-01-01T00:00:{i:02d}Z", kind="low_hours", company="ACME",
        driver_name=f"Driver {i}", chat_id="-100", audience="driver_group",
        ok=True, error=None,
    )


class ActivityLogTests(unittest.TestCase):
    def test_record_and_recent_newest_first(self):
        log = ActivityLog()  # in-memory only
        log.record(_entry(1))
        log.record(_entry(2))
        log.record(_entry(3))
        recent = log.recent(2)
        self.assertEqual([e["driver_name"] for e in recent], ["Driver 3", "Driver 2"])

    def test_trims_to_max_entries(self):
        log = ActivityLog()
        for i in range(MAX_ENTRIES + 50):
            log.record(_entry(i))
        self.assertEqual(len(log.recent(MAX_ENTRIES + 50)), MAX_ENTRIES)
        # Oldest entries dropped: the newest recorded is still present.
        newest = log.recent(1)[0]
        self.assertEqual(newest["driver_name"], f"Driver {MAX_ENTRIES + 49}")

    def test_persists_and_reloads(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "activity_log.json"
            log = ActivityLog(path)
            log.record(_entry(1))
            log.save()

            reloaded = ActivityLog(path)
            self.assertEqual(len(reloaded.recent(10)), 1)
            self.assertEqual(reloaded.recent(10)[0]["driver_name"], "Driver 1")

    def test_record_failure_entry(self):
        log = ActivityLog()
        entry = ActivityEntry(
            ts="2026-01-01T00:00:00Z", kind="disconnect", company="ACME",
            driver_name="Driver X", chat_id="-100", audience="team_group",
            ok=False, error="Telegram 403",
        )
        log.record(entry)
        got = log.recent(1)[0]
        self.assertFalse(got["ok"])
        self.assertEqual(got["error"], "Telegram 403")


if __name__ == "__main__":
    unittest.main()
