import importlib.util
import itertools
import json
import os
import pathlib
import re
import sys
import tempfile
import unittest
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parent.parent
SESSION_PY = REPO / ".config" / "hypr" / "session.py"

spec = importlib.util.spec_from_file_location("session", SESSION_PY)
session = importlib.util.module_from_spec(spec)
sys.modules["session"] = session
spec.loader.exec_module(session)

# log() is file-backed (Task 8) and many tests exercise code paths that call
# it incidentally (build_snapshot's skip log, restore's and watch's status
# log lines) without themselves caring about logging. Redirect XDG_STATE_HOME
# for the whole module so none of that incidental logging ever lands in the
# real ~/.local/state/hypr-session/session.log. Individual test classes
# (CliTest, CaptureTest) still manage their own XDG_STATE_HOME within this
# sandbox to assert on log/snapshot content directly.
_module_state_tmp = None
_saved_xdg_state_home = None


def setUpModule():
    global _module_state_tmp, _saved_xdg_state_home
    _module_state_tmp = tempfile.TemporaryDirectory()
    _saved_xdg_state_home = os.environ.get("XDG_STATE_HOME")
    os.environ["XDG_STATE_HOME"] = str(pathlib.Path(_module_state_tmp.name) / "state")


def tearDownModule():
    if _saved_xdg_state_home is None:
        os.environ.pop("XDG_STATE_HOME", None)
    else:
        os.environ["XDG_STATE_HOME"] = _saved_xdg_state_home
    _module_state_tmp.cleanup()


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

    def test_read_non_object_json_returns_none(self):
        # Valid JSON that is not an object is still a malformed snapshot:
        # every caller treats the result as a mapping, so returning one of
        # these pushes an AttributeError into restore() (killing boot before
        # the watch loop starts) and into should_write_snapshot() (failing
        # every capture for the rest of the session).
        for text in ("123", "[1, 2]", "true", "null", '"a string"'):
            with self.subTest(text=text):
                self.path.write_text(text)
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

    def test_quirk_arguments_are_not_captured_into_the_snapshot(self):
        # A Firefox window that this tool restored carries --new-window in
        # its own /proc cmdline. Saving it verbatim would put the flag in
        # the snapshot, where restore adds another copy for the second
        # window of that class.
        fake_proc(self.proc, 10, ["firefox", "--new-window"], "firefox", "/home/u")
        client = {"class": "firefox", "pid": 10}
        self.assertEqual(session.resolve_command(client, self.proc), ["firefox"])

    def test_other_arguments_of_a_quirked_class_survive(self):
        fake_proc(self.proc, 11, ["firefox", "-P", "work", "--new-window"],
                  "firefox", "/home/u")
        client = {"class": "firefox", "pid": 11}
        self.assertEqual(
            session.resolve_command(client, self.proc), ["firefox", "-P", "work"]
        )

    def test_legacy_quirk_argument_is_stripped_and_does_not_survive_restore(self):
        # QUIRKS used to mean "add --new-window from the second window of
        # this class onward"; restore_command added it, and the next
        # capture's strip_quirk_arguments() removed it again so it would not
        # accrete. That add-at-restore behaviour is gone now (single-instance
        # classes are launched once -- see SINGLE_INSTANCE_CLASSES in
        # session.py and RestoreTest), but some on-disk snapshots were
        # written by the old code and still carry --new-window baked into a
        # saved "cmd". restore_command must still strip it via
        # strip_quirk_arguments(), and it must never be replayed, however
        # many times restore_command is called for the class in one restore.
        seen = {}
        stale_entry = {"class": "firefox", "cmd": ["firefox", "--new-window"]}
        self.assertEqual(session.restore_command(stale_entry, seen), ["firefox"])
        self.assertEqual(session.restore_command(stale_entry, seen), ["firefox"])

    def test_unreadable_process_yields_empty_command(self):
        client = {"class": "ghost", "pid": 4242}
        self.assertEqual(session.resolve_command(client, self.proc), [])


