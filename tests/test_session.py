import importlib.util
import itertools
import json
import os
import pathlib
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
SESSION_PY = REPO / ".config" / "hypr" / "session.py"

spec = importlib.util.spec_from_file_location("session", SESSION_PY)
session = importlib.util.module_from_spec(spec)
sys.modules["session"] = session
spec.loader.exec_module(session)


class SnapshotIOTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.tmp.name) / "session.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_roundtrip(self):
        snap = {"version": 1, "saved_at": "now", "windows": []}
        session.write_snapshot(snap, self.path)
        self.assertEqual(session.read_snapshot(self.path), snap)

    def test_read_missing_returns_none(self):
        self.assertIsNone(session.read_snapshot(self.path))

    def test_read_corrupt_returns_none(self):
        self.path.write_text("{not json")
        self.assertIsNone(session.read_snapshot(self.path))

    def test_read_empty_returns_none(self):
        self.path.write_text("")
        self.assertIsNone(session.read_snapshot(self.path))

    def test_write_leaves_no_temp_files(self):
        session.write_snapshot({"version": 1, "windows": []}, self.path)
        names = sorted(p.name for p in self.path.parent.iterdir())
        self.assertEqual(names, ["session.json"])

    def test_write_creates_parent_directory(self):
        nested = pathlib.Path(self.tmp.name) / "a" / "b" / "session.json"
        session.write_snapshot({"version": 1, "windows": []}, nested)
        self.assertTrue(nested.exists())


if __name__ == "__main__":
    unittest.main()
