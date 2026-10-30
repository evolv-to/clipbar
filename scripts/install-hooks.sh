#!/usr/bin/env bash
# Install ClipBar's git hooks into .git/hooks. Run once after clone, or
# whenever you reset .git/hooks. Idempotent.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"
HOOK_DIR="$(git rev-parse --git-path hooks)"
mkdir -p "$HOOK_DIR"

ln -sf "$ROOT_DIR/scripts/leak-check.sh" "$HOOK_DIR/pre-commit"
chmod +x "$ROOT_DIR/scripts/leak-check.sh"
echo "Installed pre-commit → scripts/leak-check.sh"
echo "The hook requires Gitleaks 8.30.1 or newer and blocks commits when it is missing."
