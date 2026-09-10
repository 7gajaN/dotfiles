# Hyprland Session Persistence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore the set of open windows, and their workspace and monitor placement, across a reboot.

**Architecture:** A single Python script, `.config/hypr/session.py`, runs in `boot` mode from Hyprland's start hook. It first replays a saved snapshot by launching each application serially onto its recorded workspace, then enters a loop reading Hyprland's event socket and rewriting the snapshot whenever windows change. All I/O with Hyprland goes through one injectable adapter object so the logic is testable without a running compositor.

**Tech Stack:** Python 3.14 standard library only. `unittest` for tests. Hyprland 0.56.2 IPC (`hyprctl`, `.socket2.sock`).

**Spec:** `docs/superpowers/specs/2026-09-10-hypr-session-persistence-design.md`

## Global Constraints

- **Python standard library only.** `socat` and `jq` are NOT installed on this machine. Do not add dependencies. `pytest` is NOT installed; use `unittest`.
- **Target:** Hyprland `0.56.2`, Python `3.14.6`, kernel `7.1.8-arch1-3`.
- **Script path:** `.config/hypr/session.py` in the repo, which reaches the system as `~/.config/hypr/session.py` through the existing symlink.
- **State directory:** `~/.local/state/hypr-session/`, holding `session.json` and `session.log`. Never tracked in git.
- **Denylist path:** `~/.config/hypr/session-denylist.txt`, one regex per line.
- **Snapshot schema version:** `1`.
- **Restore timeouts:** 10 s per window, 120 s for the whole restore phase.
- **Debounce:** 1000 ms.
- **Commit message style, matching this repo's history:** `area: lowercase imperative summary`, for example `hyprland: bind ALT+CTRL+1 to the portrait workspace`. Use `hyprland:` as the area for every commit in this plan.
- **No network access is required or permitted at runtime.**

## File Structure

| File | Responsibility |
| --- | --- |
| `.config/hypr/session.py` | Everything: snapshot IO, capture, filtering, Hyprland adapter, restore, watch loop, CLI. Single file per the approved design. Organised into the section order the tasks below build. |
| `tests/test_session.py` | All unit tests. Imports `session.py` by explicit path via `importlib`. |
| `.config/hypr/session-denylist.txt` | Ships empty with a comment header. User-editable. |
| `.config/hypr/hyprland.lua` | Modified: one line added to the existing `hl.on("hyprland.start")` block. |

`session.py` stays one file because the approved design specifies it and because the whole
module is roughly 350 lines, small enough to reason about at once. Testability comes from the
`Hypr` adapter boundary rather than from splitting files.

---

### Task 1: Test harness and atomic snapshot IO

**Files:**
- Create: `.config/hypr/session.py`
- Create: `tests/test_session.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `SCHEMA_VERSION: int = 1`
  - `state_dir() -> pathlib.Path`
  - `write_snapshot(snapshot: dict, path: pathlib.Path) -> None`
  - `read_snapshot(path: pathlib.Path) -> dict | None`

`read_snapshot` returns `None` for a missing, empty, or malformed file. It never raises. This is
what lets restore degrade to a no-op instead of blocking login.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_session.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ~/coding/dotfiles && python -m unittest tests.test_session -v`

Expected: FAIL. The first error is `FileNotFoundError` on `session.py`, because the file does not exist yet.

- [ ] **Step 3: Write the minimal implementation**

Create `.config/hypr/session.py`:

```python
#!/usr/bin/env python
"""Save and restore Hyprland window placement across reboots.

Design: docs/superpowers/specs/2026-09-10-hypr-session-persistence-design.md
Standard library only -- socat and jq are not installed on this machine.
"""

import json
import os
import pathlib
import tempfile

SCHEMA_VERSION = 1


def state_dir() -> pathlib.Path:
    """Return the state directory, honouring XDG_STATE_HOME."""
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return pathlib.Path(base) / "hypr-session"


def write_snapshot(snapshot: dict, path: pathlib.Path) -> None:
    """Write the snapshot atomically.

    The temporary file is created in the destination directory so that
    os.replace stays on one filesystem and is therefore atomic. A reader
    never observes a truncated snapshot, even if this process is killed
    mid-write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".session-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(snapshot, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        # Leave no debris if anything above failed.
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def read_snapshot(path: pathlib.Path) -> dict | None:
    """Return the snapshot, or None if it is missing, empty, or malformed.

    Never raises. Restore must degrade to a no-op rather than block login.
    """
    try:
        text = path.read_text()
    except OSError:
        return None
    if not text.strip():
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd ~/coding/dotfiles && python -m unittest tests.test_session -v`

