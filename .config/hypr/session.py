#!/usr/bin/env python
"""Save and restore Hyprland window placement across reboots.

Design: docs/superpowers/specs/2026-09-10-hypr-session-persistence-design.md
Standard library only -- socat and jq are not installed on this machine.
"""

import json
import os
import pathlib
import re
import select
import shlex
import socket
import subprocess
import tempfile
import time

SCHEMA_VERSION = 1


def log(message: str) -> None:
    """Replaced by the file-backed logger in Task 8."""
    print(message)


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
    except (OSError, UnicodeDecodeError):
        return None
    if not text.strip():
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


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


def denylist_path() -> pathlib.Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return pathlib.Path(base) / "hypr" / "session-denylist.txt"


def load_denylist(path: pathlib.Path) -> list:
    """Compile one regex per non-comment line. Invalid patterns are dropped."""
    try:
        lines = path.read_text().splitlines()
    except (OSError, UnicodeDecodeError):
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


def _await_window(events, cls: str, deadline: float, clock, consumed: set | None = None) -> str | None:
    """Wait for an openwindow event whose class matches. Return its address.

    Skips events whose address is already in the consumed set to avoid matching
    stale events from previous timeouts to the current window launch.
    """
    if consumed is None:
        consumed = set()
    while clock() < deadline:
        for name, payload in events.events(timeout=0.25):
            if name != "openwindow":
                continue
            parts = payload.split(",", 3)
            if len(parts) >= 3 and parts[2] == cls:
                address = parts[0]
                if address not in consumed:
                    consumed.add(address)
                    return address
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
    windows = sorted(
        snapshot.get("windows", []),
        key=lambda w: w.get("order", float("inf"))
    )
    started = clock()
    seen: dict = {}
    placed = 0
    consumed: set = set()

    for window in windows:
        if clock() - started > total_timeout:
            log(f"restore: total timeout, abandoning {len(windows) - placed} window(s)")
            break

        try:
            # Drain any events already pending, unconditionally, right before
            # this window's launch. An event sitting in the buffer at this
            # instant cannot belong to a launch that has not happened yet --
            # it is stale by construction, even for the very first window,
            # since the real EventSocket connects before restore begins and
            # its kernel socket buffer may already hold events from other
            # autostart applications or a window the user opened manually.
            # Exhaustive, not single-shot: one events() call is one select +
            # one recv, so a backlog bigger than one chunk needs more than
            # one call to fully drain.
            while list(events.events(timeout=0)):
                pass

            argv = restore_command(window, seen)
            rules = f"workspace {window['workspace']} silent"
            if not hypr.exec_with_rules(rules, argv):
                log(f"restore: launch failed for {window['class']}")
                continue

            address = _await_window(
                events, window["class"], clock() + per_window_timeout, clock, consumed
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

        except (KeyError, TypeError, ValueError) as exc:
            log(f"restore: skipping malformed entry {window}: {exc}")
            continue

    log(f"restore: placed {placed}/{len(windows)} window(s)")
    return placed


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
