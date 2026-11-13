# Dotfiles for Arch Linux Environment

My personal configuration files for an Arch Linux + Hyprland (Wayland) setup.

## Layout

Two kinds of config live here, installed two different ways.

| Path | Installs to | How |
|---|---|---|
| `.config/*` | `~/.config/` | symlinked |
| `.bashrc`, `.zshrc` | `~/` | symlinked |
| `system/*` | `/etc`, `/usr/share` | copied by a script (root-owned) |

Everything under `.config/` is symlinked into `~/.config`, so edits in either
place are the same file. `system/` holds root-owned system files that cannot be
symlinked out of a user's home — see [`system/sddm/README.md`](system/sddm/README.md).

## Apps and Tools Configured

### Window Management & Desktop
- **Hyprland**: Wayland compositor and tiling WM. Configured in Lua
  (`.config/hypr/hyprland.lua`), not the usual `hyprland.conf`.
- **hy3**: Hyprland plugin providing i3-style tiling. Loaded at startup and
  version-locked to Hyprland, so both must be upgraded together.
- **Waybar**: status bar (`.config/waybar/`).
- **awww**: Wayland wallpaper daemon. Set per-output at startup — without `-o`
  the last call wins on every monitor.
- **grim** + **slurp**: screenshots, bound to `Print`.

### Terminal & Shell
- **Kitty**: GPU-based terminal emulator.
- **Ranger**: Vim-like terminal file manager.
- **Zsh**: primary shell (see `.zshrc`).
- **Bash**: fallback shell (see `.bashrc`).

### Text Editing
- **Neovim**: configured for coding and writing; plugins via lazy.nvim.

### Application Launching & Menu
- **Rofi**: window switcher, run dialog, and application launcher.

### System Info & Appearance
- **Fastfetch**: system information tool, run on shell startup.

### Login Screen
- **SDDM**: minimal black greeter, main monitor only. Root-owned, so it lives
  under `system/sddm/` and is installed by script rather than symlinked:

  ```sh
  sudo sh system/sddm/install.sh
  ```

  See [`system/sddm/README.md`](system/sddm/README.md) — it documents the Qt5
  greeter trap that makes a broken theme fail silently.

### Installed, but no config in this repo
- **mako**: Wayland notification daemon, running on defaults.
- **OpenRGB**: RGB lighting control for hardware.
- **PulseAudio/Pavucontrol**: sound system and volume control.
- **GTK 3.0**: theme and appearance settings.

## Session Persistence

`.config/hypr/session.py` saves and restores window placement across reboots.
Hyprland's `hyprland.start` hook runs it once in `boot` mode, which restores
the previous session and then keeps watching for changes — one process, not
two, so a separate watcher can never observe restore's own windows arriving
and overwrite the snapshot it just wrote.

State lives at `~/.local/state/hypr-session/session.json`, with a log
(`session.log`) alongside it recording every save and restore. Windows whose
class matches a pattern in `.config/hypr/session-denylist.txt` (one regex per
line, matched against the window class) are never saved.

Run it by hand with:

```sh
python ~/.config/hypr/session.py save     # snapshot the current windows
python ~/.config/hypr/session.py restore  # relaunch the last snapshot
```

`restore` is meant for the boot path, where the compositor is still empty.
It launches every saved window unconditionally and does **not** reconcile
against what is already open, so running it against a populated session
duplicates every window — and a second later the watcher persists the
duplicated set. It logs a warning when it starts with windows already open.

**Restored:** which applications are running, their workspace and monitor,
floating versus tiled, a floating window's position and size, true
fullscreen state, and — for terminals — the working directory of the shell
running inside them, plus (see below) one allowlisted program if it was
running in that shell.

**Not restored, and not restorable through Hyprland's IPC:** tiling split
ratios (absent from `hyprctl -j clients`, and hy3 exposes no IPC command to
read them — no tool can recover them), browser tabs, shell history, and
scrollback. For browser tabs, turn on Firefox's own "Open previous windows
and tabs" setting instead.

### Restoring a program inside a terminal