Expected: PASS, 6 tests.

- [ ] **Step 5: Commit**

```bash
cd ~/coding/dotfiles
git add .config/hypr/session.py tests/test_session.py
git commit -m "hyprland: add session snapshot atomic read and write"
```

---

### Task 2: Resolve a launch command from a PID

**Files:**
- Modify: `.config/hypr/session.py`
- Modify: `tests/test_session.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces:
  - `TERMINALS: dict[str, str]` mapping window class to the flag that sets its working directory, initially `{"kitty": "--directory"}`
  - `SHELLS: frozenset[str]` — `{"zsh", "bash", "fish", "sh", "dash"}`
  - `read_cmdline(pid: int, proc: pathlib.Path) -> list[str]` — empty list when unreadable
  - `child_shell_cwd(pid: int, proc: pathlib.Path) -> str | None`
  - `resolve_command(client: dict, proc: pathlib.Path) -> list[str]` — empty list when the window cannot be relaunched

`proc` is the filesystem root to read process information from. Production passes
`pathlib.Path("/proc")`; tests pass a temporary directory. This injection is the only reason
these functions are testable at all, so do not hardcode `/proc`.

For a terminal class, `resolve_command` walks to the child shell, reads its working directory,
and appends the directory flag. Verified on this machine: kitty's own `cwd` is not the shell's,
and kitty additionally spawns `kitten` helper processes that must be ignored.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_session.py`, before the `if __name__` block:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ~/coding/dotfiles && python -m unittest tests.test_session.CommandResolutionTest -v`

Expected: FAIL with `AttributeError: module 'session' has no attribute 'read_cmdline'`.

- [ ] **Step 3: Write the minimal implementation**

Append to `.config/hypr/session.py`:

```python
# Window classes that are terminals, mapped to the flag that sets their
# starting directory. Extend this when adding another terminal emulator.
TERMINALS = {"kitty": "--directory"}

SHELLS = frozenset({"zsh", "bash", "fish", "sh", "dash"})


def read_cmdline(pid: int, proc: pathlib.Path) -> list[str]:
    """Return the process argv, or an empty list when it cannot be read."""
    try:
        raw = (proc / str(pid) / "cmdline").read_bytes()
    except OSError:
        return []
    return [part for part in raw.decode(errors="replace").split("\0") if part]


def _children(pid: int, proc: pathlib.Path) -> list[int]:
    """Return direct child PIDs of pid."""
    try:
        raw = (proc / str(pid) / "task" / str(pid) / "children").read_text()
    except OSError:
        return []
    return [int(tok) for tok in raw.split()]


def _comm(pid: int, proc: pathlib.Path) -> str:
    try:
        return (proc / str(pid) / "comm").read_text().strip()
    except OSError:
        return ""


def child_shell_cwd(pid: int, proc: pathlib.Path) -> str | None:
    """Return the working directory of the terminal's child shell.

    A terminal's own cwd is where it was launched from, not where the user
    has navigated to. The interesting directory belongs to the shell running
    inside it. kitty also spawns 'kitten' helper children, which are skipped
    because their comm is not in SHELLS.
    """
    for child in _children(pid, proc):
        if _comm(child, proc) in SHELLS:
            try:
                return os.readlink(proc / str(child) / "cwd")
            except OSError:
                return None
    return None


def resolve_command(client: dict, proc: pathlib.Path) -> list[str]:
    """Return the argv that recreates this window, or [] if it cannot be built."""
    argv = read_cmdline(client["pid"], proc)
    if not argv:
        return []
    flag = TERMINALS.get(client["class"])
    if flag:
        cwd = child_shell_cwd(client["pid"], proc)
        if cwd:
            return [argv[0], flag, cwd]
        return [argv[0]]
    return argv
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd ~/coding/dotfiles && python -m unittest tests.test_session -v`

Expected: PASS, 12 tests.

- [ ] **Step 5: Commit**

```bash
cd ~/coding/dotfiles
git add .config/hypr/session.py tests/test_session.py
git commit -m "hyprland: resolve window launch commands from /proc"
```

---

### Task 3: Window filtering

**Files:**
- Modify: `.config/hypr/session.py`
- Modify: `tests/test_session.py`
- Create: `.config/hypr/session-denylist.txt`

**Interfaces:**
- Consumes: `resolve_command` from Task 2.
- Produces:
  - `load_denylist(path: pathlib.Path) -> list[re.Pattern]`
  - `is_transient(client: dict, clients: list[dict]) -> bool`
  - `should_skip(client: dict, clients: list[dict], denylist: list[re.Pattern], command: list[str]) -> str | None` — returns the reason as a string, or `None` to keep the window

`should_skip` returns a reason rather than a boolean so the caller can log why a window was
dropped. Evaluation order is fixed by the spec: denylist, then unresolvable command, then the
transient heuristic.

Hyprland's IPC exposes no `transient` or `modal` field — this was verified against the full key
list in the spec. The heuristic stands in for it: a floating window that shares a PID with
another window of the same class is almost always a dialog belonging to that application.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_session.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ~/coding/dotfiles && python -m unittest tests.test_session.FilteringTest -v`

