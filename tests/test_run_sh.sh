#!/usr/bin/env bash
# mutation-check: targets-predate-range ff625e94 -- fixture rename only, no behaviour pinned ::
# Exercises run.sh's dry-run path end to end: the job-start and job-end
# events land in the spool (redirected via EVENTS_SPOOL so this never
# touches the real state/events.jsonl), and the dry-run marker still prints.
set -u
unset CAPSULE_JOBS_DIR   # the jobs config dir comes from each case's fake home
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
T="$(mktemp -d "${TMPDIR:-/tmp}/run-sh.XXXXXX")"; trap 'rm -rf "$T"' EXIT
export EVENTS_SPOOL="$T/events.jsonl" AGENT_OPS_STATE_DIR="$T/state-root"
# Same overrides run.sh honours - CI has no legacy agent venv (plain python3
# is enough for events.py), no deploy.sh-created maintenance symlink, and no
# real notes vault at its default path (run.sh cd's into it even in a dry run).
_VENV_PY="$HOME/.legacy-agent/venv/bin/python"
[ -x "$_VENV_PY" ] || _VENV_PY=python3   # fresh HOME / CI: no legacy venv
export CAPSULE_OPS_PY="${CAPSULE_OPS_PY:-$_VENV_PY}"
export RUN_SH_MAINT_DIR="${RUN_SH_MAINT_DIR:-$REPO/maintenance}"
mkdir -p "$T/vault"
# Pinned unconditionally, not with a `${VAR:-...}` default: a caller with
# the vault path variable already exported to some other (non-git, non-fixture)
# directory must not leak into this suite's dry-run checks.
export OBSIDIAN_VAULT_PATH="$T/vault"
# The notes-vault skill checkout is opt-in too (no default directory): a scratch one, never the real checkout.
export OBSIDIAN_SKILL_ROOT="$T/skill"; mkdir -p "$OBSIDIAN_SKILL_ROOT"
# Same for AGENT_OPS_NO_SEND: this suite only exercises run.sh's dry-run path
# (RUN_SH_DRY_RUN=1), which never sends anything, so it must not depend on
# whatever the caller's shell had set.
unset AGENT_OPS_NO_SEND
# run.sh rewrites PATH, so a fake on PATH loses to the real tool. RUN_SH_PATH_PREPEND wins over
# it: claude/curl/gh/docker are tripwires, and the weekly job's `capsule unblocked` capture is
# pointed at a checkout that does not exist instead of the real repos.
export RUN_SH_CAPSULE_CORP_DIR="$T/no-such-checkout" RUN_SH_PATH_PREPEND="$T/tripwire"
export TRIPWIRE_MARK="$T/tripwire.fired"
mkdir -p "$T/tripwire"
for _tw in claude curl gh docker; do
  printf '#!/bin/sh\necho "%s $*" >>"$TRIPWIRE_MARK"\nexit 0\n' "$_tw" >"$T/tripwire/$_tw"
  chmod +x "$T/tripwire/$_tw"
done

fails=0
check() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; fails=$((fails+1)); fi; }

# The notes-vault prompts (nightly.md, weekly.md, sprint-plan.md, security.md, ...) are private-only: the
# public copy drops them (D254, the notes vault is optional). A check that reads or runs one of them skips,
# with a printed reason, when the prompt is absent. With the prompt present it runs exactly as before.
# the private tree (marked by its denylist) always runs these checks, so a lost prompt still fails there
have_prompt() { [ -f "$REPO/release/private-denylist.txt" ] && return 0
  local _p; for _p in "$@"; do [ -f "$REPO/maintenance/$_p.md" ] || return 1; done; }
pcheck() {  # pcheck <prompt> <name> <command>
  local _need="$1"; shift
  if have_prompt "$_need"; then check "$@"; else echo "SKIP $1: maintenance/$_need.md is private-only (not in the public copy)"; fi
}

out="$(RUN_SH_DRY_RUN=1 bash "$REPO/maintenance/run.sh" nightly 2>&1)"
rc=$?
pcheck nightly "dry run exits 0" '[ $rc -eq 0 ]'
pcheck nightly "dry run prints the marker" 'grep -q "dry run: nothing executed, nothing committed" <<<"$out"'
pcheck nightly "dry run argv line requests json output" 'grep -q "argv:.*--output-format json" <<<"$out"'
# 2026-09-09: nightly owns the semantic reindex now, and a full rebuild after a
# chunker change takes 10-12 min - the old shared 900s cap could kill it
# mid-rebuild, which leaves a half-written index and no error anyone reads.
pcheck nightly "nightly runs under a 30-minute cap" 'grep -q "cap=1800s" <<<"$out"'
# R11: the nightly trims labelled capsule-layer images; the dry run only names the step (it must
# never reach docker), and the switch defaults to dry-run (OPS_IMAGE_GC_APPLY unset reads 0).
pcheck nightly "nightly dry run names the image GC with its switch off" 'grep -q "would run image_gc.py (OPS_IMAGE_GC_APPLY=0)" <<<"$out"'
out_gc="$(OPS_IMAGE_GC_APPLY=1 RUN_SH_DRY_RUN=1 EVENTS_SPOOL="$T/gc-events.jsonl" bash "$REPO/maintenance/run.sh" nightly 2>&1)"
pcheck nightly "nightly dry run shows the image GC switch when set" 'grep -q "would run image_gc.py (OPS_IMAGE_GC_APPLY=1)" <<<"$out_gc"'
check "the nightly image GC runs under a 300s cap and never fails the job" 'grep -q "JOB_CAP_SECONDS=300" "$REPO/maintenance/run.sh" && grep -q "image_gc.py exit .* (ignored)" "$REPO/maintenance/run.sh"'
check "only the nightly job runs the image GC" '[ "$(RUN_SH_DRY_RUN=1 EVENTS_SPOOL="$T/gc2.jsonl" bash "$REPO/maintenance/run.sh" quality-nightly 2>&1 | grep -c image_gc)" = 0 ]'
pcheck nightly "spool has two job rows" '[ "$(grep -c "\"kind\": \"job\"" "$EVENTS_SPOOL" 2>/dev/null)" = 2 ]'
pcheck nightly "spool has a job start row" 'grep -q "\"phase\": \"start\"" "$EVENTS_SPOOL"'
pcheck nightly "spool has a job end row" 'grep -q "\"phase\": \"end\"" "$EVENTS_SPOOL"'

# The rest of the cap matrix, on a throwaway spool so the row counts above
# stay about the one nightly run they are asserting.
T_CAP="$(mktemp -d "${TMPDIR:-/tmp}/run-sh.XXXXXX")"
cap_of() {  # <job> [JOB_CAP_SECONDS override] -> the "cap=<n>s" the dry run printed
  env EVENTS_SPOOL="$T_CAP/events.jsonl" RUN_SH_DRY_RUN=1 ${2:+JOB_CAP_SECONDS="$2"} \
    bash "$REPO/maintenance/run.sh" "$1" 2>&1 | grep -o "cap=[0-9]*s" | head -1
}
pcheck nightly "the nightly cap is overridable" '[ "$(cap_of nightly 42)" = "cap=42s" ]'
pcheck weekly "a job with no arm of its own keeps the 15-minute default" '[ "$(cap_of weekly)" = "cap=900s" ]'

out="$(RUN_SH_DRY_RUN=1 bash "$REPO/maintenance/run.sh" quality-nightly 2>&1)"
check "quality-nightly dry run exits 0" '[ $? -eq 0 ]'
check "quality-nightly runs under a 60-minute cap" 'grep -q "cap=3600s" <<<"$out"'
check "quality-nightly prints no model line" '! grep -q "model claude" <<<"$out"'
check "quality-nightly dry run names the script" 'grep -q "quality_nightly.py" <<<"$out"'

# inbox skips (and prints no cap line) when there is nothing to triage, so its
# 40-minute arm is asserted against the case statement itself.
check "inbox and agent-review keep their 40-minute arm" 'grep -qF "inbox|agent-review) JOB_CAP_SECONDS=\"\${JOB_CAP_SECONDS:-2400}\"" "$REPO/maintenance/run.sh"'
check "nightly has an arm of its own at 1800" 'grep -qF "nightly)            JOB_CAP_SECONDS=\"\${JOB_CAP_SECONDS:-1800}\"" "$REPO/maintenance/run.sh"'
rm -rf "$T_CAP"

# 2026-09-09 (row 124): the reindex used to say only that a build ran. Phase 6
# prints --stats after --build so the job's own output carries the coverage
# number the Phase 5 log line is supposed to report.
pcheck nightly "nightly phase 6 prints the index stats" 'grep -q -- "--stats" "$REPO/maintenance/nightly.md"'
pcheck nightly "nightly phase 6 still builds the index first" \
  '[ "$(grep -n -- "--build" "$REPO/maintenance/nightly.md" | head -1 | cut -d: -f1)" -lt "$(grep -n -- "--stats" "$REPO/maintenance/nightly.md" | head -1 | cut -d: -f1)" ]'

# --- session_id_from_json / result_from_json: the --output-format json extractors
# run.sh's run_claude_once uses to build the `claude -r <session_id>` resume pointer.
# Sourced straight out of run.sh (between its marker comments) rather than
# re-declared here, so this test exercises the shipped functions - a copy
# could silently drift or break while the test kept passing (I4, 2026-09-06).
# RAW_OUTPUT is claude's stdout+stderr merged, so both extractors try
# json.loads on the whole blob first (a pretty-printed / multi-line envelope
# needs this), then fall back to a line-by-line scan for the LAST line that
# parses as a JSON object carrying the key they want.
HELPERS="$(sed -n '/# --- json helpers start ---/,/# --- json helpers end ---/p' "$REPO/maintenance/run.sh")"
check "run.sh carries the json-helpers marker block" '[ -n "$HELPERS" ]'
eval "$HELPERS"

SAMPLE_JSON='{"session_id":"abc","result":"hello"}'
SID="$(printf '%s' "$SAMPLE_JSON" | session_id_from_json)"
check "session_id_from_json returns the id" '[ "$SID" = "abc" ]'
RES="$(printf '%s' "$SAMPLE_JSON" | result_from_json)"
check "result_from_json prints result" '[ "$RES" = "hello" ]'

NONJSON="not json at all"
FALLBACK="$(printf '%s' "$NONJSON" | result_from_json)"
check "result_from_json falls back to raw text on non-JSON" '[ "$FALLBACK" = "$NONJSON" ]'

# A warning line on stderr before the JSON envelope (RAW_OUTPUT merges stdout+stderr):
# both extractors must skip it and find the JSON line, not choke on the whole blob.
NOISY_BLOB="$(printf 'WARN: something noisy on stderr\n%s\n' "$SAMPLE_JSON")"
SID2="$(printf '%s' "$NOISY_BLOB" | session_id_from_json)"
check "session_id_from_json skips a leading warning line" '[ "$SID2" = "abc" ]'
RES2="$(printf '%s' "$NOISY_BLOB" | result_from_json)"
check "result_from_json skips a leading warning line" '[ "$RES2" = "hello" ]'

# `"result": ""` with the session-limit text elsewhere in the blob (e.g. on
# stderr, outside the JSON envelope): result_from_json must fall back to the
# raw text so the session-limit grep downstream still sees it, rather than
# handing back an empty CLAUDE_OUTPUT that hides it.
LIMIT_BLOB="$(printf 'You'\''ve hit your session limit - resets 3:00pm\n{"session_id":"abc","result":""}\n')"
RES3="$(printf '%s' "$LIMIT_BLOB" | result_from_json)"
check "result_from_json falls back to raw text when result is empty" \
  'grep -qi "hit your session limit" <<<"$RES3"'

# I4: `claude -p --output-format json` can emit a pretty-printed (multi-line)
# envelope - no single line parses on its own, so the line scan alone would
# silently return an empty session id / raw blob. The whole-blob json.loads
# attempted first must catch this shape.
PRETTY_JSON="$(printf '{\n  "session_id": "pretty-id",\n  "result": "pretty-result"\n}\n')"
SID4="$(printf '%s' "$PRETTY_JSON" | session_id_from_json)"
check "session_id_from_json parses a pretty-printed multi-line envelope" '[ "$SID4" = "pretty-id" ]'
RES4="$(printf '%s' "$PRETTY_JSON" | result_from_json)"
check "result_from_json parses a pretty-printed multi-line envelope" '[ "$RES4" = "pretty-result" ]'

