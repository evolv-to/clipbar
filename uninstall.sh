#!/usr/bin/env bash
# ClipBar uninstaller — stops agents, removes binaries, leaves your history alone.
set -euo pipefail

LA_DIR="$HOME/Library/LaunchAgents"
CLIPD_PLIST="$LA_DIR/app.clipbar.clipd.plist"
CLIPBAR_PLIST="$LA_DIR/app.clipbar.ClipBar.plist"

echo "==> Stopping agents"
launchctl unload "$CLIPBAR_PLIST" 2>/dev/null || true
launchctl unload "$CLIPD_PLIST"   2>/dev/null || true

echo "==> Killing any leftover processes"
pkill -9 -f /Applications/ClipBar.app 2>/dev/null || true
pkill -9 -f clipd.py 2>/dev/null || true

echo "==> Removing app and plists"
rm -rf /Applications/ClipBar.app
rm -f "$CLIPBAR_PLIST" "$CLIPD_PLIST"

echo "==> Done"
echo
echo "  Your clipboard history at ~/.config/clipd/ was NOT removed."
echo "  For explicit local/cloud deletion, see docs/privacy.md. Pairing identity is preserved."
