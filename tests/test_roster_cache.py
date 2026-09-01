"""RosterCache: the shared roster must never block the scheduler.

The command loop calls `get()` on every iteration and the dashboard calls it on
every page load, while the scheduler calls `prime()` once per cycle. A fetch can
take minutes (three HTTP calls per company, plus rate-limit backoff), so it must
happen with the lock released.
"""

import threading
import unittest
from unittest import mock

from src.eld.base import ELDError
from src.roster_cache import RosterCache


class _Snap:
    """Minimal stand-in for DriverSnapshot (only what prime/_fetch read)."""

    def __init__(self, driver_id, name):
        self.driver_id = driver_id
        self.name = name
        self.username = None
        self.connection = mock.Mock(vehicle_number="T1")


class _Company:
    def __init__(self, name, enabled=True):
        self.name = name
        self.enabled = enabled
        self.drivers = []
        self.monitor_all_drivers = True
        self.provider = "factor"


def _config(*companies):
    return mock.Mock(companies=list(companies), secrets=mock.Mock())


class LockHeldDuringFetchTests(unittest.TestCase):
    def test_prime_is_not_blocked_while_a_fetch_is_in_flight(self):
        """A slow provider call must not stall the scheduler's prime()."""
        cache = RosterCache(lambda: _config(_Company("A")), ttl_seconds=0)
        in_fetch = threading.Event()
        release = threading.Event()

        def slow_fetch(*_a, **_k):
            in_fetch.set()
            release.wait(5)  # stand in for a long HTTP round trip
            return mock.Mock(snapshots=[_Snap("d1", "A Driver")])

        provider = mock.Mock()
        provider.fetch_snapshots.side_effect = slow_fetch

        with mock.patch("src.roster_cache.build_provider", return_value=provider):
            t = threading.Thread(target=cache.get, daemon=True)
            t.start()
            self.assertTrue(in_fetch.wait(5), "fetch never started")

            # The scheduler primes mid-fetch; with the lock held across the
            # network call this would block until the fetch finished.
            primed = threading.Thread(
                target=cache.prime, args=("B", [_Snap("d2", "B Driver")]), daemon=True
            )
            primed.start()
            primed.join(2)
            self.assertFalse(primed.is_alive(), "prime() blocked behind the fetch")

            release.set()
            t.join(5)

    def test_concurrent_get_does_not_start_a_second_fetch(self):
        cache = RosterCache(lambda: _config(_Company("A")), ttl_seconds=0)
        in_fetch = threading.Event()
        release = threading.Event()
        calls = []

        def slow_fetch(*_a, **_k):
            calls.append(1)
            in_fetch.set()
            release.wait(5)
            return mock.Mock(snapshots=[_Snap("d1", "A Driver")])

        provider = mock.Mock()
        provider.fetch_snapshots.side_effect = slow_fetch

        with mock.patch("src.roster_cache.build_provider", return_value=provider):
            t = threading.Thread(target=cache.get, daemon=True)
            t.start()
            self.assertTrue(in_fetch.wait(5))
            self.assertEqual(cache.get(), [])  # serves the (empty) cache, no fetch
            release.set()
            t.join(5)

        self.assertEqual(len(calls), 1)


class PartialFailureTests(unittest.TestCase):
    def test_company_that_errors_keeps_its_previous_roster(self):
        companies = (_Company("A"), _Company("B"))
        cache = RosterCache(lambda: _config(*companies), ttl_seconds=0)
        cache.prime("A", [_Snap("a1", "A Driver")])
        cache.prime("B", [_Snap("b1", "B Driver")])

        def build(company, _secrets):
            if company.name == "A":
                raise ELDError("A: HTTP 500")
            provider = mock.Mock()
            provider.fetch_snapshots.return_value = mock.Mock(
                snapshots=[_Snap("b2", "B Driver Two")]
            )
            return provider

        with mock.patch("src.roster_cache.build_provider", side_effect=build):
            names = {c.driver_id for c in cache.get(force=True)}

        # B refreshed; A survived its own failure instead of vanishing.
        self.assertIn("a1", names)
        self.assertIn("b2", names)
        self.assertIsNotNone(cache.last_error)

    def test_disabled_company_is_dropped_on_refresh(self):
        a, b = _Company("A"), _Company("B")
        cache = RosterCache(lambda: _config(a, b), ttl_seconds=0)
        cache.prime("A", [_Snap("a1", "A Driver")])
        cache.prime("B", [_Snap("b1", "B Driver")])

        b.enabled = False  # paused via the dashboard between refreshes
        provider = mock.Mock()
        provider.fetch_snapshots.return_value = mock.Mock(
            snapshots=[_Snap("a2", "A Driver Two")]
        )
        with mock.patch("src.roster_cache.build_provider", return_value=provider):
            names = {c.driver_id for c in cache.get(force=True)}

        self.assertEqual(names, {"a2"})

    def test_config_loader_failure_serves_stale_roster(self):
        def boom():
            raise RuntimeError("config.yaml is mid-write")

        cache = RosterCache(boom, ttl_seconds=0)
        cache.prime("A", [_Snap("a1", "A Driver")])
        self.assertEqual([c.driver_id for c in cache.get(force=True)], ["a1"])
        self.assertIn("config.yaml", cache.last_error or "")


if __name__ == "__main__":
    unittest.main()