# --target flag (run.sh's tg_send parses --to out of "$@" and passes it through
# as --target): exercise events.py's CLI directly against a scratch spool.
TARGET_SPOOL="$T/target-events.jsonl"
EVENTS_SPOOL="$TARGET_SPOOL" "$CAPSULE_OPS_PY" "$REPO/maintenance/events.py" \
  log message run.sh x --target "telegram:1:2"
check "--target flag lands in payload.target" \
  'python3 -c "import json,sys;d=json.loads(open(sys.argv[1]).read().splitlines()[0]);sys.exit(0 if d[\"payload\"][\"target\"]==\"telegram:1:2\" else 1)" "$TARGET_SPOOL"'


# --- observability -> heal: the Docker-down alert/throttle/state logic moved
# into maintenance/heal.sh (see tests/test_heal.sh); run.sh's own job now just
# execs heal.sh (dry-run marker check below) and refuses the retired job name
# `observability` outright (also checked below).
#
# The fake HOME below (FH/FM) is still needed further down by the fork-sync
# tests, which run.sh's real (non-dry) path against throwaway local repos.
FH="$T/fakehome"; FM="$FH/.config/capsule-corp/maintenance"
mkdir -p "$FM" "$FH/.legacy-agent/venv/bin" "$FH/vault"
cp "$REPO/maintenance/run.sh" "$REPO/maintenance/lib_state.sh" "$REPO/maintenance/lib_config.sh" "$REPO/maintenance/lib_vault.sh" "$REPO/maintenance/lib_docker_engine.sh" "$REPO/maintenance/docker_engine.py" "$REPO/maintenance/events.py" "$FM/"
ln -sf "$(command -v python3)" "$FH/.legacy-agent/venv/bin/python"

# --- morning / health-check: deterministic (vault_jobs.py), no Claude session ---
out="$(RUN_SH_DRY_RUN=1 bash "$REPO/maintenance/run.sh" morning 2>&1)"; rc=$?
check "morning dry run exits 0 without a model" '[ $rc -eq 0 ] && grep -q "dry run: would run vault_jobs.py morning" <<<"$out" && ! grep -q "claude" <<<"$out"'
out="$(RUN_SH_DRY_RUN=1 bash "$REPO/maintenance/run.sh" health-check 2>&1)"; rc=$?
check "health-check dry run exits 0 without a model" '[ $rc -eq 0 ] && grep -q "dry run: would run vault_jobs.py health" <<<"$out"'
check "retired prompts are gone" '[ ! -e "$REPO/maintenance/morning.md" ] && [ ! -e "$REPO/maintenance/health-check.md" ] && [ ! -e "$REPO/maintenance/report.md" ]'
check "report.sh no longer calls claude" '! grep -q "claude -p" "$REPO/maintenance/report.sh" && grep -q "vault_jobs.py\" digest" "$REPO/maintenance/report.sh"'
FIX="$(mktemp -d "${TMPDIR:-/tmp}/run-sh.XXXXXX")"; trap 'rm -rf "$T"; find "$FIX" -delete' EXIT; mkdir -p "$FIX/Boards" "$FIX/Knowledge" "$FIX/Projects"; printf '## 🔨 In Progress\n\n- [ ] **X**\n\n## ✅ Done\n' >"$FIX/Boards/Engineering.md"
check "digest has the shape guard string" 'python3 "$REPO/maintenance/vault_jobs.py" digest --vault "$FIX" | grep -q "BOARD STANDUP"'

# --- backup / restore-verify: pure bash, no Claude session ---
out="$(RUN_SH_DRY_RUN=1 bash "$REPO/maintenance/run.sh" backup 2>&1)"; rc=$?
check "backup dry run exits 0 without a model" '[ $rc -eq 0 ] && grep -q "dry run: would run backup.sh" <<<"$out" && ! grep -q "claude" <<<"$out"'
out="$(RUN_SH_DRY_RUN=1 bash "$REPO/maintenance/run.sh" restore-verify 2>&1)"; rc=$?
check "restore-verify dry run exits 0 without a model" '[ $rc -eq 0 ] && grep -q "dry run: would run restore.sh --latest --verify" <<<"$out" && ! grep -q "claude" <<<"$out"'

# I9: both jobs run under with_cap so JOB_CAP_SECONDS actually applies to
# them (the case arm at run.sh:82 was dead code otherwise)
check "backup.sh is invoked through with_cap" 'grep -qF "with_cap bash \"\$MAINT_DIR/backup.sh\"" "$REPO/maintenance/run.sh"'
check "restore.sh --latest --verify is invoked through with_cap" 'grep -qF "with_cap bash \"\$MAINT_DIR/restore.sh\" --latest --verify" "$REPO/maintenance/run.sh"'

# I5: backup and restore-verify must be in status.sh's CADENCE dict so they
# can appear (and go OVERDUE) on the one-screen status view
check "status.sh CADENCE lists backup" 'grep -qF "\"backup\": 86400" "$REPO/maintenance/status.sh"'
check "status.sh CADENCE lists restore-verify" 'grep -qF "\"restore-verify\": 7*86400" "$REPO/maintenance/status.sh"'

# --- ledger-reconcile: retired 2026-10-24 (W341, legacy-only), so run.sh must not carry it ---
out="$(RUN_SH_DRY_RUN=1 bash "$REPO/maintenance/run.sh" ledger-reconcile 2>&1)"; rc=$?
check "ledger-reconcile is no longer a run.sh job" '[ $rc -ne 0 ] && ! grep -q "ledger_reconcile" "$REPO/maintenance/run.sh" && ! grep -q "ledger-reconcile" "$REPO/maintenance/status.sh"'

# --- deps-sync: pure bash+python, no Claude session (A5 task 3, row 217) ---
out="$(RUN_SH_DRY_RUN=1 bash "$REPO/maintenance/run.sh" deps-sync 2>&1)"; rc=$?
check "deps-sync dry run exits 0 without a model" '[ $rc -eq 0 ] && grep -q "dry run: would run deps_sync.sh" <<<"$out" && ! grep -q "claude" <<<"$out"'
check "deps-sync is in the usage line" 'grep -q "deps-sync" <<<"$(grep "JOB=\"\${1:?usage:" "$REPO/maintenance/run.sh")"'
check "deps-sync is exempt from the prompt-file check" 'grep -qF "\$JOB\" != \"deps-sync\"" "$REPO/maintenance/run.sh"'
check "deps_sync.sh is invoked" 'grep -qF "bash \"\$MAINT_DIR/deps_sync.sh\"" "$REPO/maintenance/run.sh"'
check "status.sh CADENCE lists deps-sync" 'grep -qF "\"deps-sync\": 86400" "$REPO/maintenance/status.sh"'

# Round 2 NEW-3: run.sh forwards deps-sync's own flags (--check, --force) -
# the operator's escape hatch is `maintenance/run.sh deps-sync --force`; before this fix the
# args were silently dropped.
DS_T="$(mktemp -d "${TMPDIR:-/tmp}/run-sh.XXXXXX")"
cat >"$DS_T/deps_sync.sh" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >"$DS_ARGV_LOG"
exit 0
EOF
chmod +x "$DS_T/deps_sync.sh"
export DS_ARGV_LOG="$DS_T/argv.log"
out_ds="$(RUN_SH_MAINT_DIR="$DS_T" bash "$REPO/maintenance/run.sh" deps-sync --check --force 2>&1)"; rc_ds=$?
check "run.sh deps-sync --check --force forwards both flags to deps_sync.sh" \
  '[ $rc_ds -eq 0 ] && [ "$(cat "$DS_ARGV_LOG")" = "--check --force" ]'
out_ds_dry="$(RUN_SH_DRY_RUN=1 bash "$REPO/maintenance/run.sh" deps-sync --check 2>&1)"; rc_ds_dry=$?
check "deps-sync dry run with a flag still exits 0 and never runs the real script" \
  '[ $rc_ds_dry -eq 0 ] && grep -q "dry run: would run deps_sync.sh --check" <<<"$out_ds_dry"'
rm -rf "$DS_T"

# --- disk-relief (D204): the 4-hourly relief. A scratch MAINT_DIR holds a fake disk_relief.py that
# records its argv, prints an ALERT line and exits with $DR_RC, so nothing real is ever removed.
DR_T="$(mktemp -d "${TMPDIR:-/tmp}/run-sh.XXXXXX")"
cat >"$DR_T/disk_relief.py" <<'EOF'
import os, sys
open(os.environ["DR_ARGV_LOG"], "a").write("disk_relief.py " + " ".join(sys.argv[1:]) + "\n")
print("disk-relief: ALERT only 12.0 GB free, under the 25.0 GB hard floor")
sys.exit(int(os.environ.get("DR_RC", "0")))
EOF
printf '#!/usr/bin/env bash\nprintf "%%s\\n" "$*" >>"$DR_SEND_LOG"\n' >"$DR_T/fakecap"
chmod +x "$DR_T/fakecap"; mkdir "$DR_T/home"
export DR_ARGV_LOG="$DR_T/argv.log" DR_SEND_LOG="$DR_T/send.log"
run_dr() {  # <rc the script returns> [extra env...] -> run.sh's exit code
  local rc="$1"; shift; : >"$DR_ARGV_LOG"; : >"$DR_SEND_LOG"
  env HOME="$DR_T/home" RUN_SH_MAINT_DIR="$DR_T" AGENT_OPS_STATE_DIR="$DR_T/st" CAPSULE_PY="$DR_T/fakecap" \
    DR_RC="$rc" "$@" bash "$REPO/maintenance/run.sh" disk-relief >/dev/null 2>&1
}
run_dr 0; rc_dr0=$?
check "disk-relief runs disk_relief.py with no arguments, stamps 0 and sends nothing on a clean run" \
  '[ $rc_dr0 -eq 0 ] && [ "$(cat "$DR_ARGV_LOG")" = "disk_relief.py " ] && [ ! -s "$DR_SEND_LOG" ]'
# The interpreter knob: CAPSULE_OPS_PY is the only name run.sh reads; CAPSULE_PY plays no part.
printf '#!/usr/bin/env bash\necho "$0" >>"$DR_PY_LOG"\nexec python3 "$@"\n' >"$DR_T/py-new"
chmod +x "$DR_T/py-new"
export DR_PY_LOG="$DR_T/py.log"
: >"$DR_PY_LOG"; run_dr 0 CAPSULE_OPS_PY="$DR_T/py-new"; rc_py2=$?
check "run.sh runs its scripts through CAPSULE_OPS_PY, and CAPSULE_PY plays no part" \
  '[ $rc_py2 -eq 0 ] && grep -qx "$DR_T/py-new" "$DR_PY_LOG"'
run_dr 0 RUN_SH_DRY_RUN=1; rc_dr1=$?
check "disk-relief RUN_SH_DRY_RUN=1 passes --dry-run" '[ $rc_dr1 -eq 0 ] && grep -qx "disk_relief.py --dry-run" "$DR_ARGV_LOG"'
run_dr 0 DRY=1; rc_dr2=$?
check "disk-relief DRY=1 passes --dry-run" '[ $rc_dr2 -eq 0 ] && grep -qx "disk_relief.py --dry-run" "$DR_ARGV_LOG"'
run_dr 3; rc_dr3=$?
check "disk-relief exit 3 sends one incident alert naming the free GB, and the job still exits 0" \
  '[ $rc_dr3 -eq 0 ] && [ "$(wc -l <"$DR_SEND_LOG" | tr -d " ")" = 1 ] && grep -q "outbox-send incident disk-relief: ALERT only 12.0 GB free" "$DR_SEND_LOG"'
run_dr 3 AGENT_OPS_NO_SEND=1; rc_dr4=$?
check "disk-relief alert respects AGENT_OPS_NO_SEND" '[ $rc_dr4 -eq 0 ] && [ ! -s "$DR_SEND_LOG" ]'
run_dr 1; rc_dr5=$?
check "disk-relief exit 1 fails the job and sends no alert of its own" '[ $rc_dr5 -eq 1 ] && [ ! -s "$DR_SEND_LOG" ]'
check "disk-relief is in the usage line, exempt from the prompt-file check, and in status.sh CADENCE" \
  'grep -q "disk-relief" <<<"$(grep "JOB=\"\${1:?usage:" "$REPO/maintenance/run.sh")" && grep -qF "\$JOB\" != \"disk-relief\"" "$REPO/maintenance/run.sh" && grep -qF "\"disk-relief\": 4*3600" "$REPO/maintenance/status.sh"'
