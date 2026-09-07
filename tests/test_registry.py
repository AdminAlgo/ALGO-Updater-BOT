import tempfile
import unittest
from pathlib import Path

from src.registry import Candidate, GroupRegistry, match_drivers, match_title, match_unique

CANDIDATES = [
    Candidate("d1", "Ali Niezov", username="alin", truck="1833"),
    Candidate("d2", "Temirlan Baizakov", username="temir", truck="42"),
]

# Reproduces the #708 MILEMAX / ZOHA mismatch: the MILEMAX group's title
# names an office contact ("Nusratullo Nasrullaev") who isn't a roster
# driver at all, plus the "708" serial — no MILEMAX driver actually matches
# it. Only Bakhodir Saidov (a different company, ZOHA) has truck "708". The
# old code fell through to the truck tier and matched him globally, routing
# his alerts to the unrelated MILEMAX group.
CROSS_COMPANY_CANDIDATES = [
    Candidate("other", "Someone Else", truck="999", company="MILEMAX LLC"),
    Candidate("bakhodir", "Bakhodir Saidov", truck="708", company="ZOHA LLC"),
]


class MatchTitleTests(unittest.TestCase):
    def test_matches_by_full_name(self):
        driver_id, how = match_title("#truck | Ali Niezov | WWH", CANDIDATES)
        self.assertEqual((driver_id, how), ("d1", "name"))

    def test_matches_by_truck_number_as_whole_token(self):
        driver_id, how = match_title("Truck 1833 Crew", CANDIDATES)
        self.assertEqual((driver_id, how), ("d1", "truck"))

    def test_truck_number_does_not_match_as_substring(self):
        # "18" must not match candidate d1's truck "1833".
        short_truck_candidates = [Candidate("d3", "Someone", truck="18")]
        driver_id, how = match_title("Truck 1833", short_truck_candidates)
        self.assertEqual((driver_id, how), (None, None))

    def test_matches_by_username(self):
        driver_id, how = match_title("chat with @temir", CANDIDATES)
        self.assertEqual((driver_id, how), ("d2", "username"))

    def test_no_match(self):
        self.assertEqual(match_title("Random group name", CANDIDATES), (None, None))

    def test_empty_title(self):
        self.assertEqual(match_title("", CANDIDATES), (None, None))


class CrossCompanyTruckCollisionTests(unittest.TestCase):
    """A truck number can be reused across two companies (e.g. a leased truck
    dispatched under two authorities). A group title that names the company
    must not let that shared number auto-match the wrong company's driver."""

    def test_truck_number_does_not_cross_match_another_company(self):
        # Only Bakhodir (ZOHA) has truck "708" — old code matched him globally.
        # The title names MILEMAX, so narrowing to MILEMAX's own roster (which
        # has no truck-708 driver) must leave this unmatched, not mis-assign
        # Bakhodir's alerts to an unrelated company's group.
        did, how, reason = match_unique(
            "#708 Nusratullo Nasrullaev | MILEMAX LLC", CROSS_COMPANY_CANDIDATES
        )
        self.assertIsNone(did)
        self.assertIsNone(how)

    def test_correct_company_group_still_matches_by_name(self):
        did, how, reason = match_unique(
            "#708 | Bakhodir Saidov | ZOHA LLC", CROSS_COMPANY_CANDIDATES
        )
        self.assertEqual((did, how), ("bakhodir", "name"))

    def test_truck_match_still_works_within_the_same_company(self):
        did, how, reason = match_unique("Truck 708 | ZOHA LLC", CROSS_COMPANY_CANDIDATES)
        self.assertEqual((did, how), ("bakhodir", "truck"))


class MatchDriversTests(unittest.TestCase):
    def test_team_group_matches_multiple_by_name(self):
        matches = match_drivers("Ali Niezov & Temirlan Baizakov", CANDIDATES)
        self.assertEqual({m[0] for m in matches}, {"d1", "d2"})

    def test_name_tier_wins_over_truck_tier(self):
        # Title has both a name AND an unrelated truck number — name tier
        # should win exclusively (no truck-tier matches mixed in).
        matches = match_drivers("Ali Niezov truck 42", CANDIDATES)
        self.assertEqual([m[0] for m in matches], ["d1"])


class GroupRegistryTests(unittest.TestCase):
    def test_register_and_lookup(self):
        registry = GroupRegistry()
        changed = registry.register("d1", "-100", "Title", "name", "Ali Niezov")
        self.assertTrue(changed)
        self.assertEqual(registry.chat_for("d1"), "-100")
        self.assertTrue(registry.is_registered("d1"))
        self.assertEqual(registry.driver_for_chat("-100"), "d1")

    def test_register_same_chat_reports_unchanged(self):
        registry = GroupRegistry()
        registry.register("d1", "-100", "Title", "name", "Ali Niezov")
        changed = registry.register("d1", "-100", "Title", "name", "Ali Niezov")
        self.assertFalse(changed)

    def test_unregister_then_block_prevents_is_registered(self):
        registry = GroupRegistry()
        registry.register("d1", "-100", "Title", "name", "Ali Niezov")
        registry.unregister("d1")
        registry.block("d1")
        self.assertFalse(registry.is_registered("d1"))
        self.assertTrue(registry.is_blocked("d1"))
        registry.unblock("d1")
        self.assertFalse(registry.is_blocked("d1"))

    def test_coverage_counts_tagged_vs_missing(self):
        registry = GroupRegistry()
        registry.register("d1", "-100", "Title", "name", "Ali Niezov")
        registry.register("d2", "-200", "Title2", "name", "Temirlan Baizakov")
        registry.set_tag("d1", "alin", None)
        cov = registry.coverage()
        self.assertEqual(cov["total"], 2)
        self.assertEqual(cov["tagged"], 1)
        self.assertEqual(cov["missing"], ["Temirlan Baizakov"])

    def test_persistence_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "driver_groups.json"
            registry = GroupRegistry(path)
            registry.register("d1", "-100", "Title", "name", "Ali Niezov")
            registry.block("d2")
            registry.set_offset(42)
            registry.save()

            reloaded = GroupRegistry(path)
            self.assertEqual(reloaded.chat_for("d1"), "-100")
            self.assertTrue(reloaded.is_blocked("d2"))
            self.assertEqual(reloaded.offset, 42)


if __name__ == "__main__":
    unittest.main()
