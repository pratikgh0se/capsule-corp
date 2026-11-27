#!/usr/bin/env bash
# capsule-park.sh against a fake launchctl: the two lists, the stamp, unpark.
# Never calls real launchctl, never runs a real backup, never touches ~.
set -u
# P03: CAPSULE_LAUNCHD_PREFIX=dev.capsule drives the prefixed jobs; the notes-vault and ollama-serve labels are literal in their templates
export CAPSULE_LAUNCHD_PREFIX=dev.capsule
perm() { if stat -c %a / >/dev/null 2>&1; then stat -c %a "$1"; else stat -f %Lp "$1"; fi; }  # GNU stat first (its -f means filesystem), else BSD/macOS
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
# hermetic: the committed launchd/unloaded (lists the automation gateway, C02) must not shape these checks
export CAPSULE_LAUNCHD_UNLOADED="$T/no-unloaded-file"
# the obsidian-*/ollama-serve labels carry the overlay prefix lib_jobs reads off the templates
OP="$(bash -c 'REPO="$1"; . "$1/bin/lib_jobs.sh"; printf %s "$_OP"' _ "$REPO")"
fails=0; check() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; fails=$((fails+1)); fi; }
# park boots out the 9 notes-vault labels plus one tick label per served board that has a template (D269). The served
# boards come from maintenance/repos.json, which is the maintainer's own registry: the public copy has none, so there
# the tick count is 0 and the stamp holds 9 labels, not 11. The expected count is derived the way lib_jobs.sh derives it.
NTICK="$(bash -c 'REPO="$1"; . "$1/bin/lib_jobs.sh"; printf %s "${#_TICKS[@]}"' _ "$REPO")"
NLAB=$((9 + NTICK))

mkdir -p "$T/bin" "$T/state"
cat >"$T/bin/launchctl" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$LC_LOG"
[ "${FAKE_LC_FAIL_LABEL:-}" != "" ] && case "$*" in *"$FAKE_LC_FAIL_LABEL"*) exit 3 ;; esac
exit 0
EOF
chmod +x "$T/bin/launchctl"
# A fake capsule-secret: records argv, exits 0 unless FAKE_CS_FAIL=1. This is
# what keeps `unpark` hermetic - the real bin/capsule-secret reads
# ~/.config/capsule/identities and reaches the network, and this test must
# never touch either.
cat >"$T/bin/capsule-secret" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$CS_LOG"
[ "${FAKE_CS_FAIL:-}" = "1" ] && exit 1
exit 0
EOF
chmod +x "$T/bin/capsule-secret"
export LC_LOG="$T/lc.log" LAUNCHCTL_BIN="$T/bin/launchctl" \
       CS_LOG="$T/cs.log" CAPSULE_SECRET_BIN="$T/bin/capsule-secret" \
       AGENT_OPS_STATE_DIR="$T/state" CAPSULE_PARK_BACKUP=0 AGENT_OPS_NO_SEND=1

: >"$LC_LOG"; out="$(bash "$REPO/bin/capsule-park.sh" park 2>&1)"; rc=$?
check "park exits 0" '[ $rc -eq 0 ]'
for l in obsidian-sprint-plan obsidian-sprint-sync obsidian-nightly obsidian-morning \
         obsidian-inbox obsidian-architect obsidian-weekly \
         obsidian-agent-review obsidian-retrieval-eval; do
  check "park boots out $OP.$l" "grep -q 'bootout .*$OP.$l\$' \"\$LC_LOG\""
done
for l in capsule-corp-heal capsule-corp-backup capsule-corp-restore-verify capsule-corp-cleanup \
         capsule-corp-obs-probe capsule-corp-obs-inventory \
         capsule-corp-deps-sync; do
  check "park leaves dev.capsule.$l alone" "! grep -q 'dev.capsule.$l\$' \"\$LC_LOG\""
done
for l in obsidian-health obsidian-report obsidian-security ollama-serve; do
  check "park leaves $OP.$l alone" "! grep -q '$OP.$l\$' \"\$LC_LOG\""
done
check "park touches no gateway label (the gateway class is retired, D229)" '! grep -q "ai.example.gateway" "$LC_LOG"'
check "park writes a 0600 stamp naming the labels it booted out" \
  '[ "$(perm "$T/state/park.json")" = "600" ] && [ "$(python3 -c "import json;print(len(json.load(open(\"$T/state/park.json\"))[\"labels\"]))")" = "$NLAB" ]'
check "park holds the absence tier at 3 and prints the off-machine checklist" \
  '[ "$(python3 -c "import json;print(json.load(open(\"$T/state/absence.json\"))[\"held\"])")" = "3" ] && grep -qi "external disk" <<<"$out" && grep -qi "push every repo" <<<"$out"'
check "the push checklist item is prose, not a runnable command" \
  '! grep -qE "git[[:space:]]+(-C|push|fetch|pull)" <<<"$out"'
check "park says what it did NOT do" 'grep -qiF "this script does not push" <<<"$out"'