rm -r "$DR_T"

# --- ops-preview (row 643): the every-other-day branch-sweep + storage-gc PREVIEW. A scratch
# MAINT_DIR holds the real ops_preview.py (with its events.py/scrub.py imports) next to fake
# branch_sweep.py and storage_gc.py, so no real branch, container or temp dir is ever looked at.
OP_T="$(mktemp -d "${TMPDIR:-/tmp}/run-sh.XXXXXX")"
cp "$REPO/maintenance/ops_preview.py" "$REPO/maintenance/events.py" "$REPO/maintenance/docker_engine.py" "$REPO/maintenance/scrub.py" "$OP_T/"
cat >"$OP_T/branch_sweep.py" <<'EOF'
import os, sys
open(os.environ["OP_ARGV_LOG"], "a").write("branch_sweep.py " + " ".join(sys.argv[1:]) + "\n")
print("kept: branch x (open card)")
print("branch-sweep: dry run, nothing changed; 3 to remove, 5 kept; --apply removes")
EOF
cat >"$OP_T/storage_gc.py" <<'EOF'
import os, sys
open(os.environ["OP_ARGV_LOG"], "a").write("storage_gc.py " + " ".join(sys.argv[1:]) + "\n")
print("storage-gc: container: 1 chosen (10 bytes), 2 kept")
print("storage-gc: volume: 1 chosen (20 bytes), 2 kept")
print("storage-gc: images: 1048576 bytes by image_gc.py (rc 0)")
print("storage-gc: dry-run, nothing changed; --apply removes")
sys.exit(int(os.environ.get("OP_FAIL", "0")))
EOF
printf '#!/usr/bin/env bash\n[ "${1:-}" = "-P" ] && shift\nprintf "%%s\\n" "$*" >>"$OP_SEND_LOG"\n[ -z "${OP_SEND_FAIL:-}" ]\n' >"$OP_T/fakecap"
chmod +x "$OP_T/fakecap"
mkdir "$OP_T/home"   # an empty HOME: no ops.env, so the chat target is only what the test sets
export OP_ARGV_LOG="$OP_T/argv.log" OP_SEND_LOG="$OP_T/send.log"
run_op() {  # <state dir> -> run.sh's exit code
  HOME="$OP_T/home" OPS_ALERT_TARGET="${OP_TARGET-test-chat}" \
  RUN_SH_MAINT_DIR="$OP_T" AGENT_OPS_STATE_DIR="$1" CAPSULE_PY="$OP_T/fakecap" \
    bash "$REPO/maintenance/run.sh" ops-preview >/dev/null 2>&1
}
op_log() { cat "$(ls -t "$OP_T/logs/ops-preview-"*.log | head -1)"; }
out_op_dry="$(RUN_SH_DRY_RUN=1 bash "$REPO/maintenance/run.sh" ops-preview 2>&1)"; rc_op_dry=$?
check "ops-preview dry run exits 0 without a model and names both previews" \
  '[ $rc_op_dry -eq 0 ] && grep -q "would run ops_preview.py (branch_sweep.py + storage_gc.py, preview only" <<<"$out_op_dry" && ! grep -q "claude" <<<"$out_op_dry"'
check "ops-preview is in the usage line and exempt from the prompt-file check" \
  'grep -q "ops-preview" <<<"$(grep "JOB=\"\${1:?usage:" "$REPO/maintenance/run.sh")" && grep -qF "\$JOB\" != \"ops-preview\"" "$REPO/maintenance/run.sh"'
check "status.sh CADENCE lists ops-preview" 'grep -qF "\"ops-preview\": 86400" "$REPO/maintenance/status.sh"'
run_op "$OP_T/st1"; rc_op=$?
check "ops-preview runs both scripts with no arguments, so --apply never appears in their argv" \
  '[ $rc_op -eq 0 ] && [ "$(sort "$OP_ARGV_LOG")" = "$(printf "branch_sweep.py \nstorage_gc.py ")" ] && ! grep -q -e "--apply" "$OP_ARGV_LOG"'
check "ops-preview summarises each script as a counts-only line" \
  'op_log | grep -qx "branch sweep preview: 3 would go, 5 kept" && op_log | grep -qx "storage gc preview: 2 would go, 4 kept, 1048576 image bytes"'
check "ops-preview writes each script full output to a dated file under the state root" \
  'grep -q "branch-sweep: dry run" "$OP_T"/st1/ops-preview/branch_sweep-$(date +%Y-%m-%d)-*.txt && grep -q "storage-gc: volume" "$OP_T"/st1/ops-preview/storage_gc-$(date +%Y-%m-%d)-*.txt'
check "ops-preview sends the summary as class scheduled, never blocked, ask or page" \
  'grep -q "^-m capsule outbox-send scheduled .*branch sweep preview: 3 would go, 5 kept" "$OP_SEND_LOG" && ! grep -qE "outbox-send (blocked|ask|page|needs_input)" "$OP_SEND_LOG"'
: >"$OP_ARGV_LOG"
run_op "$OP_T/st1"; rc_op2=$?
check "ops-preview skips a second run inside 47h (every other day) and runs nothing" \
  '[ $rc_op2 -eq 0 ] && [ ! -s "$OP_ARGV_LOG" ] && op_log | grep -q "skipped: ran within the last 47h"'
touch -t 203012312359 "$OP_T/st1/ops-preview.stamp"   # a future-dated stamp (clock jumped back)
: >"$OP_ARGV_LOG"
run_op "$OP_T/st1"; rc_op4=$?
check "ops-preview treats a future-dated stamp as missing and runs both scripts" \
  '[ $rc_op4 -eq 0 ] && [ "$(wc -l <"$OP_ARGV_LOG" | tr -d " ")" = 2 ]'
check "ops-preview keeps both runs' dated files: a rerun on the same day overwrites nothing" \
  '[ "$(ls "$OP_T/st1/ops-preview" | grep -c "^branch_sweep-")" = 2 ] && [ "$(ls "$OP_T/st1/ops-preview" | grep -c "^storage_gc-")" = 2 ]'
OP_SEND_FAIL=1 run_op "$OP_T/st3"
check "ops-preview passes --to and --subject like the close, so a failed send spools with its target" \
  'grep -q "^to=test-chat$" "$OP_T"/st3/outbox/*-ops-preview.msg && grep -q "^subject=Capsule - ops preview" "$OP_T"/st3/outbox/*-ops-preview.msg'
: >"$OP_SEND_LOG"
OP_TARGET= run_op "$OP_T/st4"
check "ops-preview with no chat configured still previews but sends nothing" \
  '[ ! -s "$OP_SEND_LOG" ] && [ -z "$(ls "$OP_T/st4/outbox" 2>/dev/null)" ] && op_log | grep -q "no chat configured"'
OP_FAIL=1 run_op "$OP_T/st2"; rc_op3=$?
check "ops-preview exits non-zero and leaves no stamp when a script fails (job-failed alert, retry next day)" \
  '[ $rc_op3 -eq 1 ] && [ ! -e "$OP_T/st2/ops-preview.stamp" ] && op_log | grep -q "storage gc preview: FAILED"'
rm -rf "$OP_T"

# --- fork-sync: pure bash, no Claude session (controller ruling A, row 97;
# rewritten per the 2c final review I1/I2 - fetch + merge, not `gh repo sync`).
# The dry-run line echoes the checkout path, which defaults to $HOME/CodeBase/...; a HOME
# containing "claude" (agent scratch dirs) used to trip the no-model grep. Pin it neutral and
# keep a claude-named HOME so the case never depends on the host path.
out="$(HOME=/private/tmp/claude-501/h OPS_FORK_SYNC_CHECKOUT=/neutral/forkco RUN_SH_DRY_RUN=1 bash "$REPO/maintenance/run.sh" fork-sync 2>&1)"; rc=$?
check "fork-sync dry run exits 0 without a model" '[ $rc -eq 0 ] && grep -q "dry run: would fetch .* merge .*/main, push .* main" <<<"$out" && ! grep -q "claude" <<<"$out"'
check "fork-sync is in the usage line" 'grep -q "fork-sync" <<<"$(grep "JOB=\"\${1:?usage:" "$REPO/maintenance/run.sh")"'
check "fork-sync is exempt from the prompt-file check" 'grep -qF "\$JOB\" != \"fork-sync\"" "$REPO/maintenance/run.sh"'
check "fork-sync prints no model" 'grep -qF "restore-verify|fork-sync|quality-nightly|deps-sync)" "$REPO/maintenance/run.sh"'
check "status.sh CADENCE lists fork-sync" 'grep -qF "\"fork-sync\": 7*86400" "$REPO/maintenance/status.sh"'
check "fork-sync no longer calls gh repo sync" \
  '! grep -vE "^\\s*#" "$REPO/maintenance/run.sh" | grep -q "gh repo sync"'
check "fork-sync never force-pushes or resets" '! grep -qE "push .*--force|reset --hard" "$REPO/maintenance/run.sh"'
# M6: the external calls go through with_cap, so JOB_CAP_SECONDS applies to a
# git that hangs on the network.
for g in "fetch \"\$UPSTREAM_REMOTE\"" "merge --no-edit" "push \"\$FORK_REMOTE\" main"; do
  check "fork-sync runs git $g through with_cap" \
    'grep -qF "with_cap git -C \"\$FORK_CHECKOUT\" $g" "$REPO/maintenance/run.sh"'
done
# The dry-run path must return before ANY git call - the source order proves it.
check "fork-sync dry run returns before any git call" \
  '[ "$(grep -n "would fetch" "$REPO/maintenance/run.sh" | head -1 | cut -d: -f1)" -lt "$(grep -n "with_cap git -C \"\$FORK_CHECKOUT\"" "$REPO/maintenance/run.sh" | head -1 | cut -d: -f1)" ]'

# --- fork-sync, for real, against throwaway LOCAL repos only: two bare repos
# stand in for upstream and the fork, and the "checkout" is a clone with both
# as remotes. No network, no GitHub, nothing outside $T - and never the owner's
# own ~/CodeBase checkout of the notes-vault skill, which OPS_FORK_SYNC_CHECKOUT overrides.
FSR="$T/forksync"
G="git -c user.name=t -c user.email=t@example.com -c commit.gpgsign=false -c init.defaultBranch=main"
fs_reset() {
  rm -rf "$FSR"; mkdir -p "$FSR"
  $G init --bare -q "$FSR/upstream.git"; $G init --bare -q "$FSR/fork.git"
  $G clone -q "$FSR/upstream.git" "$FSR/seed" 2>/dev/null
  ( cd "$FSR/seed" && printf 'one\n' > a.txt && $G add a.txt && $G commit -qm one && $G branch -M main && $G push -q origin main )
  $G clone -q "$FSR/upstream.git" "$FSR/checkout" 2>/dev/null
  ( cd "$FSR/checkout" && $G remote rename origin upstream && $G remote add fork "$FSR/fork.git" \
      && $G config user.name t && $G config user.email t@example.com && $G push -q fork main )
}
fs_upstream_commit() {  # fs_upstream_commit <file> <text>
  ( cd "$FSR/seed" && $G pull -q --ff-only origin main && printf '%s\n' "$2" > "$1" \
      && $G add "$1" && $G commit -qm "upstream $1" && $G push -q origin main )
}
# run.sh sends everything to its own log (run.sh:657), so a real (non-dry) run
# prints nothing on stdout: fs_run keeps the exit code in FS_RC and the job's
# output in fs_out, read back from the newest fork-sync log.
fs_run() {
  HOME="$FH" OBSIDIAN_VAULT_PATH="$FH/vault" EVENTS_SPOOL="$T/fs-events.jsonl" \
    RUN_SH_MAINT_DIR="$FM" OPS_FORK_SYNC_CHECKOUT="$FSR/checkout" \
    OPS_FORK_SYNC_UPSTREAM_REMOTE=upstream OPS_FORK_SYNC_FORK_REMOTE=fork \
    bash "$FM/run.sh" fork-sync >/dev/null 2>&1
  FS_RC=$?
  fs_out="$(cat "$(ls -t "$FM"/logs/fork-sync-*.log 2>/dev/null | head -1)" 2>/dev/null)"
}
fork_head() { $G -C "$FSR/fork.git" rev-parse main; }
local_head() { $G -C "$FSR/checkout" rev-parse main; }