Expected: FAIL with `AttributeError: module 'session' has no attribute 'load_denylist'`.

- [ ] **Step 3: Write the minimal implementation**

Add `import re` to the imports at the top of `.config/hypr/session.py`, then append:

```python
def denylist_path() -> pathlib.Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return pathlib.Path(base) / "hypr" / "session-denylist.txt"


def load_denylist(path: pathlib.Path) -> list:
    """Compile one regex per non-comment line. Invalid patterns are dropped."""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    patterns = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            patterns.append(re.compile(line))
        except re.error:
            # A typo in the denylist must not break snapshotting.
            continue
    return patterns


def is_transient(client: dict, clients: list) -> bool:
    """Approximate 'this window is a dialog of another window'.

    Hyprland's IPC exposes no transient or modal field, so this heuristic
    stands in for it: a floating window that shares a PID with another
    window of the same class is treated as that application's dialog.
    """
    if not client.get("floating"):
        return False
    return any(
        other is not client
        and other.get("pid") == client.get("pid")
        and other.get("class") == client.get("class")
        and not other.get("floating")
        for other in clients
    )


def should_skip(client: dict, clients: list, denylist: list,
                command: list) -> str | None:
    """Return the reason to skip this window, or None to keep it."""
    cls = client.get("class", "")
    if any(pattern.search(cls) for pattern in denylist):
        return "denylist"
    if not command:
        return "no-command"
    if is_transient(client, clients):
        return "transient"
    return None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd ~/coding/dotfiles && python -m unittest tests.test_session -v`

Expected: PASS, 21 tests.

- [ ] **Step 5: Create the shipped denylist**

```bash
cd ~/coding/dotfiles
cat > .config/hypr/session-denylist.txt <<'EOF'
# One Python regex per line, matched against the Hyprland window class.
# A matching window is never saved and never restored.
# Lines starting with # are ignored. Example:
#   ^steam$
EOF
```

- [ ] **Step 6: Commit**

```bash
cd ~/coding/dotfiles
git add .config/hypr/session.py .config/hypr/session-denylist.txt tests/test_session.py
git commit -m "hyprland: filter denylisted, unlaunchable and transient windows"
```

---

### Task 4: Build a snapshot from a client list

**Files:**
- Modify: `.config/hypr/session.py`
- Modify: `tests/test_session.py`

**Interfaces:**
- Consumes: `resolve_command` (Task 2), `should_skip` (Task 3), `SCHEMA_VERSION` (Task 1).
- Produces:
  - `build_snapshot(clients: list[dict], denylist: list, proc: pathlib.Path, now: str) -> dict`

`now` is passed in rather than read from the clock so tests are deterministic.

Ordering is the point of this task. Windows are sorted by workspace id, then by their index in
the `hyprctl -j clients` output, which approximates stack order. Restoring in that order makes
hy3's arrival order on workspace 11 repeatable between boots — the closest thing available to
restoring a layout, given that split ratios are unreadable.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_session.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ~/coding/dotfiles && python -m unittest tests.test_session.BuildSnapshotTest -v`

Expected: FAIL with `AttributeError: module 'session' has no attribute 'build_snapshot'`.

- [ ] **Step 3: Write the minimal implementation**

Append to `.config/hypr/session.py`:

```python
def build_snapshot(clients: list, denylist: list, proc: pathlib.Path,
                   now: str) -> dict:
    """Turn a hyprctl client list into a snapshot document.

    Windows are ordered by workspace, then by their position in the client
    list, which approximates stack order. Restoring in this order makes hy3's
    arrival order on workspace 11 repeatable between boots.
    """
    ranked = []
    for index, client in enumerate(clients):
        command = resolve_command(client, proc)
        reason = should_skip(client, clients, denylist, command)
        if reason:
            log(f"skip {client.get('class', '?')}: {reason}")
            continue
        ranked.append((client["workspace"]["id"], index, client, command))

    ranked.sort(key=lambda item: (item[0], item[1]))

    windows = []
    for order, (workspace, _index, client, command) in enumerate(ranked):
        windows.append({
            "order": order,
            "class": client["class"],
            "workspace": workspace,
            "monitor": client["monitor"],
            "floating": bool(client.get("floating")),
            "at": client.get("at", [0, 0]),
            "size": client.get("size", [0, 0]),
            "fullscreen": client.get("fullscreen", 0),
            "cmd": command,
        })

    return {"version": SCHEMA_VERSION, "saved_at": now, "windows": windows}
