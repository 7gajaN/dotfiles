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


class FilteringTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.tmp.name) / "denylist.txt"

    def tearDown(self):
        self.tmp.cleanup()

    def test_load_denylist_skips_comments_and_blanks(self):
        self.path.write_text("# a comment\n\n^steam$\ndiscord\n")
        patterns = session.load_denylist(self.path)
        self.assertEqual([p.pattern for p in patterns], ["^steam$", "discord"])

    def test_load_denylist_missing_file_is_empty(self):
        self.assertEqual(session.load_denylist(self.path), [])

    def test_load_denylist_ignores_invalid_regex(self):
        self.path.write_text("^good$\n[unclosed\n")
        patterns = session.load_denylist(self.path)
        self.assertEqual([p.pattern for p in patterns], ["^good$"])

    def test_load_denylist_invalid_utf8_returns_empty(self):
        self.path.write_bytes(b"^good$\n\xff\xfe not valid utf8\n")
        patterns = session.load_denylist(self.path)
        self.assertEqual(patterns, [])

    def test_denylisted_class_is_skipped(self):
        self.path.write_text("^steam$\n")
        deny = session.load_denylist(self.path)
        client = {"class": "steam", "pid": 1, "floating": False}
        self.assertEqual(
            session.should_skip(client, [client], deny, ["steam"]), "denylist"
        )

    def test_empty_command_is_skipped(self):
        client = {"class": "ghost", "pid": 1, "floating": False}
        self.assertEqual(
            session.should_skip(client, [client], [], []), "no-command"
        )

    def test_floating_sibling_of_same_pid_is_transient(self):
        main = {"class": "gimp", "pid": 7, "floating": False}
        dialog = {"class": "gimp", "pid": 7, "floating": True}
        clients = [main, dialog]
        self.assertTrue(session.is_transient(dialog, clients))
        self.assertFalse(session.is_transient(main, clients))

    def test_lone_floating_window_is_not_transient(self):
        client = {"class": "pavucontrol", "pid": 9, "floating": True}
        self.assertFalse(session.is_transient(client, [client]))

    def test_floating_window_of_different_pid_is_not_transient(self):
        a = {"class": "kitty", "pid": 1, "floating": False}
        b = {"class": "kitty", "pid": 2, "floating": True}
        self.assertFalse(session.is_transient(b, [a, b]))

    def test_kept_window_returns_none(self):
        client = {"class": "kitty", "pid": 1, "floating": False}
        self.assertIsNone(session.should_skip(client, [client], [], ["kitty"]))


class BuildSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.proc = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def client(self, cls, pid, ws, monitor="DP-2", floating=False):
        return {
            "class": cls, "pid": pid, "floating": floating,
            "workspace": {"id": ws, "name": str(ws)},
            "monitor": 0 if monitor == "DP-2" else 1,
            "at": [10, 20], "size": [800, 600], "fullscreen": 0,
        }

    def test_orders_by_workspace_then_stack_position(self):
        for pid, cls in ((1, "a"), (2, "b"), (3, "c")):
            fake_proc(self.proc, pid, [cls], cls, "/home/u")
        clients = [
            self.client("c", 3, 10),
            self.client("a", 1, 1),
            self.client("b", 2, 1),
        ]
        snap = session.build_snapshot(clients, [], self.proc, "T")
        self.assertEqual(
            [(w["order"], w["class"], w["workspace"]) for w in snap["windows"]],
            [(0, "a", 1), (1, "b", 1), (2, "c", 10)],
        )

    def test_records_schema_version_and_timestamp(self):
        snap = session.build_snapshot([], [], self.proc, "2026-09-10T10:00:00+03:00")
        self.assertEqual(snap["version"], 1)
        self.assertEqual(snap["saved_at"], "2026-09-10T10:00:00+03:00")
        self.assertEqual(snap["windows"], [])

    def test_records_monitor_index_verbatim(self):
        # hyprctl reports monitor as an index, not a name. The field is
        # informational only: placement is driven by the workspace rule,
        # and workspaces 1-10 and 11 are already pinned to monitors in
        # hyprland.lua.
        fake_proc(self.proc, 1, ["kitty"], "kitty", "/home/u")
        clients = [self.client("kitty", 1, 11, monitor="DP-3")]
        snap = session.build_snapshot(clients, [], self.proc, "T")
        self.assertEqual(snap["windows"][0]["monitor"], 1)

    def test_geometry_and_fullscreen_are_preserved(self):
        fake_proc(self.proc, 1, ["mpv"], "mpv", "/home/u")
        c = self.client("mpv", 1, 2, floating=True)
        c["fullscreen"] = 2
        snap = session.build_snapshot([c], [], self.proc, "T")
        w = snap["windows"][0]
        self.assertEqual(w["at"], [10, 20])
        self.assertEqual(w["size"], [800, 600])
        self.assertEqual(w["fullscreen"], 2)
        self.assertTrue(w["floating"])

    def test_skipped_windows_are_absent_and_do_not_gap_order(self):
        fake_proc(self.proc, 1, ["kitty"], "kitty", "/home/u")
        # pid 2 has no fake /proc entry, so its command cannot be resolved.
        clients = [self.client("kitty", 1, 1), self.client("ghost", 2, 1)]
        snap = session.build_snapshot(clients, [], self.proc, "T")
        self.assertEqual([w["class"] for w in snap["windows"]], ["kitty"])
        self.assertEqual([w["order"] for w in snap["windows"]], [0])


