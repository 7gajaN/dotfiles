# Dotfiles for Arch Linux Environment

This repository contains my personal configuration files (dotfiles) for my Arch Linux setup. 

## Apps and Tools Configured

### Window Management & Desktop
- **i3**: Tiling window manager for fast and flexible workspace management.
- **picom**: Lightweight compositor for X11, providing transparency and shadow effects.
- **polybar**: Highly customizable status bar for window managers.
- **feh**: Lightweight image viewer, used for setting wallpapers.

### Terminal & Shell
- **Kitty**: GPU-based, fast, and feature-rich terminal emulator.
- **Ranger**: Vim-like file manager for the terminal.
- **Zsh**: Powerful shell with plugins and themes (see `.zshrc`).

### Text Editing
- **Neovim**: Hyperextensible Vim-based text editor, configured for coding and writing.

### Application Launching & Menu
- **Rofi**: Window switcher, run dialog, and application launcher.

### System Info & Appearance
- **Fastfetch**: Fast system information tool for displaying system stats on startup.

### Other Configured Apps (not included in the dot files)
- **Thunar**: Lightweight file manager (configuration included if personalized).
- **OpenRGB**: RGB lighting control for hardware components (optional).
- **GTK 3.0**: GTK theme and appearance settings (if customized).
- **PulseAudio/Pavucontrol**: Sound system and volume control config.
- **Spicetify**: Spotify client theming (optional).

## Fonts Used

The following fonts are referenced in my configuration files. For a fresh install, make sure to install all of these (including their Nerd Font or icon font variants as specified):

```text
Source Sans Pro, Semibold
Hack Nerd Font Mono
Hack Nerd Font Mono Bold
Hack Nerd Font Mono Bold Italic
Hack Nerd Font Mono Italic
Nerd Font
Fira Code
Noto Sans
NotoSans-Regular
MaterialIcons
FontAwesome
Material-Design-Iconic-Font
Fantasque Sans Mono
Iosevka Nerd Font
feather
icomoon-feather
Droid Sans
JetBrains Mono Nerd Font
```

- **Nerd Font** refers to any patched font from [nerdfonts.com](https://www.nerdfonts.com) (multiple variants used)
- Some configs use variants (Mono, Bold, Italic, etc.)
- **feather**, **FontAwesome**, **MaterialIcons**, **Material-Design-Iconic-Font**, **icomoon-feather** are icon fonts for Polybar/Rofi
- **Fantasque Sans Mono**, **JetBrains Mono Nerd Font**, **Iosevka Nerd Font** are popular patched coding fonts
- **Source Sans Pro**, **Noto Sans**, **Droid Sans** are UI fonts used in bars and menus