class ProgramDetectionTest(unittest.TestCase):
    """child_program() looks one level past child_shell_cwd(): terminal ->
    shell -> grandchildren. Only a grandchild whose comm is a key in
    ALLOWLIST is ever reported, and only for TERMINALS classes -- this is
    the entire security boundary for the allowlist feature, so each rule is
    tested in isolation.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.proc = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_allowlisted_grandchild_is_recorded(self):
        fake_proc(self.proc, 40, ["kitty"], "kitty", "/home/u", children=[41])
        fake_proc(self.proc, 41, ["-zsh"], "zsh", "/home/u/project", children=[42])
        fake_proc(self.proc, 42, ["claude", "--continue"], "claude",
                  "/home/u/project")
        self.assertEqual(session.child_program(40, self.proc), "claude")

    def test_non_allowlisted_grandchild_is_ignored(self):
        fake_proc(self.proc, 40, ["kitty"], "kitty", "/home/u", children=[41])
        fake_proc(self.proc, 41, ["-zsh"], "zsh", "/home/u/project", children=[42])
        fake_proc(self.proc, 42, ["vim", "notes.txt"], "vim", "/home/u/project")
        self.assertIsNone(session.child_program(40, self.proc))

    def test_terminal_with_no_grandchildren_is_unaffected(self):
        fake_proc(self.proc, 40, ["kitty"], "kitty", "/home/u", children=[41])
        fake_proc(self.proc, 41, ["-zsh"], "zsh", "/home/u/project")
        self.assertIsNone(session.child_program(40, self.proc))

    def test_terminal_with_no_children_at_all_is_unaffected(self):
        fake_proc(self.proc, 40, ["kitty"], "kitty", "/home/u")
        self.assertIsNone(session.child_program(40, self.proc))

    def test_non_terminal_class_is_never_inspected_for_a_program(self):
        fake_proc(self.proc, 40, ["firefox"], "firefox", "/home/u", children=[41])
        fake_proc(self.proc, 41, ["-zsh"], "zsh", "/home/u/project", children=[42])
        fake_proc(self.proc, 42, ["claude"], "claude", "/home/u/project")
        client = {"class": "firefox", "pid": 40}
        self.assertIsNone(session.resolve_program(client, self.proc))

    def test_terminal_class_is_inspected_via_resolve_program(self):
        fake_proc(self.proc, 40, ["kitty"], "kitty", "/home/u", children=[41])
        fake_proc(self.proc, 41, ["-zsh"], "zsh", "/home/u/project", children=[42])
        fake_proc(self.proc, 42, ["claude"], "claude", "/home/u/project")
        client = {"class": "kitty", "pid": 40}
        self.assertEqual(session.resolve_program(client, self.proc), "claude")

    # -- Post-restore tree shape --------------------------------------
    #
    # `kitty ... -e claude --continue` makes claude kitty's DIRECT child:
    # there is no intermediate shell to walk through the way a freshly
    # opened terminal has one. Confirmed empirically against a throwaway
    # `kitty --directory <dir> -e zsh -c "sleep 30"`: the sleep process was
    # a direct child of kitty, cwd correct on that process, and both
    # child_shell_cwd and child_program returned None against it before
    # this fallback existed. Without handling this shape, a restored
    # terminal loses its cwd AND its program on the very next capture, even
    # though the allowlisted program is still running.

    def test_direct_allowlisted_child_is_recorded_when_no_shell_exists(self):
        program_cwd = self.proc / "restored-project"
        program_cwd.mkdir()
        fake_proc(self.proc, 50, ["kitty"], "kitty", "/home/u",
                  children=[51, 52, 53])
        fake_proc(self.proc, 51, ["kitten"], "kitten", "/home/u")
        fake_proc(self.proc, 52, ["claude", "--continue"], "claude", "/home/u")
        fake_proc(self.proc, 53, ["kitten"], "kitten", "/home/u")
        (self.proc / "52" / "cwd").unlink()
        (self.proc / "52" / "cwd").symlink_to(program_cwd)
        self.assertEqual(session.child_program(50, self.proc), "claude")
        self.assertEqual(session.child_shell_cwd(50, self.proc), str(program_cwd))

    def test_fresh_shape_still_resolves_via_the_shell_unchanged(self):
        # Same assertions as the post-restore test above, but against a
        # freshly opened terminal's tree shape (shell present). This must
        # keep behaving exactly as it does today: the shell is found and
        # used, the fallback path is never reached.
        shell_cwd = self.proc / "fresh-project"
        shell_cwd.mkdir()
        fake_proc(self.proc, 54, ["kitty"], "kitty", "/home/u", children=[55])
        fake_proc(self.proc, 55, ["-zsh"], "zsh", "/home/u", children=[56])
        fake_proc(self.proc, 56, ["claude", "--continue"], "claude", "/home/u")
        (self.proc / "55" / "cwd").unlink()
        (self.proc / "55" / "cwd").symlink_to(shell_cwd)
        self.assertEqual(session.child_program(54, self.proc), "claude")
        self.assertEqual(session.child_shell_cwd(54, self.proc), str(shell_cwd))

    def test_shell_grandchild_wins_over_a_decoy_direct_child_listed_first(self):
        # Reproduces the bug found in review: a single linear pass that
        # checks `elif comm in ALLOWLIST` on a direct child returns as soon
        # as it sees an allowlisted direct child, even when a shell child
        # (whose grandchild is a different allowlisted program) appears
        # later in /proc's child order. The shell must win regardless of
        # where it falls in that order, exactly as child_shell_cwd already
        # guarantees via its own two-pass walk. This needs a second
        # ALLOWLIST entry to be observable, so it is patched in here and
        # restored afterwards -- the shipped table stays untouched.
        original_allowlist = session.ALLOWLIST
        session.ALLOWLIST = {"claude": ["claude", "--continue"], "vim": ["vim"]}
        try:
            fake_proc(self.proc, 70, ["kitty"], "kitty", "/home/u",
                      children=[71, 72])
            # Decoy: an allowlisted direct child listed BEFORE the shell.
            fake_proc(self.proc, 71, ["vim", "notes.txt"], "vim", "/home/u")
            # Shell child, listed second, whose grandchild is the real
            # allowlisted program.
            fake_proc(self.proc, 72, ["-zsh"], "zsh", "/home/u/project",
                      children=[73])
            fake_proc(self.proc, 73, ["claude", "--continue"], "claude",
                      "/home/u/project")
            self.assertEqual(session.child_program(70, self.proc), "claude")
        finally:
            session.ALLOWLIST = original_allowlist

    def test_direct_non_allowlisted_child_is_ignored_by_both_walks(self):
        # The important negative case: the fallback must not turn any
        # random direct child into a program or a cwd source -- ALLOWLIST
        # membership stays the only gate.
        other_cwd = self.proc / "unrelated"
        other_cwd.mkdir()
        fake_proc(self.proc, 60, ["kitty"], "kitty", "/home/u", children=[61])
        fake_proc(self.proc, 61, ["htop"], "htop", "/home/u")
        (self.proc / "61" / "cwd").unlink()
        (self.proc / "61" / "cwd").symlink_to(other_cwd)
        self.assertIsNone(session.child_program(60, self.proc))
        self.assertIsNone(session.child_shell_cwd(60, self.proc))


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

    def test_two_floating_siblings_are_both_kept(self):
        # The heuristic is "a floating window whose main window is also
        # open". Two floating windows sharing a pid and class have no main
        # window between them, so neither is a dialog -- and if the
        # non-floating requirement were dropped they would classify each
        # other as transient and both would vanish from the snapshot.
        a = {"class": "gimp", "pid": 7, "floating": True}
        b = {"class": "gimp", "pid": 7, "floating": True}
        self.assertFalse(session.is_transient(a, [a, b]))
        self.assertFalse(session.is_transient(b, [a, b]))

    def test_lone_floating_window_is_not_transient(self):
        client = {"class": "pavucontrol", "pid": 9, "floating": True}
        self.assertFalse(session.is_transient(client, [client]))

    def test_floating_window_of_different_pid_is_not_transient(self):
        a = {"class": "kitty", "pid": 1, "floating": False}
        b = {"class": "kitty", "pid": 2, "floating": True}
        self.assertFalse(session.is_transient(b, [a, b]))

    def test_special_workspace_window_is_skipped(self):
        # Scratchpad windows report a negative workspace id, but Hyprland
        # addresses them as special:<name>. `workspace -98 silent` is not a
        # rule that means anything.
        client = {"class": "kitty", "pid": 1, "floating": True,
                  "workspace": {"id": -98, "name": "special:magic"}}
        self.assertEqual(
            session.should_skip(client, [client], [], ["kitty"]),
            "special-workspace",
        )

    def test_normal_workspace_window_is_not_skipped_for_its_workspace(self):
        client = {"class": "kitty", "pid": 1, "floating": False,
                  "workspace": {"id": 1, "name": "1"}}
        self.assertIsNone(session.should_skip(client, [client], [], ["kitty"]))

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

    def test_denylisted_windows_are_left_out_of_the_snapshot(self):
        # should_skip is tested against a denylist directly, but nothing
        # proved build_snapshot actually passes one through. The denylist is
        # the spec's "reliable control" for windows the heuristic gets
        # wrong, so the wiring matters as much as the matching.
        for pid, cls in ((1, "kitty"), (2, "steam")):
            fake_proc(self.proc, pid, [cls], cls, "/home/u")
        clients = [self.client("kitty", 1, 1), self.client("steam", 2, 2)]
        deny = [re.compile("^steam$")]
        snap = session.build_snapshot(clients, deny, self.proc, "T")
        self.assertEqual([w["class"] for w in snap["windows"]], ["kitty"])
        # ...and with no denylist the same window is kept, so the test is
        # about the denylist rather than about steam.
        snap = session.build_snapshot(clients, [], self.proc, "T")
        self.assertEqual([w["class"] for w in snap["windows"]],
                         ["kitty", "steam"])

    def test_special_workspace_windows_are_left_out_of_the_snapshot(self):
        for pid, cls in ((1, "kitty"), (2, "pad")):
            fake_proc(self.proc, pid, [cls], cls, "/home/u")
        scratch = self.client("pad", 2, -98)
        snap = session.build_snapshot(
            [self.client("kitty", 1, 1), scratch], [], self.proc, "T"
        )
        self.assertEqual([w["class"] for w in snap["windows"]], ["kitty"])

    def test_skipped_windows_are_absent_and_do_not_gap_order(self):
        fake_proc(self.proc, 1, ["kitty"], "kitty", "/home/u")
        # pid 2 has no fake /proc entry, so its command cannot be resolved.
        clients = [self.client("kitty", 1, 1), self.client("ghost", 2, 1)]
        snap = session.build_snapshot(clients, [], self.proc, "T")
        self.assertEqual([w["class"] for w in snap["windows"]], ["kitty"])
        self.assertEqual([w["order"] for w in snap["windows"]], [0])

    def test_allowlisted_grandchild_is_recorded_as_program(self):
        fake_proc(self.proc, 1, ["kitty"], "kitty", "/home/u", children=[2])
        fake_proc(self.proc, 2, ["-zsh"], "zsh", "/home/u", children=[3])
        fake_proc(self.proc, 3, ["claude", "--continue"], "claude", "/home/u")
        snap = session.build_snapshot([self.client("kitty", 1, 1)], [], self.proc, "T")
        self.assertEqual(snap["windows"][0]["program"], "claude")

    def test_no_program_key_when_nothing_allowlisted_is_running(self):
        fake_proc(self.proc, 1, ["kitty"], "kitty", "/home/u", children=[2])
        fake_proc(self.proc, 2, ["-zsh"], "zsh", "/home/u")
        snap = session.build_snapshot([self.client("kitty", 1, 1)], [], self.proc, "T")
        self.assertNotIn("program", snap["windows"][0])

    def test_round_trip_for_post_restore_shape_keeps_cwd_and_program(self):
        # A window restored via `-e claude --continue` has claude as kitty's
        # direct child, with no intermediate shell. The NEXT capture must
        # still carry both --directory and program, so a second reboot
        # behaves like the first instead of degrading to a bare ["kitty"].
        program_cwd = self.proc / "still-going"
        program_cwd.mkdir()
        fake_proc(
            self.proc, 70,
            ["kitty", "--directory", "/home/u", "-e", "claude", "--continue"],
            "kitty", "/home/u", children=[71],
        )
        fake_proc(self.proc, 71, ["claude", "--continue"], "claude", "/home/u")
        (self.proc / "71" / "cwd").unlink()
        (self.proc / "71" / "cwd").symlink_to(program_cwd)
        snap = session.build_snapshot([self.client("kitty", 70, 1)], [], self.proc, "T")
        w = snap["windows"][0]
        self.assertEqual(w["cmd"], ["kitty", "--directory", str(program_cwd)])
        self.assertEqual(w["program"], "claude")

    def test_records_title_for_single_instance_class(self):
        # Firefox's other windows are created by its own already-running
        # process, with no exec rule ever attached, so restore() needs the
        # title recorded here to register a placement rule ahead of time.
        fake_proc(self.proc, 1, ["firefox"], "firefox", "/home/u")
        c = self.client("firefox", 1, 3)
        c["title"] = "My Firefox Window"
        snap = session.build_snapshot([c], [], self.proc, "T")
        self.assertEqual(snap["windows"][0]["title"], "My Firefox Window")

    def test_omits_title_for_non_single_instance_class(self):
        # Terminal and browser titles change constantly, and the periodic
        # refresh's unchanged-write check compares the whole windows list,
        # so recording a title for every class would cause near-constant
        # rewrites. Restricting it to single-instance classes is what
        # keeps that churn down to the few windows that actually need it.
        fake_proc(self.proc, 1, ["kitty"], "kitty", "/home/u")
        c = self.client("kitty", 1, 3)
        c["title"] = "some terminal title"
        snap = session.build_snapshot([c], [], self.proc, "T")
        self.assertNotIn("title", snap["windows"][0])


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

    # The membership of WINDOW_EVENTS is covered behaviourally by
    # WatchTest.test_each_window_event_triggers_a_snapshot and
    # WatchTest.test_non_window_events_never_trigger_a_snapshot, which say
    # what the set is for rather than restating its literal contents.


class ChunkedSocket:
    """A socket whose recv() hands back a scripted sequence of chunks."""

    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.closed = False

    def recv(self, size):
        return self.chunks.pop(0) if self.chunks else b""

    def close(self):
        self.closed = True


class EventSocketTest(unittest.TestCase):
    """EventSocket is on the login path and had no coverage at all.

    Instances are built without connecting: __init__ would need a live
    compositor, and what is under test is the buffering, not the connect.
    """

    def make(self, chunks):
        sock = session.EventSocket.__new__(session.EventSocket)
        sock.path = "/fake/hypr/instance/.socket2.sock"
        sock.sock = ChunkedSocket(chunks)
        sock.buffer = ""
        return sock

    def read(self, event_socket, ready=True):
        selected = ([event_socket.sock], [], []) if ready else ([], [], [])
        with mock.patch.object(session.select, "select", return_value=selected):
            return list(event_socket.events(timeout=0))

    def test_a_line_split_across_two_reads_is_reassembled(self):
        # A recv() boundary can fall anywhere. Dropping the partial line
        # would silently lose an openwindow event, which on the restore
        # path means a window is reported as never having mapped.
        socket_ = self.make([b"openwindow>>0xa,1,ki", b"tty,a title\n"])
        self.assertEqual(self.read(socket_), [])
        self.assertEqual(
            self.read(socket_), [("openwindow", "0xa,1,kitty,a title")]
        )

    def test_a_trailing_partial_line_is_held_for_the_next_read(self):
        socket_ = self.make([b"closewindow>>0xa\nopenwin", b"dow>>0xb,1,mpv,t\n"])
        self.assertEqual(self.read(socket_), [("closewindow", "0xa")])
        self.assertEqual(socket_.buffer, "openwin")
        self.assertEqual(self.read(socket_), [("openwindow", "0xb,1,mpv,t")])

    def test_several_events_in_one_chunk_are_all_yielded(self):
        socket_ = self.make([b"openwindow>>0xa,1,kitty,t\nclosewindow>>0xa\n"])
        self.assertEqual(
            self.read(socket_),
            [("openwindow", "0xa,1,kitty,t"), ("closewindow", "0xa")],
        )

    def test_nothing_ready_yields_nothing_and_does_not_read(self):
        socket_ = self.make([b"openwindow>>0xa,1,kitty,t\n"])
        self.assertEqual(self.read(socket_, ready=False), [])
        self.assertEqual(len(socket_.sock.chunks), 1)

    def test_a_closed_socket_raises_connection_error(self):
        # This is the signal watch() reconnects on.
        socket_ = self.make([b""])
        with self.assertRaises(ConnectionError):
            self.read(socket_)

    def test_close_is_safe(self):
        socket_ = self.make([])
        socket_.close()
        self.assertTrue(socket_.sock.closed)


class HyprClientsTest(unittest.TestCase):
    """Hypr.clients() must return [] on every failure rather than raise.

    That empty list is what feeds should_write_snapshot's guard, so a
    hyprctl that fails during logout teardown ends up protecting the
    snapshot rather than destroying it.
    """

    def clients_with(self, **run_kwargs):
        hypr = session.Hypr()
        with mock.patch.object(session.subprocess, "run", **run_kwargs):
            return hypr.clients()

    def test_subprocess_failure_returns_empty_list(self):
        self.assertEqual(
            self.clients_with(
                side_effect=session.subprocess.SubprocessError("hyprctl died")
            ),
            [],
        )

    def test_timeout_returns_empty_list(self):
        self.assertEqual(
            self.clients_with(
                side_effect=session.subprocess.TimeoutExpired("hyprctl", 10)
            ),
            [],
        )

    def test_missing_binary_returns_empty_list(self):
        self.assertEqual(
            self.clients_with(side_effect=FileNotFoundError("hyprctl")), []
        )

    def test_invalid_json_returns_empty_list(self):
        self.assertEqual(
            self.clients_with(return_value=mock.Mock(stdout="not json at all")),
            [],
        )

    def test_valid_json_is_returned(self):
        self.assertEqual(
            self.clients_with(
                return_value=mock.Mock(stdout='[{"class": "kitty"}]')
            ),
            [{"class": "kitty"}],
        )


class FakeHypr:
    def __init__(self):
        self.execs = []

    def exec_with_rules(self, rules, argv):
        self.execs.append((rules, argv))
        return True

    def clients(self):
        return []


class FailingHypr(FakeHypr):
    def exec_with_rules(self, rules, argv):
        self.execs.append((rules, argv))
        return False


class SilentEvents:
    def events(self, timeout=None):
        return iter(())


class PersistentEventBuffer:
    """A shared event queue that persists across restore()'s events() calls,
    modelling the real EventSocket: events already sitting in the kernel
    socket buffer are still there on the next read, and a call yields only
    what is currently available rather than the whole future stream.

    Anything passed as `initial_events` models a genuinely stale event: one
    that existed before restore() ever ran (from another autostart
    application, or a window the user opened manually) and that no launch in
    the test produced. A window's own event is never pre-seeded here -- in
    reality it only exists after Hyprland processes that window's launch, so
    tests enqueue it via EventEmittingHypr at exec_with_rules() time instead.
    """

    def __init__(self, initial_events=()):
        self.buffer = list(initial_events)

    def enqueue(self, event):
        self.buffer.append(event)

    def events(self, timeout=None):
        """Yield one event if available from the persistent buffer."""
        if self.buffer:
            yield self.buffer.pop(0)


class EventEmittingHypr(FakeHypr):
    """Fake Hypr that enqueues a launched window's openwindow event into a
    shared PersistentEventBuffer at the moment exec_with_rules() succeeds.

    This mirrors reality: Hyprland only emits a window's openwindow event
    after it has processed that window's launch, never before. `addresses`
    maps a class name to the list of addresses to hand out, in launch order,
    to windows of that class.
    """

    def __init__(self, events: "PersistentEventBuffer", addresses: dict):
        super().__init__()
        self.events = events
        self.addresses = {cls: list(addrs) for cls, addrs in addresses.items()}

    def exec_with_rules(self, rules, argv):
        ok = super().exec_with_rules(rules, argv)
        if ok:
            cls = argv[0]
            pending = self.addresses.get(cls)
            if pending:
                address = pending.pop(0)
                self.events.enqueue(("openwindow", f"{address},1,{cls},t"))
        return ok


class FirstCallFailsHypr(EventEmittingHypr):
    """Like EventEmittingHypr, but the very first exec_with_rules() call
    fails (as a real launch failure would) and every call after that
    behaves normally. Used to prove that a failed launch of a
    single-instance class's first window does not forfeit the rest of that
    class -- the next recorded entry must still be attempted.
    """

    def __init__(self, events: "PersistentEventBuffer", addresses: dict):
        super().__init__(events, addresses)
        self.calls = 0

    def exec_with_rules(self, rules, argv):
        self.calls += 1
        if self.calls == 1:
            self.execs.append((rules, argv))
            return False
        return super().exec_with_rules(rules, argv)


def live_client(cls, title, address, ws):
    """A minimal `hyprctl -j clients` entry, as consumed by the post-restore
    reconciliation phase: class, title, address and current workspace id."""
    return {
        "class": cls, "title": title, "address": address,
        "workspace": {"id": ws, "name": str(ws)},
    }


class ReconcileHypr(EventEmittingHypr):
    """Adds move_window()/focus_workspace() tracking and a fixed clients()
    result to EventEmittingHypr, so the post-restore reconciliation phase
    can be tested against the same fakes the rest of RestoreTest uses.

    `live_clients` is returned verbatim by every clients() call -- the
    reconciliation phase itself tracks which addresses it has already
    handled, so the fake does not need to mutate what it returns between
    polls to behave correctly.
    """

    def __init__(self, events, addresses, live_clients,
                 failing_move_addresses=()):
        super().__init__(events, addresses)
        self.live_clients = list(live_clients)
        self.moves = []
        self.focus_calls = []
        self._failing_move_addresses = set(failing_move_addresses)

    def clients(self):
        return self.live_clients

    def move_window(self, address, workspace):
        self.moves.append((address, workspace))
        return address not in self._failing_move_addresses

    def focus_workspace(self, workspace):
        self.focus_calls.append(workspace)
        return True


class RealMoveHypr(session.Hypr):
    """Uses the REAL Hypr.move_window() and Hypr.focus_workspace() -- the
    methods under test for the invalid-workspace guard -- so a regression
    in either method's error handling (e.g. the int() conversion moving
    back outside its try/except) is caught here, unlike ReconcileHypr's
    fully-faked move_window()/focus_workspace() which cannot see it. Only
    the actual subprocess boundary (_hyprctl) and clients() are stubbed, so
    no real hyprctl process ever runs.
    """

    def __init__(self, live_clients):
        self.live_clients = list(live_clients)
        self.hyprctl_calls = []

    def clients(self):
        return self.live_clients

    def _hyprctl(self, args):
        self.hyprctl_calls.append(list(args))
        return True


def window(order, cls, ws, floating=False, at=(10, 20), size=(800, 600), fs=0,
           title=None):
    entry = {
        "order": order, "class": cls, "workspace": ws, "monitor": 0,
        "floating": floating, "at": list(at), "size": list(size),
        "fullscreen": fs, "cmd": [cls],
    }
    if title is not None:
        entry["title"] = title
    return entry


class RestoreCommandTest(unittest.TestCase):
    def test_first_window_of_class_is_unmodified(self):
        seen = {}
        self.assertEqual(
            session.restore_command(window(0, "firefox", 1), seen), ["firefox"]
        )

    def test_repeated_calls_never_add_a_quirk_argument(self):
        # restore_command used to add --new-window from the second call for
        # a class onward (QUIRKS). That decision moved entirely to
        # restore()'s single-instance skip (SINGLE_INSTANCE_CLASSES) -- see
        # RestoreTest -- so restore_command itself must now behave
        # identically no matter how many times it is called for the same
        # class; it never adds anything on its own.
        seen = {}
        session.restore_command(window(0, "firefox", 1), seen)
        self.assertEqual(
            session.restore_command(window(1, "firefox", 2), seen),
            ["firefox"],
        )

    def test_class_without_quirk_is_never_modified(self):
        seen = {}
        session.restore_command(window(0, "kitty", 1), seen)
        self.assertEqual(
            session.restore_command(window(1, "kitty", 2), seen), ["kitty"]
        )

    def test_allowlisted_program_appends_dash_e_template(self):
        seen = {}
        w = window(0, "kitty", 1)
        w["cmd"] = ["kitty", "--directory", "/home/gion/coding/MS"]
        w["program"] = "claude"
        self.assertEqual(
            session.restore_command(w, seen),
            ["kitty", "--directory", "/home/gion/coding/MS", "-e",
             "claude", "--continue"],
        )

    def test_missing_program_restores_exactly_as_before(self):
        seen = {}
        w = window(0, "kitty", 1)
        self.assertNotIn("program", w)
        self.assertEqual(session.restore_command(w, seen), ["kitty"])

    def test_program_no_longer_in_allowlist_falls_back_silently(self):
        # A stale snapshot from before an entry was removed from ALLOWLIST
        # must never launch that program: the table is the whole security
        # boundary, so anything not currently a key is treated as absent.
        seen = {}
        w = window(0, "kitty", 1)
        w["program"] = "some-removed-program"
        self.assertEqual(session.restore_command(w, seen), ["kitty"])

    def test_observed_arguments_never_leak_into_restored_argv(self):
        # The recorded program name only ever identifies which fixed
        # template to run; whatever arguments were actually observed on the
        # grandchild's cmdline must never reach the launched argv.
        with tempfile.TemporaryDirectory() as tmp:
            proc = pathlib.Path(tmp)
            fake_proc(proc, 1, ["kitty"], "kitty", "/home/u", children=[2])
            fake_proc(proc, 2, ["-zsh"], "zsh", "/home/u", children=[3])
            fake_proc(
                proc, 3,
                ["claude", "--dangerously-skip-permissions", "-p", "rm -rf /"],
                "claude", "/home/u",
            )
            program = session.child_program(1, proc)
            w = window(0, "kitty", 1)
            w["cmd"] = ["kitty", "--directory", "/home/u"]
            w["program"] = program
            argv = session.restore_command(w, {})
        self.assertEqual(
            argv,
            ["kitty", "--directory", "/home/u", "-e", "claude", "--continue"],
        )


class RestoreTest(unittest.TestCase):
    def test_places_each_window_on_its_workspace(self):
        events = PersistentEventBuffer()
        hypr = EventEmittingHypr(events, {"kitty": ["0xa"]})
        snap = {"version": 1, "windows": [
            window(0, "kitty", 1),
        ]}
        placed = session.restore(snap, hypr, events)
        self.assertEqual(placed, 1)
        self.assertEqual(
            hypr.execs,
            [("workspace 1 silent", ["kitty"])],
        )

    def test_places_multiple_windows_on_different_workspaces(self):
        events = PersistentEventBuffer()
        hypr = EventEmittingHypr(events, {"kitty": ["0xa"], "firefox": ["0xb"]})
        snap = {"version": 1, "windows": [
            window(0, "kitty", 1), window(1, "firefox", 10),
        ]}
        placed = session.restore(snap, hypr, events)
        self.assertEqual(placed, 2)
        # firefox is single-instance (SINGLE_INSTANCE_CLASSES): its launch
        # carries no workspace exec rule at all, since that prefix would
        # attach to whichever window firefox happens to map first, not
        # necessarily this recorded entry -- see SingleInstanceRestoreTest.
        self.assertEqual(
            hypr.execs,
            [("workspace 1 silent", ["kitty"]),
             ("", ["firefox"])],
        )

    def test_restores_windows_in_order_field_not_list_order(self):
        events = PersistentEventBuffer()
        hypr = EventEmittingHypr(events, {"kitty": ["0xa"], "mpv": ["0xb"]})
        snap = {"version": 1, "windows": [
            window(1, "mpv", 2), window(0, "kitty", 1),
        ]}
        session.restore(snap, hypr, events)
        self.assertEqual([argv[0] for _rules, argv in hypr.execs], ["kitty", "mpv"])

    def test_floating_window_gets_geometry_in_rules(self):
        # Geometry no longer travels as post-hoc dispatch calls (those never
        # worked on this machine's Lua-config hyprctl -- see Hypr.dispatch's
        # removal). It now rides in the exec rule prefix instead, so this
        # asserts on hypr.execs rather than a (now-gone) hypr.dispatches.
        events = PersistentEventBuffer()
        hypr = EventEmittingHypr(events, {"mpv": ["0xa"]})
        snap = {"version": 1, "windows": [
            window(0, "mpv", 1, floating=True, at=(5, 6), size=(640, 480)),
        ]}
        session.restore(snap, hypr, events)
        self.assertEqual(
            hypr.execs,
            [("workspace 1 silent; float; move 5 6; size 640 480", ["mpv"])],
        )

    def test_tiled_window_gets_no_geometry_rules(self):
        events = PersistentEventBuffer()
        hypr = EventEmittingHypr(events, {"kitty": ["0xa"]})
        snap = {"version": 1, "windows": [window(0, "kitty", 1)]}
        session.restore(snap, hypr, events)
        self.assertEqual(hypr.execs, [("workspace 1 silent", ["kitty"])])

    def test_fullscreen_window_gets_fullscreen_rule(self):
        # hyprctl's fullscreen field is 0/1/2 and only 2 is true fullscreen.
        # This test used to pass fs=1, which is "maximized".
        events = PersistentEventBuffer()
        hypr = EventEmittingHypr(events, {"mpv": ["0xa"]})
        snap = {"version": 1, "windows": [window(0, "mpv", 1, fs=2)]}
        session.restore(snap, hypr, events)
        self.assertEqual(
            hypr.execs,
            [("workspace 1 silent; fullscreen", ["mpv"])],
        )

    def test_maximized_window_does_not_get_the_fullscreen_rule(self):
        # fullscreen == 1 is maximized. Restoring it as true fullscreen is a
        # one-way trip: the window comes back fullscreen, the next capture
        # records 2, and it can never return to maximized. Not restoring the
        # state is recoverable; silently converting it is not.
        events = PersistentEventBuffer()
        hypr = EventEmittingHypr(events, {"mpv": ["0xa"]})
        snap = {"version": 1, "windows": [window(0, "mpv", 1, fs=1)]}
        session.restore(snap, hypr, events)
        self.assertEqual(hypr.execs, [("workspace 1 silent", ["mpv"])])

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
        events = PersistentEventBuffer()
        hypr = EventEmittingHypr(events, {"kitty": ["0xb"]})
        snap = {"version": 1, "windows": [
            {"workspace": 1, "cmd": ["broken"]},  # missing "class" - will cause KeyError
            window(1, "kitty", 2),  # valid window
        ]}
        placed = session.restore(snap, hypr, events)
        self.assertEqual(placed, 1)
        # The broken entry should be skipped with an error, kitty should be launched
        self.assertEqual([argv[0] for _r, argv in hypr.execs], ["kitty"])

    def test_floating_entry_missing_at_is_skipped(self):
        # Missing "at" now raises KeyError while building the rule string,
        # before the launch -- geometry rides in the exec rule prefix now,
        # built from window["at"]/["size"] ahead of the single
        # exec_with_rules call, rather than in post-hoc calls made after the
        # window has already mapped. The per-window try/except must still
        # catch it and skip the entry rather than raising out of restore(),
        # it just does so earlier: hypr.execs never even sees "broken".
        events = PersistentEventBuffer()
        hypr = EventEmittingHypr(events, {"broken": ["0xc"]})
        snap = {"version": 1, "windows": [
            {
                "order": 0, "class": "broken", "workspace": 1, "cmd": ["broken"],
                "floating": True, "size": [100, 100],  # missing "at"
                "monitor": 0, "fullscreen": 0,
            },
        ]}
        placed = session.restore(snap, hypr, events)
        self.assertEqual(placed, 0)
        self.assertEqual(hypr.execs, [])

    def test_stale_event_is_not_misattributed_to_first_window(self):
        # A stale same-class event, pending before restore() ever runs, must
        # never be attributed to the window whose launch it precedes -- not
        # even the very first window. This is the exact case both earlier
        # fix attempts missed: round 1 only drained after a per-window
        # timeout, and round 2 only drained from the second window onward
        # (`if window_index > 1`), leaving window 1's launch unprotected.
        #
        # The real EventSocket connects before restore begins, so its kernel
        # socket buffer can already hold an event from another autostart
        # application, or from a window the user opened manually, by the
        # time the first window is launched here.
        events = PersistentEventBuffer([
            ("openwindow", "0xstale,1,kitty,t"),  # pending before any launch
        ])
        hypr = EventEmittingHypr(events, {"kitty": ["0xreal"]})
        snap = {"version": 1, "windows": [
            window(0, "kitty", 1, floating=True, at=(5, 6), size=(640, 480)),
        ]}
        placed = session.restore(snap, hypr, events)
        self.assertEqual(placed, 1)
        # Geometry (proof the right window was configured at all) now rides
        # in the exec rule prefix -- it carries no address, since the rules
        # apply to whichever window this exec call maps, not to an address
        # dispatched at afterward. So it can't by itself prove _await_window
        # picked "0xreal" over the stale "0xstale". What can: "0xreal" is
        # only enqueued once exec_with_rules launches the window, so it is
        # unreachable at drain time. If drain failed to clear "0xstale" first,
        # _await_window's FIFO scan would return "0xstale" instead, leaving
        # "0xreal" stranded, unconsumed, in the buffer -- so an empty buffer
        # at the end is proof "0xreal", not the stale event, was matched.
        self.assertEqual(
            hypr.execs,
            [("workspace 1 silent; float; move 5 6; size 640 480", ["kitty"])],
        )
        self.assertEqual(events.buffer, [])

    def test_drain_clears_a_multi_event_backlog(self):
        # PersistentEventBuffer.events() yields only one item per call, the
        # same way a real EventSocket read is one select() plus one recv():
        # a backlog bigger than one chunk needs more than one call to fully
        # clear. Two stale events are pending before the window even
        # launches; a single-shot drain (one events() call, as round 2's
        # `for _ in events.events(timeout=0): pass` was) would remove only
        # the first and leave the second to be misattributed.
        events = PersistentEventBuffer([
            ("openwindow", "0xstale1,1,kitty,t"),
            ("openwindow", "0xstale2,1,kitty,t"),
        ])
        hypr = EventEmittingHypr(events, {"kitty": ["0xreal"]})
        snap = {"version": 1, "windows": [
            window(0, "kitty", 1, floating=True, at=(5, 6), size=(640, 480)),
        ]}
        placed = session.restore(snap, hypr, events)
        self.assertEqual(placed, 1)
        # See test_stale_event_is_not_misattributed_to_first_window for why
        # an empty buffer, rather than a dispatched address, is the proof
        # here: "0xreal" is only reachable once both stale events are
        # drained out of the way, so it being consumed means the drain
        # cleared the whole two-event backlog, not just the first one.
        self.assertEqual(
            hypr.execs,
            [("workspace 1 silent; float; move 5 6; size 640 480", ["kitty"])],
        )
        self.assertEqual(events.buffer, [])


class SingleInstanceRestoreTest(unittest.TestCase):
    """Firefox (and anything else in SINGLE_INSTANCE_CLASSES) hands off to
    its own already-running process instead of opening a new window, so
    only the first recorded window of such a class is ever launched.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = os.environ.get("XDG_STATE_HOME")
        os.environ["XDG_STATE_HOME"] = str(pathlib.Path(self.tmp.name) / "state")

    def tearDown(self):
        if self.saved is None:
            os.environ.pop("XDG_STATE_HOME", None)
        else:
            os.environ["XDG_STATE_HOME"] = self.saved
        self.tmp.cleanup()

    def _log(self):
        return (session.state_dir() / "session.log").read_text()

    def test_three_untitled_firefox_entries_launch_once_unplaced_with_no_rules(self):
        # None of the three entries carries a title, so there is nothing to
        # key a placement rule off of for any of them -- not even the
        # launched one. The launch itself carries no workspace exec rule
        # either: that prefix would attach to whichever window firefox
        # happens to map first, which has no reliable relationship to any
        # one recorded entry (production evidence: it attached to a "New
        # Tab" window instead of the Jira tab the prefix was meant for).
        # So the app is launched once, entirely unplaced, and lands
        # wherever the compositor puts it -- which is the deliberate
        # fallback: leaving a window unplaced beats mis-placing it.
        events = PersistentEventBuffer()
        hypr = EventEmittingHypr(events, {"firefox": ["0xa"]})
        snap = {"version": 1, "windows": [
            window(0, "firefox", 3),
            window(1, "firefox", 7),
            window(2, "firefox", 9),
        ]}
        placed = session.restore(snap, hypr, events)
        self.assertEqual(placed, 1)
        self.assertEqual(hypr.execs, [("", ["firefox"])])

    def test_skipped_firefox_windows_are_logged_and_not_claimed_placed(self):
        events = PersistentEventBuffer()
        hypr = EventEmittingHypr(events, {"firefox": ["0xa"]})
        snap = {"version": 1, "windows": [
            window(0, "firefox", 3),
            window(1, "firefox", 7),
            window(2, "firefox", 9),
        ]}
        placed = session.restore(snap, hypr, events)
        self.assertEqual(placed, 1)
        text = self._log()
        # Each of the two skipped entries is logged with a reason that
        # explains, on its own, why it was not placed.
        self.assertEqual(text.count("single-instance"), 2)
        self.assertIn("workspace 7", text)
        self.assertIn("workspace 9", text)
        # The summary must be honest: only the one confirmed launch counts
        # as placed, out of all three recorded entries.
        self.assertIn("restore: placed 1/3 window(s)", text)

    def test_non_single_instance_class_still_launches_every_window(self):
        events = PersistentEventBuffer()
        hypr = EventEmittingHypr(events, {"mpv": ["0xa", "0xb", "0xc"]})
        snap = {"version": 1, "windows": [
            window(0, "mpv", 1), window(1, "mpv", 2), window(2, "mpv", 3),
        ]}
        placed = session.restore(snap, hypr, events)
        self.assertEqual(placed, 3)
        self.assertEqual(
            hypr.execs,
            [("workspace 1 silent", ["mpv"]),
             ("workspace 2 silent", ["mpv"]),
             ("workspace 3 silent", ["mpv"])],
        )

    def test_terminals_are_unaffected_each_gets_its_own_workspace(self):
        events = PersistentEventBuffer()
        hypr = EventEmittingHypr(events, {"kitty": ["0xa", "0xb", "0xc"]})
        snap = {"version": 1, "windows": [
            window(0, "kitty", 1), window(1, "kitty", 4), window(2, "kitty", 8),
        ]}
        placed = session.restore(snap, hypr, events)
        self.assertEqual(placed, 3)
        self.assertEqual(
            [rules for rules, _argv in hypr.execs],
            ["workspace 1 silent", "workspace 4 silent", "workspace 8 silent"],
        )

    def test_old_snapshot_with_new_window_baked_in_is_not_replayed(self):
        # Simulates a snapshot on disk right now, written by the previous
        # behaviour, whose saved "cmd" already carries --new-window.
        events = PersistentEventBuffer()
        hypr = EventEmittingHypr(events, {"firefox": ["0xa"]})
        stale_window = window(0, "firefox", 2)
        stale_window["cmd"] = ["firefox", "--new-window"]
        placed = session.restore(
            {"version": 1, "windows": [stale_window]}, hypr, events
        )
        self.assertEqual(placed, 1)
        # No workspace exec rule either, single-instance classes are always
        # launched unplaced now -- placement comes from title rules alone.
        self.assertEqual(hypr.execs, [("", ["firefox"])])

    def test_failed_first_launch_lets_the_next_entry_of_the_class_attempt(self):
        # A single-instance class must only be marked "seen" once a launch
        # has actually been dispatched -- exec_with_rules() returning False
        # means nothing was launched at all, so the class is not forfeited:
        # the next recorded entry gets its own independent attempt, exactly
        # as it would have before this class of window existed.
        events = PersistentEventBuffer()
        hypr = FirstCallFailsHypr(events, {"firefox": ["0xa"]})
        snap = {"version": 1, "windows": [
            window(0, "firefox", 3),
            window(1, "firefox", 7),
        ]}
        placed = session.restore(snap, hypr, events)
        self.assertEqual(placed, 1)
        # Two dispatch attempts, not one: the failed first did not consume
        # the class's "already launched" slot.
        self.assertEqual(len(hypr.execs), 2)
        # Neither attempt carries a workspace rule: single-instance launches
        # are always unplaced by exec rule now, regardless of which attempt
        # ultimately succeeds.
        self.assertEqual([rules for rules, _argv in hypr.execs], ["", ""])

    def test_first_launch_never_mapping_still_skips_the_rest_of_the_class(self):
        # The opposite case: the launch is genuinely dispatched (exec
        # succeeds) but no matching openwindow ever arrives. This must NOT
        # be retried -- retrying here is exactly how the duplicate-blank-
        # Firefox-window bug happened in the first place. The class is
        # marked seen as soon as the launch is dispatched, not only once a
        # window is confirmed mapped, so the second entry is still skipped.
        hypr = FakeHypr()  # exec_with_rules always succeeds, never emits an event
        ticks = itertools.count(0.0, 5.0)
        snap = {"version": 1, "windows": [
            window(0, "firefox", 3),
            window(1, "firefox", 7),
        ]}
        placed = session.restore(
            snap, hypr, SilentEvents(), clock=lambda: next(ticks),
        )
        self.assertEqual(placed, 0)
        # Exactly one dispatch attempt: the anti-duplicate guarantee this
        # whole feature exists for.
        self.assertEqual(len(hypr.execs), 1)
        # No workspace rule on the launch itself -- placement is entirely
        # via title rules for single-instance classes.
        self.assertEqual(hypr.execs[0][0], "")

    def test_skip_log_after_a_failed_launch_never_claims_the_window_was_restored(self):
        # Regression for a reviewer-found bug: the skip message must only
        # ever describe what was actually observed (a launch was
        # dispatched), never assert that a window was successfully
        # restored -- which would be untrue whenever the class's one
        # dispatched launch failed to map, or a launch attempt before it
        # failed outright.
        events = PersistentEventBuffer()
        hypr = FirstCallFailsHypr(events, {"firefox": ["0xa"]})
        snap = {"version": 1, "windows": [
            window(0, "firefox", 3),
            window(1, "firefox", 7),
            window(2, "firefox", 9),
        ]}
        placed = session.restore(snap, hypr, events)
        self.assertEqual(placed, 1)
        # First failed, second dispatched+mapped (marks the class seen),
        # third is skipped.
        self.assertEqual(len(hypr.execs), 2)
        text = self._log()
        self.assertIn("restore: launch failed for firefox", text)
        self.assertIn("single-instance", text)
        self.assertNotIn("already restored", text)


