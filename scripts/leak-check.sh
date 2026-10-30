#!/usr/bin/env bash
# Invoke the installed, reviewed checker. Missing installation fails closed.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
GUARD="$(git rev-parse --git-path privacy/privacy-check.py)"
if [[ ! -f "$GUARD" ]]; then
    echo 'Privacy guard missing. Review and run scripts/install-hooks.sh first.' >&2
    exit 1
fi
if [[ $# -eq 0 ]]; then set -- staged; fi
exec uv run --no-project --no-config --offline python "$GUARD" "$@"