class EventParsingTest(unittest.TestCase):
    def test_parses_event_and_payload(self):
        self.assertEqual(
            session.parse_event("openwindow>>0xabc,1,kitty,a title"),
            ("openwindow", "0xabc,1,kitty,a title"),
        )

    def test_payload_may_contain_the_separator(self):
        self.assertEqual(
            session.parse_event("activewindow>>firefox,a>>b"),
            ("activewindow", "firefox,a>>b"),
        )

    def test_empty_payload(self):
        self.assertEqual(session.parse_event("closewindow>>"), ("closewindow", ""))

    def test_malformed_line_returns_none(self):
        self.assertIsNone(session.parse_event("garbage without separator"))

    def test_blank_line_returns_none(self):
        self.assertIsNone(session.parse_event(""))

    def test_window_events_set_contents(self):
        self.assertEqual(
            session.WINDOW_EVENTS,
            frozenset({"openwindow", "closewindow", "movewindow", "movewindowv2"}),
        )


class FakeHypr:
    def __init__(self):
        self.execs = []
        self.dispatches = []

    def exec_with_rules(self, rules, argv):
        self.execs.append((rules, argv))
        return True

    def dispatch(self, args):
        self.dispatches.append(args)
        return True

    def clients(self):
        return []


class FailingHypr(FakeHypr):
    def exec_with_rules(self, rules, argv):
        self.execs.append((rules, argv))
        return False


class FakeEvents:
    """Yields one openwindow per call, from a scripted list."""

    def __init__(self, scripted):
        self.scripted = list(scripted)

    def events(self, timeout=None):
        if self.scripted:
            yield self.scripted.pop(0)


class SilentEvents:
    def events(self, timeout=None):
        return iter(())


def window(order, cls, ws, floating=False, at=(10, 20), size=(800, 600), fs=0):
    return {
        "order": order, "class": cls, "workspace": ws, "monitor": 0,
        "floating": floating, "at": list(at), "size": list(size),
        "fullscreen": fs, "cmd": [cls],
    }


class RestoreCommandTest(unittest.TestCase):
    def test_first_window_of_class_is_unmodified(self):
        seen = {}
        self.assertEqual(
            session.restore_command(window(0, "firefox", 1), seen), ["firefox"]
        )

    def test_second_window_of_class_gets_quirk_arguments(self):
        seen = {}
        session.restore_command(window(0, "firefox", 1), seen)
        self.assertEqual(
            session.restore_command(window(1, "firefox", 2), seen),
            ["firefox", "--new-window"],
        )

    def test_class_without_quirk_is_never_modified(self):
        seen = {}
        session.restore_command(window(0, "kitty", 1), seen)
        self.assertEqual(
            session.restore_command(window(1, "kitty", 2), seen), ["kitty"]
        )


