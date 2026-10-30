#!/usr/bin/env bash
# Check exactly what is staged. Never print matching secret values.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"
git diff --cached --quiet && exit 0

failed=0
while IFS= read -r -d '' file; do
  # Also reject force-added runtime data, secrets, and build output.
  if git check-ignore --no-index -q -- "$file"; then
    printf 'Privacy check: staged local/generated file is blocked: %q\n' "$file" >&2
    failed=1
  fi
done < <(git diff --cached --name-only -z --diff-filter=ACMRT)
[[ "$failed" -eq 0 ]] || exit 1

scanner="${GITLEAKS_BIN:-gitleaks}"
if ! command -v "$scanner" >/dev/null 2>&1; then
  scanner="$(git rev-parse --git-path tools/gitleaks)"
fi
if [[ ! -x "$scanner" ]] && ! command -v "$scanner" >/dev/null 2>&1; then
  echo 'Privacy check: install Gitleaks 8.30.1 or newer before committing.' >&2
  exit 1
fi

exec "$scanner" git --pre-commit --staged \
  --config .gitleaks.toml --redact=100 --no-banner \
  --ignore-gitleaks-allow --gitleaks-ignore-path /dev/null