class ReconcilePlacementTest(unittest.TestCase):
    """A single-instance class's windows carry no workspace exec rule (see
    SingleInstanceRestoreTest), because Hyprland evaluates window rules at
    map time and a restored Firefox window's title at that instant is still
    the generic "Mozilla Firefox" -- the real title only arrives once the
    page finishes loading. A rule keyed on the recorded title can therefore
    never match, proven in production: both rules registered, neither
    matched, and both windows landed on the active workspace.

    What does work, verified live: moving an already-mapped window by
    address, via `hyprctl dispatch 'hl.dsp.window.move({ workspace = N,
    window = "address:0x..." })'`. So restore() waits for its windows to
    finish self-restoring (title included), then reconciles: it polls
    hypr.clients(), matches each live single-instance-class window to a
    recorded (title -> workspace) target, and moves whichever ones are not
    already where they belong. The move follows focus (also verified live),
    so the phase finishes by refocusing a fixed workspace.

    A real poll would call time.sleep(interval) between attempts; that is
    injected here as reconcile_sleep so no test actually waits on it, and
    clock is likewise a fake monotonic counter (a tiny fixed step, so it
    never trips restore()'s own 120s total_timeout on the way to this phase)
    rather than time.monotonic, so a "title never appears" test can force
    the deadline without a real 20s wait.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = os.environ.get("XDG_STATE_HOME")
        os.environ["XDG_STATE_HOME"] = str(pathlib.Path(self.tmp.name) / "state")

    def tearDown(self):
        if self.saved is None:
            os.environ.pop("XDG_STATE_HOME", None)
        else:
            os.environ["XDG_STATE_HOME"] = self.saved
        self.tmp.cleanup()

    def _log(self):
        return (session.state_dir() / "session.log").read_text()

    def _clock(self, step=0.001):
        counter = itertools.count(0.0, step)
        return lambda: next(counter)

    def test_matching_live_windows_are_moved_to_their_recorded_workspaces(self):
        events = PersistentEventBuffer()
        hypr = ReconcileHypr(events, {"firefox": ["0xa"]}, [
            live_client("firefox", "A", "0xLiveA", 1),
            live_client("firefox", "B", "0xLiveB", 1),
        ])
        snap = {"version": 1, "windows": [
            window(0, "firefox", 3, title="A"),
            window(1, "firefox", 7, title="B"),
        ]}
        placed = session.restore(snap, hypr, events, clock=self._clock(),
                                 reconcile_sleep=lambda s: None)
        self.assertEqual(placed, 1)
        self.assertEqual(
            sorted(hypr.moves), [("0xLiveA", 3), ("0xLiveB", 7)]
        )

    def test_window_already_on_its_recorded_workspace_is_not_moved(self):
        events = PersistentEventBuffer()
        hypr = ReconcileHypr(events, {"firefox": ["0xa"]}, [
            live_client("firefox", "A", "0xLiveA", 3),
        ])
        snap = {"version": 1, "windows": [
            window(0, "firefox", 3, title="A"),
        ]}
        session.restore(snap, hypr, events, clock=self._clock(),
                        reconcile_sleep=lambda s: None)
        self.assertEqual(hypr.moves, [])

    def test_a_title_that_never_appears_is_logged_unmatched_and_does_not_block_others(self):
        events = PersistentEventBuffer()
        hypr = ReconcileHypr(events, {"firefox": ["0xa"]}, [
            live_client("firefox", "Found", "0xFound", 1),
        ])
        snap = {"version": 1, "windows": [
            window(0, "firefox", 5, title="Found"),
            window(1, "firefox", 9, title="Ghost"),
        ]}
        session.restore(snap, hypr, events, clock=self._clock(),
                        reconcile_sleep=lambda s: None, reconcile_timeout=0)
        # The one that did appear is still placed...
        self.assertEqual(hypr.moves, [("0xFound", 5)])
        # ...and the one that never showed up is named in the log, not
        # silently dropped.
        text = self._log()
        self.assertIn("Ghost", text)
        self.assertIn("timed out", text.lower())

    def test_two_live_windows_sharing_a_title_match_one_target_each(self):
        events = PersistentEventBuffer()
        hypr = ReconcileHypr(events, {"firefox": ["0xa"]}, [
            live_client("firefox", "Dup", "0xL1", 1),
            live_client("firefox", "Dup", "0xL2", 1),
        ])
        snap = {"version": 1, "windows": [
            window(0, "firefox", 3, title="Dup"),
            window(1, "firefox", 5, title="Dup"),
        ]}
        session.restore(snap, hypr, events, clock=self._clock(),
                        reconcile_sleep=lambda s: None)
        # Each live window claims a distinct target -- workspaces 3 and 5
        # each appear exactly once, never both moves landing on the same one.
        self.assertEqual(hypr.moves, [("0xL1", 3), ("0xL2", 5)])

    def test_focus_is_restored_exactly_once_at_the_end(self):
        events = PersistentEventBuffer()
        hypr = ReconcileHypr(events, {"firefox": ["0xa"]}, [
            live_client("firefox", "A", "0xLiveA", 1),
        ])
        snap = {"version": 1, "windows": [
            window(0, "firefox", 3, title="A"),
        ]}
        session.restore(snap, hypr, events, clock=self._clock(),
                        reconcile_sleep=lambda s: None)
        self.assertEqual(hypr.focus_calls, [session.RECONCILE_FOCUS_WORKSPACE])

    def test_no_titled_single_instance_entries_skips_the_phase_and_focus(self):
        events = PersistentEventBuffer()
        hypr = ReconcileHypr(events, {"kitty": ["0xa"]}, [
            live_client("firefox", "Untracked", "0xIgnored", 1),
        ])
        snap = {"version": 1, "windows": [window(0, "kitty", 1)]}
        session.restore(snap, hypr, events, clock=self._clock(),
                        reconcile_sleep=lambda s: None)
        self.assertEqual(hypr.moves, [])
        self.assertEqual(hypr.focus_calls, [])

    def test_a_move_failure_is_logged_and_the_phase_continues(self):
        events = PersistentEventBuffer()
        hypr = ReconcileHypr(events, {"firefox": ["0xa"]}, [
            live_client("firefox", "A", "0xLiveA", 1),
            live_client("firefox", "B", "0xLiveB", 1),
        ], failing_move_addresses={"0xLiveA"})
        snap = {"version": 1, "windows": [
            window(0, "firefox", 3, title="A"),
            window(1, "firefox", 7, title="B"),
        ]}
        session.restore(snap, hypr, events, clock=self._clock(),
                        reconcile_sleep=lambda s: None)
        # Both moves were attempted...
        self.assertEqual(sorted(hypr.moves), [("0xLiveA", 3), ("0xLiveB", 7)])
        # ...the failure was logged...
        text = self._log()
        self.assertIn("A", text)
        self.assertIn("failed", text.lower())
        # ...and the phase still finished, restoring focus.
        self.assertEqual(hypr.focus_calls, [session.RECONCILE_FOCUS_WORKSPACE])

    def test_a_none_workspace_target_is_logged_and_skipped_others_still_moved(self):
        # Reviewer-caught defect: a recorded entry whose "workspace" is None
        # (a corrupted or hand-edited snapshot) used to raise TypeError out
        # of move_window(), abandoning every other target mid-phase and
        # skipping the final focus_workspace() call. Uses RealMoveHypr (the
        # actual move_window()/focus_workspace() bodies) rather than
        # ReconcileHypr's faked ones, since a fake move_window cannot
        # exercise the int() conversion this test is about.
        hypr = RealMoveHypr([
            live_client("firefox", "Bad", "0xBad", 1),
            live_client("firefox", "Good", "0xGood", 1),
        ])
        windows = [
            window(0, "firefox", None, title="Bad"),
            window(1, "firefox", 7, title="Good"),
        ]
        session._reconcile_single_instance_placements(
            hypr, windows, self._clock(), sleep=lambda s: None,
        )
        move_calls = [c for c in hypr.hyprctl_calls
                      if c[0] == "dispatch" and "window.move" in c[1]]
        focus_calls = [c for c in hypr.hyprctl_calls
                       if c[0] == "dispatch" and "hl.dsp.focus" in c[1]]
        # The bad target never produced a working move -- only "Good" did.
        self.assertEqual(len(move_calls), 1)
        self.assertIn("address:0xGood", move_calls[0][1])
        self.assertIn("workspace = 7", move_calls[0][1])
        # It was logged, by title, rather than silently dropped.
        text = self._log()
        self.assertIn("Bad", text)
        self.assertIn("invalid workspace", text.lower())
        # And the phase still reached its one focus call at the end.
        self.assertEqual(len(focus_calls), 1)


class MalformedSnapshotRestoreTest(unittest.TestCase):
    """restore() promises never to raise. A snapshot that is not shaped like
    a snapshot must therefore be a logged no-op, not an AttributeError that
    escapes boot and stops the watch loop from ever starting.
    """

    def test_non_object_snapshot_is_a_no_op(self):
        for snapshot in (123, [1, 2], True, "abc", None):
            with self.subTest(snapshot=snapshot):
                hypr = FakeHypr()
                self.assertEqual(
                    session.restore(snapshot, hypr, SilentEvents()), 0
                )
                self.assertEqual(hypr.execs, [])

    def test_non_list_window_collection_is_a_no_op(self):
        for windows in ("abc", 7, {"a": 1}, None):
            with self.subTest(windows=windows):
                hypr = FakeHypr()
                self.assertEqual(
                    session.restore({"windows": windows}, hypr, SilentEvents()), 0
                )
                self.assertEqual(hypr.execs, [])

    def test_non_object_entry_is_skipped_and_the_rest_still_restore(self):
        events = PersistentEventBuffer()
        hypr = EventEmittingHypr(events, {"kitty": ["0xa"]})
        snap = {"version": 1, "windows": [None, window(1, "kitty", 2), 42]}
        placed = session.restore(snap, hypr, events)
        self.assertEqual(placed, 1)
        self.assertEqual([argv[0] for _r, argv in hypr.execs], ["kitty"])

    def test_event_socket_loss_mid_restore_does_not_raise(self):
        class DyingEvents:
            def events(self, timeout=None):
                raise ConnectionError("event socket closed")
                yield  # pragma: no cover - makes this a generator

        hypr = FakeHypr()
        snap = {"version": 1, "windows": [window(0, "a", 1), window(1, "b", 2)]}
        # Must return, not raise: restore()'s contract, and boot must still
        # reach the watch loop afterwards.
        self.assertEqual(session.restore(snap, hypr, DyingEvents()), 0)


class RestoreLiveWindowWarningTest(unittest.TestCase):
    """restore() deliberately does not reconcile against live windows, so it
    duplicates everything when run against a populated session. It must say
    so in the log, because the README documents it as a hand-run command.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = os.environ.get("XDG_STATE_HOME")
        os.environ["XDG_STATE_HOME"] = str(pathlib.Path(self.tmp.name) / "state")

    def tearDown(self):
        if self.saved is None:
            os.environ.pop("XDG_STATE_HOME", None)
        else:
            os.environ["XDG_STATE_HOME"] = self.saved
        self.tmp.cleanup()

    def _log(self):
        return (session.state_dir() / "session.log").read_text()

    def test_warns_when_windows_are_already_open(self):
        hypr = ClientListHypr([{"class": "kitty"}, {"class": "firefox"}])
        session.restore({"version": 1, "windows": []}, hypr, SilentEvents())
        text = self._log()
        self.assertIn("restore: WARNING 2 window(s) are already open", text)
        self.assertIn("duplicated", text)

    def test_no_warning_on_an_empty_compositor(self):
        session.restore({"version": 1, "windows": []}, FakeHypr(), SilentEvents())
        self.assertNotIn("WARNING", self._log())