: >"$LC_LOG"; out2="$(bash "$REPO/bin/capsule-park.sh" park --quiet-phone 2>&1)"; rc2=$?
check "--quiet-phone is gone: rejected with usage, nothing booted out, stamp untouched" \
  '[ $rc2 -eq 2 ] && grep -q "^usage:" <<<"$out2" && ! grep -q "quiet-phone" <<<"$out2" && [ ! -s "$LC_LOG" ] && [ "$(python3 -c "import json;print(len(json.load(open(\"$T/state/park.json\"))[\"labels\"]))")" = "$NLAB" ]'

out3="$(bash "$REPO/bin/capsule-park.sh" status 2>&1)"
check "status reports the park" '[ $? -eq 0 ] && grep -q "parked since" <<<"$out3"'

: >"$LC_LOG"; : >"$CS_LOG"; out4="$(bash "$REPO/bin/capsule-park.sh" unpark 2>&1)"; rc=$?
check "unpark bootstraps exactly what park booted out and clears the stamp" \
  '[ $rc -eq 0 ] && [ "$(grep -c bootstrap "$LC_LOG")" = "$NLAB" ] && [ ! -e "$T/state/park.json" ]'
check "unpark clears the held tier and says it is done" \
  '[ ! -e "$T/state/absence.json" ] && grep -qF "capsule unpark: done." <<<"$out4" && ! grep -qF "deploy.sh" <<<"$out4"'
check "unpark checks every Potara identity still reads (M222)" 'grep -qF "capsule-secret check --identity-all" <<<"$out4"'
check "unpark calls the fake capsule-secret, never the real one" \
  'grep -qF "check --identity-all" "$CS_LOG"'

: >"$CS_LOG"; out4b="$(FAKE_CS_FAIL=1 bash "$REPO/bin/capsule-park.sh" unpark 2>&1)"; rc=$?
check "a failing identity check does not crash unpark" '[ $rc -eq 0 ]'
check "unpark reports the identity-check failure" 'grep -qiF "FAILED" <<<"$out4b"'

# C9 fix wave 1 item 4: a failed absence.py call must be visible, not
# swallowed by `|| true` - park/unpark still finish (exit 0), but the
# operator sees the manual fallback command.
cat >"$T/fake_absence_fail.py" <<'EOF'
import sys
sys.exit(1)
EOF
: >"$LC_LOG"
out7="$(ABSENCE_PY_BIN="$T/fake_absence_fail.py" bash "$REPO/bin/capsule-park.sh" park 2>&1)"; rc=$?
check "a failing absence.py does not crash park" '[ $rc -eq 0 ]'
check "park reports the absence.py failure with a manual fallback command" \
  'grep -qiF "absence.py hold 3 FAILED" <<<"$out7" && grep -qiF "run manually" <<<"$out7"'

: >"$LC_LOG"; : >"$CS_LOG"
out8="$(ABSENCE_PY_BIN="$T/fake_absence_fail.py" bash "$REPO/bin/capsule-park.sh" unpark 2>&1)"; rc=$?
check "a failing absence.py does not crash unpark" '[ $rc -eq 0 ]'
check "unpark reports the absence.py failure with a manual fallback command" \
  'grep -qiF "absence.py clear FAILED" <<<"$out8" && grep -qiF "run manually" <<<"$out8"'

: >"$LC_LOG"; out5="$(FAKE_LC_FAIL_LABEL=obsidian-nightly bash "$REPO/bin/capsule-park.sh" park 2>&1)"; rc=$?
check "an already-unloaded label does not fail the park" '[ $rc -eq 0 ] && grep -q "obsidian-nightly" <<<"$out5"'

out6="$(bash "$REPO/bin/capsule-park.sh" 2>&1)"; rc=$?
check "no verb: usage, exit 2" '[ $rc -eq 2 ] && grep -q "usage: capsule-park.sh" <<<"$out6"'

check "every launchd template is classified in one of the two lists" \
  'missing=""; for f in "$REPO"/launchd/templates/*.plist; do l="$(basename "$f" .plist)"; case "$l" in __PREFIX__.capsule-tick-*) continue ;; __PREFIX__*) l="}${l#__PREFIX__}" ;; *.obsidian-*) l="}.obsidian-${l#*.obsidian-}" ;; *.ollama-serve) l=".ollama-serve" ;; esac; grep -qF "$l" "$REPO/bin/capsule-park.sh" "$REPO/bin/lib_jobs.sh" "$REPO/bin/lib_prefix.sh" || missing="$missing $l"; done; [ -z "$missing" ] || { echo "unclassified:$missing"; false; }'
check "every per-board tick template is classified (labels derived from served boards, D269)" '[ -z "$(REPO="$REPO" bash -c ". \"\$REPO/bin/lib_jobs.sh\"; classify_check" 2>&1)" ]'

