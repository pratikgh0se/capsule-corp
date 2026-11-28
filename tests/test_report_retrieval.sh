#!/usr/bin/env bash
# Exercises maintenance/report.sh's retrieval_lines() in isolation: extracts the
# function and drives it with a fake `uv` on PATH, so no real vault, index,
# Ollama or HOME is touched. Registered in CI in Task 12.
set -euo pipefail
unset CAPSULE_JOBS_DIR   # the jobs config dir comes from each case's fake home
HERE="$(cd "$(dirname "$0")" && pwd)"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
sed -n '/^retrieval_lines()/,/^}/p' "$HERE/../maintenance/report.sh" > "$TMP/fn.sh"
[ -s "$TMP/fn.sh" ] || { echo "FAIL: retrieval_lines() not found in report.sh"; exit 1; }

mkfake() {  # $1 = the JSON the fake `uv` prints
  mkdir -p "$TMP/bin"
  { echo '#!/usr/bin/env bash'; printf 'cat <<%s\n%s\n%s\n' EOF "$1" EOF; } > "$TMP/bin/uv"
  chmod +x "$TMP/bin/uv"
}
run() { PATH="$TMP/bin:$PATH" bash -c "OSB_DIR=/x; VAULT=/y; STATE_DIR='$TMP/maint/state'; source '$TMP/fn.sh'; retrieval_lines"; }
mkdir -p "$TMP/maint/state"

# --- healthy: the line is ABSENT, not blank -------------------------------
mkfake '{"exists":true,"coverage_pct":99.4,"oldest_write":0,"at_ceiling":0}'
out="$(run)"; [ -z "$out" ] || { echo "FAIL: healthy index produced: $out"; exit 1; }

# --- coverage under 98%: one line naming the threshold --------------------
mkfake '{"exists":true,"coverage_pct":94.0,"oldest_write":0,"at_ceiling":0}'
out="$(run)"
case "$out" in *"coverage 94"*"<98%"*) ;; *) echo "FAIL: coverage: $out"; exit 1 ;; esac

# --- age over 36h --------------------------------------------------------
mkfake "{\"exists\":true,\"coverage_pct\":99.9,\"oldest_write\":$(( $(date +%s) - 40*3600 )),\"at_ceiling\":0}"
out="$(run)"
case "$out" in *">36h"*) ;; *) echo "FAIL: age: $out"; exit 1 ;; esac

# --- notes at the chunk ceiling ------------------------------------------
mkfake '{"exists":true,"coverage_pct":99.9,"oldest_write":0,"at_ceiling":2}'
out="$(run)"
case "$out" in *"2 notes at the chunk ceiling"*) ;; *) echo "FAIL: ceiling: $out"; exit 1 ;; esac

# --- no index at all: says so once, does not crash -----------------------
mkfake '{"exists":false}'
out="$(run)"
case "$out" in *"no semantic index"*) ;; *) echo "FAIL: missing index: $out"; exit 1 ;; esac

# --- I5: the --stats call is wrapped in a timeout, like the index: line ----
grep -q 'timeout 60 uv run' "$TMP/fn.sh" \
  || { echo "FAIL: retrieval_lines() calls uv without a timeout (I5)"; exit 1; }

# --- I4: a floor regression reaches the brief WITH its numbers -------------
FLOOR="$TMP/maint/state/retrieval_floor-retrieval_cases-251notes.json"
mkfake '{"exists":true,"coverage_pct":99.9,"oldest_write":0,"at_ceiling":0}'

# no floor file at all: absent
out="$(run)"; [ -z "$out" ] || { echo "FAIL: no floor file produced: $out"; exit 1; }

# a floor file with no regression recorded: still absent
cat > "$FLOOR" <<'EOF'
{"recall_at_5": 0.793, "as_of": "2026-09-10", "cases_file": "retrieval_cases-251notes.jsonl"}
EOF
out="$(run)"; [ -z "$out" ] || { echo "FAIL: clean floor produced: $out"; exit 1; }

# a regression: one line naming measured, floor and the date
cat > "$FLOOR" <<'EOF'
{"recall_at_5": 0.793, "as_of": "2026-09-10",
 "cases_file": "retrieval_cases-251notes.jsonl",
 "last_regression": {"measured": 0.512, "floor": 0.793, "when": "2026-10-01"}}
EOF
out="$(run)"
case "$out" in
  *"51.2%"*"79.3%"*"2026-10-01"*) ;;
  *) echo "FAIL: regression line missing its numbers: $out"; exit 1 ;;
esac

# a corrupt floor file degrades to absent, never to a crash
printf '{"last_regression": {' > "$FLOOR"
out="$(run)"; [ -z "$out" ] || { echo "FAIL: corrupt floor produced: $out"; exit 1; }
rm -f "$FLOOR"

echo "PASS test_report_retrieval.sh"