class ExecWithRulesLuaEscapingTest(unittest.TestCase):
    """hyprctl on this machine parses its dispatch argument as Lua, not as a
    classic hyprlang dispatcher string (Hyprland 0.56.2, Lua config):
    `hyprctl dispatch exec true` raises a Lua syntax error. The working
    form, verified against the live compositor, is
    `hl.dsp.exec_cmd("[rules] command")` passed to `hyprctl dispatch` as one
    argument -- so the whole rule-prefixed command must be embedded inside a
    Lua double-quoted string literal. These tests prove that embedding is
    done safely for commands real windows actually produce: a terminal
    relaunch with a space-bearing --directory path, and any command whose
    text contains a literal double quote (which would otherwise terminate
    the Lua string early and reproduce exactly the defect being fixed).
    """

    def _dispatched_lua_expr(self, rules, argv):
        hypr = session.Hypr()
        with mock.patch.object(session.subprocess, "run") as run:
            run.return_value = mock.Mock()
            ok = hypr.exec_with_rules(rules, argv)
        self.assertTrue(ok)
        args = run.call_args.args[0]
        self.assertEqual(args[0], "hyprctl")
        self.assertEqual(args[1], "dispatch")
        self.assertEqual(len(args), 3)
        return args[2]

    def test_space_bearing_path_survives_intact(self):
        lua_expr = self._dispatched_lua_expr(
            "workspace 3 silent",
            ["kitty", "--directory", "/home/user/My Projects"],
        )
        self.assertEqual(
            lua_expr,
            'hl.dsp.exec_cmd("[workspace 3 silent] kitty --directory '
            '\'/home/user/My Projects\'")',
        )

    def test_double_quote_in_command_is_escaped_for_lua(self):
        lua_expr = self._dispatched_lua_expr(
            "workspace 1 silent",
            ["kitty", "--title", 'Session "Test"'],
        )
        # shlex.quote wraps the unsafe argument in single quotes but leaves
        # the double quotes inside it literal; the Lua layer must then
        # escape those or the generated Lua string literal would terminate
        # early at the first one, exactly like the original defect.
        self.assertEqual(
            lua_expr,
            'hl.dsp.exec_cmd("[workspace 1 silent] kitty --title '
            '\'Session \\"Test\\"\'")',
        )

    def test_lua_quote_escapes_backslashes_and_double_quotes(self):
        raw = 'a "quoted" \\ path'
        self.assertEqual(
            session.Hypr._lua_quote(raw), 'a \\"quoted\\" \\\\ path'
        )

    def test_lua_quote_escapes_newlines(self):
        # A Lua short-string literal cannot contain a raw newline: it would
        # be an unterminated string, not a launch. /proc/<pid>/cmdline can
        # in principle hold one.
        self.assertEqual(session.Hypr._lua_quote("a\nb\r\n"), "a\\nb\\r\\n")

    def test_empty_rules_emits_no_bracket_prefix_at_all(self):
        # Single-instance classes (see SingleInstanceRestoreTest) are
        # launched with an empty rules string, since any exec rule at all
        # would attach to whichever window this already-running app maps
        # first -- not necessarily the recorded entry it was launched for.
        # An empty `rules` must therefore produce a plain command with no
        # "[...]" prefix, not a vacuous "[] firefox" -- hyprctl parses the
        # dispatch argument as Lua on this compositor, and a stray empty
        # bracket pair is exactly the kind of thing that must be verified,
        # not assumed.
        lua_expr = self._dispatched_lua_expr("", ["firefox"])
        self.assertEqual(lua_expr, 'hl.dsp.exec_cmd("firefox")')
        self.assertNotIn("[", lua_expr)
        self.assertNotIn("]", lua_expr)

    def test_newline_in_an_argument_stays_inside_the_lua_string(self):
        lua_expr = self._dispatched_lua_expr(
            "workspace 1 silent", ["kitty", "--title", "two\nlines"]
        )
        self.assertNotIn("\n", lua_expr)
        self.assertEqual(
            lua_expr,
            'hl.dsp.exec_cmd("[workspace 1 silent] kitty --title '
            '\'two\\nlines\'")',
        )


