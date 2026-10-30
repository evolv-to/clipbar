#!/usr/bin/env bash
# Run only after reviewing checker/policy changes. Hooks use copies so a
# worktree or staged edit cannot weaken the check reviewing that same edit.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
HOOK_DIR="$(git rev-parse --git-path hooks)"
GUARD_DIR="$(git rev-parse --git-path privacy)"
if [[ -n "$(git config --get core.hooksPath || true)" ]]; then
    echo 'Custom core.hooksPath is set; integrate hooks explicitly before proceeding.' >&2
    exit 1
fi
mkdir -p "$HOOK_DIR" "$GUARD_DIR"
chmod 700 "$GUARD_DIR"
cp scripts/privacy-check.py .gitleaks.toml .gitignore .privacy-policy.json "$GUARD_DIR/"
if [[ -f .privacy-denylist ]]; then cp .privacy-denylist "$GUARD_DIR/"; fi
for HOOK in pre-commit commit-msg pre-push; do
    case "$HOOK" in
        pre-commit) MODE=staged ;;
        commit-msg) MODE=message ;;
        pre-push) MODE=push ;;
    esac
    # Remove the former symlink before writing; never overwrite its target.
    if [[ -L "$HOOK_DIR/$HOOK" ]]; then rm "$HOOK_DIR/$HOOK"; fi
    cat > "$HOOK_DIR/$HOOK" <<INNER
#!/usr/bin/env bash
set -euo pipefail
cd "\$(git rev-parse --show-toplevel)"
exec uv run --no-project --no-config --offline python "\$(git rev-parse --git-path privacy/privacy-check.py)" "$MODE" "\$@"
INNER
    chmod 755 "$HOOK_DIR/$HOOK"
done
echo 'Installed reviewed privacy snapshot: pre-commit, commit-msg, pre-push.'
echo 'Requires uv and Gitleaks >=8.30.1. Review policy before reinstalling.'
