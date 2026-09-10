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
