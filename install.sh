#!/usr/bin/env bash
#
# MultiFlash installer.
#
# Copies the app into your local applications area and adds a menu entry so
# MultiFlash shows up alongside your other apps. Run it from inside the repo:
#
#     ./install.sh
#
# No root needed to install (the app itself asks for root via pkexec when you
# launch it). Re-running it upgrades an existing install in place.
#
set -euo pipefail

# Where this script lives = where the source files are.
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

APP_NAME="multiflash"
INSTALL_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/$APP_NAME"
DESKTOP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
DESKTOP_FILE="$DESKTOP_DIR/$APP_NAME.desktop"
ICON_SRC="$SRC_DIR/packaging/$APP_NAME.svg"
ICON_DST="$INSTALL_DIR/$APP_NAME.svg"

# Files the app needs to run. Runtime files (config.json, flash_log.csv,
# extracted_images/) are deliberately NOT copied — the app recreates them.
CODE_FILES=(flash.py gui.py imagemap.py pishrink.sh)

echo "Installing MultiFlash to: $INSTALL_DIR"

# --- sanity check: everything present? -------------------------------------
missing=()
for f in "${CODE_FILES[@]}"; do
    [ -e "$SRC_DIR/$f" ] || missing+=("$f")
done
[ -e "$ICON_SRC" ] || missing+=("packaging/$APP_NAME.svg")
if [ "${#missing[@]}" -ne 0 ]; then
    echo "Error: missing source files: ${missing[*]}" >&2
    echo "Run this script from inside the MultiFlash repo." >&2
    exit 1
fi

command -v python3 >/dev/null || { echo "Error: python3 not found." >&2; exit 1; }

# --- copy the app ----------------------------------------------------------
mkdir -p "$INSTALL_DIR"
for f in "${CODE_FILES[@]}"; do
    cp "$SRC_DIR/$f" "$INSTALL_DIR/$f"
done
[ -d "$SRC_DIR/licenses" ] && cp -r "$SRC_DIR/licenses" "$INSTALL_DIR/"
# Carry over your existing default-image setting if it's readable.
if [ -r "$SRC_DIR/config.json" ] && [ ! -e "$INSTALL_DIR/config.json" ]; then
    cp "$SRC_DIR/config.json" "$INSTALL_DIR/config.json" 2>/dev/null || true
fi
cp "$ICON_SRC" "$ICON_DST"
chmod +x "$INSTALL_DIR/pishrink.sh" "$INSTALL_DIR/flash.py"

# --- menu entry ------------------------------------------------------------
mkdir -p "$DESKTOP_DIR"
cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=MultiFlash
Comment=Flash OS images to multiple SD cards at once
Exec=python3 $INSTALL_DIR/flash.py
Icon=$ICON_DST
Terminal=false
Categories=Utility;
Keywords=sd;card;flash;image;dd;raspberry;pi;
EOF
chmod +x "$DESKTOP_FILE"

# Refresh the menu database (harmless if the tool isn't installed).
command -v update-desktop-database >/dev/null && \
    update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true

echo
echo "Done. Look for \"MultiFlash\" in your application menu."
echo "(If it doesn't appear immediately, log out/in or run: update-desktop-database $DESKTOP_DIR)"
echo "To remove it later, run ./uninstall.sh"
