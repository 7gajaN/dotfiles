#!/usr/bin/env python
"""Save and restore Hyprland window placement across reboots.

Design: docs/superpowers/specs/2026-09-10-hypr-session-persistence-design.md
Standard library only -- socat and jq are not installed on this machine.
"""

import datetime
import json
import os
import pathlib
import re
import select
import shlex
import socket
import subprocess
import sys
import tempfile
import time

SCHEMA_VERSION = 1


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


def backup_path(path: pathlib.Path) -> pathlib.Path:
    """Return the rolling-backup path for a snapshot: session.json.1."""
    return path.with_name(path.name + ".1")


def rotate_snapshot(path: pathlib.Path) -> None:
    """Copy the current snapshot aside before it is overwritten. Never raises.

    A restore that places few or none of its windows leaves a session with
    fewer windows than the snapshot described, and the next capture writes
    that shrunken session over the good snapshot -- observed on this machine
    as "placed 0/8 window(s)" followed sixteen minutes later by "wrote 7
    window(s)". The empty-snapshot guard does not catch it, and no
    fraction-of-previous-count threshold can: it cannot tell a failed restore
    from the user closing eleven windows.

    So instead of refusing writes, keep the previous one. session.json.1
    always holds the snapshot that session.json had before the most recent
    write, and recovering is `cp session.json.1 session.json`.

    Failing to keep a backup must never prevent the write it precedes, so
    every failure here is logged and swallowed.
    """
    backup = backup_path(path)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        # Nothing to roll over yet: the first write of a fresh install.
        return
    except OSError as exc:
        log(f"rotate: could not read {path.name} for backup: {exc}")
        return
    try:
        # Same tmp-then-replace dance as write_snapshot, so a reader never
        # sees a truncated backup either.
        fd, tmp = tempfile.mkstemp(
            dir=str(path.parent), prefix=".session-bak-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, backup)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
    except OSError as exc:
        log(f"rotate: could not write {backup.name}: {exc}")


def read_snapshot(path: pathlib.Path) -> dict | None:
    """Return the snapshot, or None if it is missing, empty, or malformed.

    Never raises. Restore must degrade to a no-op rather than block login.

    "Malformed" includes valid JSON that is not an object: `123`, `[1,2]` and
    `true` all parse, and every caller then treats the result as a mapping.
    Returning them would push an AttributeError into restore() and into
    should_write_snapshot(), where it either kills boot before the watch loop
    starts or fails every capture for the rest of the session. A snapshot is
    an object or it is nothing.
    """
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError):
        return None
    if not text.strip():
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


# Window classes that are terminals, mapped to the flag that sets their
# starting directory. Extend this when adding another terminal emulator.
TERMINALS = {"kitty": "--directory"}

SHELLS = frozenset({"zsh", "bash", "fish", "sh", "dash"})

# The entire security boundary for restoring a program inside a terminal.
# Keys are the only comm names ever recognised by child_program(); values are
# the fixed argv template appended after `-e` at restore time. Recorded
# arguments from the live process are read only to decide which key matched
# -- they are never copied into the template. Add an entry here to allow a
# new program to be restored; removing one makes every snapshot that still
# names it fall back to the plain terminal command, silently.
ALLOWLIST = {"claude": ["claude", "--continue"]}

# Applications that manage their own windows: launching a second "instance"
# does not create a new window at all, it hands off to the process already
# running and exits (confirmed live -- five Firefox windows sharing one
# PID). There is no way to place window 2+ of one of these from outside it:
# a window created by an already-running process gets no exec rule, and
# `hyprctl keyword` is unavailable on this Lua-config Hyprland, so a
# dynamic title-matched window rule is not an option either. So restore()
# launches only the first recorded window of a class in this set, on that
# entry's recorded workspace, and skips every other recorded window of the
# class -- it comes back anyway, tabs and all, via the application's own
# session restore, just not necessarily onto its recorded workspace.
SINGLE_INSTANCE_CLASSES = frozenset({"firefox"})

