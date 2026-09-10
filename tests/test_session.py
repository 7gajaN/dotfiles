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

    def test_read_invalid_utf8_returns_none(self):
        self.path.write_bytes(b'\xff\xfe\x00\x01invalid-bytes-not-utf8-\x80\x81')
        self.assertIsNone(session.read_snapshot(self.path))

    def test_state_dir_respects_xdg_state_home(self):
        old_xdg = os.environ.get("XDG_STATE_HOME")
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                os.environ["XDG_STATE_HOME"] = tmpdir
                result = session.state_dir()
                self.assertEqual(result, pathlib.Path(tmpdir) / "hypr-session")
        finally:
            if old_xdg is None:
                os.environ.pop("XDG_STATE_HOME", None)
            else:
                os.environ["XDG_STATE_HOME"] = old_xdg

    def test_state_dir_fallback_without_xdg_state_home(self):
        old_xdg = os.environ.get("XDG_STATE_HOME")
        try:
            os.environ.pop("XDG_STATE_HOME", None)
            result = session.state_dir()
            self.assertTrue(str(result).endswith("/.local/state/hypr-session"))
        finally:
            if old_xdg is None:
                os.environ.pop("XDG_STATE_HOME", None)
            else:
                os.environ["XDG_STATE_HOME"] = old_xdg


def fake_proc(root: pathlib.Path, pid: int, cmdline: list[str], comm: str,
              cwd: str, children: list[int] = ()) -> None:
    """Build a minimal fake /proc entry for one process."""
    d = root / str(pid)
    (d / "task" / str(pid)).mkdir(parents=True)
    (d / "cmdline").write_bytes(b"\0".join(c.encode() for c in cmdline) + b"\0")
    (d / "comm").write_text(comm + "\n")
    (d / "task" / str(pid) / "children").write_text(
        " ".join(str(c) for c in children) + " " if children else ""
    )
    link = d / "cwd"
    target = root / f"cwdtarget{pid}"
    target.mkdir(exist_ok=True)
    link.symlink_to(target)
    # Record the intended logical cwd so the test can assert on it.
    (d / "cwd_expected").write_text(cwd)


class CommandResolutionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.proc = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_cmdline_splits_on_nul(self):
        fake_proc(self.proc, 10, ["firefox", "--new-window"], "firefox", "/home/u")
        self.assertEqual(
            session.read_cmdline(10, self.proc), ["firefox", "--new-window"]
        )

    def test_read_cmdline_missing_pid_returns_empty(self):
        self.assertEqual(session.read_cmdline(999, self.proc), [])

    def test_non_terminal_uses_cmdline_verbatim(self):
        fake_proc(self.proc, 10, ["firefox"], "firefox", "/home/u")
        client = {"class": "firefox", "pid": 10}
        self.assertEqual(session.resolve_command(client, self.proc), ["firefox"])

    def test_terminal_appends_child_shell_directory(self):
        shell_cwd = self.proc / "project"
        shell_cwd.mkdir()
        fake_proc(self.proc, 20, ["kitty"], "kitty", "/home/u", children=[21, 22])
        fake_proc(self.proc, 21, ["kitten"], "kitten", "/home/u")
        fake_proc(self.proc, 22, ["-zsh"], "zsh", str(shell_cwd))
        # Point pid 22's cwd symlink at the real directory we want reported.
        (self.proc / "22" / "cwd").unlink()
        (self.proc / "22" / "cwd").symlink_to(shell_cwd)
        client = {"class": "kitty", "pid": 20}
        self.assertEqual(
            session.resolve_command(client, self.proc),
            ["kitty", "--directory", str(shell_cwd)],
        )

    def test_terminal_without_shell_child_falls_back_to_bare_command(self):
        fake_proc(self.proc, 30, ["kitty"], "kitty", "/home/u", children=[31])
        fake_proc(self.proc, 31, ["kitten"], "kitten", "/home/u")
        client = {"class": "kitty", "pid": 30}
        self.assertEqual(session.resolve_command(client, self.proc), ["kitty"])

    def test_unreadable_process_yields_empty_command(self):
        client = {"class": "ghost", "pid": 4242}
        self.assertEqual(session.resolve_command(client, self.proc), [])


if __name__ == "__main__":
    unittest.main()