# 1. Nothing to do: an already-current checkout still exits 0.
fs_reset
fs_run; rc="$FS_RC"
check "fork-sync with nothing upstream exits 0" '[ $rc -eq 0 ]'

# 2. Fast-forward: upstream moved, the checkout added nothing.
fs_upstream_commit b.txt two
fs_run; rc="$FS_RC"
check "fork-sync fast-forwards when the checkout added nothing" \
  '[ $rc -eq 0 ] && [ -f "$FSR/checkout/b.txt" ]'
check "fork-sync pushes the result to the fork remote" '[ "$(fork_head)" = "$(local_head)" ]'
check "a fast-forward makes no merge commit" \
  '[ "$($G -C "$FSR/checkout" rev-list --count --merges main)" = 0 ]'

# 3. Divergence with no textual conflict: a merge commit, not a refusal.
fs_reset
( cd "$FSR/checkout" && printf 'owner\n' > local.txt && $G add local.txt && $G commit -qm "owner local edits" )
fs_upstream_commit c.txt three
fs_run; rc="$FS_RC"
check "fork-sync merges a diverged fork instead of refusing" \
  '[ $rc -eq 0 ] && [ -f "$FSR/checkout/c.txt" ] && [ -f "$FSR/checkout/local.txt" ]'
check "the divergence merge is a merge commit" \
  '[ "$($G -C "$FSR/checkout" rev-list --count --merges main)" = 1 ]'
check "the merge commit is pushed to the fork" '[ "$(fork_head)" = "$(local_head)" ]'

# 4. Real conflict: abort, exit 3, checkout left clean and unmoved.
fs_reset
( cd "$FSR/checkout" && printf 'mine\n' > a.txt && $G add a.txt && $G commit -qm "local a.txt" )
before="$(local_head)"
fs_upstream_commit a.txt theirs
fs_run; rc="$FS_RC"
check "a conflicting merge exits 3" '[ $rc -eq 3 ]'
check "a conflicting merge says how to resolve it" 'grep -q "conflicted" <<<"$fs_out"'
check "a conflicting merge aborts and leaves the checkout clean" \
  '[ -z "$($G -C "$FSR/checkout" status --porcelain)" ] && [ "$(local_head)" = "$before" ]'

# 4b. A merge that fails with no conflict (unrelated histories, rc 128) is a
# plain failed job (exit 1), not the exit-3 "needs a hand merge" INFO.
fs_reset
$G init -q "$FSR/other" && ( cd "$FSR/other" && printf 'x\n' > z.txt && $G add z.txt && $G commit -qm unrelated )
$G -C "$FSR/checkout" remote set-url upstream "$FSR/other"
fs_run; rc="$FS_RC"
check "a non-conflict merge failure exits 1, not 3" '[ $rc -eq 1 ]'
check "a non-conflict merge failure is not reported as a conflict" \
  '! grep -q "conflicted" <<<"$fs_out" && grep -q "without a conflict" <<<"$fs_out"'

# 5. Refusals, before any merge: dirty tree, wrong branch, missing remote.
fs_reset
printf 'uncommitted\n' > "$FSR/checkout/dirty.txt"
$G -C "$FSR/checkout" add dirty.txt
fs_run; rc="$FS_RC"
check "a dirty checkout is refused with exit 1" '[ $rc -eq 1 ] && grep -q "is dirty" <<<"$fs_out"'

fs_reset
$G -C "$FSR/checkout" checkout -q -b side
fs_run; rc="$FS_RC"
check "a checkout not on main is refused with exit 1" \
  '[ $rc -eq 1 ] && grep -q "not main" <<<"$fs_out"'

fs_reset
$G -C "$FSR/checkout" remote rename upstream elsewhere
fs_run; rc="$FS_RC"
check "a missing upstream remote is refused with a clear message, nothing added" \
  '[ $rc -eq 1 ] && grep -q "no remote named .upstream." <<<"$fs_out" && ! $G -C "$FSR/checkout" remote | grep -qx upstream'


# --- C1: sprint-plan.md's board_bridge.py commands must render to the same
# absolute prefix run.sh's ALLOWED line uses - a `~/`-based command in the
# prompt does not match a `$HOME`-based Bash(...) allowlist entry, so Claude
# Code would refuse to run it. Render __HOME__ the same way run.sh does
# (run.sh:353) and compute the ALLOWED prefix the same way run.sh does
# (run.sh:372), independent of the eligible-count precheck that gates
# whether run.sh actually reaches the sprint-plan case arm.
SPRINT_PROMPT="$(cat "$REPO/maintenance/sprint-plan.md" 2>/dev/null)"
SPRINT_PROMPT="${SPRINT_PROMPT//__HOME__/$HOME}"
SPRINT_PROMPT="${SPRINT_PROMPT//__CONFIG__/$HOME/.config/capsule-corp}"
pcheck sprint-plan "sprint-plan prompt has no bare ~/ path" '! grep -q "~/" <<<"$SPRINT_PROMPT"'
# The prompt file always hardcodes __HOME__/<config dir>/maintenance
# (like architect.md/nightly.md do) rather than parameterizing on
# RUN_SH_MAINT_DIR (a test-only override, run.sh:39) - so the un-overridden
# default is the path that has to match in production.
# The prompt files also hardcode the PRODUCTION interpreter path
# (run.sh:48's default), never the caller's interpreter override - CI exports
# that override as python3 (no venv on the runner), which must not leak into this
# check or it stops matching the prompts' hardcoded prefix.
PROD_PY="python3"
ALLOWED_PREFIX="$PROD_PY $HOME/.config/capsule-corp/maintenance/board_bridge.py"
pcheck sprint-plan "sprint-plan eligible command matches the ALLOWED prefix" \
  'grep -qF "$ALLOWED_PREFIX eligible" <<<"$SPRINT_PROMPT"'
pcheck sprint-plan "sprint-plan link-spec command matches the ALLOWED prefix" \
  'grep -qF "$ALLOWED_PREFIX link-spec" <<<"$SPRINT_PROMPT"'


# --- C1: ops.env is sourced with `.`, which sets but does NOT export. Every
# variable ops.env owns that a Python child has to see must be exported by name
# CAPSULE_BOARD_VIEW is the board-view
# rollout brake, read inside board_bridge.py - if it does not cross the process
# boundary the generator runs anyway and overwrites the live board file.
EH="$T/envhome"; mkdir -p "$EH/.config/capsule-corp" "$T/maint"
printf 'CAPSULE_BOARD_VIEW=0\n' > "$EH/.config/capsule-corp/ops.env"
env_out="$(HOME="$EH" RUN_SH_MAINT_DIR="$T/maint" RUN_SH_PRINT_ENV=1 \
  bash "$REPO/maintenance/run.sh" sprint-sync 2>&1)"
check "ops.env CAPSULE_BOARD_VIEW=0 is exported to children" \
  'grep -qx "CAPSULE_BOARD_VIEW=0" <<<"$env_out"'
: > "$EH/.config/capsule-corp/ops.env"
env_out="$(HOME="$EH" RUN_SH_MAINT_DIR="$T/maint" RUN_SH_PRINT_ENV=1 \
  bash "$REPO/maintenance/run.sh" sprint-sync 2>&1)"
check "an ops.env without the knob exports nothing (unset = view on)" \
  'grep -qx "CAPSULE_BOARD_VIEW=" <<<"$env_out"'

# Same boundary for the window pre-flight knobs (power level, design 7.4): the
# flag and its two floors are read inside board_bridge.py's dispatch path, so
# an ops.env value that is set but not exported leaves the child on the
# built-in defaults - the flag would read as OFF however ops.env is written.
printf 'OPS_WINDOW_PREFLIGHT=1\nOPS_WINDOW_PREFLIGHT_FLOOR_PCT=12\nOPS_WINDOW_PREFLIGHT_MIN_S=1200\n' \
  > "$EH/.config/capsule-corp/ops.env"
env_out="$(HOME="$EH" RUN_SH_MAINT_DIR="$T/maint" RUN_SH_PRINT_ENV=1 \
  bash "$REPO/maintenance/run.sh" sprint-sync 2>&1)"
check "ops.env OPS_WINDOW_PREFLIGHT=1 is exported to children" \
  'grep -qx "OPS_WINDOW_PREFLIGHT=1" <<<"$env_out"'
check "ops.env OPS_WINDOW_PREFLIGHT_FLOOR_PCT is exported to children" \
  'grep -qx "OPS_WINDOW_PREFLIGHT_FLOOR_PCT=12" <<<"$env_out"'
check "ops.env OPS_WINDOW_PREFLIGHT_MIN_S is exported to children" \
  'grep -qx "OPS_WINDOW_PREFLIGHT_MIN_S=1200" <<<"$env_out"'
: > "$EH/.config/capsule-corp/ops.env"
env_out="$(HOME="$EH" RUN_SH_MAINT_DIR="$T/maint" RUN_SH_PRINT_ENV=1 \
  bash "$REPO/maintenance/run.sh" sprint-sync 2>&1)"
check "an ops.env without the pre-flight knobs exports nothing (unset = off)" \
  'grep -qx "OPS_WINDOW_PREFLIGHT=" <<<"$env_out" &&
   grep -qx "OPS_WINDOW_PREFLIGHT_FLOOR_PCT=" <<<"$env_out" &&
   grep -qx "OPS_WINDOW_PREFLIGHT_MIN_S=" <<<"$env_out"'

# --- Absence DRAIN (row 137, C9/M215): sprint-plan skips before any Claude
# session starts when the tier is 2+ and the flag is on; the flag off must not
# touch this at all, however old the approvals tree is. HOME is redirected to
# a throwaway dir so the ELIGIBLE precheck that runs after this guard
# (board_bridge.py eligible --count, which reads the board under HOME) never
# reaches the real home board - this suite only cares whether the absence skip
# fires, not whether the eligible-count skip also does.
ABS_HOME="$T/abs-home"; ABS_GATE="$T/abs-gate"; ABS_STATE="$T/abs-state"
mkdir -p "$ABS_HOME" "$ABS_GATE/approvals/spec" "$ABS_STATE"
touch "$ABS_GATE/approvals/spec/some-slug.json"
age_abs_approval() {  # age_abs_approval <hours>
  python3 -c '
import os, sys, time
t = time.time() - float(sys.argv[1]) * 3600
os.utime(sys.argv[2], (t, t))
' "$1" "$ABS_GATE/approvals/spec/some-slug.json"
}

age_abs_approval 72   # tier 2 (DRAIN)
abs_out="$(HOME="$ABS_HOME" CAPSULE_OPS_PY=python3 RUN_SH_DRY_RUN=1 \
  OPS_ABSENCE_PROTOCOL=1 MERGE_GATE_STATE="$ABS_GATE" AGENT_OPS_STATE_DIR="$ABS_STATE" \
  bash "$REPO/maintenance/run.sh" sprint-plan 2>&1)"
pcheck sprint-plan "tier 2 with the flag on skips sprint-plan before any session starts" \
  'grep -q "sprint-plan skipped: absence drain: tier 2" <<<"$abs_out"'

abs_out="$(HOME="$ABS_HOME" CAPSULE_OPS_PY=python3 RUN_SH_DRY_RUN=1 \
  MERGE_GATE_STATE="$ABS_GATE" AGENT_OPS_STATE_DIR="$ABS_STATE" \
  bash "$REPO/maintenance/run.sh" sprint-plan 2>&1)"
check "the flag off never mentions absence drain, tier-2-aged or not" \
  '! grep -q "absence drain" <<<"$abs_out"'

age_abs_approval 2    # tier 1: well under the 48h DRAIN floor
abs_out="$(HOME="$ABS_HOME" CAPSULE_OPS_PY=python3 RUN_SH_DRY_RUN=1 \
  OPS_ABSENCE_PROTOCOL=1 MERGE_GATE_STATE="$ABS_GATE" AGENT_OPS_STATE_DIR="$ABS_STATE" \
  bash "$REPO/maintenance/run.sh" sprint-plan 2>&1)"
