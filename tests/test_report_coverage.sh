#!/usr/bin/env bash
# Exercises report.sh's power_lines() in isolation (the coverage row retired with quality_gate): extracts the
# function (the sed pattern tests/test_report_notices.sh uses) and drives it
# with a fake `docker` on PATH, so no real HOME, board, vault or Postgres is
# touched. Registered in CI in Task 8.
set -u
unset CAPSULE_JOBS_DIR   # the jobs config dir comes from each case's fake home
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
fails=0
check() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; fails=$((fails+1)); fi; }

mkdir -p "$T/bin"
cat >"$T/bin/docker" <<'EOF'
#!/usr/bin/env bash
cat "$FAKE_PSQL_CSV"
EOF
chmod +x "$T/bin/docker"
export PATH="$T/bin:$PATH"

sed -n '/^power_lines() {/,/^}/p' "$REPO/maintenance/report.sh" >"$T/fn.sh"

printf 'rulings,4\ndecisions,1\ncoverage,92\n' >"$T/with.csv"
export FAKE_PSQL_CSV="$T/with.csv"
out="$(. "$T/fn.sh"; power_lines)"
check "no coverage line (quality_pass retired, so the row is gone)" '! grep -q "coverage:" <<<"$out"'
check "delegation line still rendered" 'grep -q "^delegation: 4 machine rulings : 1 your decisions" <<<"$out"'

printf 'rulings,0\ndecisions,0\ncoverage,\n' >"$T/without.csv"
export FAKE_PSQL_CSV="$T/without.csv"
out="$(. "$T/fn.sh"; power_lines)"
check "no coverage line on an idle day" '! grep -q "coverage:" <<<"$out"'

[ $fails -eq 0 ] && echo "test_report_coverage: all passed" || { echo "test_report_coverage: $fails failed"; exit 1; }