# Restore-time arguments that an earlier revision of this tool used to add,
# from the second window of a class onward, to focus/open via the running
# instance (the behaviour SINGLE_INSTANCE_CLASSES replaces). Nothing adds
# these any more, but an on-disk snapshot written by that earlier code can
# still carry one baked into a saved "cmd" -- and it must not be replayed --
# so this table stays, for strip_quirk_arguments() alone.
LEGACY_RESTORE_ARGS = {"firefox": ["--new-window"]}


def strip_quirk_arguments(cls: str, argv: list) -> list:
    """Drop legacy restore-time arguments from a command line.

    Used at two points: capture time (via resolve_command), so that a
    Firefox window whose live /proc cmdline still carries --new-window
    (because it was, say, the very first window launched at login) does not
    put that flag in the snapshot; and restore time (via restore_command),
    so that an OLD on-disk snapshot -- written back when restore itself
    added --new-window -- has it stripped before the saved cmd is replayed,
    rather than launching a flag that has not meant anything since.
    """
    quirk_args = LEGACY_RESTORE_ARGS.get(cls)
    if not quirk_args or not argv:
        return argv
    return [argv[0]] + [arg for arg in argv[1:] if arg not in quirk_args]


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

    A terminal restored via `-e <program>` has no shell at all: the program
    is kitty's direct child. When no SHELLS child is found, fall back to a
    direct child whose comm is a key in ALLOWLIST and use its cwd instead --
    but only that; a non-allowlisted direct child is never used as a cwd
    source. The shell is always preferred when one exists, so a freshly
    opened terminal behaves exactly as it always has.
    """
    children = _children(pid, proc)
    for child in children:
        if _comm(child, proc) in SHELLS:
            try:
                return os.readlink(proc / str(child) / "cwd")
            except OSError:
                return None
    for child in children:
        if _comm(child, proc) in ALLOWLIST:
            try:
                return os.readlink(proc / str(child) / "cwd")
            except OSError:
                return None
    return None


def child_program(pid: int, proc: pathlib.Path) -> str | None:
    """Return the allowlisted program running in the terminal, or None.

    Normally one level deeper than child_shell_cwd: terminal -> child shell
    -> grandchildren. Only a grandchild whose comm is a key in ALLOWLIST is
    ever reported -- a name is read here only to check it against that
    table, and the rest of the grandchild's cmdline is never looked at, so
    nothing observed can travel any further than this one word.

    A terminal restored via `-e <program>` has no shell: the program is
    kitty's direct child instead of its grandchild. A direct child is
    therefore also checked against ALLOWLIST, on the same terms as a
    grandchild -- membership is still the only gate, so this widens where a
    match can be found, never what counts as one.

    Mirrors child_shell_cwd's two-pass shape: pass 1 walks shell children
    and their grandchildren; only if that finds nothing does pass 2 look
    for an allowlisted direct child. This makes the shell's precedence
    structural rather than dependent on /proc child ordering.
    """
    children = _children(pid, proc)
    for child in children:
        if _comm(child, proc) in SHELLS:
            for grandchild in _children(child, proc):
                name = _comm(grandchild, proc)
                if name in ALLOWLIST:
                    return name
    for child in children:
        comm = _comm(child, proc)
        if comm in ALLOWLIST:
            return comm
    return None


def resolve_program(client: dict, proc: pathlib.Path) -> str | None:
    """Return the allowlisted program running inside this window, or None.

    Only window classes in TERMINALS are ever inspected -- a program running
    inside, say, a browser is not something this tool can identify or trust.
    """
    if client.get("class") not in TERMINALS:
        return None
    return child_program(client["pid"], proc)


def resolve_command(client: dict, proc: pathlib.Path) -> list[str]:
    """Return the argv that recreates this window, or [] if it cannot be built."""
    argv = read_cmdline(client["pid"], proc)
    if not argv:
        return []
    cls = client["class"]
    flag = TERMINALS.get(cls)
    if flag:
        cwd = child_shell_cwd(client["pid"], proc)
        if cwd:
            return [argv[0], flag, cwd]
        return [argv[0]]
    return strip_quirk_arguments(cls, argv)


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
    workspace = client.get("workspace") or {}
    if workspace.get("id", 0) < 0:
        # Special workspaces (the scratchpad) carry a negative id, but
        # Hyprland addresses them by name -- `special:magic`, not `-98`. A
        # rule of `workspace -98 silent` is meaningless, so these windows
        # are not captured at all rather than restored to the wrong place.
        return "special-workspace"
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
        entry = {
            "order": order,
            "class": client["class"],
            "workspace": workspace,
            "monitor": client["monitor"],
            "floating": bool(client.get("floating")),
            "at": client.get("at", [0, 0]),
            "size": client.get("size", [0, 0]),
            "fullscreen": client.get("fullscreen", 0),
            "cmd": command,
        }
        program = resolve_program(client, proc)
        if program:
            entry["program"] = program
        windows.append(entry)

    return {"version": SCHEMA_VERSION, "saved_at": now, "windows": windows}


# Verified present in the Hyprland 0.56.2 binary.
WINDOW_EVENTS = frozenset({"openwindow", "closewindow", "movewindow", "movewindowv2"})

# How often watch() forces a capture even with no window event, on top of
# the event-driven trigger above. openwindow/closewindow/movewindow(v2) see
# only a window's existence and geometry -- none of them fire for state
# that changes *inside* a window: a terminal's shell cd-ing to a new
# directory, or an ALLOWLIST program (e.g. claude) starting up. Without
# this, a snapshot taken before either of those happens is what survives a
# reboot, even though the detection itself (child_shell_cwd, child_program)
# is correct -- the trigger to look again simply never fires.
REFRESH_INTERVAL = 30.0


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

    def _hyprctl(self, args: list) -> bool:
        try:
            subprocess.run(
                ["hyprctl", *args],
                capture_output=True, text=True, timeout=10, check=True,
            )
            return True
        except (subprocess.SubprocessError, OSError) as exc:
            log(f"hyprctl failed [{' '.join(args)}]: {exc}")
            return False

    @staticmethod
    def _lua_quote(text: str) -> str:
        """Escape text for embedding inside a Lua double-quoted string literal.

        Backslash first, or the escapes introduced afterwards get escaped
        again. Newlines and carriage returns matter too: a Lua short string
        literal cannot contain a raw one, so an argv that holds a newline --
        /proc/<pid>/cmdline can in principle hold anything -- would produce
        an unterminated-string error rather than a launch.
        """
        return (
            text.replace("\\", "\\\\")
                .replace('"', '\\"')
                .replace("\n", "\\n")
                .replace("\r", "\\r")
        )

    def exec_with_rules(self, rules: str, argv: list) -> bool:
        """Run 'hyprctl dispatch exec [rules] cmd' via the Lua dispatcher.

        On this machine Hyprland 0.56.2 runs a Lua config, and hyprctl parses
        its dispatch argument as Lua (`return hl.dispatch(<arg>)`), not as a
        classic hyprlang dispatcher string -- verified against the live
        compositor: `hyprctl dispatch exec true` raises a Lua syntax error.
        The working form, also verified live, is
        `hl.dsp.exec_cmd("[rules] command")` passed to `hyprctl dispatch` as
        a single argument. The rule-prefixed command therefore has to be
        embedded inside a Lua double-quoted string literal, hence the
        escaping below (at minimum backslashes and double quotes).

        The rule prefix is applied when the window maps, so the window never
        appears on the wrong workspace first.
        """
        command = " ".join(shlex.quote(part) for part in argv)
        payload = f"[{rules}] {command}"
        lua_expr = f'hl.dsp.exec_cmd("{self._lua_quote(payload)}")'
        return self._hyprctl(["dispatch", lua_expr])


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


def restore_command(window: dict, seen: dict) -> list:
    """Return the argv for this window, applying the program allowlist.

    `seen` is accepted for backward compatibility with every existing call
    site, but this function no longer reads or writes it: deciding whether
    to launch a window of a single-instance class at all is restore()'s job
    now (SINGLE_INSTANCE_CLASSES) -- restore_command() is only ever called
    for a window that has already been decided on, so its own behaviour is
    the same no matter how many times, or in what order, it is called.

    strip_quirk_arguments() removes any legacy restore-time argument (e.g.
    --new-window) that an OLD on-disk snapshot's saved "cmd" might still
    carry from before that add-at-restore behaviour was removed.

    A saved "program" is only ever used as a lookup key into ALLOWLIST --
    never as, or to build, anything that reaches argv directly. If the key
    is missing, or names something no longer in ALLOWLIST (the table shrank
    since this snapshot was written), nothing is appended and the window
    restores exactly as it would with no program at all. That silent
    fallback is deliberate: a stale program name must never run.
    """
    cls = window["class"]
    argv = strip_quirk_arguments(cls, list(window["cmd"]))
    template = ALLOWLIST.get(window.get("program"))
    if template:
        argv.append("-e")
        argv.extend(template)
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
    That contract covers a snapshot that is not shaped like a snapshot at all
    (not an object, no window list, a window list holding non-objects) as well
    as the event socket dying mid-restore.

    No reconciliation against live windows is performed: every entry is
    launched unconditionally, with one exception (below). That is correct on
    the boot path, where the compositor is empty, and it is the only path
    that matters. Run by hand against a populated session it duplicates
    windows, so it warns below.

    The exception: a class in SINGLE_INSTANCE_CLASSES (Firefox) hands off to
    its own already-running process instead of opening a new window, so only
    the first recorded window of such a class is launched here at all -- see
    the skip check at the top of the loop below. Every other recorded window
    of that class is skipped, not launched, and not counted as placed.
    """
    if not isinstance(snapshot, dict):
        log(f"restore: snapshot is not an object ({type(snapshot).__name__}), "
            "nothing to do")
        return 0
    entries = snapshot.get("windows")
    if not isinstance(entries, list):
        log(f"restore: snapshot has no usable window list "
            f"({type(entries).__name__}), nothing to do")
        return 0

    live = hypr.clients()
    if live:
        log(f"restore: WARNING {len(live)} window(s) are already open. restore "
            "does not reconcile against live windows -- every saved window is "
            "launched again, so these will be duplicated. restore is meant for "
            "the boot path, where the compositor is empty.")

    windows = sorted(
        entries,
        key=lambda w: w.get("order", float("inf")) if isinstance(w, dict)
        else float("inf")
    )
    started = clock()
    seen: dict = {}
    placed = 0
    # Belt and braces alongside the unconditional exhaustive drain below: the
    # drain is what actually keeps stale events out of a launch's way, and no
    # scenario is known in which this set is the only thing preventing a
    # misattribution. It is kept because this is the code path that cost three
    # rounds of fixes, and a second address-level guard costs nothing.
    consumed: set = set()

    for index, window in enumerate(windows):
        if clock() - started > total_timeout:
            log(f"restore: total timeout, abandoning {len(windows) - placed} window(s)")
            break

        try:
            cls = window["class"]

            # A single-instance class (Firefox: confirmed live, five windows
            # sharing one PID) hands off to its already-running process
            # instead of opening a new one, so only the first recorded
            # window of the class is launched here at all. Every later one
            # is skipped -- not attempted and not counted as placed -- and
            # logged with the reason, so the log explains why the restored
            # count is lower than the recorded one.
            #
            # The class is marked "seen" (below, near exec_with_rules) only
            # once a launch for it has actually been dispatched -- not
            # merely contemplated here, and not only once a window has been
            # confirmed mapped. Those are deliberately different thresholds:
            # a launch that never got dispatched (exec_with_rules returned
            # False) must not forfeit the whole class, so the next recorded
            # entry gets its own independent attempt, exactly as it would
            # for any other class. But a launch that WAS dispatched and
            # simply never mapped in time must still count as "seen" --
            # retrying it here risks relaunching Firefox a second time and
            # recreating the exact duplicate-blank-window bug this feature
            # exists to eliminate. Not retrying is the safer failure.
            if cls in SINGLE_INSTANCE_CLASSES and seen.get(cls, 0) > 0:
                log(f"restore: skipping {cls} window recorded on workspace "
                    f"{window.get('workspace')} -- {cls} is single-instance "
                    "and a launch for it was already dispatched earlier in "
                    "this restore; not launched again here regardless of "
                    "whether that earlier launch succeeded")
                continue

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
            rule_parts = [f"workspace {window['workspace']} silent"]
            if window.get("floating"):
                x, y = window["at"]
                w, h = window["size"]
                rule_parts.append("float")
                rule_parts.append(f"move {x} {y}")
                rule_parts.append(f"size {w} {h}")
            # hyprctl reports fullscreen as 0 (none), 1 (maximized) or 2
            # (true fullscreen), and only 2 gets the rule. Emitting
            # `fullscreen` for a maximized window makes it come back
            # genuinely fullscreen; the next capture then records 2, and the
            # window can never get back to maximized. The rule name that
            # would restore maximize could not be verified without
            # dispatching against the live session, so maximized state is
            # deliberately not restored at all -- not restoring it is
            # recoverable, silently converting it is not.
            if window.get("fullscreen") == 2:
                rule_parts.append("fullscreen")
            rules = "; ".join(rule_parts)
            if not hypr.exec_with_rules(rules, argv):
                log(f"restore: launch failed for {window['class']}")
                continue

            # The launch was actually dispatched (exec_with_rules succeeded),
            # so mark the class seen now -- whether or not the window goes on
            # to map is decided next, but that outcome must not change this:
            # see the long comment above the skip check for why both a
            # confirmed-mapped launch and a dispatched-but-never-mapped one
            # mark the class seen, while a launch that never dispatched at
            # all does not.
            if cls in SINGLE_INSTANCE_CLASSES:
                seen[cls] = seen.get(cls, 0) + 1

            address = _await_window(
                events, window["class"], clock() + per_window_timeout, clock, consumed
            )
            if not address:
                log(f"restore: {window['class']} did not map in {per_window_timeout}s")
                continue

            placed += 1

        except ConnectionError as exc:
            # The event socket died under us. Launching the rest blind would
            # place windows nobody can confirm, so stop -- but stop by
            # returning, never by raising: restore must not block login.
            log(f"restore: event socket lost, abandoning "
                f"{len(windows) - index} window(s): {exc}")
            break

        except (AttributeError, KeyError, OSError, TypeError, ValueError) as exc:
            # AttributeError: an entry that is not a dict (a bare null in the
            # window list) reaches .get()/[] here. OSError: select/recv on a
            # dying socket raises plain OSError (EBADF), not just
            # ConnectionError.
            log(f"restore: skipping malformed entry {window}: {exc}")
            continue

    log(f"restore: placed {placed}/{len(windows)} window(s)")
    return placed