Whatever else happened to be running inside a terminal is not restored — with
one narrow exception. `session.py` looks one level past the terminal's child
shell (or, failing that, at the terminal's direct children — see below): if
a process's name there is a key in the `ALLOWLIST` table, that name is saved
as the window's `program` field, e.g.:

```json
{"class": "kitty", "cmd": ["kitty", "--directory", "/home/gion/coding/MS"], "program": "claude"}
```

At restore time, if `program` is present and still a key in `ALLOWLIST`, its
fixed template is appended after `-e`:

```
kitty --directory /home/gion/coding/MS -e claude --continue
```

This is deliberately not "replay the recorded command line": the arguments a
program was actually invoked with are read only to identify which program is
running, then discarded. `ALLOWLIST` holds fixed templates, never recorded
arguments — that table is the entire security boundary, since it is what
runs unattended at every login.

Detection understands two tree shapes so this survives repeated reboots, not
just one: a freshly opened terminal has an intermediate shell (terminal ->
shell -> program), while a terminal that was itself restored via `-e
<program>` has the program as the terminal's direct child instead (`-e`
means there is no shell in between). `child_program`/`child_shell_cwd` check
for the shell shape first and only fall back to a direct child when no shell
exists, so a window keeps carrying both `--directory` and `program` on every
capture after every restore — it does not degrade to a bare `["kitty"]`
after the first round trip. The fallback is still gated on `ALLOWLIST`
membership alone: a direct child that is not an allowlisted program is never
used as a program or a cwd source.

To allow another program, add it to the table:

```python
ALLOWLIST = {"claude": ["claude", "--continue"]}
```

An entry removed from `ALLOWLIST` makes every snapshot that still names it
fall back silently to the plain terminal command — a stale `program` value
can never launch anything.

Two trade-offs are accepted as part of this design:

- **Only one program per terminal, chosen from a fixed list.** Anything not
  in `ALLOWLIST` is invisible to restore. When the terminal has a child
  shell, only an allowlisted match among that shell's own children is used;
  when it does not (as after a `-e <program>` restore, where the program is
  the terminal's direct child instead), an allowlisted direct child is used
  instead. The shell is preferred whenever one is present.
- **`-e` means quitting the program closes the window.** The window does not
  drop back to a shell on exit, unlike a terminal opened normally.

### Reattaching a tmux session

A terminal attached to a named tmux session is restored by reattaching that
exact session, taking precedence over the program allowlist above — a
terminal running tmux is not "running claude" as far as `session.py` is
concerned, even if claude happens to be running inside that session.
Whatever tmux-resurrect/tmux-continuum restore *inside* the session (the
text that was on screen, the pane layout) is their job once the tmux server
is running again; `session.py` only gets that server started and the
terminal window pointed at the right session, since nothing else does.

Detection uses `tmux list-clients -F '#{client_pid} #{session_name}'`, the
one authoritative source for which session a client process is attached to
— never the client's own `/proc` cmdline, which only carries a session name
when the user happened to type `tmux new -s <name>`; a bare `tmux` or
`tmux attach` carries none. The pid map this produces is checked against
the same two process-tree shapes `program` detection already understands: a
grandchild via an interactive shell (`kitty` → `zsh` → `tmux`), or a direct
child when kitty launches tmux itself (`kitty -e tmux ...`).

```json
{"class": "kitty", "cmd": ["kitty", "--directory", "/home/gion/coding/MS"], "tmux_session": "work"}
```

At restore time this becomes:

```
kitty --directory /home/gion/coding/MS -e tmux new -A -s work
```

`new -A -s` attaches to the session if it already exists (tmux-continuum
restored it before this window came back) and creates it otherwise, so this
is correct regardless of which happens first.

A session name is observed data, and unlike `program` it is interpolated
directly into that command line rather than used only as a lookup key into
a fixed table — so it is validated before it is ever recorded, and again
before restore builds a command from it: only letters, digits, and `_ - .`
are accepted, and anything else (whitespace, quotes, shell metacharacters,
control characters) is rejected and logged rather than recorded. A rejected
name simply means that window restores as a plain terminal — the same
fallback an unrecognised `program` already has.

**Installing the tmux plugins.** `.config/tmux/tmux.conf` loads
tmux-resurrect and tmux-continuum from `~/.local/share/tmux/plugins`. They
are third-party code, so they are not in this repo and no plugin manager
fetches them. On a new machine, clone them before starting tmux:

```sh
mkdir -p ~/.local/share/tmux/plugins
git clone https://github.com/tmux-plugins/tmux-resurrect.git ~/.local/share/tmux/plugins/tmux-resurrect
git clone https://github.com/tmux-plugins/tmux-continuum.git ~/.local/share/tmux/plugins/tmux-continuum
```

Without them, the `run-shell` lines in `tmux.conf` print an error when tmux
starts. Sessions still work, but their contents do not survive a reboot.
Save by hand with prefix + S, restore with prefix + R (prefix is
Ctrl-Space).

**Also not restored, deliberately:**

- *Maximized* windows come back at their normal size. `hyprctl` reports
  maximized and fullscreen as the same field (1 and 2), and restoring a
  maximized window with the `fullscreen` rule makes it genuinely fullscreen
  — which the next snapshot then records as fullscreen, so it could never
  get back. Losing the state is recoverable; converting it silently is not.
- *Scratchpad windows.* Special workspaces have a negative id in
  `hyprctl -j clients` but are addressed by name (`special:magic`), so these
  windows are skipped at capture time rather than restored somewhere wrong.

### If a snapshot goes bad

A snapshot is never overwritten with an empty one while a session is being
torn down at logout. That guard is narrow: it only refuses an empty snapshot
over a non-empty one. It cannot tell a restore that placed one of twelve
windows from the user having closed eleven, so a failed or partial restore
can still shrink the saved session.

The backstop is a rolling backup. Every write first rotates the current
snapshot to `session.json.1`, so the previous one is always recoverable:

```sh
pkill -f 'session.py boot'                    # stop the watcher overwriting it
cd ~/.local/state/hypr-session
cp session.json.1 session.json
```

`session.log` records every write as `capture: wrote N window(s) (previously
M)`, which is how a shrinking snapshot is spotted in the first place.

Single-instance applications such as Firefox — ones whose already-running
process hands off to itself instead of opening a new window when launched
again, so a "new" window is really the same process, just told to make
another window — are launched once and left to manage the rest of their
own windows. That one launch carries no workspace of its own: an earlier
revision launched it on its first recorded window's workspace, via the
same `[workspace N silent]` exec rule prefix used for every other window,
but that prefix is applied to whichever window the process happens to map
FIRST — and for an app restoring its own session, that has no relationship
to the entry `session.py` launched it for. Observed live: Firefox mapped a
"New Tab" window before the Jira tab it had also been asked to restore, so
the exec rule meant for the Jira tab landed on the New Tab window instead,
and the Jira tab — with no rule of its own — landed on whatever workspace
happened to be active. Both windows wrong.

A window *rule* keyed on the recorded title cannot fix this either — an
earlier revision of this tool tried exactly that, and it is provably wrong.
Hyprland evaluates window rules at map time, and a restored Firefox
window's title at that instant is still the generic `Mozilla Firefox`; the
real title only arrives once the page finishes loading. Confirmed live:
both rules registered (the log showed it), neither matched anything, and
both windows landed on the active workspace regardless.

Placement for these apps instead happens after the fact, once every window
has had a chance to map and pick up its real title: `session.py` polls
`hyprctl -j clients`, and for each live window of a single-instance class
it finds a class-and-title match against the recorded snapshot and moves
it onto that entry's workspace by address —
`hyprctl dispatch 'hl.dsp.window.move({ workspace = N, window =
"address:0x..." })'`. This has to go through `hyprctl dispatch`, not
`hyprctl eval`: `eval` on the same expression returns `ok` and moves
nothing, since it only constructs the dispatcher object and never calls
it — `dispatch` wraps the argument in `hl.dispatch(...)`, which actually
executes it. A window already on its recorded workspace is left alone. A
move also drags the active workspace along with it — confirmed live, even
with `silent = true` passed to the move — so once every window has been
placed (or the poll times out) `session.py` refocuses a fixed workspace
(`RECONCILE_FOCUS_WORKSPACE`, default `1`) to put focus back where the
session actually is. `session.py` logs each move, each failed move, and
each recorded title that never showed up by the deadline, so the outcome
is explained rather than silent. Terminals and other applications that
spawn one process per window are placed normally, workspace exec rule
included; add a class to `SINGLE_INSTANCE_CLASSES` in `session.py` if
another single-instance application is encountered — `firefox` is already
there.

Placement of these windows is best-effort, not guaranteed: it depends on
the window's title, once loaded, matching what was recorded at save time.
A title that changes in between — a notification counter like `(3)`, or a
page that rewrites its own title after loading — will not match, and that
window is logged as unmatched at the poll deadline and lands wherever the
compositor's default happens to put it, rather than being mis-placed onto
some other window's workspace: `session.py` never invents a fallback
workspace for a single-instance launch, since a wrong placement is worse
than none.

Existing snapshots written before titles were recorded at all carry none,
so the first reboot after deploying this launches Firefox once, unplaced,
and reconciles nothing — there is no title to match against yet. The
capture that follows records titles for the first time, and the next
reboot places them normally.

Tests: `python -m unittest discover -s tests`

## Fonts Used

Only four fonts are actually referenced by the configs in this repo. Install
these (Nerd Font variants where noted) and nothing will fall back:

| Font | Used by |
|---|---|
| Hack Nerd Font Mono — Regular, Bold, Italic, Bold Italic | kitty |
| Ubuntu Nerd Font | waybar |
| JetBrains Mono Nerd Font | rofi |
| Times New Roman | waybar (one rule) |

Nerd Fonts are patched fonts from [nerdfonts.com](https://www.nerdfonts.com);
the patched variant is required for the glyphs in waybar and rofi to render.