```

`build_snapshot` calls `log`, which Task 8 defines. Add this temporary stub immediately after
the imports so the module still runs; Task 8 replaces it with the real implementation:

```python
def log(message: str) -> None:
    """Replaced by the file-backed logger in Task 8."""
    print(message)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd ~/coding/dotfiles && python -m unittest tests.test_session -v`

Expected: PASS, 26 tests.

- [ ] **Step 5: Commit**

```bash
cd ~/coding/dotfiles
git add .config/hypr/session.py tests/test_session.py
git commit -m "hyprland: build ordered session snapshots from client list"
```

---

### Task 5: Hyprland adapter and event parsing

**Files:**
- Modify: `.config/hypr/session.py`
- Modify: `tests/test_session.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `parse_event(line: str) -> tuple[str, str] | None`
  - `WINDOW_EVENTS: frozenset[str]` — `{"openwindow", "closewindow", "movewindow", "movewindowv2"}`
  - `class Hypr` with:
    - `clients() -> list[dict]`
    - `dispatch(args: str) -> bool`
    - `exec_with_rules(rules: str, argv: list[str]) -> bool`
  - `class EventSocket` with `events(timeout: float | None) -> Iterator[tuple[str, str]]` and `close()`

Event names were verified present in the Hyprland 0.56.2 binary: `openwindow`, `closewindow`,
`movewindow`, `movewindowv2`, `activewindow`. The socket emits `eventname>>data` lines.

`Hypr` is the single seam through which every later task talks to the compositor. Tasks 6 and 7
are tested with fake `Hypr` objects, so keep this class free of logic beyond process invocation.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_session.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ~/coding/dotfiles && python -m unittest tests.test_session.EventParsingTest -v`

Expected: FAIL with `AttributeError: module 'session' has no attribute 'parse_event'`.

- [ ] **Step 3: Write the minimal implementation**

Add `import shlex`, `import socket`, `import subprocess` and `import select` to the imports, then append:

```python
# Verified present in the Hyprland 0.56.2 binary.
WINDOW_EVENTS = frozenset({"openwindow", "closewindow", "movewindow", "movewindowv2"})


def parse_event(line: str) -> tuple | None:
    """Split a socket2 line of the form 'eventname>>payload'."""
    if ">>" not in line:
        return None
    name, _, payload = line.partition(">>")
    if not name:
        return None
    return name, payload


class Hypr:
    """Every call into the compositor goes through here.

    Kept deliberately thin: restore and watch are tested against fakes that
    implement this same interface, so any logic added here escapes testing.
    """

    def clients(self) -> list:
        try:
            out = subprocess.run(
                ["hyprctl", "-j", "clients"],
                capture_output=True, text=True, timeout=10, check=True,
            ).stdout
        except (subprocess.SubprocessError, OSError) as exc:
            log(f"hyprctl clients failed: {exc}")
            return []
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            log("hyprctl clients returned invalid JSON")
            return []

    def _hyprctl(self, args: list, label: str) -> bool:
        try:
            subprocess.run(
                ["hyprctl", *args],
                capture_output=True, text=True, timeout=10, check=True,
            )
            return True
        except (subprocess.SubprocessError, OSError) as exc:
            log(f"hyprctl failed [{label}]: {exc}")
            return False

    def dispatch(self, args: str) -> bool:
        return self._hyprctl(["dispatch", *shlex.split(args)], args)

    def exec_with_rules(self, rules: str, argv: list) -> bool:
        """Run 'hyprctl dispatch exec [rules] cmd'.

        The rule prefix is applied when the window maps, so the window never
        appears on the wrong workspace first. Verified supported in 0.56.2.

        This deliberately does NOT go through dispatch(): shlex.split would
        shatter '[workspace 1 silent]' into four argv elements. The whole
        prefixed command must reach hyprctl as one argument.
        """
        command = " ".join(shlex.quote(part) for part in argv)
        payload = f"[{rules}] {command}"
        return self._hyprctl(["dispatch", "exec", payload], payload)


