"""Factor/DriveHOS client hardening.

Every case here is a malformed-but-plausible API payload that used to raise a
non-ELDError (KeyError / AttributeError / ValueError). That matters because
scheduler.run_cycle only isolates ELDError per company — anything else escapes
and aborts the entire cycle, for every company, including the send phase.
"""

import io
import json
import unittest
import urllib.error
from unittest import mock

from src.config import Company, Secrets
from src.eld import build_provider
from src.eld.base import ELDError
from src.eld.factor import FactorELD, _as_int


class _OkResp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(code):
    return urllib.error.HTTPError(
        url="http://x", code=code, msg="err", hdrs={}, fp=io.BytesIO(b'{"e":1}')
    )


def _envelope(rows):
    return {"status_code": 200, "data": rows, "total_pages": 1}


class _Driver:
    def __init__(self, name, eld_driver_id=None):
        self.name = name
        self.eld_driver_id = eld_driver_id


class AsIntTests(unittest.TestCase):
    def test_coerces_api_number_shapes(self):
        self.assertEqual(_as_int(3600), 3600)
        self.assertEqual(_as_int(3600.0), 3600)
        self.assertEqual(_as_int("3600"), 3600)
        self.assertEqual(_as_int(" 3600.5 "), 3600)
        self.assertEqual(_as_int(-120), -120)

    def test_unparseable_becomes_zero(self):
        for bad in (None, "", "n/a", {}, [], True):
            self.assertEqual(_as_int(bad), 0, bad)


class SnapshotHardeningTests(unittest.TestCase):
    def setUp(self):
        self.f = FactorELD("pk", "ck", "http://api.test")
        self.f._BACKOFF_BASE = 0.0

    def _run(self, roster, statuses=(), vehicles=(), drivers=(), all_active=False):
        pages = [
            _OkResp(_envelope(list(roster))),
            _OkResp(_envelope(list(statuses))),
            _OkResp(_envelope(list(vehicles))),
        ]
        with mock.patch("src.eld.factor.urllib.request.urlopen", side_effect=pages):
            return self.f.fetch_snapshots(list(drivers), all_active=all_active)

    def test_roster_row_without_driver_id_is_skipped_not_fatal(self):
        roster = [
            {"first_name": "No", "last_name": "Id"},           # no driver_id
            {"driver_id": "d1", "first_name": "Real", "last_name": "Driver"},
        ]
        result = self._run(roster, all_active=True)
        self.assertEqual([s.driver_id for s in result.snapshots], ["d1"])

    def test_non_mapping_rows_are_filtered_out(self):
        roster = ["garbage", None, 42, {"driver_id": "d1", "first_name": "A", "last_name": "B"}]
        result = self._run(roster, all_active=True)
        self.assertEqual([s.driver_id for s in result.snapshots], ["d1"])

    def test_named_driver_matching_row_without_id_counts_as_unresolved(self):
        roster = [{"first_name": "Ali", "last_name": "Niezov"}]  # matched, but no id
        result = self._run(roster, drivers=[_Driver("Ali Niezov")])
        self.assertEqual(result.snapshots, [])
        self.assertEqual(result.unresolved_names, ["Ali Niezov"])

    def test_string_hos_values_do_not_raise(self):
        roster = [{"driver_id": "d1", "first_name": "A", "last_name": "B"}]
        statuses = [{"driver_id": "d1", "drive": "3600", "shift": None,
                     "break": "bad", "cycle": 7200.0, "current_status": "DS_D"}]
        result = self._run(roster, statuses=statuses, all_active=True)
        hos = result.snapshots[0].hos
        self.assertEqual(hos.drive_seconds, 3600)
        self.assertEqual(hos.shift_seconds, 0)
        self.assertEqual(hos.break_seconds, 0)
        self.assertEqual(hos.cycle_seconds, 7200)


class RetryPolicyTests(unittest.TestCase):
    def setUp(self):
        self.f = FactorELD("pk", "ck", "http://api.test")
        self.f._BACKOFF_BASE = 0.0

    @mock.patch("src.eld.factor.time.sleep")
    @mock.patch("src.eld.factor.urllib.request.urlopen")
    def test_transient_gateway_error_is_retried(self, urlopen, sleep):
        urlopen.side_effect = [_http_error(503), _OkResp(_envelope([{"driver_id": "d1"}]))]
        self.assertEqual(self.f._get_all("/v2/drivers"), [{"driver_id": "d1"}])
        self.assertEqual(urlopen.call_count, 2)

    @mock.patch("src.eld.factor.time.sleep")
    @mock.patch("src.eld.factor.urllib.request.urlopen")
    def test_network_error_is_retried_then_raises_eld_error(self, urlopen, sleep):
        urlopen.side_effect = urllib.error.URLError("connection reset")
        with self.assertRaises(ELDError):
            self.f._get("/v2/drivers")
        self.assertEqual(urlopen.call_count, self.f._MAX_RETRIES + 1)

    @mock.patch("src.eld.factor.time.sleep")
    @mock.patch("src.eld.factor.urllib.request.urlopen")
    def test_500_still_does_not_retry(self, urlopen, sleep):
        urlopen.side_effect = _http_error(500)
        with self.assertRaises(ELDError):
            self.f._get("/v2/drivers")
        self.assertEqual(urlopen.call_count, 1)

    @mock.patch("src.eld.factor.urllib.request.urlopen")
    def test_pagination_is_capped(self, urlopen):
        # total_pages far beyond the cap: stop rather than loop forever.
        urlopen.side_effect = lambda *a, **k: _OkResp(
            {"status_code": 200, "data": [{"driver_id": "d"}], "total_pages": 10_000}
        )
        rows = self.f._get_all("/v2/drivers")
        self.assertEqual(len(rows), self.f._MAX_PAGES)


class BuildProviderTests(unittest.TestCase):
    def _secrets(self, factor_key):
        return Secrets(
            factor_api_base_url="https://api.drivehos.app",
            leader_api_base_url="https://api.drivehos.app",
            telegram_bot_token="t",
            factor_api_key=factor_key,
        )

    def _company(self, company_key):
        return Company(
            name="ACME", provider="factor", driver_group_chat_id="-1",
            company_key_env="ACME_KEY", company_key=company_key,
        )

    def test_missing_provider_key_raises_eld_error(self):
        with self.assertRaises(ELDError) as cm:
            build_provider(self._company("ck"), self._secrets(None))
        self.assertIn("Provider API key", str(cm.exception))

    def test_missing_company_key_raises_eld_error(self):
        with self.assertRaises(ELDError) as cm:
            build_provider(self._company(None), self._secrets("pk"))
        self.assertIn("ACME_KEY", str(cm.exception))

    def test_both_keys_present_builds_client(self):
        provider = build_provider(self._company("ck"), self._secrets("pk"))
        self.assertEqual(provider.name, "factor")
        headers = provider._headers()
        self.assertEqual(headers["X-API-Provider-Key"], "pk")
        self.assertEqual(headers["X-API-Company-Key"], "ck")


if __name__ == "__main__":
    unittest.main()