def should_write_snapshot(new_snapshot: dict, existing_path: pathlib.Path) -> bool:
    """Refuse to overwrite a non-empty snapshot with an empty one.

    At logout, Hyprland closes every window, emitting closewindow events
    BEFORE the event socket dies. With a 1s debounce, watch may observe the
    teardown and write a snapshot of vanishing windows, destroying the good
    snapshot at exactly the moment it is needed for the next login.

    This guard protects the saved session: if the newly built snapshot has
    zero windows and an existing snapshot on disk has windows, skip the write.
    A missing or unreadable existing snapshot never blocks the write.
    """
    new_window_count = len(new_snapshot.get("windows", []))
    if new_window_count > 0:
        return True
    # New snapshot is empty. Check what's on disk.
    existing = read_snapshot(existing_path)
    if existing is None:
        return True
    existing_window_count = len(existing.get("windows", []))
    if existing_window_count > 0:
        # Existing snapshot has windows; refuse to overwrite with empty.
        return False
    return True


def compositor_is_gone(events) -> bool:
    """True when the event socket's path no longer exists.

    Hyprland removes its runtime socket when it exits. If the path is gone
    there is nothing to reconnect to and the process should end, which is
    the "Hyprland exiting ends the process" half of the spec's rule. Fakes
    without a path are never treated as gone.
    """
    path = getattr(events, "path", None)
    if not path:
        return False
    return not os.path.exists(path)