class EventSocket:
    """Reader for Hyprland's .socket2.sock event stream."""

    def __init__(self, path: str | None = None):
        if path is None:
            runtime = os.environ["XDG_RUNTIME_DIR"]
            signature = os.environ["HYPRLAND_INSTANCE_SIGNATURE"]
            path = f"{runtime}/hypr/{signature}/.socket2.sock"
        self.path = path
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self.path)
        self.buffer = ""

    def events(self, timeout: float | None = None):
        """Yield (name, payload) pairs. Returns when timeout elapses."""
        ready, _, _ = select.select([self.sock], [], [], timeout)
        if not ready:
            return
        chunk = self.sock.recv(8192)
        if not chunk:
            raise ConnectionError("event socket closed")
        self.buffer += chunk.decode(errors="replace")
        while "\n" in self.buffer:
            line, _, self.buffer = self.buffer.partition("\n")
            parsed = parse_event(line.strip())
            if parsed:
                yield parsed

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd ~/coding/dotfiles && python -m unittest tests.test_session -v`

Expected: PASS, 32 tests.

- [ ] **Step 5: Verify the adapter against the live compositor**

Run: `cd ~/coding/dotfiles && python -c "
import importlib.util, pathlib
s = importlib.util.spec_from_file_location('session', '.config/hypr/session.py')
m = importlib.util.module_from_spec(s); s.loader.exec_module(m)
print('clients:', len(m.Hypr().clients()))
sock = m.EventSocket(); print('socket connected'); sock.close()
"`

Expected: a client count of at least 1, then `socket connected`.

- [ ] **Step 6: Commit**

```bash
cd ~/coding/dotfiles
git add .config/hypr/session.py tests/test_session.py
git commit -m "hyprland: add compositor adapter and event socket reader"
```

---

### Task 6: Restore engine

**Files:**
- Modify: `.config/hypr/session.py`
- Modify: `tests/test_session.py`

**Interfaces:**
- Consumes: `Hypr`, `EventSocket`, `WINDOW_EVENTS` (Task 5); `read_snapshot` (Task 1).
- Produces:
  - `QUIRKS: dict[str, list[str]]` — initially `{"firefox": ["--new-window"]}`
  - `restore_command(window: dict, seen: dict) -> list[str]` — mutates `seen`, a class-to-count map
  - `restore(snapshot: dict, hypr, events, per_window_timeout: float = 10.0, total_timeout: float = 120.0, clock=time.monotonic) -> int` — returns the number of windows successfully placed

`events` is any object with an `events(timeout)` method yielding `(name, payload)`, so tests pass
a fake. `clock` is injected so timeout behaviour is testable without sleeping.

Some applications are single-instance: a second `firefox` invocation focuses the existing window
instead of opening a new one. `QUIRKS` supplies the extra argument needed from the second window
of a class onward.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_session.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ~/coding/dotfiles && python -m unittest tests.test_session.RestoreTest -v`

Expected: FAIL with `AttributeError: module 'session' has no attribute 'restore'`.

- [ ] **Step 3: Write the minimal implementation**

Add `import time` to the imports, then append:

```python
# Applications that focus an existing window instead of opening a new one.
# The listed arguments are added from the second window of that class onward.
# Expect to add entries here as single-instance applications are encountered.
QUIRKS = {"firefox": ["--new-window"]}


def restore_command(window: dict, seen: dict) -> list:
    """Return the argv for this window, applying single-instance quirks."""
    cls = window["class"]
    count = seen.get(cls, 0)
    seen[cls] = count + 1
    argv = list(window["cmd"])
    if count and cls in QUIRKS:
        argv.extend(QUIRKS[cls])
    return argv


def _await_window(events, cls: str, deadline: float, clock) -> str | None:
    """Wait for an openwindow event whose class matches. Return its address."""
    while clock() < deadline:
        for name, payload in events.events(timeout=0.25):
            if name != "openwindow":
                continue
            parts = payload.split(",", 3)
            if len(parts) >= 3 and parts[2] == cls:
                return parts[0]
    return None