check "tier 1 with the flag on never mentions absence drain" \
  '! grep -q "absence drain" <<<"$abs_out"'

rm -rf "$ABS_HOME" "$ABS_GATE" "$ABS_STATE"

# C9 fix wave 1 item 6: no local OPS_ABSENCE_PROTOCOL parsing in run.sh - the
# absence-drain guard defers entirely to absence.py's own enabled() (a
# second parser here could silently drift from it).
check "run.sh has no local OPS_ABSENCE_PROTOCOL != \"0\" parse - absence.py's enabled() is the only parser" \
  '! grep -qF '"'"'"${OPS_ABSENCE_PROTOCOL:-0}" != "0"'"'"' "$REPO/maintenance/run.sh"'
# Same boundary again (Capsule row 139 / C11): OPS_BET_FIELD is read inside
# board.bet_enabled(), called from board_bridge.py's dispatch path and
# intake.py's consume path - both children of run.sh. An ops.env value that is
# set but not exported leaves the child on the default, so the flag would read
# as OFF however ops.env is written. Spec section 4, discrepancy 6.
printf 'OPS_BET_FIELD=1\n' > "$EH/.config/capsule-corp/ops.env"
env_out="$(HOME="$EH" RUN_SH_MAINT_DIR="$T/maint" RUN_SH_PRINT_ENV=1 \
  bash "$REPO/maintenance/run.sh" sprint-sync 2>&1)"
check "ops.env OPS_BET_FIELD=1 is exported to children" \
  'grep -qx "OPS_BET_FIELD=1" <<<"$env_out"'
: > "$EH/.config/capsule-corp/ops.env"
env_out="$(HOME="$EH" RUN_SH_MAINT_DIR="$T/maint" RUN_SH_PRINT_ENV=1 \
  bash "$REPO/maintenance/run.sh" sprint-sync 2>&1)"
check "an ops.env without the bet knob exports nothing (unset = off)" \
  'grep -qx "OPS_BET_FIELD=" <<<"$env_out"'

# --- C3: the sprint-sync branch no longer commits the generated board file.
# board_view.regenerate -> vault_git.push owns Boards/Engineering.md; a second
# committer in run.sh is how two writers interleave.
check "run.sh does not git add the generated board file" \
  '! grep -q "git add Boards/Engineering.md" "$REPO/maintenance/run.sh"'
check "run.sh has no sprint-sync board commit" \
  '! grep -q "sprint-sync: \$STAMP" "$REPO/maintenance/run.sh"'

check "security guard allowlist is its note folder only" \
  'grep -qF "GUARD_ALLOW=\"Knowledge/**\"" "$REPO/maintenance/run.sh"'
check "agent-review guard allowlist is its note folder only" \
  'grep -qF "GUARD_ALLOW=\"Reviews/**\"" "$REPO/maintenance/run.sh"'
check "no GUARD_ALLOW line names the generated board file any more" \
  '! grep "GUARD_ALLOW=" "$REPO/maintenance/run.sh" | grep -qF "Boards/Engineering.md"'
pcheck security "the security prompt takes its board list from run.sh" \
  'grep -qF "__BOARDS__" "$REPO/maintenance/security.md" && grep -qF "__BOARDS__" "$REPO/maintenance/run.sh"'

# --- C1 (final review): neither review job may hold Edit on Boards/Intake.md.
# Both sessions read untrusted code and prose; an exact-file Edit on Intake was
# enough to forge a `## New work` card or an `## Approvals` spec approval. The
# add-finding verb is the whole privilege they get instead.
check "no GUARD_ALLOW line names Boards/Intake.md any more" \
  '! grep "GUARD_ALLOW=" "$REPO/maintenance/run.sh" | grep -qF "Boards/Intake.md"'
for job in security agent-review; do
  check "$job ALLOWED carries the intake.py add-finding verb" \
    'grep "ALLOWED=" "$REPO/maintenance/run.sh" | grep -qF "intake.py add-finding *)"'
done
check "both ALLOWED lines carry the verb (two occurrences)" \
  '[ "$(grep -c "intake.py add-finding \*)" "$REPO/maintenance/run.sh")" = 2 ]'
# Same prefix check the planner has: a prompt command must render to the exact
# absolute prefix the Bash(...) allowlist entry uses, or Claude Code refuses it.
# Like ALLOWED_PREFIX above, this must use the PRODUCTION interpreter path
# (run.sh:48's default), not the caller's interpreter override, since the
# prompt files hardcode the production path regardless of what CI sets.
INTAKE_PREFIX="$PROD_PY $HOME/.config/capsule-corp/maintenance/intake.py add-finding"
for f in security agent-review; do
  have_prompt "$f" || { echo "SKIP $f add-finding prompt checks: maintenance/$f.md is private-only (not in the public copy)"; continue; }
  PROMPT="$(cat "$REPO/maintenance/$f.md")"
  PROMPT="${PROMPT//__HOME__/$HOME}"
  PROMPT="${PROMPT//__CONFIG__/$HOME/.config/capsule-corp}"
  check "$f add-finding command matches the ALLOWED prefix" \
    'grep -qF "$INTAKE_PREFIX" <<<"$PROMPT"'
  check "$f prompt no longer tells the session to edit Intake.md by hand" \
    '! grep -q "append ONE line at the END" <<<"$PROMPT"'
done

# I3: an empty board list used to fall back to `capsule-corp`, silently narrowing
# every finding onto one board. It is a failed job now.
check "an empty board list fails the security job instead of falling back" \
  'grep -qF "refusing to guess a board list" "$REPO/maintenance/run.sh" && ! grep -qF "__BOARDS__/${BOARDS:-capsule-corp}" "$REPO/maintenance/run.sh"'

# The refusal keeps the last stderr line of `boards`, so a Python failure is not silent.
MF="$T/maint-boards-fail"; cp -R "$REPO/maintenance" "$MF"
printf 'import sys\nprint("boards-boom-line", file=sys.stderr)\nsys.exit(1)\n' > "$MF/board_bridge.py"
out="$(HOME="$T" EVENTS_SPOOL="$T/boards-fail.jsonl" RUN_SH_MAINT_DIR="$MF" RUN_SH_DRY_RUN=1 bash "$REPO/maintenance/run.sh" security 2>&1)"; rc=$?
pcheck security "an empty board list keeps the last stderr line in the event text" \
  '[ $rc -eq 1 ] && grep -qF "boards-boom-line" "$T/boards-fail.jsonl"'

# --- C1: the write guard must actually DENY an Edit of Boards/Intake.md under
# the security job's rendered settings. Feed the hook the payload Claude Code
# would send, with the job's own GUARD_ALLOW.
GH="$T/guardvault"; mkdir -p "$GH/Boards" "$GH/Knowledge"
guard() {  # guard <GUARD_ALLOW> <file_path>; prints nothing, returns the hook's exit
  printf '{"tool_name":"Edit","tool_input":{"file_path":"%s"}}' "$2" \
    | OBSIDIAN_VAULT_PATH="$GH" GUARD_ALLOW="$1" \
      python3 "$REPO/maintenance/hooks/write-guard.py" 2>/dev/null
}
guard "Knowledge/**" "$GH/Boards/Intake.md"; grc=$?
check "security guard denies an Edit of Boards/Intake.md" '[ "$grc" -eq 2 ]'
guard "Reviews/**" "$GH/Boards/Intake.md"; grc=$?
check "agent-review guard denies an Edit of Boards/Intake.md" '[ "$grc" -eq 2 ]'
guard "Knowledge/**" "$GH/Knowledge/Security Review 2026-09-08.md"; grc=$?
check "security guard allows its own note folder" '[ "$grc" -eq 0 ]'
guard "Knowledge/**" "$GH/Boards/Engineering.md"; grc=$?
check "security guard denies the generated board" '[ "$grc" -eq 2 ]'

# --- 2026-09-09: the four unattended vault jobs (inbox, nightly, weekly,
# architect) had Write/Edit in the vault with no guard at all. The inbox job
# triages clipped web pages, so a poisoned clipping could have written an
# `approve spec: <slug>` line under `## Approvals` in the intake board - a
# real spec approval the sync tick consumes. Assert each job's rendered
# allowlist denies the pipeline-owned paths and allows its own work.
mkdir -p "$GH/Ledger" "$GH/Decisions" "$GH/Specs" "$GH/Daily" "$GH/Inbox" \
  "$GH/Reviews" "$GH/Projects/my-app" "$GH/Knowledge" "$GH/Debugging" \
  "$GH/Architecture"
job_allow() {  # job_allow <job>; prints the GUARD_ALLOW the run.sh arm sets
  sed -n "/^    $1)\$/,/;;\$/p" "$REPO/maintenance/run.sh" \
    | sed -n 's/^ *GUARD_ALLOW="\(.*\)".*/\1/p' | head -1
}
for job in inbox nightly weekly architect; do
  a="$(job_allow "$job")"
  check "$job sets a GUARD_ALLOW" '[ -n "$a" ]'
  for bad in "Boards/Intake.md" "Boards/Engineering.md" "Ledger/capsule-corp/t_1.jsonl" \
             "Decisions/d.md" "Specs/s.md" "_CLAUDE.md"; do
    guard "$a" "$GH/$bad"; grc=$?
    check "$job guard denies $bad" '[ "$grc" -eq 2 ]'
  done
done
guard "$(job_allow inbox)" "$GH/Inbox/clipped.md"; grc=$?
check "inbox guard allows the capture it is triaging" '[ "$grc" -eq 0 ]'
guard "$(job_allow nightly)" "$GH/Daily/2026-09-09.md"; grc=$?
check "nightly guard allows today's daily note" '[ "$grc" -eq 0 ]'
# Row 128, M069/M070 + final review I1: the nightly job regenerates the
# decision-log sidecar through the ALLOWED *Bash* verb `decision_log.py
# sidecar`, which hooks/write-guard.py never sees. So nothing under
# Decisions/ belongs in nightly's GUARD_ALLOW - the entry would only have
# granted the nightly MODEL an Edit path into Decisions/.
na="$(job_allow nightly)"
check "nightly GUARD_ALLOW names nothing under Decisions/" \
  '! grep -qF "Decisions/" <<<"$na"'
check "nightly GUARD_ALLOW does not open all of Decisions/" \
  '! grep -qF "Decisions/**" <<<"$na"'
guard "$(job_allow nightly)" "$GH/Decisions/decisions.jsonl"; grc=$?
check "nightly guard denies an Edit of the sidecar (Bash writes it)" '[ "$grc" -eq 2 ]'
check "nightly ALLOWED still carries the decision_log.py sidecar Bash verb" \
  'nightly_allowed="$(sed -n "/^    nightly)\$/,/;;\$/p" "$REPO/maintenance/run.sh" | sed -n "s/^ *ALLOWED=\"\(.*\)\"$/\1/p")"; \
   grep -qF "decision_log.py sidecar *)" <<<"$nightly_allowed"'
guard "$(job_allow weekly)" "$GH/Reviews/2026-09-11 - Weekly Review.md"; grc=$?
check "weekly guard allows its review note" '[ "$grc" -eq 0 ]'
# Row 128, M085 + final review I2: weekly's Sunday resume-pointer sweep marks
# `claude -r` handles in place, but only under Knowledge/** and Debugging/**.
# Architecture/** is the ONE-WAY docs mirror (row 6 / §7.5, machine-written,
# re-rendered by docs_sync.py) and Decisions/ is pipeline-owned (M048): a
# handle there is REPORTED in the Weekly Review, never edited, so both must
# FAIL the guard.
wa="$(job_allow weekly)"
for ok in "Knowledge/x.md" "Debugging/x.md"; do
  guard "$wa" "$GH/$ok"; grc=$?
  check "weekly guard allows $ok" '[ "$grc" -eq 0 ]'
done
for no in "Architecture/x.md" "Decisions/x.md"; do
  guard "$wa" "$GH/$no"; grc=$?
  check "weekly guard denies $no (report-only, never edited)" '[ "$grc" -eq 2 ]'
done
check "weekly GUARD_ALLOW does not open the docs mirror" \
  '! grep -qF "Architecture/" <<<"$wa"'
pcheck weekly "weekly.md says the other folders are reported, never edited" \
  'grep -qF "reported, never edited" "$REPO/maintenance/weekly.md"'
