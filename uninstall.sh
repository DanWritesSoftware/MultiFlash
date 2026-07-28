#!/usr/bin/env bash
#
# Removes the MultiFlash menu entry and installed app files.
# Does not touch the source repo you ran install.sh from.
#
set -euo pipefail

APP_NAME="multiflash"
INSTALL_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/$APP_NAME"
DESKTOP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
DESKTOP_FILE="$DESKTOP_DIR/$APP_NAME.desktop"

# The install may have created root-owned runtime files (config.json etc.)
# because the app runs as root, so fall back to sudo if a plain rm fails.
rm -f "$DESKTOP_FILE"
if [ -d "$INSTALL_DIR" ]; then
    rm -rf "$INSTALL_DIR" 2>/dev/null || sudo rm -rf "$INSTALL_DIR"
fi

command -v update-desktop-database >/dev/null && \
    update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true

echo "MultiFlash removed."