def restore(snapshot: dict, hypr, events, per_window_timeout: float = 10.0,
            total_timeout: float = 120.0, clock=time.monotonic) -> int:
    """Relaunch each saved window onto its workspace, one at a time.

    Serial by design: with several windows of the same class, parallel launches
    give no guarantee about which window arrives first, so windows can land on
    the wrong workspace.

    Returns the count of windows confirmed placed. Never raises -- a failure at
    any point degrades to a log line, because restore must not block login.
    """
    windows = sorted(snapshot.get("windows", []), key=lambda w: w["order"])
    started = clock()
    seen: dict = {}
    placed = 0

    for window in windows:
        if clock() - started > total_timeout:
            log(f"restore: total timeout, abandoning {len(windows) - placed} window(s)")
            break

        argv = restore_command(window, seen)
        rules = f"workspace {window['workspace']} silent"
        if not hypr.exec_with_rules(rules, argv):
            log(f"restore: launch failed for {window['class']}")
            continue

        address = _await_window(
            events, window["class"], clock() + per_window_timeout, clock
        )
        if not address:
            log(f"restore: {window['class']} did not map in {per_window_timeout}s")
            continue

        if window.get("floating"):
            x, y = window["at"]
            w, h = window["size"]
            hypr.dispatch(f"setfloating address:{address}")
            hypr.dispatch(f"movewindowpixel exact {x} {y},address:{address}")
            hypr.dispatch(f"resizewindowpixel exact {w} {h},address:{address}")

        if window.get("fullscreen"):
            hypr.dispatch(f"focuswindow address:{address}")
            hypr.dispatch(f"fullscreen {window['fullscreen']}")

        placed += 1

    log(f"restore: placed {placed}/{len(windows)} window(s)")
    return placed
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd ~/coding/dotfiles && python -m unittest tests.test_session -v`

Expected: PASS, 44 tests.

- [ ] **Step 5: Commit**

```bash
cd ~/coding/dotfiles
git add .config/hypr/session.py tests/test_session.py
git commit -m "hyprland: add serial window restore with single-instance quirks"
```

---

### Task 7: Debounced watch loop

**Files:**
- Modify: `.config/hypr/session.py`
- Modify: `tests/test_session.py`

**Interfaces:**
- Consumes: `WINDOW_EVENTS` (Task 5), `build_snapshot` (Task 4), `write_snapshot` (Task 1).
- Produces:
  - `watch(hypr, events, snapshot_fn, debounce: float = 1.0, clock=time.monotonic, stop=None) -> None`

`snapshot_fn` is a zero-argument callable that captures and writes the current state; Task 8
supplies the real one. `stop` is a zero-argument predicate returning `True` to end the loop, so
tests can terminate it. Production passes `stop=None`, meaning run forever.

Debouncing matters: dragging one window across workspaces emits a burst of `movewindow` events,
and without it each event would trigger a full `hyprctl` call and file write.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_session.py`:

```python
class ScriptedEvents:
    """Yields a scripted batch per call, then nothing."""

    def __init__(self, batches):
        self.batches = list(batches)

    def events(self, timeout=None):
        if self.batches:
            for item in self.batches.pop(0):
                yield item


class WatchTest(unittest.TestCase):
    def test_burst_of_events_produces_one_snapshot(self):
        writes = []
        events = ScriptedEvents([[
            ("movewindow", "0xa,2"),
            ("movewindow", "0xa,3"),
            ("movewindow", "0xa,4"),
        ]])
        ticks = iter([0.0, 0.0, 0.0, 0.0, 2.0, 2.0, 2.0])
        calls = {"n": 0}

        def stop():
            calls["n"] += 1
            return calls["n"] > 3

        session.watch(FakeHypr(), events, lambda: writes.append(1),
                      debounce=1.0, clock=lambda: next(ticks), stop=stop)
        self.assertEqual(len(writes), 1)

    def test_irrelevant_events_never_trigger_a_snapshot(self):
        writes = []
        events = ScriptedEvents([[("activewindow", "kitty,title")]])
        ticks = iter([0.0, 0.0, 5.0, 5.0, 5.0])
        calls = {"n": 0}

        def stop():
            calls["n"] += 1
            return calls["n"] > 2

        session.watch(FakeHypr(), events, lambda: writes.append(1),
                      debounce=1.0, clock=lambda: next(ticks), stop=stop)
        self.assertEqual(writes, [])

    def test_snapshot_failure_does_not_end_the_loop(self):
        def boom():
            raise RuntimeError("hyprctl exploded")

        events = ScriptedEvents([[("openwindow", "0xa,1,kitty,t")]])
        ticks = iter([0.0, 0.0, 2.0, 2.0, 2.0])
        calls = {"n": 0}

        def stop():
            calls["n"] += 1
            return calls["n"] > 2

        # Must return normally rather than propagate.
        session.watch(FakeHypr(), events, boom, debounce=1.0,
                      clock=lambda: next(ticks), stop=stop)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ~/coding/dotfiles && python -m unittest tests.test_session.WatchTest -v`

