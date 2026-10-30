#!/usr/bin/env bash
# ClipBar installer — builds the app, sets up the daemon, registers launchd agents.
set -euo pipefail
umask 077

ROOT="$(cd "$(dirname "$0")" && pwd)"
APP_NAME="ClipBar"
APP_BUNDLE="$ROOT/$APP_NAME/$APP_NAME.app"
INSTALLED_APP="/Applications/$APP_NAME.app"
LA_DIR="$HOME/Library/LaunchAgents"
CLIPD_PLIST="$LA_DIR/app.clipbar.clipd.plist"
CLIPBAR_PLIST="$LA_DIR/app.clipbar.ClipBar.plist"
LOG_DIR="$HOME/.config/clipd/logs"

red()   { printf "\033[31m%s\033[0m\n" "$*"; }
green() { printf "\033[32m%s\033[0m\n" "$*"; }
blue()  { printf "\033[34m%s\033[0m\n" "$*"; }

# --- preflight ---------------------------------------------------------------
blue "==> Checking prerequisites"

command -v swiftc >/dev/null || {
    red "swiftc not found. Install Xcode Command Line Tools:  xcode-select --install"
    exit 1
}
command -v uv >/dev/null || {
    red "uv not found. Install:  curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
}

UV_BIN="$(command -v uv)"

# --- build the Swift app -----------------------------------------------------
blue "==> Building $APP_NAME"
( cd "$ROOT/$APP_NAME" && bash build.sh )

# --- stop any running instance before replacing ------------------------------
launchctl unload "$CLIPBAR_PLIST" 2>/dev/null || true
launchctl unload "$CLIPD_PLIST"   2>/dev/null || true
pkill -9 -f "$INSTALLED_APP" 2>/dev/null || true

# --- install the app ---------------------------------------------------------
blue "==> Installing to $INSTALLED_APP"
rm -rf "$INSTALLED_APP"
cp -r "$APP_BUNDLE" "$INSTALLED_APP"

# --- prepare daemon venv -----------------------------------------------------
blue "==> Setting up clipd Python venv"
( cd "$ROOT/clipd" && uv sync )

# Prepare private logs before launchd opens them. Refuse symlinks/unowned files.
( cd "$ROOT/clipd" && uv run python - "$LOG_DIR" <<'PYLOG'
from pathlib import Path
import sys
from private_files import private_directory, secure_file
logs = Path(sys.argv[1])
private_directory(logs.parent)
private_directory(logs)
for name in ('clipd.log', 'clipd.err', 'clipbar.log', 'clipbar.err'):
    secure_file(logs / name, create=True)
    # Older installers wrote here. Restrict only owned regular legacy logs;
    # never read or remove their contents during installation.
    try:
        secure_file(Path('/tmp') / name)
    except (OSError, ValueError):
        pass
PYLOG
)

# --- write launchd plists with absolute paths --------------------------------
blue "==> Writing launchd agents"
mkdir -p "$LA_DIR"

cat > "$CLIPD_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>app.clipbar.clipd</string>
    <key>ProgramArguments</key>
    <array>
        <string>$UV_BIN</string>
        <string>run</string>
        <string>--directory</string>
        <string>$ROOT/clipd</string>
        <string>python</string>
        <string>$ROOT/clipd/clipd.py</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>$LOG_DIR/clipd.log</string>
    <key>StandardErrorPath</key><string>$LOG_DIR/clipd.err</string>
</dict>
</plist>
EOF

cat > "$CLIPBAR_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>app.clipbar.ClipBar</string>
    <key>ProgramArguments</key>
    <array>
        <string>$INSTALLED_APP/Contents/MacOS/$APP_NAME</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>$LOG_DIR/clipbar.log</string>
    <key>StandardErrorPath</key><string>$LOG_DIR/clipbar.err</string>
</dict>
</plist>
EOF

# --- load --------------------------------------------------------------------
blue "==> Loading agents"
launchctl load "$CLIPD_PLIST"
launchctl load "$CLIPBAR_PLIST"

sleep 1

green "==> Done"
echo
echo "  • Press ⌘⇧V to open ClipBar"
echo "  • Right-click the menu bar icon for the menu"
echo "  • First paste will prompt for Accessibility — grant it, then click Restart from the menu"
echo
echo "  Logs:    $LOG_DIR/clipbar.log  $LOG_DIR/clipd.log"
echo "  History: ~/.config/clipd/history.db"
echo "  Uninstall: ./uninstall.sh"
