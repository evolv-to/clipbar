#!/data/data/com.termux/files/usr/bin/bash
# clipa installer — Android via Termux.
#
# Run inside Termux:
#   curl -fsSL https://raw.githubusercontent.com/evolv-to/clipbar/main/clipa/install.sh | bash
#
# Or, with this repo cloned on-device:
#   bash clipa/install.sh

set -euo pipefail

PREFIX="${PREFIX:-/data/data/com.termux/files/usr}"
HOME_DIR="${HOME:-/data/data/com.termux/files/home}"
INSTALL_DIR="$HOME_DIR/.local/bin"
CONFIG_DIR="$HOME_DIR/.config/clipa"
LOG_FILE="$HOME_DIR/.cache/clipa.log"
BOOT_DIR="$HOME_DIR/.termux/boot"
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
info() { printf '  %s\n' "$*"; }
warn() { printf '  ⚠ %s\n' "$*" >&2; }

bold "==> clipa installer"

# 1. Dependencies
if ! command -v go >/dev/null 2>&1; then
    info "installing golang via pkg…"
    pkg install -y golang
fi

if ! command -v termux-clipboard-get >/dev/null 2>&1; then
    info "installing termux-api package…"
    pkg install -y termux-api
    warn "Also install the Termux:API APK from F-Droid if you haven't:"
    warn "  https://f-droid.org/en/packages/com.termux.api/"
fi

# 2. Build
bold "==> Building clipa…"
mkdir -p "$INSTALL_DIR"
( cd "$SRC_DIR" && go build -o "$INSTALL_DIR/clipa" . )
info "binary at $INSTALL_DIR/clipa"

# 3. Config scaffold
mkdir -p "$CONFIG_DIR" "$(dirname "$LOG_FILE")"
if [ ! -f "$CONFIG_DIR/peers.json" ]; then
    cat > "$CONFIG_DIR/peers.json" <<'EOF'
[
  {
    "_comment": "Edit this file: one entry per ClipBar Mac on your tailnet.",
    "_comment2": "Look up each Mac's tailnet name and IPv4 with 'tailscale status'.",
    "_comment3": "hostname is shown in ClipBar's menu; address is the tailnet IP (no port = :2029).",
    "hostname": "REPLACE_WITH_TAILNET_HOSTNAME",
    "address":  "REPLACE_WITH_TAILNET_IPV4"
  }
]
EOF
    info "wrote starter peers.json at $CONFIG_DIR/peers.json"
    warn "Edit it: replace REPLACE_WITH_* values with your real Mac peers, then restart clipa."
else
    info "peers.json already exists at $CONFIG_DIR/peers.json (left untouched)"
fi

# 4. Termux:Boot autostart
mkdir -p "$BOOT_DIR"
BOOT_SCRIPT="$BOOT_DIR/clipa"
cat > "$BOOT_SCRIPT" <<EOF
#!$PREFIX/bin/bash
# Auto-start clipa at boot. Requires the Termux:Boot APK installed.
termux-wake-lock
exec $INSTALL_DIR/clipa --log $LOG_FILE
EOF
chmod +x "$BOOT_SCRIPT"
info "boot script at $BOOT_SCRIPT"
warn "Install the Termux:Boot APK from F-Droid if you haven't:"
warn "  https://f-droid.org/en/packages/com.termux.boot/"

bold "==> Done."
echo
echo "  Next:"
echo "    1) Edit $CONFIG_DIR/peers.json with your Mac peers."
echo "    2) Start now without rebooting: nohup $INSTALL_DIR/clipa --log $LOG_FILE >/dev/null 2>&1 &"
echo "    3) Tail the log:                tail -f $LOG_FILE"
echo "    4) Verify on a Mac:             curl -s http://127.0.0.1:2028/peers | jq"