Expected: FAIL with `AttributeError: module 'session' has no attribute 'watch'`.

- [ ] **Step 3: Write the minimal implementation**

Append to `.config/hypr/session.py`:

```python
def watch(hypr, events, snapshot_fn, debounce: float = 1.0,
          clock=time.monotonic, stop=None) -> None:
    """Rewrite the snapshot whenever windows change, debounced.

    Dragging a window across workspaces emits a burst of events. Without
    debouncing, each one would trigger a full hyprctl call and file write.
    """
    pending_since = None

    while True:
        if stop is not None and stop():
            return
        try:
            for name, _payload in events.events(timeout=0.25):
                if name in WINDOW_EVENTS:
                    pending_since = clock()
        except ConnectionError:
            log("watch: event socket closed, exiting")
            return

        if pending_since is not None and clock() - pending_since >= debounce:
            pending_since = None
            try:
                snapshot_fn()
            except Exception as exc:
                # A transient hyprctl or disk failure must not kill the watcher.
                log(f"watch: snapshot failed: {exc}")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd ~/coding/dotfiles && python -m unittest tests.test_session -v`

Expected: PASS, 47 tests.

- [ ] **Step 5: Commit**

```bash
cd ~/coding/dotfiles
git add .config/hypr/session.py tests/test_session.py
git commit -m "hyprland: add debounced snapshot watch loop"
```

---

### Task 8: Logging and the command line interface

**Files:**
- Modify: `.config/hypr/session.py`
- Modify: `tests/test_session.py`

**Interfaces:**
- Consumes: everything from Tasks 1 through 7.
- Produces:
  - `log(message: str) -> None` — replaces the Task 4 stub
  - `capture(hypr, denylist, proc, path) -> dict`
  - `main(argv: list[str]) -> int`

Modes: `boot`, `watch`, `save`, `restore`.

`boot` runs restore to completion and then enters the watch loop, in one process. This is the
whole reason `boot` exists: two separate processes would race, because the watcher would observe
the windows restore is creating and overwrite the good snapshot with a half-restored one. A power
loss at that moment would lose the session permanently.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_session.py`:

```python
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
```

`import os` is already present at the top of the test file from Task 2's use of it; if it is not,
add it.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ~/coding/dotfiles && python -m unittest tests.test_session.CliTest -v`

Expected: FAIL with `AttributeError: module 'session' has no attribute 'main'`.

- [ ] **Step 3: Replace the log stub and add the CLI**

Delete the temporary `log` stub added in Task 4 and put this in its place, immediately after the
imports:

```python
def log(message: str) -> None:
    """Append one timestamped line to the log. Never raises.

    Logging failures must not take down the watcher or block login.
    """
    line = f"{datetime.datetime.now().isoformat(timespec='seconds')} {message}\n"
    try:
        directory = state_dir()
        directory.mkdir(parents=True, exist_ok=True)
        with open(directory / "session.log", "a") as handle:
            handle.write(line)
    except OSError:
        pass
```

Add `import datetime` and `import sys` to the imports. Then append to the end of the file:

```python
def snapshot_path() -> pathlib.Path:
    return state_dir() / "session.json"


def capture(hypr, denylist: list, proc: pathlib.Path,
            path: pathlib.Path) -> dict:
    """Read the live client list, build a snapshot, and write it."""
    now = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    snapshot = build_snapshot(hypr.clients(), denylist, proc, now)
    write_snapshot(snapshot, path)
    return snapshot


def main(argv: list) -> int:
    if not argv:
        print("usage: session.py {boot|watch|save|restore}", file=sys.stderr)
        return 2
    mode = argv[0]
    if mode not in {"boot", "watch", "save", "restore"}:
        print(f"unknown mode: {mode}", file=sys.stderr)
        return 2

    proc = pathlib.Path("/proc")
    path = snapshot_path()
    hypr = Hypr()
    denylist = load_denylist(denylist_path())

    if mode == "save":
        snapshot = capture(hypr, denylist, proc, path)
        log(f"save: wrote {len(snapshot['windows'])} window(s)")
        return 0

    events = EventSocket()

    if mode == "restore":
        snapshot = read_snapshot(path)
        if snapshot is None:
            log("restore: no usable snapshot, nothing to do")
            return 0
        restore(snapshot, hypr, events)
        return 0

    if mode == "boot":
        snapshot = read_snapshot(path)
        if snapshot is None:
            log("boot: no usable snapshot, going straight to watch")
        else:
            restore(snapshot, hypr, events)

    log("watch: started")
    watch(hypr, events, lambda: capture(hypr, denylist, proc, path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd ~/coding/dotfiles && python -m unittest tests.test_session -v`

