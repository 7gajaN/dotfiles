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
