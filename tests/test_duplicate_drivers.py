"""One driver must never read as two people.

The bug these cover: SUPREME CARRIER SERVICES CORP was added as a company while
GOWIN EXPEDITED TRANSPORTING LLC was already configured on the SAME ELD account,
so every one of those drivers came back twice — once per company entry. Telegram
then showed "matches 2 drivers" for a name that belongs to one person, /assign
refused to pick, and auto-linking called every SUPREME group title ambiguous, so
those drivers could not be linked to a group at all (and an unlinked driver gets
no alerts).
"""

import unittest
from datetime import datetime, timezone

from src.eld.base import ConnectionState, DriverSnapshot, HosTimers, SnapshotResult
from src.commands import process_updates
from src.registry import (Candidate, GroupRegistry, best_match, dedupe_drivers,
                          unique_drivers)
from src.roster_cache import RosterCache
from src.scheduler import run_cycle


def _snap(driver_id: str, name: str, truck: str | None = None) -> DriverSnapshot:
    return DriverSnapshot(
        driver_id=driver_id,
        name=name,
        username=None,
        duty_status_code="DS_OFF",
        hos=HosTimers(drive_seconds=36000, shift_seconds=36000,
                      break_seconds=28800, cycle_seconds=200000),
        connection=ConnectionState(has_vehicle=bool(truck), vehicle_status="ACTIVE",
                                   vehicle_number=truck,
                                   last_telemetry=datetime.now(timezone.utc)),
        raw={},
    )


class DedupeHelperTests(unittest.TestCase):
    def test_second_company_loses_the_driver_and_is_reported(self):
        seen: dict[str, str] = {}
        first, dupes = dedupe_drivers(
            [Candidate("ab1938", "FARIDUN YORMAHMADZODA")], seen, "GOWIN")
        second, dupes2 = dedupe_drivers(
            [Candidate("ab1938", "FARIDUN YORMAHMADZODA")], seen, "SUPREME")
        self.assertEqual(len(first), 1)
        self.assertEqual(dupes, [])
        self.assertEqual(second, [])
        self.assertEqual(dupes2, ["FARIDUN YORMAHMADZODA (SUPREME duplicates GOWIN)"])

    def test_two_different_drivers_both_survive(self):
        seen: dict[str, str] = {}
        kept, dupes = dedupe_drivers(
            [Candidate("d1", "AZIZ KARIMOV"), Candidate("d2", "AZIZ TOSHEV")],
            seen, "WWH INC")
        self.assertEqual(len(kept), 2)
        self.assertEqual(dupes, [])

    def test_unique_drivers_keeps_first_row_and_order(self):
        rows = [Candidate("d1", "A", None, None, "GOWIN"),
                Candidate("d2", "B", None, None, "ZOHA"),
                Candidate("d1", "A", None, None, "SUPREME")]
        out = unique_drivers(rows)
        self.assertEqual([c.driver_id for c in out], ["d1", "d2"])
        self.assertEqual(out[0].company, "GOWIN")


class RosterCacheTests(unittest.TestCase):
    def _cache(self) -> RosterCache:
        cache = RosterCache(lambda: None)
        cache.prime("GOWIN", [_snap("ab1938", "FARIDUN YORMAHMADZODA", "0101"),
                              _snap("6b9ed5", "Ramzullo Zubaidov", "0101")])
        cache.prime("SUPREME", [_snap("ab1938", "FARIDUN YORMAHMADZODA", "0101"),
                                _snap("6b9ed5", "Ramzullo Zubaidov", "0101")])
        return cache

    def test_roster_lists_each_driver_once(self):
        roster = self._cache().get()
        self.assertEqual([c.driver_id for c in roster], ["ab1938", "6b9ed5"])

    def test_duplicates_are_reported_for_the_operator(self):
        self.assertEqual(len(self._cache().duplicates), 2)

    def test_snapshot_records_are_not_double_counted(self):
        records = self._cache().get_snapshot_records()
        self.assertEqual(len(records), 2)
        self.assertEqual({company for company, _ in records}, {"GOWIN"})

    def test_a_company_losing_a_driver_does_not_lose_the_roster(self):
        """The same driver moving to another carrier still appears once."""
        cache = RosterCache(lambda: None)
        cache.prime("GOWIN", [_snap("ab1938", "FARIDUN YORMAHMADZODA")])
        cache.prime("SUPREME", [_snap("ab1938", "FARIDUN YORMAHMADZODA"),
                                _snap("6c9904", "HALIMJON PIRNAZAROV")])
        self.assertEqual({c.driver_id for c in cache.get()}, {"ab1938", "6c9904"})


class GroupLinkingTests(unittest.TestCase):
    """A title that names one driver must link, not read as ambiguous."""

    def test_duplicated_driver_no_longer_blocks_auto_linking(self):
        roster = unique_drivers([
            Candidate("6c9904", "HALIMJON PIRNAZAROV", None, "1985", "GOWIN"),
            Candidate("6c9904", "HALIMJON PIRNAZAROV", None, "1985", "SUPREME"),
        ])
        m = best_match("#1985 Halimjon Pirnazarov | SUPREME CARRIER SERVICES CORP", roster)
        self.assertEqual(m.driver_id, "6c9904")
        self.assertEqual(m.confidence, "high")

    def test_two_real_drivers_sharing_a_title_stay_ambiguous(self):
        """Dedup must not weaken the real ambiguity guard."""
        roster = [Candidate("d1", "AZIZ KARIMOV", None, "118", "WWH INC"),
                  Candidate("d2", "AZIZ KARIMOV", None, "900", "ZOHA LLC")]
        self.assertIsNone(best_match("Aziz Karimov", roster).driver_id)