def watch(hypr, events, snapshot_fn, debounce: float = 1.0,
          interval: float = REFRESH_INTERVAL, clock=time.monotonic,
          stop=None, connect=None, sleep=time.sleep,
          backoff_start: float = 1.0, backoff_max: float = 30.0,
          max_attempts: int = 8) -> None:
    """Rewrite the snapshot whenever windows change, debounced -- and also
    every `interval` seconds regardless of events (see REFRESH_INTERVAL).

    Dragging a window across workspaces emits a burst of events. Without
    debouncing, each one would trigger a full hyprctl call and file write.

    The event trigger and the periodic timer share one clock: a capture
    just taken by either path resets it, so the other has no reason to fire
    again right behind it. An event arriving mid-interval still debounces
    normally. Nothing here decides whether the snapshot file actually gets
    rewritten -- that guard (skip the write when the windows list did not
    change) lives in capture(), which both triggers call through, so a
    refresh every `interval` seconds does not spam the log or the disk when
    nothing changed.

    Losing the socket is not the end of the loop. Per the spec's error
    handling: reconnect with exponential backoff; Hyprland exiting ends the
    process. `connect` is the callable that produces a fresh event source
    (EventSocket on the real path); without one there is nothing to
    reconnect to and the loop ends as it used to.

    Both ConnectionError and plain OSError are treated as socket loss:
    select() and recv() on a closed descriptor raise OSError (EBADF), which
    previously escaped watch(), escaped main(), and killed the process with
    no log line at all.

    The retry budget is bounded (max_attempts consecutive failures, delays
    doubling from backoff_start up to backoff_max) so a socket that refuses
    every connection cannot spin forever. The budget resets after any read
    that succeeds, so a session that loses the socket twice, hours apart,
    gets a full budget each time.
    """
    pending_since = None
    last_capture = None
    attempts = 0

    while True:
        if stop is not None and stop():
            return
        try:
            for name, _payload in events.events(timeout=0.25):
                if name in WINDOW_EVENTS:
                    pending_since = clock()
            attempts = 0
        except (ConnectionError, OSError) as exc:
            try:
                events.close()
            except Exception:
                pass
            if compositor_is_gone(events):
                log(f"watch: event socket is gone, Hyprland has exited: {exc}")
                return
            if connect is None:
                log(f"watch: event socket closed and no reconnector, exiting: {exc}")
                return
            attempts += 1
            if attempts > max_attempts:
                log(f"watch: giving up after {max_attempts} reconnect attempts: {exc}")
                return
            delay = min(backoff_start * (2 ** (attempts - 1)), backoff_max)
            log(f"watch: event socket lost ({exc}), reconnecting in {delay:g}s "
                f"(attempt {attempts}/{max_attempts})")
            sleep(delay)
            try:
                events = connect()
            except (KeyError, OSError) as reconnect_exc:
                log(f"watch: reconnect failed: {reconnect_exc}")
                continue
            log("watch: event socket reconnected")
            continue

        do_capture = False
        if pending_since is not None:
            now = clock()
            if last_capture is None:
                last_capture = now
            if now - pending_since >= debounce:
                pending_since = None
                last_capture = now
                do_capture = True
            elif now - last_capture >= interval:
                # The debounce has not elapsed, but the periodic refresh
                # has: capture now instead of making the periodic trigger
                # wait for this event to finish debouncing. Either way the
                # state just observed is fresh, so the pending event is
                # resolved by this capture too.
                pending_since = None
                last_capture = now
                do_capture = True
        else:
            now = clock()
            if last_capture is None:
                # First time through with nothing pending: start the
                # periodic clock from here rather than firing immediately.
                last_capture = now
            elif now - last_capture >= interval:
                last_capture = now
                do_capture = True

        if do_capture:
            try:
                snapshot_fn()
            except Exception as exc:
                # A transient hyprctl or disk failure must not kill the watcher.
                log(f"watch: snapshot failed: {exc}")


