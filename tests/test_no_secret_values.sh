#!/usr/bin/env bash
# No tracked env-shaped file carries a real-looking value (spec section 9).
set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fails=0; check() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; fails=$((fails+1)); fi; }
files="$(git -C "$REPO" ls-files '*.env' '*.env.*' '*env.example' '*.template' 'observability/*.env*')"
# An empty list would leave grep with no file operands, so it would read stdin
# and the guard would silently "pass" having scanned nothing. Fail loudly
# instead, and pin stdin to /dev/null so no grep here can ever block on it.
[ -n "$files" ] || { echo "FAIL no files matched"; exit 1; }
hits="$(grep -nE '^[A-Z_]*(TOKEN|PASSWORD|SECRET|KEY)[A-Z_]*=([0-9]{6,}:[A-Za-z0-9_-]{30,}|[A-Za-z0-9+/=_-]{24,})$' $files </dev/null 2>/dev/null || true)"
check "no token-shaped value in tracked env files" '[ -z "$hits" ]'
[ -n "$hits" ] && printf '%s\n' "$hits" | sed -E 's/=.*/=<redacted>/'
check "observability template has no OPS_ secret line" '! grep -qE "^OPS_(PG_PASSWORD|GRAFANA_PASSWORD|TG_BOT_TOKEN|TG_CHAT_ID|TG_THREAD_ID)=" "$REPO/observability/env.template"'
echo; if [ $fails -eq 0 ]; then echo "test_no_secret_values: all passed"; else echo "test_no_secret_values: $fails failed"; exit 1; fi
