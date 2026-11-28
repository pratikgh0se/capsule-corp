#!/usr/bin/env bash
# Exercises maintenance/report.sh's notices_section() in isolation: extracts
# the function (same sed pattern tests/test_run_sh.sh uses for tg_send) so no
# real HOME, vault, or Grafana is ever touched, and drives it with a fake
# `capsule-secret` and a fake `curl` on PATH. Registered in CI in Task 8.
set -u
unset CAPSULE_JOBS_DIR   # the jobs config dir comes from each case's fake home
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT

fails=0
check() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; fails=$((fails+1)); fi; }

NOTICES_SRC="$(sed -n '/^notices_section()/,/^}/p' "$REPO/maintenance/report.sh")"
check "notices_section() found in report.sh" '[ -n "$NOTICES_SRC" ]'

FAKEBIN="$T/fakebin"; mkdir -p "$FAKEBIN"

# --- fake capsule-secret: prints a password, never receives it on argv ------
cat >"$FAKEBIN/capsule-secret" <<'SH'
#!/usr/bin/env bash
# get <project> <key>, identity via CAPSULE_IDENTITY env (never argv)
[ "${CAPSULE_IDENTITY:-}" = "compose" ] || { echo "capsule-secret: wrong identity" >&2; exit 2; }
[ "$1" = "get" ] && [ "$2" = "capsule-corp" ] && [ "$3" = "OPS_GRAFANA_PASSWORD" ] || { echo "capsule-secret: bad args" >&2; exit 2; }
echo "test-grafana-pw-not-a-secret"
SH
chmod +x "$FAKEBIN/capsule-secret"

# --- fake curl: reads -K - config from stdin (password must be there, not argv) ---
write_fake_curl() {  # write_fake_curl <body-to-print> <exit-code>
  local body="$1" rc="$2"
  cat >"$FAKEBIN/curl" <<SH
#!/usr/bin/env bash
# assert -K - is present (config-on-stdin) and no "admin:" string on argv
for a in "\$@"; do
  case "\$a" in *admin:*) echo "curl: password leaked on argv" >&2; exit 9 ;; esac
done
cfg="\$(cat)"
printf '%s' "\$cfg" | grep -q 'user = "admin:test-grafana-pw-not-a-secret"' || { echo "curl: bad -K config on stdin" >&2; exit 9; }
case " \$* " in *"alertmanager/grafana/api/v2/alerts"*) : ;; *) echo "curl: wrong URL" >&2; exit 9 ;; esac
cat <<'BODY'
$body
BODY
exit $rc
SH
  chmod +x "$FAKEBIN/curl"
}

run_notices() {
  ( export PATH="$FAKEBIN:$PATH" CAPSULE_SECRET_BIN="$FAKEBIN/capsule-secret" HOME="$T/home"
    eval "$NOTICES_SRC"
    notices_section )
}

# --- one notice, one page (page must be filtered out) -----------------------
write_fake_curl '[
  {"labels": {"alertname": "Disk over 85 %", "severity": "notice"}, "annotations": {"summary": "laptop disk over 85 % - run ops-cleanup"}, "startsAt": "2026-09-09T10:15:00Z"},
  {"labels": {"alertname": "Job failed", "severity": "page"}, "annotations": {"summary": "a vault job exited non-zero"}, "startsAt": "2026-09-09T10:16:00Z"}
]' 0
out="$(run_notices)"
check "notice alert listed" 'grep -q "^- Disk over 85 %: laptop disk over 85 % - run ops-cleanup (since" <<<"$out"'
check "page alert filtered out" '! grep -q "Job failed" <<<"$out"'

# --- no notice-severity alerts firing ---------------------------------------
write_fake_curl '[
  {"labels": {"alertname": "Job failed", "severity": "page"}, "annotations": {"summary": "x"}, "startsAt": "2026-09-09T10:16:00Z"}
]' 0
out="$(run_notices)"
check "notices: none when nothing at notice severity" '[ "$out" = "notices: none" ]'

# --- empty alerts array -------------------------------------------------------
write_fake_curl '[]' 0
out="$(run_notices)"
check "notices: none on an empty alerts array" '[ "$out" = "notices: none" ]'