def snapshot_path() -> pathlib.Path:
    return state_dir() / "session.json"


def capture(hypr, denylist: list, proc: pathlib.Path,
            path: pathlib.Path) -> dict:
    """Read the live client list, build a snapshot, and write it.

    The write is guarded by should_write_snapshot(): at logout, Hyprland
    closes every window before the event socket dies, so watch() can observe
    a session in the act of disappearing. Writing that empty snapshot over a
    good one would destroy it right when it is needed for the next login, so
    a refused write is logged and skipped -- the snapshot actually observed
    is still returned so callers can see what happened.

    Every write that does happen is preceded by rotate_snapshot(), so the
    previous snapshot survives at session.json.1. That is the protection
    against a partial restore quietly writing a shrunken session over a good
    one, which the empty-snapshot guard above cannot catch.

    watch() now captures periodically (REFRESH_INTERVAL), not just on window
    events, to catch state that changes inside a window with no event of its
    own. A refresh that finds nothing different must not rewrite the file or
    spam the log every 30 seconds, so if the newly built snapshot's windows
    match what is already on disk -- saved_at excluded, since that differs on
    every build -- the write (and the rotation that precedes it) is skipped
    entirely. Skipping the rotation matters as much as skipping the write:
    rotating on a no-op would overwrite session.json.1 with an identical copy
    of session.json, destroying the one recoverable previous snapshot for no
    reason. This protects both triggers that feed capture(), event-driven and
    periodic alike, since both call through here.

    The write itself is never allowed to propagate: write_snapshot() can
    raise OSError (an unwritable or unreachable state directory, for
    instance) and this must not take down a caller -- main()'s save mode
    calls capture() directly, with no surrounding try/except of its own.
    """
    now = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    snapshot = build_snapshot(hypr.clients(), denylist, proc, now)
    if should_write_snapshot(snapshot, path):
        previous = read_snapshot(path)
        previous_count = None if previous is None else len(
            previous.get("windows", []) if isinstance(previous.get("windows"), list)
            else []
        )
        if previous is not None and previous.get("windows") == snapshot["windows"]:
            log("capture: skipped write, snapshot is unchanged")
            return snapshot
        rotate_snapshot(path)
        try:
            write_snapshot(snapshot, path)
        except OSError as exc:
            log(f"capture: failed to write snapshot: {exc}")
        else:
            # watch() drives every automatic write through this function, so
            # without this line the whole snapshotting path is silent and a
            # snapshot that shrinks from twelve windows to two leaves no
            # trace of whether the user closed ten or a restore failed.
            count = len(snapshot["windows"])
            if previous_count is None:
                log(f"capture: wrote {count} window(s)")
            else:
                log(f"capture: wrote {count} window(s) "
                    f"(previously {previous_count})")
    else:
        log("capture: skipped write, new snapshot is empty but existing "
            "snapshot is not (likely a logout in progress)")
    return snapshot


