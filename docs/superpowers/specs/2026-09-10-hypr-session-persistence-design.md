# Hyprland session persistence — design

Date: 2026-09-10
Status: approved, not yet implemented

## Purpose

Restore the set of open windows, and their workspace and monitor placement, across a
reboot. Windows are opened ad hoc; no static app-to-workspace map is maintained by hand.

## Scope

In scope:

- Which applications were running.
- The workspace and monitor each window was on.
- Floating versus tiled state, and the geometry of floating windows.
- Fullscreen state.
- The working directory of terminal windows.

Out of scope, and not achievable by any available means (see "Investigated ceiling"):

- Tiling split ratios, both dwindle and hy3.
- Application-internal state: browser tabs, editor buffers, shell history, scrollback,
  and programs that were running inside a terminal.

## Investigated ceiling

The complete field set returned by `hyprctl -j clients` on Hyprland 0.56.2 is:

```
acceptsInput address allowedOverFullscreen at class contentType floating
focusHistoryID fullscreen fullscreenClient fullscreenHandler grouped hidden
initialClass initialTitle mapped monitor pid pinFullscreened pinned size
stableId swallowing tags title workspace xdgDescription xdgTag xwayland
```

Two consequences drive this design.

First, there is no `splitratio` field and no layout-tree field. The hy3 plugin registers
with Hyprland but exposes no `hyprctl` command; `hyprctl hy3layout` returns
`unknown request`. Tiling arrangement therefore cannot be read through the public IPC by
any tool. Every published session manager for Hyprland is subject to the same limit. The
`at` and `size` fields exist, but for a tiled window they are outputs of the layout engine
rather than inputs, so replaying them has no effect.

Second, there is no `transient` or `modal` field. Dialogs and file pickers cannot be
identified directly. The heuristic below approximates the distinction, and the denylist is
the reliable control.

## Design decisions

| Decision | Choice | Rationale |
| --- | --- | --- |
| Snapshot trigger | Hyprland event socket | Snapshot always current. Survives crash and power loss, unlike save-on-exit. |
| Capture scope | All toplevel windows, minus transient heuristic and denylist | Matches ad-hoc usage, with an escape hatch for apps that keep returning unwanted. |
| Terminal state | Working directory only | Useful without re-executing recorded command lines unattended at every login. |
| Restore strategy | Serial: place, launch, wait, next | Deterministic for duplicate window classes. Login cost is paid once. |
| Placement mechanism | `exec` rule prefix | Places the window at map time. No window flash, and no persistent window rule to clean up. |

Rejected alternatives:

- **Static window rules** pinning classes to workspaces. Rejected: requires a hand-maintained
  map and cannot capture ad-hoc windows.
- **Existing AUR session managers** (`hyprflow`, `hyprresume`, `hypr-persist`, `hyprsession`,
  `hyprvault`, `hyprdrover`, `hyprsessionmanager-git`, `hyprsession-sh`). Rejected: all carry
  zero or one AUR vote and near-zero popularity, one is orphaned and one has no upstream URL.
  A session daemon re-executes stored command lines at every login, which is the highest-trust
  slot on the machine. None of them can exceed the ceiling documented above, so adopting one
  buys no capability in exchange for that trust.
- **Launch all windows in parallel, then move them by address.** Rejected: with several windows
  of the same class, arrival order is not guaranteed, so windows can land on the wrong
  workspace. Windows also visibly jump after appearing.

## Architecture

One file, `~/.config/hypr/session.py`, deployed through the existing symlink from
`~/coding/dotfiles/.config/hypr/`. Python standard library only — `socat` and `jq` are not
installed on this machine and are not required.

Four modes:

- `boot` — runs `restore` to completion, then enters the `watch` loop. This is the only mode
  invoked at login.
- `watch` — long-running. Reads the event socket and writes snapshots.
- `save` — one-shot snapshot. Used for testing.
- `restore` — replays a snapshot. Used for testing.

`boot` exists to remove a race. If `restore` and `watch` ran as two processes, `watch` would
observe the windows that `restore` is creating and write a snapshot describing a
half-restored session, destroying the good snapshot. A power loss at that moment would lose
the session permanently. Running both phases in one process, strictly ordered, makes the
race impossible without a lock file.

State lives in `~/.local/state/hypr-session/`, holding `session.json` and `session.log`.
State is machine-local and changes constantly, so it is deliberately not tracked in git.

## Capture

The event socket is `$XDG_RUNTIME_DIR/hypr/$HYPRLAND_INSTANCE_SIGNATURE/.socket2.sock`,
read directly with `socket.AF_UNIX`. Verified reachable from the Python standard library.

Snapshot-triggering events: `openwindow`, `closewindow`, `movewindow`, `movewindowv2`.
Writes are debounced by 1000 ms so that a drag across workspaces produces one write rather
than a storm.