class HyprMoveAndFocusTest(unittest.TestCase):
    """Hypr.move_window() and Hypr.focus_workspace() dispatch through
    `hyprctl dispatch`, not `hyprctl eval` -- verified live: `eval` merely
    constructs the dispatcher object and discards it, while `dispatch`
    wraps the argument in `hl.dispatch(...)`, which actually executes it.
    Also verified live: a move follows focus (the active workspace becomes
    the destination), which is why restore() must refocus afterwards.
    """

    def _dispatch_args(self, method, *call_args, ok=True):
        hypr = session.Hypr()
        with mock.patch.object(session.subprocess, "run") as run:
            if ok:
                run.return_value = mock.Mock()
            else:
                run.side_effect = session.subprocess.SubprocessError("hyprctl died")
            result = getattr(hypr, method)(*call_args)
        return result, run

    def test_move_window_dispatches_the_verified_live_expression(self):
        result, run = self._dispatch_args(
            "move_window", "0x55f75cfb2200", 3
        )
        self.assertTrue(result)
        self.assertEqual(
            run.call_args.args[0],
            ["hyprctl", "dispatch",
             'hl.dsp.window.move({ workspace = 3, window = '
             '"address:0x55f75cfb2200" })'],
        )

    def test_move_window_failure_is_logged_and_returns_false(self):
        result, _run = self._dispatch_args(
            "move_window", "0xabc", 1, ok=False
        )
        self.assertFalse(result)

    def test_move_window_escapes_the_address_for_lua(self):
        # Hyprland addresses are always plain hex in practice, but the
        # address string still passes through the same Lua-string embedding
        # as every other value this module dispatches -- see _lua_quote.
        result, run = self._dispatch_args(
            "move_window", 'weird"addr', 2
        )
        self.assertTrue(result)
        lua_expr = run.call_args.args[0][2]
        self.assertIn('address:weird\\"addr', lua_expr)

    def test_move_window_with_a_non_numeric_workspace_returns_false_not_raise(self):
        # Reviewer-caught defect: int(workspace) used to run outside
        # _hyprctl's try/except, so a malformed recorded workspace (e.g.
        # None, from a corrupted snapshot) raised TypeError straight out of
        # move_window() instead of the ordinary logged-False failure every
        # other path through this method produces. No hyprctl subprocess
        # should even be attempted once the conversion itself has failed.
        hypr = session.Hypr()
        with mock.patch.object(session.subprocess, "run") as run:
            result = hypr.move_window("0xabc", None)
        self.assertFalse(result)
        run.assert_not_called()

    def test_focus_workspace_dispatches_the_verified_live_expression(self):
        result, run = self._dispatch_args("focus_workspace", 4)
        self.assertTrue(result)
        self.assertEqual(
            run.call_args.args[0],
            ["hyprctl", "dispatch", "hl.dsp.focus({ workspace = 4 })"],
        )

    def test_focus_workspace_failure_is_logged_and_returns_false(self):
        result, _run = self._dispatch_args("focus_workspace", 1, ok=False)
        self.assertFalse(result)


class ShouldWriteSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.tmp.name) / "session.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_non_empty_snapshot_always_writes(self):
        new = {"version": 1, "windows": [{"class": "kitty"}]}
        self.assertTrue(session.should_write_snapshot(new, self.path))

    def test_empty_snapshot_over_empty_existing_writes(self):
        session.write_snapshot({"version": 1, "windows": []}, self.path)
        new = {"version": 1, "windows": []}
        self.assertTrue(session.should_write_snapshot(new, self.path))

    def test_empty_snapshot_over_non_empty_existing_refused(self):
        session.write_snapshot({"version": 1, "windows": [{"class": "kitty"}]}, self.path)
        new = {"version": 1, "windows": []}
        self.assertFalse(session.should_write_snapshot(new, self.path))

    def test_missing_existing_snapshot_does_not_block_empty_write(self):
        new = {"version": 1, "windows": []}
        self.assertTrue(session.should_write_snapshot(new, self.path))

    def test_unreadable_existing_snapshot_does_not_block_empty_write(self):
        self.path.write_bytes(b'\xff\xfe invalid json')
        new = {"version": 1, "windows": []}
        self.assertTrue(session.should_write_snapshot(new, self.path))

    def test_non_object_existing_snapshot_does_not_raise(self):
        # A file holding valid non-object JSON used to raise AttributeError
        # here. Inside watch that is swallowed but fires on every capture,
        # so one bad file permanently stopped the snapshot being updated.
        for text in ("123", "[1, 2]", "true"):
            with self.subTest(text=text):
                self.path.write_text(text)
                self.assertTrue(
                    session.should_write_snapshot(
                        {"version": 1, "windows": []}, self.path
                    )
                )


class ScriptedEvents:
    """Yields a scripted batch per call, then nothing."""

    def __init__(self, batches):
        self.batches = list(batches)

    def events(self, timeout=None):
        if self.batches:
            for item in self.batches.pop(0):
                yield item


class RaisingEvents:
    """An event source whose every read fails, as a dead socket's does."""

    def __init__(self, exc, path=None):
        self.exc = exc
        self.closed = 0
        if path is not None:
            self.path = path

    def events(self, timeout=None):
        raise self.exc
        yield  # pragma: no cover - makes this a generator function

    def close(self):
        self.closed += 1


class SequencedEvents:
    """Each read yields the batch, or raises the exception, that stands in
    its place in the script. Models a socket that fails, recovers, and
    fails again."""

    def __init__(self, script):
        self.script = list(script)
        self.closed = 0

    def events(self, timeout=None):
        item = self.script.pop(0) if self.script else []
        if isinstance(item, BaseException):
            raise item
        for event in item:
            yield event

    def close(self):
        self.closed += 1


class WatchTest(unittest.TestCase):
    def test_events_in_separate_reads_within_debounce_produce_one_snapshot(self):
        """Events in separate reads coalesce if clock advances < debounce between them."""
        writes = []
        events = ScriptedEvents([
            [("movewindow", "0xa,2")],  # First event
            [],                          # Nothing
            [("movewindow", "0xb,3")],  # Second event (0.5s later)
            [],                          # Nothing
        ])
        # Ticks: event1(0), check(0), nothing, check(0.5), event2(0.5), check(0.5),
        #        nothing, check(1.5 - triggers snapshot because 1.5-0.5 >= 1.0)
        ticks = iter([0.0, 0.0, 0.5, 0.5, 1.5, 1.5])
        calls = {"n": 0}

        def stop():
            calls["n"] += 1
            return calls["n"] > 4

        session.watch(FakeHypr(), events, lambda: writes.append(1),
                      debounce=1.0, clock=lambda: next(ticks), stop=stop)
        # Events 0.5s apart (< 1.0s debounce) should produce ONE snapshot
        # firing when clock reaches 1.5s (1.0s after the last event at 0.5s)
        self.assertEqual(len(writes), 1)

    def test_two_bursts_separated_by_more_than_debounce_produce_two_snapshots(self):
        """Two event bursts > debounce interval apart produce two snapshots, with coalescing within each burst."""
        writes = []
        events = ScriptedEvents([
            [("movewindow", "0xa,2"), ("movewindow", "0xa,3"), ("movewindow", "0xa,4")],  # Burst 1: 3 events
            [],                          # Nothing (time passes)
            [],                          # Nothing
            [("movewindow", "0xb,5"), ("movewindow", "0xb,6")],  # Burst 2: 2 events at t=2.0
            [],                          # Nothing
            [],                          # Nothing
        ])
        # Iter1: burst1(0,0,0) all update pending_since to 0, check(0)
        # Iter2: nothing, check(0.5), Iter3: nothing, check(1.5→snap1!)
        # Iter4: burst2(2.0,2.0) update to 2.0, check(2.0), Iter5: nothing, check(2.5)
        # Iter6: nothing, check(3.0→snap2!)
        ticks = iter([0.0, 0.0, 0.0, 0.0, 0.5, 1.5, 2.0, 2.0, 2.0, 2.5, 3.0])
        calls = {"n": 0}

        def stop():
            calls["n"] += 1
            return calls["n"] > 6

        session.watch(FakeHypr(), events, lambda: writes.append(1),
                      debounce=1.0, clock=lambda: next(ticks), stop=stop)
        # First burst (3 events) coalesces to one snapshot at t=1.5
        # Second burst (2 events) coalesces to one snapshot at t=3.0
        self.assertEqual(len(writes), 2)

    def test_snapshot_does_not_fire_before_debounce_elapses(self):
        """No snapshot fires while clock has not advanced past debounce interval."""
        writes = []
        events = ScriptedEvents([
            [("movewindow", "0xa,2")],  # Event at t=0
            [],                          # Nothing
            [],                          # Nothing
        ])
        # Ticks: event(0), check(0), nothing, check(0.5), nothing, check(0.8)
        # Clock never reaches 1.0, so debounce never elapses
        ticks = iter([0.0, 0.0, 0.5, 0.8])
        calls = {"n": 0}

        def stop():
            calls["n"] += 1
            return calls["n"] > 3

        session.watch(FakeHypr(), events, lambda: writes.append(1),
                      debounce=1.0, clock=lambda: next(ticks), stop=stop)
        # No snapshot should fire because debounce never elapses
        self.assertEqual(writes, [])

    def test_snapshot_failure_does_not_end_the_loop(self):
        """A transient hyprctl or disk failure must not kill the watcher.

        Not propagating is only half of it: the loop has to keep running and
        keep taking snapshots afterwards, so this asserts that a second
        attempt happens after the first one raises.
        """
        attempts = []

        def boom():
            attempts.append(1)
            raise RuntimeError("hyprctl exploded")

        events = ScriptedEvents([
            [("openwindow", "0xa,1,kitty,t")],
            [("closewindow", "0xa")],
        ])
        # iter1: event at 0.0, check at 2.0 -> first (failing) snapshot.
        # iter2: event at 3.0, check at 5.0 -> second snapshot, proving the
        #        loop survived the first failure and is still working.
        ticks = iter([0.0, 2.0, 3.0, 5.0])
        calls = {"n": 0}

        def stop():
            calls["n"] += 1
            return calls["n"] > 2

        # Must return normally rather than propagate the exception.
        session.watch(FakeHypr(), events, boom, debounce=1.0,
                      clock=lambda: next(ticks), stop=stop)
        self.assertEqual(len(attempts), 2)

    def test_non_window_events_never_trigger_a_snapshot(self):
        """activewindow and focusedmon fire on every focus change.

        If they reached the debounce timer, merely moving the mouse between
        monitors would cost a full `hyprctl clients` call and a disk write.
        The clock advances 5s per reading, well past the 1s debounce, so a
        single spurious pending_since would produce a write.
        """
        writes = []
        events = ScriptedEvents([
            [("activewindow", "kitty,a title"), ("focusedmon", "DP-2,1")],
            [("activewindowv2", "0xa"), ("workspace", "3")],
            [("createworkspace", "3"), ("urgent", "0xa")],
            [],
        ])
        ticks = itertools.count(0.0, 5.0)
        calls = {"n": 0}

        def stop():
            calls["n"] += 1
            return calls["n"] > 4

        session.watch(FakeHypr(), events, lambda: writes.append(1),
                      debounce=1.0, clock=lambda: next(ticks), stop=stop)
        self.assertEqual(writes, [])

    def test_each_window_event_triggers_a_snapshot(self):
        """The behavioural counterpart: every name in WINDOW_EVENTS does
        reach the debounce timer and produce exactly one write."""
        for name in sorted(session.WINDOW_EVENTS):
            with self.subTest(event=name):
                writes = []
                events = ScriptedEvents([[(name, "0xa,1,kitty,t")], []])
                ticks = iter([0.0, 0.0, 2.0])
                calls = {"n": 0}

                def stop():
                    calls["n"] += 1
                    return calls["n"] > 2

                session.watch(FakeHypr(), events, lambda: writes.append(1),
                              debounce=1.0, clock=lambda: next(ticks),
                              stop=stop)
                self.assertEqual(len(writes), 1)

    # -- periodic refresh --------------------------------------------------
    # openwindow/closewindow/movewindow(v2) see only a window's existence
    # and geometry. They never fire for state that changes *inside* a
    # window -- a terminal's shell cd-ing elsewhere, or an ALLOWLIST program
    # starting up -- so watch() also captures periodically, on a timer
    # independent of any event.

    def test_no_events_but_interval_elapsed_triggers_one_capture(self):
        """With no window events at all, the periodic timer alone must
        still produce exactly one capture once it elapses."""
        writes = []
        ticks = iter([0.0, 3.0, 6.0, 6.5])
        calls = {"n": 0}

        def stop():
            calls["n"] += 1
            return calls["n"] > 4

        session.watch(FakeHypr(), SilentEvents(), lambda: writes.append(1),
                      debounce=1.0, interval=5.0, clock=lambda: next(ticks),
                      stop=stop)
        self.assertEqual(len(writes), 1)

    def test_event_driven_capture_resets_the_interval_timer(self):
        """A capture just taken via the debounce path must reset the
        periodic timer too -- otherwise a refresh fires right behind it."""
        writes = []
        events = ScriptedEvents([
            [],                          # iter1: nothing yet, bootstrap
            [("movewindow", "0xa,2")],   # iter2: event arrives
            [],                          # iter3: debounce not elapsed yet
            [],                          # iter4: debounce elapses -> capture
            [],                          # iter5: interval would have fired
                                          #        again by now measured from
                                          #        the original bootstrap,
                                          #        but must not: the event
                                          #        capture reset the clock.
        ])
        ticks = iter([0.0, 4.0, 4.0, 4.5, 5.5, 6.0])
        calls = {"n": 0}

        def stop():
            calls["n"] += 1
            return calls["n"] > 5

        session.watch(FakeHypr(), events, lambda: writes.append(1),
                      debounce=1.0, interval=5.0, clock=lambda: next(ticks),
                      stop=stop)
        self.assertEqual(len(writes), 1)

    def test_event_mid_interval_still_debounces_and_is_not_lost(self):
        """A much longer interval running in the background must not
        swallow or duplicate the ordinary debounce-driven capture."""
        writes = []
        events = ScriptedEvents([
            [("movewindow", "0xa,2")],  # event at t=0
            [],
            [],                          # debounce elapses at t=1.2
        ])
        ticks = iter([0.0, 0.0, 0.6, 1.2])
        calls = {"n": 0}

        def stop():
            calls["n"] += 1
            return calls["n"] > 3

        session.watch(FakeHypr(), events, lambda: writes.append(1),
                      debounce=1.0, interval=10.0, clock=lambda: next(ticks),
                      stop=stop)
        self.assertEqual(len(writes), 1)

    def test_interval_elapsing_before_debounce_still_captures_exactly_once(self):
        """The branch this review specifically flagged: an event is
        pending and its own debounce has not elapsed, but the periodic
        interval has. That must still capture -- exactly once, with
        pending_since cleared so the event's now-moot debounce deadline,
        once it too passes, does not trigger a second, redundant capture.
        """
        writes = []
        checkpoint = {}
        events = ScriptedEvents([
            [],                          # iter1: nothing yet, bootstrap
            [("movewindow", "0xa,2")],   # iter2: event arrives (t=5)
            [],                          # iter3: interval (6s since t=0)
                                          #        elapses before the 4s
                                          #        debounce (since t=5) does
                                          #        -> capture via the elif.
            [],                          # iter4: the event's own debounce
                                          #        deadline (t=9) has now
                                          #        passed too, but must not
                                          #        fire a second capture.
        ])
        ticks = iter([0.0, 5.0, 5.0, 7.0, 10.0])
        calls = {"n": 0}

        def stop():
            calls["n"] += 1
            if calls["n"] == 4:
                # iter3 has just run; iter4 has not started yet.
                checkpoint["after_iter3"] = len(writes)
            return calls["n"] > 4

        session.watch(FakeHypr(), events, lambda: writes.append(1),
                      debounce=4.0, interval=6.0, clock=lambda: next(ticks),
                      stop=stop)

        # The interval fired the capture on its own, ahead of the event's
        # own debounce -- not merely eventually, once the debounce (later)
        # elapsed too.
        self.assertEqual(checkpoint["after_iter3"], 1)
        # And nothing captured a second time once that stale deadline passed.
        self.assertEqual(len(writes), 1)


