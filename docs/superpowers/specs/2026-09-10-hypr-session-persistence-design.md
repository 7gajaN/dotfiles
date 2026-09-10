# Hyprland session persistence — design

Date: 2026-09-10
Status: implemented. Sections marked *(as built)* were revised after implementation to match the code, which is the authority the code is checked against.

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

*(as built)* The rule prefix carries **all** placement, geometry included —
see "Restore" below for why nothing is dispatched after the window maps.

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

State lives in `~/.local/state/hypr-session/`, holding `session.json`, its rolling backup
`session.json.1`, and `session.log`.
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
3. The window is floating and shares a PID with another *non-floating* window of the same
   class. This approximates a dialog belonging to an already-captured main window. It is a
   heuristic, not a guarantee. Two floating windows sharing a PID are both kept: with no main
   window between them, neither is a dialog.
4. *(as built)* The window's workspace id is negative, i.e. it is on a special workspace
   (the scratchpad). Hyprland addresses those by name — `special:magic`, not `-98` — so a
   `workspace -98 silent` rule would mean nothing, and the window is not captured at all.

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

*(as built)* `fullscreen` is stored as the integer `hyprctl` reports: 0 none, 1 maximized,
2 fullscreen. Only 2 is replayed — see "Restore".

`cmd` never contains quirk-table arguments. Those are a restore-time affordance, and a window
this tool restored carries them in its own `/proc/<pid>/cmdline`; capturing them verbatim
would make them accumulate on every save/restore cycle, so they are stripped when the command
is resolved.

`monitor` is the integer index reported by `hyprctl`, not a name. It is informational only:
placement is driven by the workspace rule, and `hyprland.lua` already pins workspaces 1-10 to
DP-2 and workspace 11 to DP-3.

`order` is assigned by sorting on workspace id, then on the window's current position within
that workspace. Restoring in this order makes hy3 arrival order on workspace 11 repeatable
between boots, which is the most that can be done given that ratios are unreadable.

Writes are atomic: the snapshot is written to a temporary file in the same directory and then
moved into place with `os.replace`. A snapshot is therefore never observed truncated, even if
`watch` is killed mid-write.

## Restore *(as built)*

Windows are restored one at a time, in `order`:

1. Drain any events already pending. An event sitting in the buffer before a launch happens
   cannot belong to that launch, so it is stale by construction and must not be matched to it.
2. Build the command. If this class has already been restored `n` times in this run, apply the
   quirk-table arguments for `n >= 1`.
3. Build the rule prefix: `workspace <N> silent`, plus `float; move <x> <y>; size <w> <h>` for
   a floating window, plus `fullscreen` when `fullscreen == 2`. Launch it as one call.
4. Wait for an `openwindow` event on the socket whose class matches. Time out after 10 s.
5. Continue to the next window.

Tiled windows receive no geometry replay; the layout engine owns their geometry. Maximized
windows (`fullscreen == 1`) get no rule either: the `fullscreen` rule would bring the window
back genuinely fullscreen, the next capture would record `2`, and the state could never
return. The rule that restores maximize could not be verified without dispatching against the
live session, and not restoring a state is recoverable where silently converting it is not.

### Why this differs from the original plan

The original design said `hyprctl dispatch exec [rules] cmd` for step 3 and post-hoc
`setfloating` / `movewindowpixel` / `resizewindowpixel` dispatches for the geometry. Neither
works on this machine. Hyprland 0.56.2 here runs a Lua config, and `hyprctl` parses its
dispatch argument as Lua (`return hl.dispatch(<arg>)`): `hyprctl dispatch exec true` raises a
Lua syntax error. Verified live, the working form is a single argument
`hl.dsp.exec_cmd("[rules] command")`, with the rule-prefixed command embedded in a Lua
double-quoted string literal (hence the escaping in `Hypr._lua_quote`). Since that is one
call that must carry everything, all geometry moved into the rule prefix, where it also
applies at map time rather than after the window is already visible.

Restore does not reconcile against live windows: every entry is launched unconditionally.
That is correct on the boot path, where the compositor is empty, and it is the only path that
matters. Run by hand against a populated session it duplicates windows, and logs a warning
saying so.

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
- `watch` loses the socket: reconnect with exponential backoff — delays doubling from 1 s to
  a 30 s cap, bounded at 8 consecutive failures so a socket that refuses every connection
  cannot spin, and the budget resets after any read that succeeds. A socket path that no
  longer exists means Hyprland has exited, and the process ends.
- *(as built)* A snapshot file that is unreadable, unparseable, or valid JSON that is not an
  object is treated as absent. Nothing restore raises can prevent the watch loop starting:
  losing one restore is a bad login, but losing the watch loop loses the next session too.
- *(as built)* Before every snapshot write, the current `session.json` is rotated to
  `session.json.1`. The empty-snapshot guard cannot catch a restore that placed one window of
  twelve, and no fraction-of-previous-count threshold could either — it cannot tell that from
  the user closing eleven windows — so the previous snapshot is kept instead of the write
  being refused.

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