class RestoreTest(unittest.TestCase):
    def test_places_each_window_on_its_workspace(self):
        hypr = FakeHypr()
        events = FakeEvents([
            ("openwindow", "0xa,1,kitty,t"),
            ("openwindow", "0xb,10,firefox,t"),
        ])
        snap = {"version": 1, "windows": [
            window(0, "kitty", 1), window(1, "firefox", 10),
        ]}
        placed = session.restore(snap, hypr, events)
        self.assertEqual(placed, 2)
        self.assertEqual(
            hypr.execs,
            [("workspace 1 silent", ["kitty"]),
             ("workspace 10 silent", ["firefox"])],
        )

    def test_restores_windows_in_order_field_not_list_order(self):
        hypr = FakeHypr()
        events = FakeEvents([
            ("openwindow", "0xa,1,kitty,t"),
            ("openwindow", "0xb,2,mpv,t"),
        ])
        snap = {"version": 1, "windows": [
            window(1, "mpv", 2), window(0, "kitty", 1),
        ]}
        session.restore(snap, hypr, events)
        self.assertEqual([argv[0] for _rules, argv in hypr.execs], ["kitty", "mpv"])

    def test_floating_window_gets_geometry_dispatches(self):
        hypr = FakeHypr()
        events = FakeEvents([("openwindow", "0xa,1,mpv,t")])
        snap = {"version": 1, "windows": [
            window(0, "mpv", 1, floating=True, at=(5, 6), size=(640, 480)),
        ]}
        session.restore(snap, hypr, events)
        self.assertIn("setfloating address:0xa", hypr.dispatches)
        self.assertIn("movewindowpixel exact 5 6,address:0xa", hypr.dispatches)
        self.assertIn("resizewindowpixel exact 640 480,address:0xa", hypr.dispatches)

    def test_tiled_window_gets_no_geometry_dispatches(self):
        hypr = FakeHypr()
        events = FakeEvents([("openwindow", "0xa,1,kitty,t")])
        snap = {"version": 1, "windows": [window(0, "kitty", 1)]}
        session.restore(snap, hypr, events)
        self.assertEqual(hypr.dispatches, [])

    def test_fullscreen_window_is_refocused_and_fullscreened(self):
        hypr = FakeHypr()
        events = FakeEvents([("openwindow", "0xa,1,mpv,t")])
        snap = {"version": 1, "windows": [window(0, "mpv", 1, fs=1)]}
        session.restore(snap, hypr, events)
        self.assertIn("focuswindow address:0xa", hypr.dispatches)
        self.assertIn("fullscreen 1", hypr.dispatches)

    def test_failed_launch_does_not_stop_the_next_window(self):
        hypr = FailingHypr()
        snap = {"version": 1, "windows": [
            window(0, "broken", 1), window(1, "kitty", 2),
        ]}
        placed = session.restore(snap, hypr, SilentEvents())
        self.assertEqual(placed, 0)
        self.assertEqual([argv[0] for _r, argv in hypr.execs], ["broken", "kitty"])

    def test_window_that_never_maps_is_abandoned_after_timeout(self):
        # A clock that advances 5s per reading crosses the 10s per-window
        # deadline without ever running out of values.
        hypr = FakeHypr()
        ticks = itertools.count(0.0, 5.0)
        placed = session.restore(
            {"version": 1, "windows": [window(0, "kitty", 1)]},
            hypr, SilentEvents(), clock=lambda: next(ticks),
        )
        self.assertEqual(placed, 0)
        self.assertEqual([argv[0] for _r, argv in hypr.execs], ["kitty"])

    def test_total_timeout_abandons_remaining_windows(self):
        # 200s per reading exceeds the 120s budget on the first check, so
        # nothing is ever launched.
        hypr = FakeHypr()
        ticks = itertools.count(0.0, 200.0)
        placed = session.restore(
            {"version": 1, "windows": [window(0, "a", 1), window(1, "b", 2)]},
            hypr, SilentEvents(), clock=lambda: next(ticks),
        )
        self.assertEqual(placed, 0)
        self.assertEqual(hypr.execs, [])

    def test_empty_snapshot_is_a_no_op(self):
        hypr = FakeHypr()
        self.assertEqual(
            session.restore({"version": 1, "windows": []}, hypr, SilentEvents()), 0
        )

    def test_missing_order_field_does_not_stop_restore(self):
        hypr = FakeHypr()
        events = FakeEvents([
            ("openwindow", "0xb,2,kitty,t"),
        ])
        snap = {"version": 1, "windows": [
            {"workspace": 1, "cmd": ["broken"]},  # missing "class" - will cause KeyError
            window(1, "kitty", 2),  # valid window
        ]}
        placed = session.restore(snap, hypr, events)
        self.assertEqual(placed, 1)
        # The broken entry should be skipped with an error, kitty should be launched
        self.assertEqual([argv[0] for _r, argv in hypr.execs], ["kitty"])

    def test_floating_entry_missing_at_is_skipped(self):
        hypr = FakeHypr()
        events = FakeEvents([
            ("openwindow", "0xa,1,broken,t"),
            ("openwindow", "0xb,2,kitty,t"),
        ])
        snap = {"version": 1, "windows": [
            {
                "order": 0, "class": "broken", "workspace": 1, "cmd": ["broken"],
                "floating": True, "size": [100, 100],  # missing "at"
                "monitor": 0, "fullscreen": 0,
            },
            window(1, "kitty", 2),
        ]}
        placed = session.restore(snap, hypr, events)
        self.assertEqual(placed, 1)
        self.assertEqual([argv[0] for _r, argv in hypr.execs], ["broken", "kitty"])

    def test_stale_event_not_matched_to_next_window_same_class(self):
        hypr = FakeHypr()
        # Provide a stale event for the first window, then the correct event for the second.
        # The consumed set ensures 0xa is not matched to window 2.
        events = FakeEvents([
            ("openwindow", "0xa,1,firefox,t"),  # event for window 1
            ("openwindow", "0xb,2,firefox,t"),  # event for window 2
        ])
        snap = {"version": 1, "windows": [
            window(0, "firefox", 1),
            window(1, "firefox", 2, floating=True),  # window 2 is floating
        ]}
        placed = session.restore(snap, hypr, events)
        self.assertEqual(placed, 2)
        # Both windows should be placed. Window 2 is floating, so its address
        # should appear in setfloating dispatch (should be 0xb, not 0xa).
        self.assertIn("setfloating address:0xb", hypr.dispatches)


if __name__ == "__main__":
    unittest.main()