Expected: PASS, 52 tests.

- [ ] **Step 5: Verify save against the live session**

```bash
cd ~/coding/dotfiles
python .config/hypr/session.py save
python -m json.tool ~/.local/state/hypr-session/session.json
hyprctl -j clients | python -c "import json,sys; print(len(json.load(sys.stdin)), 'live clients')"
```

Expected: the snapshot's window count is at most the live client count, differing only by windows
the log records as skipped. Confirm each entry has a non-empty `cmd`.

- [ ] **Step 6: Commit**

```bash
cd ~/coding/dotfiles
git add .config/hypr/session.py tests/test_session.py
git commit -m "hyprland: add session CLI with boot, watch, save and restore modes"
```

---

### Task 9: Wire into Hyprland and document

**Files:**
- Modify: `.config/hypr/hyprland.lua` — the `hl.on("hyprland.start")` block, currently lines 80-89
- Modify: `README.md`

**Interfaces:**
- Consumes: `main` from Task 8.
- Produces: nothing consumed by later tasks. This is the final task.

- [ ] **Step 1: Verify restore end-to-end before wiring it to login**

A broken restore wired into the start hook is awkward to recover from, so prove it works by hand
first.

```bash
cd ~/coding/dotfiles
# Snapshot the current session.
python .config/hypr/session.py save
cp ~/.local/state/hypr-session/session.json /tmp/session-backup.json
# Close a window by hand, then restore.
python .config/hypr/session.py restore
```

Expected: the closed window reopens on its original workspace. Check
`~/.local/state/hypr-session/session.log` for the `restore: placed N/M window(s)` line.

- [ ] **Step 2: Add the start-hook line**

Edit `.config/hypr/hyprland.lua`. Inside the existing `hl.on("hyprland.start", function()` block,
after the `hl.exec_cmd("waybar")` line, add:

```lua
    -- Restore the previous session's windows, then keep the snapshot current.
    -- One process, not two: a separate watcher would observe the windows that
    -- restore is creating and overwrite the good snapshot with a half-restored
    -- one. The two-second delay lets the monitor and workspace rules settle
    -- before any window is placed.
    hl.exec_cmd("sleep 2 && python ~/.config/hypr/session.py boot")
```

- [ ] **Step 3: Verify the config still parses**

Run: `hyprctl reload && hyprctl -j version | head -3`

Expected: no parse error. Note that `hyprctl reload` does not re-run the start hook, so this
checks syntax only; the hook itself is verified by the reboot in Step 6.

- [ ] **Step 4: Document it in the README**

Add this section to `README.md`:

```markdown
## Session persistence

`.config/hypr/session.py` saves which windows are open, and on which workspace and
monitor, then restores them at login. Hyprland's start hook runs it in `boot` mode.

- State: `~/.local/state/hypr-session/session.json`, log alongside it.
- Denylist: `.config/hypr/session-denylist.txt`, one regex per line, matched on window class.
- Manual use: `python ~/.config/hypr/session.py save|restore`.

Restored: which applications run, their workspace and monitor, floating versus tiled,
floating geometry, fullscreen state, and each terminal's working directory.

Not restored, and not restorable through Hyprland's IPC: tiling split ratios, browser tabs,
shell history, scrollback, and programs that were running inside a terminal. Split ratios
are absent from `hyprctl -j clients` and hy3 exposes no IPC command, so no tool can recover
them. For browser tabs, set Firefox's "Open previous windows and tabs" instead.

Single-instance applications need an entry in the `QUIRKS` table in `session.py`; `firefox`
is there already.

Tests: `python -m unittest tests.test_session -v`
```

- [ ] **Step 5: Commit**

```bash
cd ~/coding/dotfiles
git add .config/hypr/hyprland.lua README.md
git commit -m "hyprland: restore the previous session's windows at login"
```

- [ ] **Step 6: Verify by reboot**

Open several windows across at least two workspaces, including two kitty windows in different
directories and, if convenient, two Firefox windows. Then reboot.

Expected after login: every window returns on its original workspace and monitor; each kitty
window opens in its recorded directory; two Firefox windows appear rather than one. Split
positions inside a workspace will differ from before — that is the documented ceiling, not a bug.

Check `~/.local/state/hypr-session/session.log` for the `restore: placed N/M window(s)` line and
for any `skip` or `did not map` lines.

If restore misbehaves and login becomes unpleasant, comment out the `session.py boot` line in
`hyprland.lua` and reboot; nothing else in the config depends on it.