# Final review C3: without --scan, resume_pointers.py prints its usage and
# exits 2, so the Sunday sweep would report "none" every week and look green.
pcheck weekly "weekly.md runs resume_pointers.py with --scan" \
  'grep -qF "resume_pointers.py --scan" "$REPO/maintenance/weekly.md"'
check "weekly ALLOWED carries the resume_pointers.py Bash verb" \
  'weekly_allowed="$(sed -n "/^    weekly)\$/,/;;\$/p" "$REPO/maintenance/run.sh" | sed -n "s/^ *ALLOWED=\"\(.*\)\"$/\1/p")"; \
   grep -qF "resume_pointers.py *)" <<<"$weekly_allowed"'
guard "$(job_allow architect)" "$GH/Projects/my-app/Architecture - Overview.md"; grc=$?
check "architect guard allows the architecture notes" '[ "$grc" -eq 0 ]'

out="$(RUN_SH_DRY_RUN=1 bash "$REPO/maintenance/run.sh" heal 2>&1)"; rc=$?
check "heal dry run prints its marker" '[ $rc -eq 0 ] && grep -q "would run heal.sh" <<<"$out"'
out="$(RUN_SH_DRY_RUN=1 bash "$REPO/maintenance/run.sh" observability 2>&1)"; rc=$?
check "observability job name is refused with a pointer to heal" '[ $rc -eq 2 ] && grep -q "now '\''heal'\''" <<<"$out"'

out="$(RUN_SH_DRY_RUN=1 bash "$REPO/maintenance/run.sh" obs-probe 2>&1)"; rc=$?
check "obs-probe dry run exits 0 without a model" '[ $rc -eq 0 ] && grep -q "dry run: would run obs_probe.py (7 collectors → samples)" <<<"$out" && ! grep -q "model" <<<"$out"'

out="$(RUN_SH_DRY_RUN=1 bash "$REPO/maintenance/run.sh" obs-inventory 2>&1)"; rc=$?
check "obs-inventory dry run exits 0 without a model" '[ $rc -eq 0 ] && grep -q "dry run: would run obs_inventory.py (vault inventory note)" <<<"$out" && ! grep -q "model" <<<"$out"'

# --- Task 5: tg_send spools a failed send into the outbox for heal.sh to
# flush later, and still returns non-zero so callers (e.g. report.sh's
# on_exit) know delivery failed right now. Sourced straight out of run.sh
# (same reasoning as the json-helpers block above), via `eval` rather than
# `source <(...)`: macOS's bash 3.2 does not reliably define functions
# sourced from a process substitution (confirmed - it leaves them undefined).
TG_SEND_SRC=". \"$REPO/maintenance/lib_send.sh\"
$(sed -n '/^tg_send()/,/^}/p' "$REPO/maintenance/run.sh")"
check "run.sh carries a sourceable tg_send() function" '[ -n "$TG_SEND_SRC" ]'
mkdir -p "$T/fakebin"; printf '#!/usr/bin/env bash\ntouch "%s/legacy-ran"\n' "$T" >"$T/fakebin/legacy-agent"
printf '#!/usr/bin/env bash\necho "$*" >>"%s/capsule-argv"\nexit 1\n' "$T" >"$T/fakebin/python3"
chmod +x "$T/fakebin/legacy-agent" "$T/fakebin/python3"
mkdir -p "$T/state"
out="$(PATH="$T/fakebin:$PATH" RUN_SH_STATE_DIR="$T/state" OPS_CHAT_PROFILE=telegram TG_SEND_SRC="$TG_SEND_SRC" bash -c 'eval "$TG_SEND_SRC"; STATE_DIR="'"$T"'/state"; JOB=nightly; ev_log(){ :; }; printf "hello" | tg_send --to telegram:-1:2; echo rc=$?')"
check "tg_send delivers via capsule outbox-send and never runs the legacy agent" 'grep -q "outbox-send blocked hello" "$T/capsule-argv" && [ ! -e "$T/legacy-ran" ]'
check "tg_send spools when the Slack send fails and still returns non-zero" 'grep -q "rc=1" <<<"$out" && f="$(ls "$T/state/outbox"/*-nightly.msg)" && [ "$(sed -n 1p "$f")" = "to=telegram:-1:2" ] && grep -q "^at=" "$f" && [ "$(tail -1 "$f")" = "hello" ]'
check "the spool header carries a subject= line (empty when none was passed)" '[ "$(sed -n 3p "$(ls "$T/state/outbox"/*-nightly.msg)")" = "subject=" ]'
# TG_SEND_NO_SPOOL=1 is the contract heal.sh's outbox flush relies on: a
# failed send must report the failure and write nothing, so replaying a
# spooled file can never leave a second copy of it behind.
rm -rf "$T/state/outbox"
out="$(PATH="$T/fakebin:$PATH" RUN_SH_STATE_DIR="$T/state" OPS_CHAT_PROFILE=telegram TG_SEND_SRC="$TG_SEND_SRC" bash -c 'eval "$TG_SEND_SRC"; STATE_DIR="'"$T"'/state"; JOB=nightly; ev_log(){ :; }; printf "hello" | TG_SEND_NO_SPOOL=1 tg_send --to telegram:-1:2; echo rc=$?')"
check "TG_SEND_NO_SPOOL=1 still returns non-zero but spools nothing" 'grep -q "rc=1" <<<"$out" && [ -z "$(ls -A "$T/state/outbox" 2>/dev/null)" ]'

# --- C3/M047: the inbox job loses git mv/rm (row 127) - Bash verbs are
# invisible to write-guard.py, so `git mv Inbox/x Boards/Intake.md` was
# unguarded. The job now only records what it filed in Inbox/.processed; the
# runner performs the moves (git rm on the Inbox/ source) host-side.
check "inbox ALLOWED names neither git mv nor git rm" \
  '! grep -F "Bash(git rm" "$REPO/maintenance/run.sh" | grep -q "inbox" ; \
   inbox_allowed="$(sed -n "/^    inbox)\$/,/;;\$/p" "$REPO/maintenance/run.sh" | sed -n "s/^ *ALLOWED=\"\(.*\)\"$/\1/p")"; \
   ! grep -qF "git mv" <<<"$inbox_allowed" && ! grep -qF "git rm" <<<"$inbox_allowed"'
check "inbox ALLOWED keeps git add" \
  'inbox_allowed="$(sed -n "/^    inbox)\$/,/;;\$/p" "$REPO/maintenance/run.sh" | sed -n "s/^ *ALLOWED=\"\(.*\)\"$/\1/p")"; \
   grep -qF "Bash(git add *)" <<<"$inbox_allowed"'
pcheck inbox "inbox.md no longer names any git command" \
  '! grep -qiE "git (mv|rm|add|commit|status|diff|log)\b" "$REPO/maintenance/inbox.md"'

MARKER_SRC="$(sed -n '/MARKER="\$VAULT\/Inbox\/.processed"/,/^  fi$/p' "$REPO/maintenance/run.sh")"
check "run.sh carries a sourceable inbox marker block" '[ -n "$MARKER_SRC" ]'

MV="$T/markervault"; rm -rf "$MV"; mkdir -p "$MV/Inbox" "$MV/Knowledge"
(cd "$MV" && git init -q && git -c user.name=t -c user.email=t@t commit -q --allow-empty -m init)
printf 'a\n' > "$MV/Inbox/a.md"
printf 'b\n' > "$MV/Inbox/b.md"
printf 'k\n' > "$MV/Knowledge/x.md"
(cd "$MV" && git add -A && git -c user.name=t -c user.email=t@t commit -q -m seed)
printf 'Inbox/a.md\n../../etc/passwd\nKnowledge/x.md\nInbox/missing.md\n' > "$MV/Inbox/.processed"
marker_out="$(cd "$MV" && JOB=inbox VAULT="$MV" bash -c "$MARKER_SRC" 2>&1)"
check "marker step removes the file it names under Inbox/" '[ ! -e "$MV/Inbox/a.md" ]'
check "marker step leaves an untouched Inbox/ file alone" '[ -f "$MV/Inbox/b.md" ]'
check "marker step never touches a path outside Inbox/" '[ -f "$MV/Knowledge/x.md" ]'
check "marker step logs a refusal for the traversal line" 'grep -q "refusing" <<<"$marker_out"'
check "marker step never resolves the traversal line to a real /etc/passwd" \
  '! grep -qF "removed ../../etc/passwd" <<<"$marker_out"'
check "marker step logs a skip for the missing-file line" 'grep -q "Inbox/missing.md is not a file (skipped)" <<<"$marker_out"'
check "marker step deletes the marker file itself" '[ ! -f "$MV/Inbox/.processed" ]'

MV2="$T/markervault-empty"; rm -rf "$MV2"; mkdir -p "$MV2/Inbox"
(cd "$MV2" && git init -q && git -c user.name=t -c user.email=t@t commit -q --allow-empty -m init)
printf 'a\n' > "$MV2/Inbox/a.md"
(cd "$MV2" && git add -A && git -c user.name=t -c user.email=t@t commit -q -m seed)
(cd "$MV2" && JOB=inbox VAULT="$MV2" bash -c "$MARKER_SRC" >/dev/null 2>&1)
check "marker step is a no-op with no marker file" '[ -f "$MV2/Inbox/a.md" ]'

check "no bare Bash(git *) for inbox (contract test still passes)" \
  'inbox_allowed="$(sed -n "/^    inbox)\$/,/;;\$/p" "$REPO/maintenance/run.sh" | sed -n "s/^ *ALLOWED=\"\(.*\)\"$/\1/p")"; \
   ! grep -qF "Bash(git *)" <<<"$inbox_allowed"'

check "close is in the usage string" 'grep -qE "^JOB=.*usage: run\.sh <.*\bclose\b" "$REPO/maintenance/run.sh"'
# Content match, not a line-number window (T6-I1): matches the actual case
# pattern that suppresses the model line, wherever it lives in the file.
check "close is a script job (no model line)" \
  'grep -qE "sprint-sync\|retrieval-eval\|heal\|obs-probe\|obs-inventory\|cleanup\|close\|morning\|health-check\|backup\|restore-verify\|fork-sync\|quality-nightly\|deps-sync\)" "$REPO/maintenance/run.sh"'
check "close runs report.sh --close" 'grep -q "report.sh\" --close" "$REPO/maintenance/run.sh"'

# --- close: behavioural, not just a source grep (T6-C1 / T6-I1). Before the
# fix, run.sh:178's PROMPT_FILE exclusion chain did not list "close", so the
# job died at "no prompt file at .../close.md" before ever reaching the
# close branch - a nightly false failure alert with the close never sending.
# This must FAIL against that broken state and PASS once "close" is excluded
# from the chain, same as every other script job.
out="$(RUN_SH_DRY_RUN=1 bash "$REPO/maintenance/run.sh" close 2>&1)"; rc=$?
check "close dry run exits 0 without a model" '[ $rc -eq 0 ] && grep -q "dry run: would run report.sh --close" <<<"$out" && ! grep -q "model" <<<"$out"'
check "close dry run never says no prompt file" '! grep -q "no prompt file" <<<"$out"'

# --- C8 row 136: Saturday zero, behaviourally, not by prose (T10 fix round 1,
# T10-I2). RUN_SH_WEEKDAY_OVERRIDE (run.sh, same family as RUN_SH_DRY_RUN) lets
# this pin the weekday deterministically instead of depending on the day this
# suite happens to run. skip_job's stable "=== $JOB skipped: ... ===" line is
# the actual behaviour asserted, not a grep over the source.
# RUN_SH_DRY_RUN=1 here only for a real stdout to capture (non-dry jobs
# redirect their whole run to a log FILE, run.sh:800) - skip_job fires
# identically either way, before DRY is ever consulted.
# OPS_SATURDAY_ZERO is routed through a scratch ops.env (the $EH pattern
# used above and below), not a bare command-line env var: run.sh sources
# ops.env with `.` AFTER inheriting the environment (run.sh:73), so on the
# operator's real machine a real ops.env would silently overwrite a
# command-line OPS_SATURDAY_ZERO=0 here - exactly the day the owner sets
# OPS_SATURDAY_ZERO=1 in his real ops.env, the first step of the documented
# rollout (M8).
# The "flag off"/"non-Saturday" checks below run the security job for real
# (past the Saturday-zero gate) and it refuses to run with an empty board
# list. `board_bridge.py boards` prints board.intake_board_names(), which reads
# $CAPSULE_HOME/boards, so point CAPSULE_HOME at a throwaway dir holding one
# fake board (a */kanban.db glob, no subprocess) rather than the operator's real Capsule boards.
CH="$EH/capsule"; mkdir -p "$CH/boards/fake" && : > "$CH/boards/fake/kanban.db"
printf 'OPS_SATURDAY_ZERO=1\n' > "$EH/.config/capsule-corp/ops.env"
out="$(HOME="$EH" RUN_SH_MAINT_DIR="$REPO/maintenance" RUN_SH_WEEKDAY_OVERRIDE=6 RUN_SH_DRY_RUN=1 bash "$REPO/maintenance/run.sh" security 2>&1)"; rc=$?
pcheck security "flag on + Saturday skips security" \
  '[ $rc -eq 0 ] && grep -qF "=== security skipped: Saturday zero (OPS_SATURDAY_ZERO=1) ===" <<<"$out"'
