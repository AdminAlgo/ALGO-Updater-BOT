import io
import json
import unittest
import urllib.error
from unittest import mock

from src.eld.base import ELDError
from src.eld.factor import FactorELD


def _http_error(code):
    return urllib.error.HTTPError(
        url="http://x", code=code, msg="err", hdrs={}, fp=io.BytesIO(b'{"e":1}')
    )


class _OkResp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Factor429Tests(unittest.TestCase):
    def setUp(self):
        self.f = FactorELD("pk", "ck", "http://api.test")
        self.f._BACKOFF_BASE = 0.0  # no real waiting in tests

    @mock.patch("src.eld.factor.time.sleep")
    @mock.patch("src.eld.factor.urllib.request.urlopen")
    def test_retries_429_then_succeeds(self, urlopen, sleep):
        urlopen.side_effect = [
            _http_error(429),
            _http_error(429),
            _OkResp({"status_code": 200, "data": [{"driver_id": "d1"}]}),
        ]
        rows = self.f._get_all("/v2/drivers")
        self.assertEqual(rows, [{"driver_id": "d1"}])
        self.assertEqual(urlopen.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    @mock.patch("src.eld.factor.time.sleep")
    @mock.patch("src.eld.factor.urllib.request.urlopen")
    def test_gives_up_after_max_retries(self, urlopen, sleep):
        urlopen.side_effect = _http_error(429)
        with self.assertRaises(ELDError) as cm:
            self.f._get("/v2/drivers")
        self.assertIn("429", str(cm.exception))
        self.assertEqual(urlopen.call_count, self.f._MAX_RETRIES + 1)

    @mock.patch("src.eld.factor.time.sleep")
    @mock.patch("src.eld.factor.urllib.request.urlopen")
    def test_non_429_does_not_retry(self, urlopen, sleep):
        urlopen.side_effect = _http_error(500)
        with self.assertRaises(ELDError):
            self.f._get("/v2/drivers")
        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