# Tick-label checks run a fixture copy with a neutral board (sample-app): they never read the real repos.json or the export-excluded templates.
FX="$T/fx"; mkdir -p "$FX/launchd" "$FX/maintenance" && cp -R "$REPO/bin" "$FX/bin" && cp -R "$REPO/launchd/templates" "$FX/launchd/templates" && ln -s "$REPO/capsule" "$FX/capsule"
: >"$FX/launchd/templates/__PREFIX__.capsule-tick-sample-app.plist"
printf '[{"name":"a","board":"capsule-corp"},{"name":"b","board":"sample-app"}]' >"$T/repos.json"
export AGENT_OPS_REPOS_JSON="$T/repos.json"

# factory-autonomy Task 11: the ticks are dispatching jobs; (Q10) an unloaded label is never restored
bash "$FX/bin/capsule-park.sh" unpark >/dev/null 2>&1
: >"$LC_LOG"; bash "$FX/bin/capsule-park.sh" park >/dev/null 2>&1
check "park boots out both tick labels" 'grep -q "bootout .*dev.capsule.capsule-tick-sample-app$" "$LC_LOG" && grep -q "bootout .*dev.capsule.capsule-tick-capsule-corp$" "$LC_LOG"'
: >"$LC_LOG"; bash "$FX/bin/capsule-park.sh" unpark >/dev/null 2>&1
check "unpark bootstraps both tick labels" 'grep -q "bootstrap .*dev.capsule.capsule-tick-sample-app.plist$" "$LC_LOG" && grep -q "bootstrap .*dev.capsule.capsule-tick-capsule-corp.plist$" "$LC_LOG"'
printf 'dev.capsule.capsule-tick-capsule-corp\n' >"$T/unloaded"
CAPSULE_LAUNCHD_UNLOADED="$T/unloaded" bash "$FX/bin/capsule-park.sh" park >/dev/null 2>&1
: >"$LC_LOG"; CAPSULE_LAUNCHD_UNLOADED="$T/unloaded" bash "$FX/bin/capsule-park.sh" unpark >/dev/null 2>&1
check "unpark never bootstraps an unloaded label" '! grep -q "dev.capsule.capsule-tick-capsule-corp" "$LC_LOG"'
# row 526 (card t_1758e0bd) item 2: a trailing space or CR on the unloaded label does not defeat it
printf 'dev.capsule.capsule-tick-capsule-corp \r\n' >"$T/unloaded-ws"
CAPSULE_LAUNCHD_UNLOADED="$T/unloaded-ws" bash "$FX/bin/capsule-park.sh" park >/dev/null 2>&1
: >"$LC_LOG"; CAPSULE_LAUNCHD_UNLOADED="$T/unloaded-ws" bash "$FX/bin/capsule-park.sh" unpark >/dev/null 2>&1
check "unpark never bootstraps an unloaded label written with a trailing space and CR" \
  'grep -q "bootstrap .*dev.capsule.capsule-tick-sample-app.plist$" "$LC_LOG" && ! grep -q "dev.capsule.capsule-tick-capsule-corp" "$LC_LOG"'

# factory-autonomy Task 13 (round 2 finding 5): a stamp written BEFORE a label is added to
# launchd/unloaded must not bootstrap that label back on unpark.
printf '' >"$T/none"
CAPSULE_LAUNCHD_UNLOADED="$T/none" bash "$FX/bin/capsule-park.sh" park >/dev/null 2>&1
printf 'dev.capsule.capsule-tick-capsule-corp\n' >"$T/unloaded-late"
: >"$LC_LOG"; CAPSULE_LAUNCHD_UNLOADED="$T/unloaded-late" bash "$FX/bin/capsule-park.sh" unpark >/dev/null 2>&1
check "unpark filters a pre-existing stamp through the unloaded list" \
  'grep -q "bootstrap .*dev.capsule.capsule-tick-sample-app.plist$" "$LC_LOG" && ! grep -q "dev.capsule.capsule-tick-capsule-corp" "$LC_LOG"'

# An old stamp from a `--quiet-phone` park (gateway class since retired) must not bootstrap gateway labels.
printf '{"v":1,"parked_since":1,"labels":["ai.example.gateway-slack","ai.example.gateway-automation","%s.obsidian-sprint-sync","%s.obsidian-nightly"],"quiet_phone":true}\n' "$OP" "$OP" >"$T/state/park.json"
outs="$(CAPSULE_LAUNCHD_UNLOADED="$T/none" bash "$REPO/bin/capsule-park.sh" status 2>&1)"
check "status counts only labels park can boot out (2 of the 4 in an old stamp)" 'grep -q "(2 jobs stopped" <<<"$outs"'
: >"$LC_LOG"; CAPSULE_LAUNCHD_UNLOADED="$T/none" bash "$REPO/bin/capsule-park.sh" unpark >/dev/null 2>&1
check "unpark of an old quiet-phone stamp bootstraps no ai.example label, only the kept ones" \
  '! grep -q "ai.example" "$LC_LOG" && [ "$(grep -c bootstrap "$LC_LOG")" = "2" ]'

echo; if [ $fails -eq 0 ]; then echo "test_capsule_park: all passed"; else echo "test_capsule_park: $fails failed"; exit 1; fi