def _restore_safely(mode: str, snapshot: dict, hypr, events) -> None:
    """Call restore() so that nothing it raises can escape.

    restore() promises never to raise, and is written to keep that promise.
    This is the belt for that braces: on the boot path an escaped exception
    would skip the watch loop entirely, so no snapshot would be written for
    the rest of the session and the next reboot would lose the real session
    too. One bad snapshot file must never cost more than one restore.
    """
    try:
        restore(snapshot, hypr, events)
    except Exception as exc:
        log(f"{mode}: restore failed, continuing: {exc!r}")


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
        # "observed", not "wrote": capture() may legitimately refuse the
        # write (an empty snapshot over a good one), and this line used to
        # claim a write had happened either way. capture() itself logs the
        # outcome of the write.
        snapshot = capture(hypr, denylist, proc, path)
        log(f"save: observed {len(snapshot['windows'])} window(s)")
        return 0

    try:
        events = EventSocket()
    except (KeyError, OSError) as exc:
        # Missing HYPRLAND_INSTANCE_SIGNATURE/XDG_RUNTIME_DIR, or the socket
        # itself refusing a connection, means there is no live compositor to
        # talk to. main() must never raise -- not even here -- so this
        # degrades to a logged no-op rather than blocking login.
        log(f"{mode}: cannot connect to the event socket: {exc}")
        return 0

    if mode == "restore":
        snapshot = read_snapshot(path)
        if snapshot is None:
            log("restore: no usable snapshot, nothing to do")
            return 0
        _restore_safely(mode, snapshot, hypr, events)
        return 0

    if mode == "boot":
        snapshot = read_snapshot(path)
        if snapshot is None:
            log("boot: no usable snapshot, going straight to watch")
        else:
            _restore_safely(mode, snapshot, hypr, events)

    log("watch: started")
    watch(hypr, events, lambda: capture(hypr, denylist, proc, path),
          connect=EventSocket)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