out="$(HOME="$EH" RUN_SH_MAINT_DIR="$REPO/maintenance" RUN_SH_WEEKDAY_OVERRIDE=6 RUN_SH_DRY_RUN=1 bash "$REPO/maintenance/run.sh" architect 2>&1)"; rc=$?
pcheck architect "flag on + Saturday skips architect" \
  '[ $rc -eq 0 ] && grep -qF "=== architect skipped: Saturday zero (OPS_SATURDAY_ZERO=1) ===" <<<"$out"'
printf 'OPS_SATURDAY_ZERO=0\n' > "$EH/.config/capsule-corp/ops.env"
# the architect job walks host.json maintenance.scan_repos (no project name in run.sh)
mkdir -p "$T/ch-scan" && printf '{"maintenance": {"scan_repos": ["sample-app", "other-app"]}}' > "$T/ch-scan/host.json"
out="$(HOME="$EH" CAPSULE_HOME="$T/ch-scan" RUN_SH_CAPSULE_CORP_DIR="$REPO" RUN_SH_MAINT_DIR="$REPO/maintenance" RUN_SH_WEEKDAY_OVERRIDE=3 RUN_SH_DRY_RUN=1 bash "$REPO/maintenance/run.sh" architect 2>&1)"
pcheck architect "architect walks the scan_repos from host.json" 'grep -q "architect: changed: sample-app(->none) other-app(->none)" <<<"$out"'
out="$(HOME="$EH" CAPSULE_HOME="$T/ch-scan-none" RUN_SH_CAPSULE_CORP_DIR="$REPO" RUN_SH_MAINT_DIR="$REPO/maintenance" RUN_SH_WEEKDAY_OVERRIDE=3 RUN_SH_DRY_RUN=1 bash "$REPO/maintenance/run.sh" architect 2>&1)"
pcheck architect "architect with no scan_repos configured says so and skips" 'grep -q "architect skipped: no scan_repos configured; skipping" <<<"$out" && ! grep -q "all repos unchanged" <<<"$out"'
mkdir -p "$T/ch-bad" && printf '{"maintenance": {"scan_repos": "x y"}}' > "$T/ch-bad/host.json"
out="$(HOME="$EH" CAPSULE_HOME="$T/ch-bad" RUN_SH_CAPSULE_CORP_DIR="$REPO" RUN_SH_MAINT_DIR="$REPO/maintenance" RUN_SH_WEEKDAY_OVERRIDE=3 RUN_SH_DRY_RUN=1 bash "$REPO/maintenance/run.sh" architect 2>&1)"
pcheck architect "a bad scan_repos value says so on stderr and the job still ends cleanly" 'grep -q "maintenance.scan_repos not readable" <<<"$out" && grep -q "no scan_repos configured; skipping" <<<"$out"'
out="$(HOME="$EH" CAPSULE_HOME="$CH" RUN_SH_MAINT_DIR="$REPO/maintenance" RUN_SH_WEEKDAY_OVERRIDE=6 RUN_SH_DRY_RUN=1 bash "$REPO/maintenance/run.sh" security 2>&1)"; rc=$?
pcheck security "flag off does not skip security even on a Saturday" \
  '[ $rc -eq 0 ] && grep -q "dry run: nothing executed, nothing committed" <<<"$out" && ! grep -q "Saturday zero" <<<"$out"'
printf 'OPS_SATURDAY_ZERO=1\n' > "$EH/.config/capsule-corp/ops.env"
out="$(HOME="$EH" CAPSULE_HOME="$CH" RUN_SH_MAINT_DIR="$REPO/maintenance" RUN_SH_WEEKDAY_OVERRIDE=3 RUN_SH_DRY_RUN=1 bash "$REPO/maintenance/run.sh" security 2>&1)"; rc=$?
pcheck security "flag on does not skip security on a non-Saturday (weekday 3)" \
  '[ $rc -eq 0 ] && grep -q "dry run: nothing executed, nothing committed" <<<"$out" && ! grep -q "Saturday zero" <<<"$out"'
out="$(HOME="$EH" RUN_SH_MAINT_DIR="$REPO/maintenance" RUN_SH_WEEKDAY_OVERRIDE=6 RUN_SH_DRY_RUN=1 bash "$REPO/maintenance/run.sh" heal 2>&1)"; rc=$?
check "flag on + Saturday does not touch a third job (heal)" \
  '[ $rc -eq 0 ] && grep -q "would run heal.sh" <<<"$out" && ! grep -q "Saturday zero" <<<"$out"'
: > "$EH/.config/capsule-corp/ops.env"

# --- C1/T10-I1: ops.env is sourced with `.`, which sets but does NOT export -
# same boundary as the CAPSULE_BOARD_VIEW/OPS_WINDOW_PREFLIGHT checks above.
# The naive version of this check (an env-var prefix on the run.sh call) is
# vacuous: `VAR=1 bash run.sh ...` already puts VAR in run.sh's own process
# environment regardless of whether run.sh exports it itself, so deleting the
# `export` lines under test would not fail it. Route the values through a
# scratch ops.env instead, the same pattern used above.
printf 'OPS_BRIEF_V2=1\nOPS_LOG_TARGET=telegram:-100:119\nOPS_SATURDAY_ZERO=1\n' \
  > "$EH/.config/capsule-corp/ops.env"
env_out="$(HOME="$EH" RUN_SH_MAINT_DIR="$T/maint" RUN_SH_PRINT_ENV=1 \
  bash "$REPO/maintenance/run.sh" sprint-sync 2>&1)"
check "ops.env OPS_BRIEF_V2=1 is exported to children" \
  'grep -qx "OPS_BRIEF_V2=1" <<<"$env_out"'
# M-4: OPS_LOG_TARGET is deliberately NOT exported (unlike its C8 siblings) -
# board_bridge.py reads it straight from the ops.env FILE via _OPS at import
# time, never from the environment, so exporting it would be a dead line.
check "ops.env OPS_LOG_TARGET is deliberately not exported to children" \
  '! grep -q "^OPS_LOG_TARGET=" <<<"$env_out"'
check "ops.env OPS_SATURDAY_ZERO=1 is exported to children" \
  'grep -qx "OPS_SATURDAY_ZERO=1" <<<"$env_out"'
: > "$EH/.config/capsule-corp/ops.env"
env_out="$(HOME="$EH" RUN_SH_MAINT_DIR="$T/maint" RUN_SH_PRINT_ENV=1 \
  bash "$REPO/maintenance/run.sh" sprint-sync 2>&1)"
check "an ops.env without the C8 flags exports nothing for the other two (unset = off)" \
  'grep -qx "OPS_BRIEF_V2=" <<<"$env_out" &&
   grep -qx "OPS_SATURDAY_ZERO=" <<<"$env_out"'

# --- M214 (row 137): the healthchecks.io dead-man ping ----------------------
# Every other watchdog in Capsule Corp is a container on the laptop it
# watches; this is the only signal that survives the machine sleeping. It must
# be impossible for it to change a job's outcome.
#
# This job dispatch happens through a real (non-dry) run.sh invocation, so it
# gets its own scratch MAINT_DIR (fake backup.sh/cleanup.sh, no PROMPT_FILE
# needed - both are pure-bash jobs) rather than the $REPO/maintenance one the
# rest of this suite points RUN_SH_MAINT_DIR at.
FAKEBIN="$T/bin"; mkdir -p "$FAKEBIN"; export PATH="$FAKEBIN:$PATH"
HC_MAINT="$T/hc-maint"; mkdir -p "$HC_MAINT"
STATE="$HC_MAINT/state"
cat >"$HC_MAINT/backup.sh" <<'EOF'
#!/usr/bin/env bash
[ "${RUN_SH_FORCE_FAIL:-0}" = 1 ] && exit 3
exit 0
EOF
cat >"$HC_MAINT/cleanup.sh" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod +x "$HC_MAINT/backup.sh" "$HC_MAINT/cleanup.sh"

run_job() {  # <job> -> run.sh's exit code; stdout/stderr discarded
  RUN_SH_MAINT_DIR="$HC_MAINT" AGENT_OPS_STATE_DIR="$STATE" EVENTS_SPOOL="$T/hc-events.jsonl" \
    bash "$REPO/maintenance/run.sh" "$1" >/dev/null 2>&1
}

run_job_out() {  # <job> -> the job's own log file content on stdout; returns
                 # run.sh's own exit code (run.sh redirects the whole job
                 # body, hc_ping's stderr included, into
                 # $MAINT_DIR/logs/<job>-<stamp>.log - not the caller's stdout)
  local job_rc
  RUN_SH_MAINT_DIR="$HC_MAINT" AGENT_OPS_STATE_DIR="$STATE" EVENTS_SPOOL="$T/hc-events.jsonl" \
    bash "$REPO/maintenance/run.sh" "$1" >/dev/null 2>&1
  job_rc=$?
  local logf; logf="$(ls -t "$HC_MAINT/logs/$1"-*.log 2>/dev/null | head -1)"
  [ -n "$logf" ] && cat "$logf"
  return "$job_rc"
}

UUID="00000000-0000-4000-8000-000000000000"     # fixture, not a real check
# C9 fix wave 1 item 1: the uuid must never land on curl's argv (or `ps`), so
# the fake records argv and stdin SEPARATELY - HC_LOG for argv (must never
# carry the uuid), HC_STDIN_LOG for the piped `-K -` config text (must carry
# the url, uuid included - that is the only place it may appear).
cat >"$FAKEBIN/curl" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$HC_LOG"
cat >>"$HC_STDIN_LOG"
[ "${FAKE_CURL_FAIL:-0}" = 1 ] && exit 7
exit 0
EOF
cat >"$FAKEBIN/capsule-secret" <<'EOF'
#!/usr/bin/env bash
# $1=get $2=project $3=NAME
[ "$1" = get ] || exit 2
case "$3" in
  HC_PING_BACKUP_UUID) printf '%s\n' "${FAKE_HC_UUID-}" ;;
  *) exit 1 ;;
esac
EOF
chmod +x "$FAKEBIN/curl" "$FAKEBIN/capsule-secret"

export HC_LOG="$T/hc.log" HC_STDIN_LOG="$T/hc-stdin.log" \
       CURL_BIN="$FAKEBIN/curl" CAPSULE_SECRET_BIN="$FAKEBIN/capsule-secret"

: >"$HC_LOG"; : >"$HC_STDIN_LOG"
start=$(date +%s); out="$(FAKE_HC_UUID="" run_job_out backup)"; rc=$?; dur=$(( $(date +%s) - start ))
check "no uuid in Potara: no ping, job unaffected" '[ $rc -eq 0 ] && [ ! -s "$HC_LOG" ]'
check "no uuid: skips fast (well under a second), no retries, one logged line" \
  '[ $dur -le 2 ] && grep -qiF "no ping sent" <<<"$out"'

: >"$HC_LOG"; : >"$HC_STDIN_LOG"
start=$(date +%s)
out="$(CAPSULE_SECRET_BIN="$FAKEBIN/does-not-exist" FAKE_HC_UUID="$UUID" run_job_out backup)"; rc=$?
dur=$(( $(date +%s) - start ))
check "missing capsule-secret: no ping, job unaffected" '[ $rc -eq 0 ] && [ ! -s "$HC_LOG" ]'
check "missing capsule-secret: skips fast (well under a second), no retries, one logged line" \
  '[ $dur -le 2 ] && grep -qiF "no ping sent" <<<"$out"'