# --- curl fails ---------------------------------------------------------------
write_fake_curl '' 22
out="$(run_notices)"
check "notices: unavailable on a failed curl" '[ "$out" = "notices: unavailable (grafana api)" ]'

# --- capsule-secret fails (no password) --------------------------------------
cat >"$FAKEBIN/capsule-secret" <<'SH'
#!/usr/bin/env bash
exit 1
SH
chmod +x "$FAKEBIN/capsule-secret"
write_fake_curl '[]' 0
out="$(run_notices)"
check "notices: unavailable when capsule-secret fails" '[ "$out" = "notices: unavailable (grafana api)" ]'

# --- the flag gates the section itself, not just its contents ----------------
# The `## Notices` block is assembled inline in report.sh's digest; extract
# just that conditional and drive it with a stub notices_section, so the
# assertion is about the flag and nothing else.
FLAG_SRC="$(sed -n '/# Notices: flagged/,/^  fi$/p' "$REPO/maintenance/report.sh")"
check "the Notices conditional was found in report.sh" '[ -n "$FLAG_SRC" ] && grep -q "OPS_ALERT_SEVERITY_SPLIT" <<<"$FLAG_SRC"'

run_digest_flag() {  # run_digest_flag <flag value>
  ( export OPS_ALERT_SEVERITY_SPLIT="$1"
    DIGEST="## Heal
heal: all ok"
    notices_section() { echo "- Disk over 85 %: laptop disk over 85 % (since 10:15)"; }
    eval "$FLAG_SRC"
    printf '%s\n' "$DIGEST" )
}

out="$(run_digest_flag 0)"
check "flag 0: the digest has NO ## Notices section" '! grep -q "## Notices" <<<"$out"'
check "flag 0: no notice line leaks in either" '! grep -q "Disk over 85" <<<"$out"'

out="$(run_digest_flag 1)"
check "flag 1: the digest has a ## Notices section" 'grep -q "^## Notices$" <<<"$out"'
check "flag 1: the section carries notices_section output" 'grep -q "^- Disk over 85 %: laptop disk over 85 % (since 10:15)$" <<<"$out"'
check "flag 1: Notices comes after Heal" '[ "$(grep -n "## Heal" <<<"$out" | cut -d: -f1)" -lt "$(grep -n "## Notices" <<<"$out" | cut -d: -f1)" ]'

# --- retention digest line: reads state/retention.json, one line after
# ## Heal, only while the flag is still dry-run ------------------------------
RETENTION_SRC="$(sed -n '/# Retention: dry-run flag reminder/,/^\$RETENTION_LINE"$/p' "$REPO/maintenance/report.sh")"
check "the Retention block was found in report.sh" '[ -n "$RETENTION_SRC" ] && grep -q "RETENTION_LINE" <<<"$RETENTION_SRC"'

run_digest_retention() {  # run_digest_retention <mode> <files> <rows>
  local mode="$1" files="$2" rows="$3"
  local rt="$T/retention-$mode-$files-$rows"
  mkdir -p "$rt/state"
  printf '{"mode": "%s", "files": %s, "rows": %s, "at": "2026-09-09T00:00:00Z"}\n' "$mode" "$files" "$rows" >"$rt/state/retention.json"
  ( STATE_DIR="$rt/state"
    DIGEST="## Heal
heal: all ok"
    eval "$RETENTION_SRC"
    printf '%s\n' "$DIGEST" )
}

out="$(run_digest_retention dry-run 2 3)"
check "mode dry-run: the digest carries the exact retention line" 'grep -q "^retention: dry-run (5 pending)$" <<<"$out"'
check "mode dry-run: retention line comes after Heal" '[ "$(grep -n "## Heal" <<<"$out" | cut -d: -f1)" -lt "$(grep -n "^retention: dry-run" <<<"$out" | cut -d: -f1)" ]'

out="$(run_digest_retention applied 2 3)"
check "mode applied: no retention line in the digest" '! grep -q "^retention:" <<<"$out"'

out="$(run_digest_retention dry-run 0 0)"
check "mode dry-run, nothing pending: line still renders with 0" 'grep -q "^retention: dry-run (0 pending)$" <<<"$out"'

echo; if [ $fails -eq 0 ]; then echo "test_report_notices: all passed"; else echo "test_report_notices: $fails failed"; exit 1; fi
