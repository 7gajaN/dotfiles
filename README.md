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