: >"$HC_LOG"; : >"$HC_STDIN_LOG"; FAKE_HC_UUID="$UUID" run_job backup; rc=$?
check "uuid present: exactly one ping, no /fail" \
  '[ $rc -eq 0 ] && [ "$(wc -l <"$HC_STDIN_LOG" | tr -d " ")" = "1" ] && grep -qF "https://hc-ping.com/$UUID" "$HC_STDIN_LOG" && ! grep -qF "/fail" "$HC_STDIN_LOG"'
check "the ping body is the job name and exit code only" 'grep -qF -- "--data-binary backup exit 0" "$HC_LOG"'
check "the uuid never appears on curl's argv (only on stdin, via -K -)" \
  '! grep -qF "$UUID" "$HC_LOG" && grep -qF -- "-K -" "$HC_LOG"'

: >"$HC_LOG"; : >"$HC_STDIN_LOG"; FAKE_HC_UUID="$UUID" RUN_SH_FORCE_FAIL=1 run_job backup; rc=$?
check "a failed backup pings /fail and keeps its own exit code" \
  '[ $rc -ne 0 ] && grep -qF "https://hc-ping.com/$UUID/fail" "$HC_STDIN_LOG" && ! grep -qF "$UUID" "$HC_LOG"'

: >"$HC_LOG"; : >"$HC_STDIN_LOG"; FAKE_HC_UUID="not-a-uuid" run_job backup; rc=$?
check "a malformed uuid is refused, no call made, job unaffected" \
  '[ $rc -eq 0 ] && [ ! -s "$HC_LOG" ]'

: >"$HC_LOG"; : >"$HC_STDIN_LOG"; FAKE_HC_UUID="$UUID" FAKE_CURL_FAIL=1 run_job backup; rc=$?
check "a failing curl never fails the job and never changes the stamp" \
  '[ $rc -eq 0 ] && [ "$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))[\"exit\"])" "$STATE/jobs/backup.json")" = "0" ]'

: >"$HC_LOG"; : >"$HC_STDIN_LOG"; FAKE_HC_UUID="$UUID" run_job cleanup; rc=$?
check "a job outside OPS_HC_JOBS never pings" '[ ! -s "$HC_LOG" ]'

: >"$HC_LOG"; : >"$HC_STDIN_LOG"; FAKE_HC_UUID="$UUID" RUN_SH_DRY_RUN=1 run_job backup
check "a dry run never pings" '[ ! -s "$HC_LOG" ]'

check "run.sh carries the hc-ping.com literal exactly once" \
  '[ "$(grep -c "hc-ping\.com" "$REPO/maintenance/run.sh")" = 1 ]'

check "no run.sh run in this suite reached a real claude, curl, gh or docker (tripwires silent)" \
  '[ ! -e "$TRIPWIRE_MARK" ]'

# D254: the vault is a host setting. Env var wins; else host.json vault.path; else unset, and the
# vault jobs skip cleanly (rc 0) instead of exporting an empty or made-up path.
VH="$T/vault-host"; mkdir -p "$VH/home" "$VH/chome" "$VH/hostvault" "$VH/envvault"
printf '{"vault": {"path": "%s"}}\n' "$VH/hostvault" > "$VH/chome/host.json"
vrun() {  # vrun <extra env assignments...> <command>: the vault env var is unset unless the case sets it
  env -u OBSIDIAN_VAULT_PATH HOME="$VH/home" CAPSULE_HOME="$VH/chome" RUN_SH_CAPSULE_CORP_DIR="$REPO" \
    RUN_SH_MAINT_DIR="$REPO/maintenance" "$@"
}
env_out="$(vrun OBSIDIAN_VAULT_PATH="$VH/envvault" RUN_SH_PRINT_ENV=1 bash "$REPO/maintenance/run.sh" sprint-sync 2>&1)"
check "vault: the env var wins over host.json" 'grep -qx "OBSIDIAN_VAULT_PATH=$VH/envvault" <<<"$env_out"'
env_out="$(vrun RUN_SH_PRINT_ENV=1 bash "$REPO/maintenance/run.sh" sprint-sync 2>&1)"
check "vault: host.json vault.path is used when the env var is unset" 'grep -qx "OBSIDIAN_VAULT_PATH=$VH/hostvault" <<<"$env_out"'
# launchd's bare PATH finds an old system python3 that rejects -P: run.sh must resolve only after it
# rebuilds PATH, so a failing python3 ahead of it in the caller's PATH must not matter.
mkdir -p "$VH/stub"; printf '#!/bin/sh\necho "python3: Unknown option: -P" >&2\nexit 2\n' > "$VH/stub/python3"; chmod +x "$VH/stub/python3"
env_out="$(PATH="$VH/stub:$PATH" vrun RUN_SH_PRINT_ENV=1 bash "$REPO/maintenance/run.sh" sprint-sync 2>&1)"
check "vault: a failing python3 in the caller's PATH does not hide the host.json vault" 'grep -qx "OBSIDIAN_VAULT_PATH=$VH/hostvault" <<<"$env_out"'
check "vault: run.sh and report.sh resolve the vault only after rebuilding PATH" \
  'for f in run.sh report.sh; do [ "$(grep -n "^export PATH=" "$REPO/maintenance/$f" | head -1 | cut -d: -f1)" -lt "$(grep -n "^vault_resolve " "$REPO/maintenance/$f" | head -1 | cut -d: -f1)" ] || exit 1; done'
env_out="$(vrun RUN_SH_CAPSULE_CORP_DIR="$VH/no-checkout" RUN_SH_PRINT_ENV=1 bash "$REPO/maintenance/run.sh" sprint-sync 2>&1)"
check "vault: an unreadable host setting prints one stderr line and leaves the vault unset" \
  '[ "$(grep -c "^vault_resolve: " <<<"$env_out")" = 1 ] && grep -qx "OBSIDIAN_VAULT_PATH=" <<<"$env_out"'
printf '{}\n' > "$VH/chome/host.json"
env_out="$(vrun RUN_SH_PRINT_ENV=1 bash "$REPO/maintenance/run.sh" sprint-sync 2>&1)"
check "vault: neither set exports nothing" 'grep -qx "OBSIDIAN_VAULT_PATH=" <<<"$env_out"'
for job in nightly agent-review; do
  have_prompt "$job" || { echo "SKIP vault: neither set skips $job: maintenance/$job.md is private-only (not in the public copy)"; continue; }
  out_nv="$(vrun RUN_SH_DRY_RUN=1 EVENTS_SPOOL="$VH/ev-$job.jsonl" bash "$REPO/maintenance/run.sh" $job 2>&1)"; rc=$?
  check "vault: neither set skips $job with rc 0" '[ $rc -eq 0 ] && grep -q "$job skipped: no notes vault configured" <<<"$out_nv"'
done
out_nv="$(vrun RUN_SH_DRY_RUN=1 EVENTS_SPOOL="$VH/ev2.jsonl" bash "$REPO/maintenance/run.sh" backup 2>&1)"; rc=$?
check "vault: neither set still runs a job that needs no vault" '[ $rc -eq 0 ] && ! grep -q "no notes vault configured" <<<"$out_nv"'
out_nv="$(vrun RUN_SH_DRY_RUN=1 EVENTS_SPOOL="$VH/ev3.jsonl" bash "$REPO/maintenance/run.sh" quality-full 2>&1)"; rc=$?
check "vault: neither set still runs quality-full (the nightly full suite is not skipped)" '[ $rc -eq 0 ] && ! grep -q "no notes vault configured" <<<"$out_nv"'

# D254: the skill checkout is a host setting too. Env var wins; else host.json vault.skill_root; else unset,
# the jobs that run the skill skip (rc 0) and fork-sync, which is opt-in, skips.
VS="$T/skill-host"; mkdir -p "$VS/home" "$VS/chome" "$VS/hostskill" "$VS/envskill" "$VS/vault"
printf '{"vault": {"skill_root": "%s"}}\n' "$VS/hostskill" > "$VS/chome/host.json"
srun() {  # srun <extra env assignments...> <command>: a vault is set, the skill env var is unset unless the case sets it
  env -u OBSIDIAN_SKILL_ROOT OBSIDIAN_VAULT_PATH="$VS/vault" HOME="$VS/home" CAPSULE_HOME="$VS/chome" \
    RUN_SH_CAPSULE_CORP_DIR="$REPO" RUN_SH_MAINT_DIR="$REPO/maintenance" "$@"
}
env_out="$(srun OBSIDIAN_SKILL_ROOT="$VS/envskill" RUN_SH_PRINT_ENV=1 bash "$REPO/maintenance/run.sh" sprint-sync 2>&1)"
check "skill root: the env var wins over host.json" 'grep -qx "OBSIDIAN_SKILL_ROOT=$VS/envskill" <<<"$env_out"'
env_out="$(srun RUN_SH_PRINT_ENV=1 bash "$REPO/maintenance/run.sh" sprint-sync 2>&1)"
check "skill root: host.json vault.skill_root is used when the env var is unset" 'grep -qx "OBSIDIAN_SKILL_ROOT=$VS/hostskill" <<<"$env_out"'
printf '{}\n' > "$VS/chome/host.json"
env_out="$(srun RUN_SH_PRINT_ENV=1 bash "$REPO/maintenance/run.sh" sprint-sync 2>&1)"
check "skill root: neither set exports nothing" 'grep -qx "OBSIDIAN_SKILL_ROOT=" <<<"$env_out"'
for job in nightly inbox architect; do
  have_prompt "$job" || { echo "SKIP skill root: neither set skips $job: maintenance/$job.md is private-only (not in the public copy)"; continue; }
  out_ns="$(srun RUN_SH_DRY_RUN=1 EVENTS_SPOOL="$VS/ev-$job.jsonl" bash "$REPO/maintenance/run.sh" $job 2>&1)"; rc=$?
  check "skill root: neither set skips $job with rc 0" '[ $rc -eq 0 ] && grep -q "$job skipped: no notes-vault skill checkout configured" <<<"$out_ns"'
done
out_ns="$(srun RUN_SH_DRY_RUN=1 EVENTS_SPOOL="$VS/ev-fork.jsonl" bash "$REPO/maintenance/run.sh" fork-sync 2>&1)"; rc=$?
check "skill root: neither set skips the opt-in fork-sync with rc 0" '[ $rc -eq 0 ] && grep -q "fork-sync skipped: fork-sync is opt-in" <<<"$out_ns"'
out_ns="$(srun OBSIDIAN_SKILL_ROOT="$VS/envskill" RUN_SH_DRY_RUN=1 EVENTS_SPOOL="$VS/ev-fork2.jsonl" bash "$REPO/maintenance/run.sh" fork-sync 2>&1)"; rc=$?
check "skill root: fork-sync runs on the configured skill checkout" '[ $rc -eq 0 ] && grep -qF "in $VS/envskill," <<<"$out_ns"'

# PATH order: run.sh rebuilds PATH before sourcing lib_config.sh, so a caller whose PATH starts with a
# broken python3 still resolves host.json config_dir (the lookup must not fall back to the default).
PO="$T/pathorder"; mkdir -p "$PO/stub" "$PO/chome" "$PO/home"
printf '#!/bin/sh\nexit 3\n' >"$PO/stub/python3"; chmod +x "$PO/stub/python3"
printf '{"config_dir": "%s/cfg"}\n' "$PO" >"$PO/chome/host.json"
out_po="$(env -u CAPSULE_JOBS_DIR -u RUN_SH_MAINT_DIR -u AGENT_OPS_HOME PATH="$PO/stub:$PATH" HOME="$PO/home" CAPSULE_HOME="$PO/chome" \
  bash "$REPO/maintenance/run.sh" inbox 2>&1)"
check "PATH order: a broken python3 first on the caller's PATH still resolves host.json config_dir" \
  'grep -qF "no prompt file at $PO/cfg/maintenance/inbox.md" <<<"$out_po"'

echo; if [ $fails -eq 0 ]; then echo "test_run_sh: all passed"; else echo "test_run_sh: $fails failed"; exit 1; fi
