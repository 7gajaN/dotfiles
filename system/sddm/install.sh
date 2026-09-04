#!/bin/sh
# Install the black SDDM greeter (see ./README.md).
#
# These files are copied, never symlinked. /etc/sddm/Xsetup runs as root before
# anyone logs in, so pointing it at a file inside a user-writable home would
# hand root to anything that can write there -- and /home may not even be
# mounted that early in boot.
#
# Usage:
#   sudo sh install.sh              install for real
#   DESTDIR=/tmp/x sh install.sh    stage into a prefix (no root needed; for testing)
#
# Idempotent: safe to re-run.

set -eu

SRC=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DESTDIR=${DESTDIR:-}

if [ -z "$DESTDIR" ] && [ "$(id -u)" -ne 0 ]; then
    echo "error: run as root (sudo sh $0), or set DESTDIR to stage elsewhere." >&2
    exit 1
fi

# mode:relative path -- the path under $SRC mirrors the destination exactly, so
# there is no mapping table here to drift out of sync with the tree.
FILES="
0755:etc/sddm/Xsetup
0644:etc/sddm.conf.d/10-black-greeter.conf
0644:usr/share/sddm/themes/black/Main.qml
0644:usr/share/sddm/themes/black/theme.conf
0644:usr/share/sddm/themes/black/metadata.desktop
"

for entry in $FILES; do
    mode=${entry%%:*}
    rel=${entry#*:}
    dst="$DESTDIR/$rel"
    install -d -m 0755 "$(dirname "$dst")"
    install -m "$mode" "$SRC/$rel" "$dst"
    echo "  $rel  ($mode)"
done

echo
echo "checks:"

qml="$DESTDIR/usr/share/sddm/themes/black/Main.qml"

# SDDM launches the Qt5 greeter, which rejects versionless imports with
# "Library import requires a version" and then silently falls back to the
# embedded theme. Catch that here rather than at the next reboot.
if grep -q '^import QtQuick 2\.' "$qml"; then
    echo "  ok    QML import is versioned (Qt5 greeter needs this)"
else
    echo "  FAIL  QML import is not versioned -- the Qt5 greeter will reject it" >&2
fi

if grep -vE '^[[:space:]]*//' "$qml" | grep -q 'QtQuick\.Controls'; then
    echo "  FAIL  theme references QtQuick.Controls, unavailable to the Qt5 greeter" >&2
else
    echo "  ok    no QtQuick.Controls dependency"
fi

serial=$(sed -n 's/^MAIN_EDID_MATCH="\(.*\)".*/\1/p' "$DESTDIR/etc/sddm/Xsetup")
echo "  note  greeter monitor is matched on EDID serial '$serial'."
echo "        Different monitor after a rebuild? Update MAIN_EDID_MATCH in"
echo "        etc/sddm/Xsetup -- 'hyprctl monitors' prints the serial."
echo
echo "Done. Verify without rebooting:"
echo "  sddm-greeter --test-mode --theme /usr/share/sddm/themes/black"
echo "(the Qt5 binary -- NOT sddm-greeter-qt6, which has different QML modules)"