class WatchReconnectTest(unittest.TestCase):
    """The spec's error handling: "`watch` loses the socket: reconnect with
    exponential backoff. Hyprland exiting ends the process." The
    implementation used to log and return, ending snapshotting for the rest
    of the session on any transient socket failure.
    """

    def test_socket_loss_reconnects_and_keeps_watching(self):
        writes, sleeps, connects = [], [], []
        dead = RaisingEvents(ConnectionError("event socket closed"))
        fresh = ScriptedEvents([[("openwindow", "0xa,1,kitty,t")], []])

        def connect():
            connects.append(1)
            return fresh

        ticks = iter([0.0, 0.0, 2.0])
        calls = {"n": 0}

        def stop():
            calls["n"] += 1
            return calls["n"] > 3

        session.watch(FakeHypr(), dead, lambda: writes.append(1),
                      debounce=1.0, clock=lambda: next(ticks), stop=stop,
                      connect=connect, sleep=sleeps.append)
        self.assertEqual(len(connects), 1)
        self.assertEqual(sleeps, [1.0])
        self.assertEqual(dead.closed, 1, "the dead socket must be closed")
        # And the reconnected socket's events still drive snapshots.
        self.assertEqual(len(writes), 1)

    def test_plain_oserror_is_treated_as_socket_loss(self):
        # select() and recv() on a closed descriptor raise OSError (EBADF),
        # not ConnectionError. That used to escape watch(), escape main(),
        # and kill the process with no log line.
        sleeps, connects = [], []
        dead = RaisingEvents(OSError(9, "Bad file descriptor"))

        def connect():
            connects.append(1)
            return SilentEvents()

        calls = {"n": 0}

        def stop():
            calls["n"] += 1
            return calls["n"] > 2

        session.watch(FakeHypr(), dead, lambda: None, debounce=1.0,
                      stop=stop, connect=connect, sleep=sleeps.append)
        self.assertEqual(len(connects), 1)
        self.assertEqual(sleeps, [1.0])

    def test_backoff_is_exponential_capped_and_bounded(self):
        # A socket that refuses every reconnection must not spin: the delays
        # double up to the cap and the loop gives up after max_attempts.
        # No stop() here on purpose -- terminating is what is under test.
        sleeps, connects = [], []
        dead = RaisingEvents(ConnectionError("gone"))

        def connect():
            connects.append(1)
            raise OSError("connection refused")

        session.watch(FakeHypr(), dead, lambda: None, debounce=1.0,
                      connect=connect, sleep=sleeps.append,
                      backoff_start=1.0, backoff_max=4.0, max_attempts=5)
        self.assertEqual(sleeps, [1.0, 2.0, 4.0, 4.0, 4.0])
        self.assertEqual(len(connects), 5)

    def test_a_vanished_socket_path_ends_the_process(self):
        # Hyprland removes its runtime socket when it exits. Nothing to
        # reconnect to, so the watcher stops rather than burning its budget.
        connects = []
        dead = RaisingEvents(ConnectionError("closed"),
                             path="/nonexistent/hypr/none/.socket2.sock")
        session.watch(FakeHypr(), dead, lambda: None, debounce=1.0,
                      connect=lambda: connects.append(1), sleep=lambda d: None)
        self.assertEqual(connects, [])
        self.assertEqual(dead.closed, 1)

    def test_without_a_reconnector_the_loop_still_ends_cleanly(self):
        dead = RaisingEvents(ConnectionError("closed"))
        session.watch(FakeHypr(), dead, lambda: None, debounce=1.0)

    def test_a_successful_read_resets_the_retry_budget(self):
        # Two separate outages must each get the full budget, rather than
        # the second one inheriting the first one's spent attempts.
        sleeps = []
        source = SequencedEvents([
            ConnectionError("first outage"),
            [],                              # one clean read
            ConnectionError("second outage"),
            [],
        ])
        calls = {"n": 0}

        def stop():
            calls["n"] += 1
            return calls["n"] > 4

        session.watch(FakeHypr(), source, lambda: None, debounce=1.0,
                      stop=stop, connect=lambda: source, sleep=sleeps.append)
        # Both outages waited backoff_start, not backoff_start then double.
        self.assertEqual(sleeps, [1.0, 1.0])


class ClientListHypr(FakeHypr):
    """A FakeHypr whose clients() returns a fixed, caller-supplied list."""

    def __init__(self, clients):
        super().__init__()
        self._clients = clients

    def clients(self):
        return self._clients


class CaptureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.tmp.name)
        self.env = {
            "XDG_STATE_HOME": str(self.home / "state"),
            "XDG_CONFIG_HOME": str(self.home / "config"),
        }
        self.saved = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        self.proc = self.home / "proc"
        self.proc.mkdir()
        self.path = self.home / "state" / "hypr-session" / "session.json"

    def tearDown(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def client(self, cls, pid, ws):
        return {
            "class": cls, "pid": pid, "floating": False,
            "workspace": {"id": ws, "name": str(ws)},
            "monitor": 0, "at": [0, 0], "size": [100, 100], "fullscreen": 0,
        }

    def _log(self):
        return (session.state_dir() / "session.log").read_text()

    def test_capture_writes_a_normal_snapshot(self):
        fake_proc(self.proc, 1, ["kitty"], "kitty", "/home/u")
        hypr = ClientListHypr([self.client("kitty", 1, 1)])
        snapshot = session.capture(hypr, [], self.proc, self.path)
        self.assertEqual(len(snapshot["windows"]), 1)
        on_disk = session.read_snapshot(self.path)
        self.assertEqual(on_disk, snapshot)

    def test_capture_refuses_to_overwrite_good_snapshot_with_empty_one(self):
        # Seed an existing, non-empty snapshot on disk.
        good = {"version": 1, "saved_at": "T", "windows": [
            {"order": 0, "class": "kitty", "workspace": 1, "monitor": 0,
             "floating": False, "at": [0, 0], "size": [100, 100],
             "fullscreen": 0, "cmd": ["kitty"]},
        ]}
        session.write_snapshot(good, self.path)

        # The live client list is now empty (logout teardown).
        hypr = ClientListHypr([])
        snapshot = session.capture(hypr, [], self.proc, self.path)

        # capture() still returns what it observed...
        self.assertEqual(snapshot["windows"], [])
        # ...but the good snapshot on disk must be untouched.
        on_disk = session.read_snapshot(self.path)
        self.assertEqual(on_disk, good)

    # -- rolling backup --------------------------------------------------
    # The empty-snapshot guard only refuses "0 new over N existing". It
    # cannot refuse "1 new over 12 existing", and no count threshold can:
    # a failed restore and the user closing eleven windows look identical.
    # So the previous snapshot is kept instead, at session.json.1.

    def _seed(self, count):
        snapshot = {"version": 1, "saved_at": "T", "windows": [
            {"order": i, "class": f"app{i}", "workspace": 1, "monitor": 0,
             "floating": False, "at": [0, 0], "size": [100, 100],
             "fullscreen": 0, "cmd": [f"app{i}"]}
            for i in range(count)
        ]}
        session.write_snapshot(snapshot, self.path)
        return snapshot

    def test_capture_rotates_the_previous_snapshot_into_a_backup(self):
        previous = self._seed(3)
        fake_proc(self.proc, 1, ["kitty"], "kitty", "/home/u")
        session.capture(
            ClientListHypr([self.client("kitty", 1, 1)]), [], self.proc, self.path
        )
        self.assertEqual(
            [w["class"] for w in session.read_snapshot(self.path)["windows"]],
            ["kitty"],
        )
        self.assertEqual(
            session.read_snapshot(session.backup_path(self.path)), previous
        )

    def test_a_shrunken_write_leaves_the_bigger_snapshot_recoverable(self):
        # The exact incident: "restore: placed 0/8" then "save: wrote 7".
        self._seed(8)
        fake_proc(self.proc, 1, ["kitty"], "kitty", "/home/u")
        session.capture(
            ClientListHypr([self.client("kitty", 1, 1)]), [], self.proc, self.path
        )
        self.assertEqual(len(session.read_snapshot(self.path)["windows"]), 1)
        recovered = session.read_snapshot(session.backup_path(self.path))
        self.assertEqual(len(recovered["windows"]), 8)

    def test_missing_original_does_not_break_the_write(self):
        fake_proc(self.proc, 1, ["kitty"], "kitty", "/home/u")
        session.capture(
            ClientListHypr([self.client("kitty", 1, 1)]), [], self.proc, self.path
        )
        self.assertEqual(len(session.read_snapshot(self.path)["windows"]), 1)
        self.assertFalse(session.backup_path(self.path).exists())

    def test_rotation_failure_is_logged_rather_than_raised(self):
        self._seed(2)
        # A directory where the backup file belongs: os.replace onto it
        # raises IsADirectoryError, an OSError, from inside rotate_snapshot.
        session.backup_path(self.path).mkdir()
        fake_proc(self.proc, 1, ["kitty"], "kitty", "/home/u")
        session.capture(
            ClientListHypr([self.client("kitty", 1, 1)]), [], self.proc, self.path
        )
        # The write still happened...
        self.assertEqual(len(session.read_snapshot(self.path)["windows"]), 1)
        # ...and the failure was logged, not raised.
        self.assertIn(
            "rotate: could not write session.json.1", self._log()
        )

    def test_rotation_leaves_no_temp_files_behind(self):
        self._seed(1)
        fake_proc(self.proc, 1, ["kitty"], "kitty", "/home/u")
        session.capture(
            ClientListHypr([self.client("kitty", 1, 1)]), [], self.proc, self.path
        )
        self.assertEqual(
            sorted(p.name for p in self.path.parent.iterdir()),
            ["session.json", "session.json.1", "session.log"],
        )

    # -- write logging ---------------------------------------------------

    def test_capture_logs_the_number_of_windows_written(self):
        fake_proc(self.proc, 1, ["kitty"], "kitty", "/home/u")
        session.capture(
            ClientListHypr([self.client("kitty", 1, 1)]), [], self.proc, self.path
        )
        self.assertIn("capture: wrote 1 window(s)", self._log())

    def test_capture_logs_the_previous_count_so_shrinkage_is_visible(self):
        self._seed(12)
        fake_proc(self.proc, 1, ["kitty"], "kitty", "/home/u")
        session.capture(
            ClientListHypr([self.client("kitty", 1, 1)]), [], self.proc, self.path
        )
        self.assertIn("capture: wrote 1 window(s) (previously 12)", self._log())

    def test_a_failing_hyprctl_cannot_destroy_the_good_snapshot(self):
        # The end of the chain that Hypr.clients()'s empty list feeds: a
        # hyprctl that fails (as it does while the compositor is shutting
        # down) yields no clients, which the empty-snapshot guard then
        # refuses to write over a good snapshot.
        good = self._seed(5)
        hypr = session.Hypr()
        with mock.patch.object(session.subprocess, "run",
                               side_effect=OSError("no hyprctl")):
            session.capture(hypr, [], self.proc, self.path)
        self.assertEqual(session.read_snapshot(self.path), good)

    def test_a_refused_write_is_not_logged_as_a_write(self):
        self._seed(4)
        session.capture(ClientListHypr([]), [], self.proc, self.path)
        text = self._log()
        self.assertNotIn("capture: wrote", text)
        self.assertIn("capture: skipped write", text)

    # -- skip write when nothing changed ----------------------------------
    # A periodic refresh (Task: 30s timer alongside the event trigger) must
    # not rewrite the file, or spam the log, 2,880 times a day when nothing
    # actually changed. saved_at differs on every build, so the comparison
    # is against the windows list only.

    def test_repeated_capture_with_no_change_does_not_rewrite(self):
        # Both capture() calls below execute within the same wall-clock
        # second, so datetime.now() alone would make saved_at identical
        # regardless of whether the comparison actually excludes it -- this
        # test would then pass by accident, whether or not the exclusion is
        # real. Force a genuinely advancing clock so the windows-only
        # comparison is what's actually being exercised, not a same-second
        # coincidence.
        fake_proc(self.proc, 1, ["kitty"], "kitty", "/home/u")
        hypr = ClientListHypr([self.client("kitty", 1, 1)])

        counter = itertools.count()
        base = session.datetime.datetime(2026, 1, 1, 0, 0, 0)

        class _AdvancingDateTime(session.datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return base + session.datetime.timedelta(seconds=next(counter))

        with mock.patch.object(session.datetime, "datetime", _AdvancingDateTime):
            first_snapshot = session.capture(hypr, [], self.proc, self.path)
            on_disk_first = session.read_snapshot(self.path)

            second_snapshot = session.capture(hypr, [], self.proc, self.path)
            on_disk_second = session.read_snapshot(self.path)

        # The two builds really do carry different saved_at values --
        # confirming the test below isn't vacuously true.
        self.assertNotEqual(first_snapshot["saved_at"], second_snapshot["saved_at"])
        # ...yet the file on disk was not rewritten with the second one.
        self.assertEqual(on_disk_second, on_disk_first)
        self.assertIn("capture: skipped write, snapshot is unchanged", self._log())

    def test_capture_writes_when_windows_actually_changed(self):
        fake_proc(self.proc, 1, ["kitty"], "kitty", "/home/u")
        hypr = ClientListHypr([self.client("kitty", 1, 1)])
        session.capture(hypr, [], self.proc, self.path)
        on_disk_first = session.read_snapshot(self.path)

        fake_proc(self.proc, 2, ["kitty"], "kitty", "/home/u")
        hypr2 = ClientListHypr([
            self.client("kitty", 1, 1), self.client("kitty", 2, 1),
        ])
        session.capture(hypr2, [], self.proc, self.path)
        on_disk_second = session.read_snapshot(self.path)

        self.assertNotEqual(on_disk_second, on_disk_first)
        self.assertEqual(len(on_disk_second["windows"]), 2)

    def test_unchanged_capture_does_not_rotate_the_backup(self):
        # Rotating on a no-op write would overwrite session.json.1 with an
        # identical copy of session.json -- destroying the one recoverable
        # previous snapshot for no reason, on every unchanged refresh.
        fake_proc(self.proc, 1, ["kitty"], "kitty", "/home/u")
        hypr = ClientListHypr([self.client("kitty", 1, 1)])
        session.capture(hypr, [], self.proc, self.path)

        marker = {"version": 1, "saved_at": "T", "windows": [
            {"order": 0, "class": "distinct-backup-marker", "workspace": 1,
             "monitor": 0, "floating": False, "at": [0, 0], "size": [1, 1],
             "fullscreen": 0, "cmd": ["marker"]},
        ]}
        session.write_snapshot(marker, session.backup_path(self.path))

        session.capture(hypr, [], self.proc, self.path)

        self.assertEqual(
            session.read_snapshot(session.backup_path(self.path)), marker
        )


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.tmp.name)
        self.env = {
            "XDG_STATE_HOME": str(self.home / "state"),
            "XDG_CONFIG_HOME": str(self.home / "config"),
        }
        self.saved = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)

    def tearDown(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def test_unknown_mode_returns_nonzero(self):
        self.assertNotEqual(session.main(["nonsense"]), 0)

    def test_no_mode_returns_nonzero(self):
        self.assertNotEqual(session.main([]), 0)

    def test_log_writes_to_state_directory(self):
        session.log("hello from the test")
        logfile = session.state_dir() / "session.log"
        self.assertIn("hello from the test", logfile.read_text())

    def test_log_never_raises_when_directory_is_unwritable(self):
        os.environ["XDG_STATE_HOME"] = "/proc/cannot/write/here"
        session.log("this must not raise")

    def test_snapshot_path_lives_under_state_dir(self):
        self.assertEqual(
            session.snapshot_path(), session.state_dir() / "session.json"
        )

    def test_main_does_not_raise_when_event_socket_is_unavailable(self):
        # main() must never raise, not even when run outside a live
        # Hyprland session (missing HYPRLAND_INSTANCE_SIGNATURE /
        # XDG_RUNTIME_DIR, so EventSocket() cannot be constructed). No
        # snapshot exists on disk, which isolates the failure to
        # EventSocket() construction rather than to restore().
        saved_sig = os.environ.pop("HYPRLAND_INSTANCE_SIGNATURE", None)
        saved_runtime = os.environ.pop("XDG_RUNTIME_DIR", None)
        try:
            self.assertEqual(session.main(["watch"]), 0)
        finally:
            if saved_sig is not None:
                os.environ["HYPRLAND_INSTANCE_SIGNATURE"] = saved_sig
            if saved_runtime is not None:
                os.environ["XDG_RUNTIME_DIR"] = saved_runtime

    # -- boot mode -------------------------------------------------------
    # boot is the only mode login actually uses, and it is the one place
    # where a failure costs the whole session: an exception escaping restore
    # means the watch loop never starts, so nothing is snapshotted for the
    # rest of the session and the next reboot loses the real session too.

    SNAPSHOT_WITH_ONE_KITTY = {
        "version": 1, "saved_at": "T", "windows": [
            {"order": 0, "class": "kitty", "workspace": 1, "monitor": 0,
             "floating": False, "at": [0, 0], "size": [10, 10],
             "fullscreen": 0, "cmd": ["kitty"]},
        ],
    }

    def _run_boot(self, hypr=None, events=None):
        """Run main(["boot"]) against fakes.

        Returns (exit code, list of watch() calls). watch() is replaced so
        the test does not enter an endless loop; reaching it at all is the
        thing being asserted.
        """
        events = PersistentEventBuffer() if events is None else events
        hypr = FakeHypr() if hypr is None else hypr
        reached = []
        original = (session.Hypr, session.EventSocket, session.watch)
        session.Hypr = lambda: hypr
        session.EventSocket = lambda: events
        session.watch = lambda *args, **kwargs: reached.append((args, kwargs))
        try:
            code = session.main(["boot"])
        finally:
            session.Hypr, session.EventSocket, session.watch = original
        return code, reached

    def _write_snapshot_text(self, text):
        path = session.snapshot_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def test_boot_restores_the_snapshot_then_enters_the_watch_loop(self):
        session.write_snapshot(self.SNAPSHOT_WITH_ONE_KITTY,
                               session.snapshot_path())
        events = PersistentEventBuffer()
        hypr = EventEmittingHypr(events, {"kitty": ["0xa"]})
        code, reached = self._run_boot(hypr, events)
        self.assertEqual(code, 0)
        self.assertEqual([argv[0] for _r, argv in hypr.execs], ["kitty"])
        self.assertEqual(len(reached), 1, "boot must reach the watch loop")

    def test_boot_reaches_watch_with_no_snapshot_at_all(self):
        code, reached = self._run_boot()
        self.assertEqual(code, 0)
        self.assertEqual(len(reached), 1)

    def test_boot_reaches_watch_when_the_snapshot_is_not_an_object(self):
        # `123` parses as JSON but is not a snapshot. This used to end boot
        # with an AttributeError traceback before the watch loop started.
        self._write_snapshot_text("123")
        code, reached = self._run_boot()
        self.assertEqual(code, 0)
        self.assertEqual(len(reached), 1)

    def test_boot_reaches_watch_when_an_entry_is_not_an_object(self):
        self._write_snapshot_text('{"version": 1, "windows": [null]}')
        code, reached = self._run_boot()
        self.assertEqual(code, 0)
        self.assertEqual(len(reached), 1)

    def test_boot_reaches_watch_even_if_restore_raises(self):
        session.write_snapshot(self.SNAPSHOT_WITH_ONE_KITTY,
                               session.snapshot_path())

        def exploding(*args, **kwargs):
            raise RuntimeError("restore exploded")

        original_restore = session.restore
        session.restore = exploding
        try:
            code, reached = self._run_boot()
        finally:
            session.restore = original_restore
        self.assertEqual(code, 0)
        self.assertEqual(len(reached), 1,
                         "an exception from restore must not skip watch")
        self.assertIn("restore failed",
                      (session.state_dir() / "session.log").read_text())

    def test_save_does_not_claim_a_write_that_was_refused(self):
        # capture() refuses to overwrite a good snapshot with an empty one,
        # but save mode logged "wrote N window(s)" regardless.
        good = {"version": 1, "saved_at": "T", "windows": [
            {"order": 0, "class": "kitty", "workspace": 1, "monitor": 0,
             "floating": False, "at": [0, 0], "size": [10, 10],
             "fullscreen": 0, "cmd": ["kitty"]},
        ]}
        session.write_snapshot(good, session.snapshot_path())
        original = session.Hypr
        session.Hypr = FakeHypr  # clients() -> [], as at logout
        try:
            self.assertEqual(session.main(["save"]), 0)
        finally:
            session.Hypr = original
        text = (session.state_dir() / "session.log").read_text()
        self.assertNotIn("save: wrote", text)
        self.assertIn("save: observed 0 window(s)", text)
        self.assertIn("capture: skipped write", text)
        # And the good snapshot is still there.
        self.assertEqual(session.read_snapshot(session.snapshot_path()), good)

    def test_main_save_does_not_raise_when_state_dir_is_unwritable(self):
        # main() must never raise -- not even in save mode when the state
        # directory cannot be created (e.g. /proc, which refuses mkdir).
        # session.Hypr is swapped for FakeHypr for the duration of this call
        # so the test never touches the live compositor via hyprctl; that
        # substitution is undone in finally regardless of outcome.
        os.environ["XDG_STATE_HOME"] = "/proc/cannot/write/here"
        original_hypr = session.Hypr
        session.Hypr = FakeHypr
        try:
            self.assertEqual(session.main(["save"]), 0)
        finally:
            session.Hypr = original_hypr


if __name__ == "__main__":
    unittest.main()
