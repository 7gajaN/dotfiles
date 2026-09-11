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
another window — are launched once, on their first recorded window's
workspace, and left to manage the rest of their own windows. Those extra
windows come back anyway, with their tabs, through the application's own
session restore (Firefox's `browser.startup.page`, for instance), and
`session.py` still places them on their recorded workspaces: before that
one launch, it registers a Hyprland window rule (via `hyprctl eval
hl.window_rule(...)`) matched on class and recorded title for every other
window of that class, and a rule registered ahead of time still applies to
a window the app goes on to create by itself. That placement depends on
the recorded title still matching by the time the window reappears — a
tab switch between saving and restoring changes it — so it is best-effort,
not guaranteed. `session.py` logs each recorded window it skips launching
this way, and separately logs each window rule it registers (and any that
fails to register), so both the skip and the placement attempt are
explained rather than silent. Terminals and other applications that spawn
one process per window are placed normally; add a class to
`SINGLE_INSTANCE_CLASSES` in `session.py` if another single-instance
application is encountered — `firefox` is already there.

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