class _StubProvider:
    def __init__(self, snapshots):
        self._snapshots = snapshots

    def fetch_snapshots(self, drivers, all_active=False):
        result = SnapshotResult()
        result.snapshots.extend(self._snapshots)
        return result


class AssignCommandTests(unittest.TestCase):
    """The exact exchange from the #0101 group: /assign had to pick a driver
    and instead reported the one driver as two."""

    DUPLICATED = [
        Candidate("ab1938", "FARIDUN YORMAHMADZODA", "fdn1997", "0101", "GOWIN"),
        Candidate("6b9ed5", "Ramzullo Zubaidov", "rz2000", "0101", "GOWIN"),
        Candidate("ab1938", "FARIDUN YORMAHMADZODA", "fdn1997", "0101", "SUPREME"),
        Candidate("6b9ed5", "Ramzullo Zubaidov", "rz2000", "0101", "SUPREME"),
    ]

    def _run(self, text):
        from tests.test_commands import FakeSender, _grp

        sender = FakeSender([{"update_id": 1, "message": _grp(text, chat_id=-5307862548,
                                                              title="#0101 SUPREME")}])
        registry = GroupRegistry()
        process_updates(sender, registry, list(self.DUPLICATED), admin_user_ids=[999])
        return sender.last(), registry

    def test_assign_links_the_driver_instead_of_calling_it_ambiguous(self):
        reply, registry = self._run(
            "/assign Ramzullo Zubaidov | SUPREME CARRIER SERVICES CORP")
        self.assertIn("✅", reply)
        self.assertEqual(registry.chat_for("6b9ed5"), "-5307862548")

    def test_roster_reports_one_person(self):
        reply, _ = self._run("/roster FARIDUN YORMAHMADZODA")
        self.assertIn("Matches for", reply)
        self.assertEqual(reply.count("FARIDUN YORMAHMADZODA"), 2)  # query + 1 hit


class SchedulerTests(unittest.TestCase):
    """A driver returned by two companies is polled — and alerted — once."""

    def test_cycle_counts_the_driver_once(self):
        from src import scheduler
        from src.config import Company, Config, LowHoursThresholds, Secrets
        from src.state import AlertState

        config = Config(
            poll_interval_seconds=120, team_group_chat_id="-100",
            low_hours_thresholds_minutes=LowHoursThresholds(
                driver_group=[120], team_group=[]),
            disconnect_realert_minutes=60, disconnect_stale_minutes=30,
            shift_limit_hours=14,
            connection_required_statuses=["Driving", "On Duty", "Yard Move"],
            companies=[
                Company(name="GOWIN", provider="leader",
                        driver_group_chat_id="-100", monitor_all_drivers=True),
                Company(name="SUPREME", provider="leader",
                        driver_group_chat_id="-100", monitor_all_drivers=True),
            ],
            secrets=Secrets(factor_api_base_url="https://x",
                            leader_api_base_url="https://x",
                            telegram_bot_token="123456780:AAaaBBbbCCccDDddEEeeFFffGGgg"),
            attach_log_image=False, disconnect_alerts_enabled=False,
            on_duty_alert_hours=0,
        )
        snaps = [_snap("ab1938", "FARIDUN YORMAHMADZODA")]
        original = scheduler.build_provider
        scheduler.build_provider = lambda company, secrets: _StubProvider(snaps)
        try:
            stats = run_cycle(config, sender=None, state=AlertState(),
                              registry=GroupRegistry(), discover=False)
        finally:
            scheduler.build_provider = original

        self.assertEqual(stats.monitored, 1)
        self.assertEqual(stats.duplicates,
                         ["FARIDUN YORMAHMADZODA (SUPREME duplicates GOWIN)"])


class SharedKeyWarningTests(unittest.TestCase):
    """Two entries on one key is the config mistake behind all of the above."""

    def test_two_companies_on_one_key_are_named_in_the_log(self):
        from src.config import Company, _warn_shared_company_keys

        companies = [
            Company(name="GOWIN", provider="leader", driver_group_chat_id="-100",
                    company_key="same-key"),
            Company(name="SUPREME", provider="leader", driver_group_chat_id="-100",
                    company_key="same-key"),
            Company(name="ZOHA", provider="leader", driver_group_chat_id="-100",
                    company_key="own-key"),
        ]
        with self.assertLogs("eld_alert_bot", level="ERROR") as caught:
            _warn_shared_company_keys(companies)
        self.assertEqual(len(caught.records), 1)
        self.assertIn("GOWIN and SUPREME", caught.output[0])

    def test_a_disabled_entry_is_not_reported(self):
        from src.config import Company, _warn_shared_company_keys

        companies = [
            Company(name="GOWIN", provider="leader", driver_group_chat_id="-100",
                    company_key="same-key"),
            Company(name="SUPREME", provider="leader", driver_group_chat_id="-100",
                    company_key="same-key", enabled=False),
        ]
        with self.assertNoLogs("eld_alert_bot", level="ERROR"):
            _warn_shared_company_keys(companies)


if __name__ == "__main__":
    unittest.main()
