#!/usr/bin/env bash
# tests/test_security_verdicts.sh - SEC-10: the nightly security-scan job runs the verdict check
# report-only, and the CLI works end to end on fixtures. No network, no docker.
set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
WF="$REPO/.github/workflows/verify.yml"

fails=0
check() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; fails=$((fails+1)); fi; }

STEP="$(sed -n '/- name: security verdicts/,/- name: osv-scanner/p' "$WF")"
check "step exists" '[ -n "$STEP" ]'
check "step is continue-on-error" 'grep -q "continue-on-error: true" <<<"$STEP"'
check "step runs findings and check" \
  'grep -q "capsule security findings" <<<"$STEP" && grep -q "capsule security check" <<<"$STEP"'
check "step never passes --strict" '! grep -q -- "--strict" <<<"$STEP"'

printf '[{"RuleID":"r","File":"a.py","StartLine":3,"Description":"d","Secret":"HUNTER2"}]' \
  >"$T/g.json"
OUT="$(cd "$REPO" && python3 -m capsule security findings --gitleaks "$T/g.json")"
check "findings lists the id" 'grep -q "gitleaks:r:a.py:3" <<<"$OUT"'
check "findings hides the secret" '! grep -q HUNTER2 <<<"$OUT"'
printf '%s' "$OUT" >"$T/f.json"
CHK="$(cd "$REPO" && python3 -m capsule security check --repo "$T" --findings "$T/f.json")"
check "check counts it untriaged" 'grep -q "findings=1 triaged=0 untriaged=1 accepted=0" <<<"$CHK"'
(cd "$REPO" && python3 -m capsule security check --repo "$T" --findings "$T/f.json" --strict \
  >/dev/null)
check "strict exits 1" '[ $? -eq 1 ]'

[ "$fails" -eq 0 ] && echo "all passed" || { echo "$fails failed"; exit 1; }
