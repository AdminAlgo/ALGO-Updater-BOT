import tempfile
import unittest
from pathlib import Path

from src.storage import atomic_write_json, read_json


class AtomicWriteJsonTests(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "sub" / "data.json"
            atomic_write_json(path, {"a": 1, "b": [1, 2, 3]})
            self.assertEqual(read_json(path), {"a": 1, "b": [1, 2, 3]})

    def test_no_leftover_tmp_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "data.json"
            atomic_write_json(path, {"x": 1})
            tmp = path.with_name(f".{path.name}.tmp")
            self.assertFalse(tmp.exists())

    def test_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "data.json"
            atomic_write_json(path, {"x": 1})
            atomic_write_json(path, {"x": 2})
            self.assertEqual(read_json(path), {"x": 2})

    def test_read_missing_returns_default(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "missing.json"
            self.assertIsNone(read_json(path))
            self.assertEqual(read_json(path, default=[]), [])

    def test_read_corrupt_returns_default(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "bad.json"
            path.write_text("{not valid json", "utf-8")
            self.assertEqual(read_json(path, default={}), {})


if __name__ == "__main__":
    unittest.main()
