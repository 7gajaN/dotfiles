# Black SDDM greeter

The login screen: solid black, the username, and one password box. Only the
main monitor is lit while the greeter is up; the second comes back when
Hyprland starts and applies its own `hl.monitor()` layout.

## Install

```sh
sudo sh install.sh
```

Idempotent. Verify without rebooting:

```sh
sddm-greeter --test-mode --theme /usr/share/sddm/themes/black
```

## Two traps worth knowing before editing

**SDDM runs the Qt5 greeter.** The package ships both `/usr/bin/sddm-greeter`
(Qt5) and `/usr/bin/sddm-greeter-qt6`, and the daemon launches the Qt5 one. So:

- imports must be versioned (`import QtQuick 2.15`); the versionless Qt6 form
  fails with `Library import requires a version`;
- `QtQuick.Controls` is off-limits — `qt5-quickcontrols2` isn't installed, so
  `TextField` can't resolve. The password box is a plain `TextInput`.

When the theme fails to load SDDM logs `Fallback to embedded theme` and quietly
shows its built-in greeter, so the only symptom is "my theme didn't apply".
Testing against `sddm-greeter-qt6` passes while the real boot falls back — use
the Qt5 binary. `install.sh` checks both of these.

**The monitor is matched by EDID serial, not output name.** The NVIDIA X driver
names outputs on its own scheme (`DFP-1`, `DFP-4`) which does not match the DRM
connector names Hyprland uses (`DP-2`, `DP-3`), so a hardcoded name would be a
guess. `etc/sddm/Xsetup` decodes each connected output's EDID and keeps the one
whose serial matches `MAIN_EDID_MATCH`. New monitor? Update that value —
`hyprctl monitors` prints the serial in the description field.

If nothing matches, `Xsetup` leaves every output exactly as found. A dead
greeter is worse than an extra monitor being on.

## Files

| Repo path | Installs to | Mode |
|---|---|---|
| `etc/sddm/Xsetup` | `/etc/sddm/Xsetup` | 0755 |
| `etc/sddm.conf.d/10-black-greeter.conf` | `/etc/sddm.conf.d/` | 0644 |
| `usr/share/sddm/themes/black/*` | `/usr/share/sddm/themes/black/` | 0644 |

Copied, never symlinked: `Xsetup` runs as root before login, so sourcing it from
a user-writable home would hand root to anything that can write there, and
`/home` may not be mounted that early.

## Keys

`F1` cycles the session (Hyprland / i3) and names it briefly — the only way back
to i3, since the greeter is otherwise bare.