For each window, the launch command is resolved from `/proc/<pid>/cmdline`. For terminals
the useful working directory belongs to the child shell rather than the terminal process, so
the resolver walks `/proc/<pid>/task/*/children` to the shell and reads its `cwd`, emitting
`kitty --directory <cwd>`. Helper children such as kitty's `kitten` processes are ignored.

Windows are skipped when any of the following holds, evaluated in order:

1. The class matches a regex in `~/.config/hypr/session-denylist.txt`, one pattern per line.
2. `/proc/<pid>/cmdline` is unreadable or empty, so the window could not be relaunched.
3. The window is floating and shares a PID with another window of the same class. This
   approximates a dialog belonging to an already-captured main window. It is a heuristic,
   not a guarantee.

### Snapshot format

```json
{
  "version": 1,
  "saved_at": "2026-09-10T10:40:00+03:00",
  "windows": [
    {
      "order": 0,
      "class": "kitty",
      "workspace": 1,
      "monitor": 0,
      "floating": false,
      "at": [7, 41],
      "size": [1906, 1032],
      "fullscreen": 0,
      "cmd": ["kitty", "--directory", "/home/gion/coding"]
    }
  ]
}
```

`monitor` is the integer index reported by `hyprctl`, not a name. It is informational only:
placement is driven by the workspace rule, and `hyprland.lua` already pins workspaces 1-10 to
DP-2 and workspace 11 to DP-3.

`order` is assigned by sorting on workspace id, then on the window's current position within
that workspace. Restoring in this order makes hy3 arrival order on workspace 11 repeatable
between boots, which is the most that can be done given that ratios are unreadable.

Writes are atomic: the snapshot is written to a temporary file in the same directory and then
moved into place with `os.replace`. A snapshot is therefore never observed truncated, even if
`watch` is killed mid-write.

## Restore

Windows are restored one at a time, in `order`:

1. Build the command. If this class has already been restored `n` times in this run, apply the
   quirk-table arguments for `n >= 1`.
2. Run `hyprctl dispatch exec [workspace <N> silent] <cmd>`. The rule prefix places the window
   when it maps, so it never appears on the wrong workspace first.
3. Wait for an `openwindow` event on the socket whose class matches. Time out after 10 s.
4. If the window was floating, dispatch `setfloating`, then `movewindowpixel` and
   `resizewindowpixel` to the saved `at` and `size`.
5. Continue to the next window.

Tiled windows receive no geometry replay; the layout engine owns their geometry.

### Quirk table

A dictionary in `session.py` mapping window class to extra arguments needed for the second and
subsequent windows of that class. Some applications are single-instance: a second `firefox`
invocation focuses the existing window instead of opening a new one. The table starts with:

```python
QUIRKS = {"firefox": ["--new-window"]}
```

This table is expected to grow as single-instance applications are encountered. That is ongoing
manual maintenance and is accepted.

## Wiring

One line is added to the existing `hl.on("hyprland.start")` block in `hyprland.lua`:

```lua
hl.exec_cmd("sleep 2 && python ~/.config/hypr/session.py boot")
```

The two-second delay lets monitor and workspace rules settle before any window is placed.
Once the restore phase finishes, the same process begins watching, so the first snapshot
written after login already describes the fully restored session.

## Error handling

Restore must never block or delay login. Every failure degrades to a logged line and continues.

- Missing or corrupt snapshot: log and exit 0.
- A launch fails: log the class and command, continue with the next window.
- A window does not map within 10 s: log and continue.
- The restore phase exceeds 120 s in total: abandon the remaining windows, log, and enter the
  watch loop anyway. A hung restore must never prevent snapshots from resuming.
- `watch` loses the socket: reconnect with exponential backoff. Hyprland exiting ends the process.

All modes log to `~/.local/state/hypr-session/session.log`.

## Testing

- Run `save`, then diff the resulting JSON against live `hyprctl -j clients`.
- Run `restore` against a scratch snapshot containing two kitty windows on different
  workspaces, to verify duplicate-class ordering.
- Restore two Firefox windows, to verify the quirk table produces two windows rather than one.
- Add a denylist entry and verify the matching window is skipped.
- Kill `watch` during a write and verify the snapshot on disk is still valid JSON.
- Run `boot` against a snapshot whose first entry is an unlaunchable command, and confirm the
  process still reaches the watch loop and writes a correct snapshot afterwards.
- Full verification: reboot.

## Known risks

- The transient heuristic will misclassify some windows in both directions. The denylist is the
  correction mechanism.
- The quirk table is discovered empirically, one application at a time.
- hy3 is version-locked to Hyprland 0.56.2 and fails silently on a version mismatch. If hy3 is
  absent, workspace 11 falls back to dwindle and restored windows there will be arranged
  differently. This is a pre-existing property of the configuration, not introduced here.
