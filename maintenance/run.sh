#!/usr/bin/env bash
# Headless Claude vault-maintenance runner. Invoked by launchd (see
# launchd/templates/*.obsidian-*.plist in the capsule-corp repo).
#
# Usage: run.sh <nightly|morning|weekly|health-check|inbox|architect|sprint-plan|sprint-sync|reconciler|emerge|retrieval-eval|heal|obs-probe|obs-inventory|cleanup|backup|restore-verify|fork-sync|quality-nightly|deps-sync>
#
# Runs `claude -p` non-interactively in the vault with the matching prompt
# from this directory, then git-commits whatever changed so every autonomous
# run is a revertible checkpoint. Model is chosen per job: Sonnet for the
# judgement-heavy passes. A few jobs (sprint-sync, retrieval-eval, morning,
# health-check, heal, cleanup, backup, restore-verify,
# fork-sync, quality-nightly, deps-sync) are pure Python/bash and never touch a model at all - see their
# branches above the `# ---- Claude jobs` marker.
#
# Every run, whatever its path, leaves state/jobs/<job>.json (start, end, exit,
# duration, commit, skip reason) - status.sh and the daily digest read those.
# A job that has nothing to do skips before any Claude session starts. A job
# that fails records exit != 0 on its kind=job event; Grafana's job-failed rule
# owns the alert, so run.sh no longer sends one itself.
#
# Env knobs: RUN_SH_DRY_RUN=1 (resolve everything, run nothing), RUN_SH_FORCE_FAIL=1
# (simulate a Claude failure - exercises the alert path), JOB_CAP_SECONDS,
# SPRINT_MAX_CARDS, OBSIDIAN_VAULT_PATH.
#
# This script and its prompts live OUTSIDE the vault on purpose - keeping them
# in the vault made vault_health / search / the graph treat them as notes.

set -u

JOB="${1:?usage: run.sh <nightly|morning|weekly|health-check|inbox|architect|sprint-plan|sprint-sync|retrieval-eval|security|agent-review|heal|obs-probe|obs-inventory|cleanup|close|blocked-tonight|order-book-lint|backup|restore-verify|fork-sync|quality-nightly|deps-sync|remove-card|ops-preview|disk-relief|quality-full>}"
# The retired `observability` job name is refused here, above every other
# guard - before the job-stamp trap exists, before PROMPT_FILE is even
# resolved, and long before the job-start event would be logged - so a call
# with this name writes no job event and prints no model line, just the
# pointer to its replacement.
if [ "$JOB" = "observability" ]; then echo "run.sh: the observability job is now 'heal'" >&2; exit 2; fi

# Overridable for CI: a fresh checkout has no deploy.sh-created symlink at
# $CAPSULE_JOBS_DIR/maintenance, so tests/test_run_sh.sh points
# this at the checked-out repo's own maintenance/ instead.
# launchd hands us a bare environment - rebuild enough PATH for claude/uv/git/node. First, before
# lib_config.sh: its host.json config_dir lookup needs a python3 of 3.11+, not the system one.
# RUN_SH_PATH_PREPEND: test-only; a dir of fakes/tripwires that must win over the real tools.
export PATH="${RUN_SH_PATH_PREPEND:+$RUN_SH_PATH_PREPEND:}/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.local/bin"
. "$(dirname "${BASH_SOURCE[0]}")/lib_config.sh"   # CAPSULE_JOBS_DIR
MAINT_DIR="${RUN_SH_MAINT_DIR:-$CAPSULE_JOBS_DIR/maintenance}"
# Row 470/473: weekly's own capsule unblocked capture (below) runs in this repo.
CAPSULE_CORP_DIR="${RUN_SH_CAPSULE_CORP_DIR:-$HOME/CodeBase/capsule-corp}"
. "$(dirname "${BASH_SOURCE[0]}")/lib_vault.sh"
# Plain python3 (C28): every script OPS_PY runs is stdlib-only. Overridable for tests;
# the prompts spell it __OPS_PY__.
# CAPSULE_OPS_PY is the neutral name (CAPSULE_PY is a different knob: the python for `-m capsule`).
OPS_PY="${CAPSULE_OPS_PY:-python3}"
PROMPT_FILE="$MAINT_DIR/${JOB}.md"
LOG_DIR="$MAINT_DIR/logs"
. "$(dirname "${BASH_SOURCE[0]}")/lib_state.sh"   # STATE_DIR: $AGENT_OPS_STATE_DIR or ~/.local/state/capsule-corp/maint
. "$(dirname "${BASH_SOURCE[0]}")/lib_docker_engine.sh"   # DOCKER_ENGINE*, DOCKER_CONTEXT, docker_path_export
. "$(dirname "${BASH_SOURCE[0]}")/lib_send.sh"
JOBS_DIR="$STATE_DIR/jobs"
STAMP="$(date +%Y-%m-%dT%H%M%S)"
LOG="$LOG_DIR/${JOB}-${STAMP}.log"
ENV_DRY="${DRY:-0}"   # disk-relief alone also honours a plain DRY=1 (its dry run is a safe preview)
DRY="${RUN_SH_DRY_RUN:-0}"
# RUN_SH_DRY_RUN=1: resolve everything, print the claude command, run nothing, commit nothing.
[ "$DRY" = "1" ] && LOG=/dev/stdout

# VAULT: $OBSIDIAN_VAULT_PATH, else host.json vault.path, else empty (vault jobs skip). After the PATH
# rebuild on purpose: launchd's bare PATH finds a system python3 too old for the reader.
vault_resolve "$CAPSULE_CORP_DIR" "$OPS_PY"
# OSB_DIR: the notes-vault skill checkout, $OBSIDIAN_SKILL_ROOT, else host.json vault.skill_root, else empty.
# Prompts name it __SKILL_ROOT__ and the vault __VAULT__; both are rendered per run. No default directory.
skill_root_resolve "$CAPSULE_CORP_DIR"

mkdir -p "$LOG_DIR" "$JOBS_DIR"

# Event trail: every job start/end and every phone message the trail can see.
# ev_log never fails the job over trail plumbing (spool dir missing, python
# import error, etc.) - a lost event row is not worth an alert.
EV="$OPS_PY $MAINT_DIR/events.py"
ev_log() { $EV log "$@" >/dev/null 2>&1 || true; }

# Chat profile and delivery targets come from ops.env (written by deploy.sh for the
# chosen chat platform). Empty = no chat: sends become no-ops.
OPS_ENV="$CAPSULE_JOBS_DIR/ops.env"; [ -f "$OPS_ENV" ] && . "$OPS_ENV"
# `.` sources ops.env into THIS shell without exporting: a variable set there is
# invisible to the Python children run.sh spawns unless it is exported by name.
# CAPSULE_BOARD_VIEW=0 is the whole rollout brake (board_view.enabled()), and it
# is read inside board_bridge.py - so it has to cross that boundary.
[ -n "${CAPSULE_BOARD_VIEW:-}" ] && export CAPSULE_BOARD_VIEW
# Same boundary, same reason: the window pre-flight flag and its two floors
# (design 2026-09-09-power-level section 7.4) are read inside board_bridge.py's
# dispatch path, so an unexported ops.env value leaves the child on the
# built-in defaults - i.e. the flag reads as OFF no matter what ops.env says.
[ -n "${OPS_WINDOW_PREFLIGHT:-}" ] && export OPS_WINDOW_PREFLIGHT
[ -n "${OPS_WINDOW_PREFLIGHT_FLOOR_PCT:-}" ] && export OPS_WINDOW_PREFLIGHT_FLOOR_PCT
[ -n "${OPS_WINDOW_PREFLIGHT_MIN_S:-}" ] && export OPS_WINDOW_PREFLIGHT_MIN_S
# Same boundary, same reason (M210, row 137): backup.sh is a child process, so
# an unexported OPS_BACKUP_ENCRYPT from ops.env reads as OFF in the job that
# is supposed to act on it.
[ -n "${OPS_BACKUP_ENCRYPT:-}" ] && export OPS_BACKUP_ENCRYPT
# Same boundary, same reason (row 137, C9/M215): board_bridge.py is a child
# process too, so an unexported OPS_ABSENCE_PROTOCOL from ops.env reads as
# OFF in the absence check.
[ -n "${OPS_ABSENCE_PROTOCOL:-}" ] && export OPS_ABSENCE_PROTOCOL
# Same boundary, same reason (row 139 / C11): OPS_BET_FIELD is read inside
# board.bet_enabled(), called from board_bridge.py's dispatch path and
# intake.py's consume path - both children of this script. Unexported means
# the flag reads OFF whatever ops.env says: safe, and wrong.
[ -n "${OPS_BET_FIELD:-}" ] && export OPS_BET_FIELD
# Same boundary, same reason (row 136 / C8): report.sh and brief.py are child
# processes, so an unexported OPS_BRIEF_V2 from ops.env reads as OFF in the
# job that composes the two scheduled messages.
[ -n "${OPS_BRIEF_V2:-}" ] && export OPS_BRIEF_V2
# M-4: OPS_LOG_TARGET is deliberately NOT exported here, unlike its C8
# siblings above - board_bridge.py never reads it from the environment at
# all; it reads OPS_LOG_TARGET straight out of the ops.env FILE via _OPS at
# import time. Exporting it here would be a dead line that reads like it
# matters and does not.
[ -n "${OPS_SATURDAY_ZERO:-}" ] && export OPS_SATURDAY_ZERO
[ -n "${OPS_ACTIONABLE_MAX:-}" ] && export OPS_ACTIONABLE_MAX
# Inspection hatch for tests/test_run_sh.sh: `printenv` is an external command,
# so it sees only EXPORTED variables - exactly the property asserted. Prints and
# exits before any job runs, so the script stays pure.
if [ "${RUN_SH_PRINT_ENV:-0}" = "1" ]; then
  echo "CAPSULE_BOARD_VIEW=$(printenv CAPSULE_BOARD_VIEW || true)"
  echo "OPS_WINDOW_PREFLIGHT=$(printenv OPS_WINDOW_PREFLIGHT || true)"
  echo "OPS_WINDOW_PREFLIGHT_FLOOR_PCT=$(printenv OPS_WINDOW_PREFLIGHT_FLOOR_PCT || true)"
  echo "OPS_WINDOW_PREFLIGHT_MIN_S=$(printenv OPS_WINDOW_PREFLIGHT_MIN_S || true)"
  echo "OBSIDIAN_VAULT_PATH=$(printenv OBSIDIAN_VAULT_PATH || true)"
  echo "OBSIDIAN_SKILL_ROOT=$(printenv OBSIDIAN_SKILL_ROOT || true)"
  echo "OPS_BET_FIELD=$(printenv OPS_BET_FIELD || true)"
  echo "OPS_BRIEF_V2=$(printenv OPS_BRIEF_V2 || true)"
  echo "OPS_SATURDAY_ZERO=$(printenv OPS_SATURDAY_ZERO || true)"
  echo "OPS_ACTIONABLE_MAX=$(printenv OPS_ACTIONABLE_MAX || true)"
  exit 0
fi
STANDUP_TARGET="${OPS_ALERT_TARGET:-}"
ACTIVITY_TARGET="${OPS_ACTIVITY_TARGET:-}"
# Phone messages: deliver_via_slack comes from lib_send.sh (sourced above).
tg_send() {
  local body; body="$(cat)"
  local TO="" SUBJ="" prev=""
  for a in "$@"; do
    [ "$prev" = "--to" ] && TO="$a"
    [ "$prev" = "--subject" ] && SUBJ="$a"
    prev="$a"
  done
  ev_log message run.sh "$body" --title "$JOB" ${TO:+--target "$TO"}
  if printf '%s\n' "$body" | deliver_via_slack; then return 0; fi
  # TG_SEND_NO_SPOOL=1: report the failure, spool nothing. heal.sh's own
  # outbox flush sets it, so a failed replay leaves exactly the file it was
  # replaying and never a second copy of the same message.
  [ "${TG_SEND_NO_SPOOL:-0}" = 1 ] && return 1
  # Fail closed on the token, not on the message: spool it for heal.sh to
  # flush (spec section 6). Alert/report text only - never a secret.
  # The header is three lines - to=, at=, subject= (possibly empty) - then a
  # blank line, then the body; heal.sh's flush reads them back by name and
  # passes --subject on only when it is non-empty.
  mkdir -p "$STATE_DIR/outbox"
  printf 'to=%s\nat=%s\nsubject=%s\n\n%s\n' "$TO" "$(date +%Y-%m-%dT%H:%M:%S%z)" "$SUBJ" "$body" >"$STATE_DIR/outbox/$(date +%s)-$JOB.msg"
  return 1
}

# Wall-clock cap per Claude invocation. macOS has no `timeout`; perl's alarm is
# always there. 40 min for inbox and agent-review (ingests and the monthly
# review are long), 30 min for nightly (the semantic reindex runs inside it now,
# and a full rebuild after a chunker change takes 10-12 min - at the old 15 min
# it could be killed mid-rebuild), 15 min for everything else.
case "$JOB" in
  inbox|agent-review) JOB_CAP_SECONDS="${JOB_CAP_SECONDS:-2400}" ;;
  nightly)            JOB_CAP_SECONDS="${JOB_CAP_SECONDS:-1800}" ;;
  quality-nightly)    JOB_CAP_SECONDS="${JOB_CAP_SECONDS:-3600}" ;;
  quality-full)       JOB_CAP_SECONDS="${JOB_CAP_SECONDS:-17400}" ;;   # 4 suites, each capped at 5400s; 17400 is below the 21600 worst case (timeouts are skips)
  backup|restore-verify) JOB_CAP_SECONDS="${JOB_CAP_SECONDS:-900}" ;;
  disk-relief)        JOB_CAP_SECONDS="${JOB_CAP_SECONDS:-2400}" ;;
  *)                  JOB_CAP_SECONDS="${JOB_CAP_SECONDS:-900}" ;;
esac
with_cap() { perl -e 'alarm shift @ARGV; exec @ARGV or die "exec: $!"' "$JOB_CAP_SECONDS" "$@"; }
# Nightly vault eval (spec section 6): a non-zero exit or the cap (142) raises one notice event
# through ev_log (the trail the alert rules read); it always returns 0 so the nightly goes on.
vault_eval_nightly() {
  local rc
  OBSIDIAN_SKILL_ROOT="${OBSIDIAN_SKILL_ROOT:-$OSB_DIR}" OBSIDIAN_VAULT_PATH="$VAULT" JOB_CAP_SECONDS=300 \
    with_cap "$OPS_PY" "$CAPSULE_CORP_DIR/bin/vault-eval" nightly
  rc=$?
  [ "$rc" -eq 0 ] && return 0
  echo "=== vault-eval exit $rc (ignored) ==="
  local why="exit $rc"; [ "$rc" -eq 142 ] && why="timed out (300s cap)"
  ev_log audit run.sh "vault-eval nightly did not complete: $why" --title vault-eval --severity notice
  return 0
}

# --- job stamp: every exit path (skip, success, failure, crash) records itself.
START_EPOCH="$(date +%s)"
JOB_EXIT=""      # set explicitly on the normal paths; the trap falls back to $?
JOB_SKIP=""      # non-empty = the job did nothing on purpose (never alerts)
JOB_COMMIT=""    # short sha when the run committed
SESSION_ID=""    # claude session id (--output-format json), for the `claude -r` resume pointer

# --- healthchecks.io dead-man ping (M214, row 137, spec section 7.4).
# The one signal in Capsule Corp that survives its own host: every other
# watchdog - Grafana's backup-missing rule included - is a container on the
# laptop it is watching, so a machine asleep at 03:35 takes its own alarm
# clock down with it (founder's-week study section 5: "slept 08:31-10:31 on
# battery with the lid closed").
# The uuid, and ONLY the uuid, comes from Potara at run time; the host literal
# lives here on purpose so test_egress.py's scanner can see the fourth door
# (docs/EGRESS.md) and so a tampered secret cannot redirect the request.
# It can never fail a job: it runs after JOB_EXIT is fixed and after the job
# stamp is written, and returns 0 on every path. With no secret in Potara it
# makes no call at all, which is why it needs no flag - creating the secret is
# what turns it on and deleting it is the rollback.
HC_JOBS="${OPS_HC_JOBS:-backup}"
CURL_BIN="${CURL_BIN:-curl}"
: "${CAPSULE_SECRET_BIN:=$(cd -P "$MAINT_DIR/.." 2>/dev/null && pwd -P)/bin/capsule-secret}"
hc_ping() {  # <exit code>; always returns 0
  case " $HC_JOBS " in *" $JOB "*) ;; *) return 0 ;; esac
  [ -z "$JOB_SKIP" ] || return 0   # "nothing to do" is not "healthy"
  [ "$DRY" = "1" ] && return 0
  local var uuid suffix=""
  var="HC_PING_$(printf '%s' "$JOB" | tr '[:lower:]-' '[:upper:]_')_UUID"
  # A missing capsule-secret binary or an empty secret both land here with
  # no retry and no network attempt - "skip" must stay well under a second,
  # not fall through to a 10s+retried curl call for nothing.
  uuid="$(CAPSULE_IDENTITY="${CAPSULE_IDENTITY:-bridge}" \
            "$CAPSULE_SECRET_BIN" get capsule-corp "$var" 2>/dev/null | tr -d '[:space:]')"
  if [ -z "$uuid" ]; then
    echo "run.sh: no healthchecks uuid for $JOB - no ping sent" >&2
    return 0
  fi
  if ! printf '%s' "$uuid" | grep -qE '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'; then
    echo "run.sh: $var is not a healthchecks uuid - no ping sent" >&2
    return 0
  fi
  [ "$1" = 0 ] || suffix="/fail"
  # the uuid is a bearer secret and must never sit on
  # argv or show up in `ps` - curl's -K config-from-stdin form keeps it off
  # the command line entirely; a here-string (never a file) carries the URL
  # as config text. The literal host stays right here, after the trigger, on
  # purpose - it is what lets test_egress.py's scanner see this door.
  "$CURL_BIN" -K - -fsS -m 10 --retry 2 --retry-delay 2 -o /dev/null \
      --data-binary "$JOB exit $1" \
      <<<"$(printf 'url = "https://hc-ping.com/%s%s"\n' "$uuid" "$suffix")" \
    || echo "run.sh: healthchecks ping failed (job unaffected)" >&2
  return 0
}

write_stamp() {
  local dur=$(( $(date +%s) - START_EPOCH ))
  ev_log job run.sh "$JOB exit ${1} ${JOB_SKIP:+skipped: $JOB_SKIP}" --title "$JOB" \
    --severity "$([ "$1" = 0 ] && echo info || echo error)" --state closed \
    --job "$JOB" --phase end --exit "$1" --duration "$dur" ${JOB_SKIP:+--skipped "$JOB_SKIP"} \
    ${SESSION_ID:+--session "$SESSION_ID"}
  [ "$DRY" = "1" ] && return 0
  python3 - "$JOBS_DIR/$JOB.json" "$JOB" "$START_EPOCH" "$(date +%s)" "$1" "$JOB_SKIP" "$JOB_COMMIT" "$LOG" <<'PY'
import json, sys, datetime
path, job, start, end, exit_, skip, commit, log = sys.argv[1:]
iso = lambda e: datetime.datetime.fromtimestamp(int(e)).astimezone().isoformat(timespec="seconds")
json.dump({"job": job, "start": iso(start), "end": iso(end), "duration_s": int(end) - int(start),
           "exit": int(exit_), "skipped": skip or None, "commit": commit or None, "log": log},
          open(path, "w"), indent=1)
PY
  hc_ping "$1"
}
on_exit() { local rc=$?; write_stamp "${JOB_EXIT:-$rc}"; }
trap on_exit EXIT
# An interrupted run (Ctrl-C, launchd stop) must not be stamped as a success.
trap 'JOB_EXIT=130; echo "=== $JOB interrupted ==="; exit 130' INT TERM

# image-gc (R11): the nightly also trims labelled capsule-layer images (image_gc.py header has the
# keep rules). It removes nothing unless OPS_IMAGE_GC_APPLY=1 (ops.env, default off: it prints
# what it would remove plus `docker system df`), and never fails the job. image_gc.py runs docker
# through events.docker_run, which puts the engine's CLI on PATH (a launchd PATH lacks it).
# It needs no vault, so the nightly runs it before the vault skips too; GC_DONE keeps it to one run.
image_gc_step() {
  [ "$JOB" = "nightly" ] && [ -z "${GC_DONE:-}" ] || return 0
  GC_DONE=1
  if [ "$DRY" = "1" ]; then
    echo "=== dry run: would run image_gc.py (OPS_IMAGE_GC_APPLY=${OPS_IMAGE_GC_APPLY:-0}) ==="
  else
    OPS_IMAGE_GC_APPLY="${OPS_IMAGE_GC_APPLY:-0}" JOB_CAP_SECONDS=300 \
      with_cap "$OPS_PY" "$MAINT_DIR/image_gc.py" || echo "=== image_gc.py exit $? (ignored) ==="
  fi
}

skip_job() { JOB_SKIP="$1"; JOB_EXIT=0; echo "=== $JOB skipped: $1 ==="; echo "=== $JOB done $(date) ==="; exit 0; }

# No alert_failure() here on purpose (removed 2026-09-06): every job writes a
# kind=job event with its exit code and Grafana's job-failed rule owns that alert.

# Public copy (no private marker above MAINT_DIR): the nightly prompt is not shipped, so only its model
# step is skipped (below). In the private tree a lost prompt stays exit 1, which alerts.
PUBLIC_NIGHTLY=0
[ "$JOB" = "nightly" ] && [ -d "$MAINT_DIR" ] && [ ! -f "$(cd -P "$MAINT_DIR/.." 2>/dev/null && pwd -P)/release/private-denylist.txt" ] && PUBLIC_NIGHTLY=1
if [ "$JOB" != "ops-preview" ] && [ "$JOB" != "sprint-sync" ] && [ "$JOB" != "retrieval-eval" ] && [ "$JOB" != "heal" ] && [ "$JOB" != "obs-probe" ] && [ "$JOB" != "obs-inventory" ] && [ "$JOB" != "cleanup" ] && [ "$JOB" != "close" ] && [ "$JOB" != "blocked-tonight" ] && [ "$JOB" != "order-book-lint" ] && [ "$JOB" != "morning" ] && [ "$JOB" != "health-check" ] && [ "$JOB" != "backup" ] && [ "$JOB" != "restore-verify" ] && [ "$JOB" != "fork-sync" ] && [ "$JOB" != "quality-nightly" ] && [ "$JOB" != "quality-full" ] && [ "$JOB" != "deps-sync" ] && [ "$JOB" != "disk-relief" ] && [ "$JOB" != "remove-card" ] && [ "$PUBLIC_NIGHTLY" != "1" ] && [ ! -f "$PROMPT_FILE" ]; then
  echo "run.sh: no prompt file at $PROMPT_FILE" >&2
  JOB_EXIT=1; exit 1
fi

case "$JOB" in
  sprint-plan)              MODEL="claude-opus-5" ;;   # specs are the leverage point (the owner, 2026-09-05)
  nightly|inbox|architect|sprint-sync|weekly|security) MODEL="claude-sonnet-5" ;;
  agent-review)             MODEL="${AGENT_REVIEW_MODEL:-claude-fable-5-1}" ;;   # frontier model on purpose
  *)                    MODEL="claude-sonnet-5" ;;
esac

{
  # Script jobs (no Claude session, ever) print no model - saying "model
  # claude-sonnet-5" next to a job that never starts a session is misleading.
  case "$JOB" in
    ops-preview|quality-full|disk-relief|remove-card|order-book-lint|blocked-tonight|sprint-sync|retrieval-eval|heal|obs-probe|obs-inventory|cleanup|close|morning|health-check|backup|restore-verify|fork-sync|quality-nightly|deps-sync)
      echo "=== $JOB start $(date) ===" ;;
    *)
      echo "=== $JOB start $(date) (model $MODEL) ===" ;;
  esac
  ev_log job run.sh "$JOB start" --title "$JOB" --state open --job "$JOB" --phase start
  if [ -n "$VAULT" ]; then
    cd "$VAULT" || { echo "cannot cd to $VAULT"; JOB_EXIT=1; exit 1; }
  else
    # No vault: only these script jobs, none of which commit or push in the working directory, still run
    # (from $HOME). Every other job, every Claude session included, skips; none may write to git in $HOME.
    case "$JOB" in
      ops-preview|quality-full|disk-relief|remove-card|blocked-tonight|sprint-sync|heal|obs-probe|cleanup|backup|restore-verify|fork-sync|quality-nightly|deps-sync) cd "$HOME" ;;
      *) image_gc_step; skip_job "no notes vault configured (OBSIDIAN_VAULT_PATH or host.json vault.path)" ;;
    esac
  fi
  # The jobs that run the vault skill itself need its checkout; with none configured they skip.
  case "$JOB" in
    nightly|inbox|architect|retrieval-eval)
      [ -n "$OSB_DIR" ] || { image_gc_step; skip_job "no notes-vault skill checkout configured (OBSIDIAN_SKILL_ROOT or host.json vault.skill_root)"; } ;;
  esac

  # Vault commit identity: no personal default in the repo. CAPSULE_GIT_NAME and
  # CAPSULE_GIT_EMAIL (e.g. in ops.env) override; otherwise git uses the vault's or the
  # user's own `git config user.name` / `user.email`.
  vault_commit() {
    git ${CAPSULE_GIT_NAME:+-c "user.name=$CAPSULE_GIT_NAME"} ${CAPSULE_GIT_EMAIL:+-c "user.email=$CAPSULE_GIT_EMAIL"} commit "$@"
  }

  # Absence DRAIN (row 137, C9/M215): stop writing specs nobody can approve.
  # skip_job stamps exit 0 with a skip reason, which by design never alerts.
  # no local `!= "0"` parse of OPS_ABSENCE_PROTOCOL here
  # - absence.py's own enabled() (1/true/yes/on, same as every other flag) is
  # the single parser; a second one here could silently drift from it.
  if [ "$JOB" = "sprint-plan" ]; then
    ABS_TIER="$("$OPS_PY" "$MAINT_DIR/absence.py" tier 2>/dev/null || echo 1)"
    [ "${ABS_TIER:-1}" != "1" ] && skip_job "absence drain: tier $ABS_TIER"
  fi

  # sprint-sync: pure Python, no Claude. Runs every 10 min via launchd, so it
  # (a) takes a dir-lock to skip a tick if the previous run is still going -
  # two unsynchronised writers on Boards/Engineering.md would interleave; and
  # (b) prunes its own high-frequency log pile (the Claude-job prune at the end
  # of this script is unreachable from here because this branch exits early).
  if [ "$JOB" = "sprint-sync" ]; then
    LOCK="$LOG_DIR/.sprint-sync.lock"
    find "$LOCK" -maxdepth 0 -mmin +30 -exec rmdir {} \; 2>/dev/null   # reap a crashed run's lock
    if ! mkdir "$LOCK" 2>/dev/null; then
      skip_job "previous tick still holds $LOCK"
    fi
    trap 'rmdir "$LOCK" 2>/dev/null; on_exit' EXIT
    find "$LOG_DIR" -name 'sprint-sync-*.log' -mtime +7 -delete 2>/dev/null

    if [ "$DRY" = "1" ]; then echo "=== dry run: would run board_bridge.py sync ==="; JOB_EXIT=0; exit 0; fi
    "$OPS_PY" "$MAINT_DIR/board_bridge.py" sync
    rc=$?
    echo "=== board_bridge sync exit $rc ==="
    # No board-file commit here: sprint-sync no longer runs board_view.regenerate(); whoever
    # does pushes the generated file through vault_git.push (which short-circuits when the
    # bytes did not move), and intake.consume() pushes Boards/Intake.md the same way. A
    # second committer racing them is how the two writers used to interleave.
    # Its own Telegram alert was removed 2026-09-06: every job (this one
    # included) writes a kind=job event with its exit code, and Grafana's
    # job-failed rule matches every job kind - a second alert_failure call
    # here would page the owner twice for the same failure.
    [ $rc -ne 0 ] && echo "=== sprint-sync exited $rc ==="
    JOB_EXIT=$rc; exit $rc
  fi

  # retrieval-eval: pure Python, no Claude - the eval scoring itself is
  # deterministic (runs the real search() and checks recall, no model call).
  # See retrieval_eval_job.py's own header for the full design.
  if [ "$JOB" = "retrieval-eval" ]; then
    if [ "$DRY" = "1" ]; then echo "=== dry run: would run retrieval_eval_job.py ==="; JOB_EXIT=0; exit 0; fi
    EVAL_OUTPUT="$(python3 "$MAINT_DIR/retrieval_eval_job.py")"
    rc=$?
    printf '%s\n' "$EVAL_OUTPUT"
    echo "=== retrieval_eval_job.py exit $rc ==="
    if [ -n "$(git status --porcelain "Knowledge/Retrieval Eval"*.md 2>/dev/null)" ]; then
      git add "Knowledge/Retrieval Eval"*.md
      vault_commit \
        -q -m "maint(retrieval-eval): $STAMP" && JOB_COMMIT="$(git rev-parse --short HEAD)" && echo "=== committed $JOB_COMMIT ==="
    else
      echo "=== no changes ==="
    fi
    if git remote get-url origin >/dev/null 2>&1; then
      git push -q origin HEAD 2>&1 && echo "=== pushed to origin ===" || echo "=== push failed (will retry next run) ==="
    fi
    if [ $rc -eq 0 ]; then
      SUMMARY_LINE="$(printf '%s\n' "$EVAL_OUTPUT" | grep -E '^recall@5=' | head -1)"
      echo "=== ${SUMMARY_LINE:-ran} ==="
    else
      # Its own Telegram alert was removed 2026-09-06: same reasoning as
      # sprint-sync above - Grafana's job-failed rule already owns this.
      echo "=== retrieval-eval exited $rc ==="
    fi
    echo "=== $JOB done $(date) ==="
    JOB_EXIT=$rc; exit $rc
  fi

  # morning / health-check: pure Python, no Claude (2026-09-06). vault_jobs.py
  # renders the daily note / the health report from vault text and the
  # checker's JSON; see docs/superpowers/specs/2026-09-06-vault-jobs-design.md.
  if [ "$JOB" = "morning" ] || [ "$JOB" = "health-check" ]; then
    SUB="morning"; PATHS="Daily"; [ "$JOB" = "health-check" ] && { SUB="health"; PATHS="Knowledge"; }
    if [ "$DRY" = "1" ]; then echo "=== dry run: would run vault_jobs.py $SUB ==="; JOB_EXIT=0; exit 0; fi
    OUT="$(python3 "$MAINT_DIR/vault_jobs.py" "$SUB")"; rc=$?
    printf '%s\n' "$OUT"; echo "=== vault_jobs.py $SUB exit $rc ==="
    if [ -n "$(git status --porcelain "$PATHS" 2>/dev/null)" ]; then
      git add "$PATHS"
      vault_commit \
        -q -m "maint($JOB): $STAMP" && JOB_COMMIT="$(git rev-parse --short HEAD)" && echo "=== committed $JOB_COMMIT ==="
    else
      echo "=== no changes ==="
    fi
    if git remote get-url origin >/dev/null 2>&1; then
      git push -q origin HEAD 2>&1 && echo "=== pushed to origin ===" || echo "=== push failed (will retry next run) ==="
    fi
    echo "=== $JOB done $(date) ==="
    JOB_EXIT=$rc; exit $rc
  fi

  # heal: pure bash, no Claude - maintenance/heal.sh owns the eight checks.
  # (the retired `observability` name is refused far above, before this
  # logged block even starts - see the top of the file.)
  if [ "$JOB" = "heal" ]; then
    if [ "$DRY" = "1" ]; then echo "=== dry run: would run heal.sh (docker, infisical, stack, secrets, slack-token, gateway, claude-login, outbox) ==="; JOB_EXIT=0; exit 0; fi
    OPS_REPO="$(cd "$(dirname "$(readlink "$MAINT_DIR")")" && pwd -P)"
    docker_path_export
    export STATE_DIR EV JOB OPS_CHAT_PROFILE STANDUP_TARGET OPS_PY MAINT_DIR
    export OBS_ENV="$CAPSULE_JOBS_DIR/observability.env" COMPOSE_FILE="$OPS_REPO/observability/docker-compose.yml"
    bash "$MAINT_DIR/heal.sh"; JOB_EXIT=$?; exit $JOB_EXIT
  fi

  # obs-probe: pure Python, no Claude. Ten-minute tick that runs the seven
  # collectors and writes one COPY payload of samples - see obs_probe.py's
  # own header.
  if [ "$JOB" = "obs-probe" ]; then
    if [ "$DRY" = "1" ]; then echo "=== dry run: would run obs_probe.py (7 collectors → samples) ==="; JOB_EXIT=0; exit 0; fi
    "$OPS_PY" "$MAINT_DIR/obs_probe.py"
    JOB_EXIT=$?; exit $JOB_EXIT
  fi

  # obs-inventory: pure Python, no Claude. Weekly regeneration of the
  # Observability Inventory vault note from the repo tree - see
  # obs_inventory.py's own header.
  if [ "$JOB" = "obs-inventory" ]; then
    if [ "$DRY" = "1" ]; then echo "=== dry run: would run obs_inventory.py (vault inventory note) ==="; JOB_EXIT=0; exit 0; fi
    OPS_REPO="$(cd "$(dirname "$(readlink "$MAINT_DIR")")" && pwd -P)"
    "$OPS_PY" "$MAINT_DIR/obs_inventory.py" --repo "$OPS_REPO" --vault "$VAULT"
    JOB_EXIT=$?; exit $JOB_EXIT
  fi

  # cleanup: pure bash, no Claude. Weekly log/disk hygiene - see cleanup.sh's
  # own header for the 7-day/50 MB/30-day rules.
  if [ "$JOB" = "cleanup" ]; then
    if [ "$DRY" = "1" ]; then echo "=== dry run: would run cleanup.sh ==="; JOB_EXIT=0; exit 0; fi
    bash "$MAINT_DIR/cleanup.sh"
    JOB_EXIT=$?; exit $JOB_EXIT
  fi

  # deps-sync (A5, row 217): pure bash+python, no Claude, host-side with
  # network (cleanup.sh's shape - see deps_sync.sh's own header). Populates
  # the vendored pip/npm cache every sandboxed profile's docker_env points
  # at from each repo's deps-allowlist.txt.
  if [ "$JOB" = "deps-sync" ]; then
    # Forward every arg after the job name - `--check` and
    # `--force` are deps_sync.sh's own flags, and
    # `maintenance/run.sh deps-sync --force` is the operator's escape hatch;
    # without this they were silently dropped and an ordinary sync ran instead.
    if [ "$DRY" = "1" ]; then echo "=== dry run: would run deps_sync.sh${2:+ }${*:2} ==="; JOB_EXIT=0; exit 0; fi
    bash "$MAINT_DIR/deps_sync.sh" "${@:2}"
    JOB_EXIT=$?; exit $JOB_EXIT
  fi

  # ops-preview (row 643): pure bash+python, no Claude. Previews branch_sweep.py and storage_gc.py
  # (ops_preview.py runs both with no --apply, ever; only the CoS applies, by hand). launchd
  # cannot say "every other day", so the plist fires daily and a 47-hour window stamp skips the
  # off day (not remove-card's per-quarter marker: this one expires by age). A future-dated stamp
  # (the clock jumped back) counts as missing, so the job runs. The stamp is touched only after
  # a clean run, so a failure retries the next day. Counts-only lines go out as class `scheduled` (the quiet project
  # channel, like the close); a non-zero exit is the usual job-failed path, not a message.
  if [ "$JOB" = "ops-preview" ]; then
    if [ "$DRY" = "1" ]; then echo "=== dry run: would run ops_preview.py (branch_sweep.py + storage_gc.py, preview only, every other day) ==="; JOB_EXIT=0; exit 0; fi
    OPS_STAMP="$STATE_DIR/ops-preview.stamp"
    OPS_NOW="$(mktemp "$STATE_DIR/ops-preview.now.XXXXXX")"   # "now" as a file: a stamp newer than it is future-dated
    OPS_RECENT="$(find "$OPS_STAMP" -mmin -2820 ! -newer "$OPS_NOW" 2>/dev/null)"; rm "$OPS_NOW"
    [ -z "$OPS_RECENT" ] || skip_job "ran within the last 47h ($OPS_STAMP)"
    docker_path_export
    OUT="$(with_cap "$OPS_PY" "$MAINT_DIR/ops_preview.py")"; rc=$?
    printf '%s\n' "$OUT"
    # --to/--subject exactly as report.sh's close does it (an empty to= would be spooled, then
    # moved to dead/ by heal.sh, and the summary lost). No chat configured: nothing to deliver.
    TARGET="${OPS_ALERT_TARGET:-}"
    if [ -z "$TARGET" ]; then echo "ops-preview: no chat configured (ops.env) - summary not sent"
    elif [ -n "$OUT" ]; then
      printf '%s\n' "$OUT" | MSG_CLASS=scheduled tg_send --to "$TARGET" --subject "Capsule - ops preview $(date '+%A %d %b')"
    fi
    [ $rc -ne 0 ] || touch "$OPS_STAMP"
    echo "=== $JOB done $(date) ==="
    JOB_EXIT=$rc; exit $rc
  fi

  # disk-relief (D204): pure Python, no Claude. Every 4 hours; does nothing while free space on
  # the $HOME volume is >= OPS_DISK_RELIEF_FREE_GB (60), else removes rebuildable/finished things
  # by fixed rules (disk_relief.py's header). A dry run (RUN_SH_DRY_RUN=1 or DRY=1) runs it with
  # --dry-run: it prints what would go and changes nothing. Exit 3 = still under the hard floor
  # after everything: one incident-class alert (AGENT_OPS_NO_SEND sends nothing), the job itself is
  # stamped 0 so job-failed does not alert twice.
  if [ "$JOB" = "disk-relief" ]; then
    DR_ARGS=(); { [ "$DRY" = "1" ] || [ "$ENV_DRY" = "1" ]; } && DR_ARGS=(--dry-run)
    DOCKER_EXTRA_PATH=:/usr/local/go/bin docker_path_export
    OUT="$(with_cap "$OPS_PY" "$MAINT_DIR/disk_relief.py" ${DR_ARGS[@]+"${DR_ARGS[@]}"})"; rc=$?
    printf '%s\n' "$OUT"
    if [ $rc -eq 3 ]; then
      grep '^disk-relief: ALERT' <<<"$OUT" | tail -1 \
        | MSG_CLASS=incident tg_send ${OPS_ALERT_TARGET:+--to "$OPS_ALERT_TARGET"} --subject "Capsule - disk almost full"
      rc=0
    fi
    echo "=== $JOB done $(date) ==="
    JOB_EXIT=$rc; exit $rc
  fi

  # close: the 18:00 evening close (row 136 / M188). Pure bash+python, no
  # Claude: report.sh --close composes it through maintenance/brief.py and
  # sends it. REPORT_NO_STAMP=1 because this script already writes
  # state/jobs/close.json and owns the failure path for the job it runs.
  if [ "$JOB" = "close" ]; then
    if [ "$DRY" = "1" ]; then echo "=== dry run: would run report.sh --close ==="; JOB_EXIT=0; exit 0; fi
    REPORT_NO_STAMP=1 bash "$MAINT_DIR/report.sh" --close
    JOB_EXIT=$?; exit $JOB_EXIT
  fi

  # remove-card (M256, row 139): the quarterly "what do we remove?" card. Pure bash, no Claude: files
  # templates/remove-card.md on the capsule-corp board through `capsule intake`. The per-quarter stamp
  # is written only after a successful file, so a failed intake leaves no stamp and files on the next
  # manual re-run (launchd fires only quarterly; the job-failed alert is what says it failed).
  if [ "$JOB" = "remove-card" ]; then
    Q="$(date +%Y)-Q$(( (10#$(date +%m) - 1) / 3 + 1 ))"
    if [ "$DRY" = "1" ]; then echo "=== dry run: would file the quarterly remove card for $Q via capsule intake ==="; JOB_EXIT=0; exit 0; fi
    if [ -e "$STATE_DIR/remove-card-$Q" ]; then echo "remove-card: $Q already filed - nothing to do"; JOB_EXIT=0; exit 0; fi
    capsule_cli intake capsule-corp --title "Quarterly: what do we remove? $Q" \
      --body-file "$MAINT_DIR/templates/remove-card.md" && : >"$STATE_DIR/remove-card-$Q"
    JOB_EXIT=$?; exit $JOB_EXIT
  fi

  # blocked-tonight: the 20:15 line (M190), one message only when a decision waits on the owner.
  if [ "$JOB" = "blocked-tonight" ]; then
    if [ "$DRY" = "1" ]; then echo "=== dry run: would run report.sh --blocked-tonight ==="; JOB_EXIT=0; exit 0; fi
    REPORT_NO_STAMP=1 bash "$MAINT_DIR/report.sh" --blocked-tonight
    JOB_EXIT=$?; exit $JOB_EXIT
  fi

  # order-book-lint (M243): pure Python, no Claude, report-only. The weekly Monday 09:00 lint of
  # the vault order book against its own policy. Findings (lint exit 1) stay in the log and the
  # job exits 0, so job-failed stays quiet like a lookout quarantine; exit 2 (no book) fails.
  if [ "$JOB" = "order-book-lint" ]; then
    if [ "$DRY" = "1" ]; then echo "=== dry run: would run order_book_lint.py --vault $VAULT ==="; JOB_EXIT=0; exit 0; fi
    "$OPS_PY" "$MAINT_DIR/order_book_lint.py" --vault "$VAULT"
    JOB_EXIT=$?; [ "$JOB_EXIT" = 1 ] && JOB_EXIT=0; exit $JOB_EXIT
  fi

  # backup: pure bash, no Claude. Nightly tarball of everything not on GitHub
  # (kanban dbs, Postgres events, profiles, merge-gate state, vault bundle) -
  # see backup.sh's own header. Never touches the vault.
  if [ "$JOB" = "backup" ]; then
    if [ "$DRY" = "1" ]; then echo "=== dry run: would run backup.sh ==="; JOB_EXIT=0; exit 0; fi
    with_cap bash "$MAINT_DIR/backup.sh"
    JOB_EXIT=$?; exit $JOB_EXIT
  fi

  # restore-verify: pure bash, no Claude. Weekly rehearsal that the newest
  # backup actually restores (see restore.sh --verify's own header for the
  # five checks). Never writes to any live location. Never touches the vault.
  if [ "$JOB" = "restore-verify" ]; then
    if [ "$DRY" = "1" ]; then echo "=== dry run: would run restore.sh --latest --verify ==="; JOB_EXIT=0; exit 0; fi
    with_cap bash "$MAINT_DIR/restore.sh" --latest --verify
    JOB_EXIT=$?; exit $JOB_EXIT
  fi

  # quality-nightly: pure Python, no Claude. The free layer of the testing
  # standard - rotating mutation, the five-times flake run, dependency audit
  # and secret scan (design 2026-09-10-correctness-over-green section 7.4).
  # Opens cards through intake.py add-finding; never blocks, never pages, and
  # its exit code is 0 unless the script itself crashed.
  if [ "$JOB" = "quality-nightly" ]; then
    if [ "$DRY" = "1" ]; then echo "=== dry run: would run quality_nightly.py (mutation, flake, deps, secrets) cap=${JOB_CAP_SECONDS}s ==="; JOB_EXIT=0; exit 0; fi
    docker_path_export
    with_cap "$OPS_PY" "$MAINT_DIR/quality_nightly.py"
    JOB_EXIT=$?; exit $JOB_EXIT
  fi

  # quality-full: pure Python, no Claude (D273). The whole capsule, maintenance and shell suites in
  # a clone of main, so a break a selected landing missed is found within a day. Names its repo,
  # so the default nightly's rotation never moves.
  if [ "$JOB" = "quality-full" ]; then
    if [ "$DRY" = "1" ]; then echo "=== dry run: would run quality_nightly.py --phases full --repos capsule-corp cap=${JOB_CAP_SECONDS}s ==="; JOB_EXIT=0; exit 0; fi
    docker_path_export
    with_cap "$OPS_PY" "$MAINT_DIR/quality_nightly.py" --phases full --repos capsule-corp
    JOB_EXIT=$?; exit $JOB_EXIT
  fi

  # fork-sync: pure bash, no Claude. Weekly: brings upstream's commits into
  # the notes-vault skill's fork through the local skill checkout (opt-in: needs vault.skill_root)
  # (controller ruling A, row 97).
  #
  # `gh repo sync` is deliberately NOT used: the fork's `main` carries the owner's
  # own local edits (2389283), so the fork has diverged and `gh repo sync`
  # refuses every time - the job could never succeed. A merge can: it keeps his
  # commits and brings upstream's in, fast-forwarding when he has added nothing
  # since and making a merge commit when he has.
  #
  # Nothing here force-resets anything, and it refuses rather than guesses:
  # a dirty working tree, a checkout that is not on `main` or a missing upstream
  # remote is a FAILED job (exit 1, the git message on the `kind=job` event),
  # never a silent overwrite. A merge conflict exits 3 (dark_test reads that as
  # "needs a hand merge", not a crash): it leaves the checkout clean
  # (`git merge --abort`) for the owner to resolve by hand.
  #
  # Remote NAMES come from env, because they differ per machine: on the owner's
  # laptop upstream is `origin` (eugeniughelbur) and his fork is `fork`
  # (his GitHub account), so his ops.env carries OPS_FORK_SYNC_UPSTREAM_REMOTE=origin.
  if [ "$JOB" = "fork-sync" ]; then
    # Opt-in: the checkout is OPS_FORK_SYNC_CHECKOUT, else the configured skill root; with neither the job skips.
    FORK_CHECKOUT="${OPS_FORK_SYNC_CHECKOUT:-$OSB_DIR}"
    [ -n "$FORK_CHECKOUT" ] || skip_job "fork-sync is opt-in: set OPS_FORK_SYNC_CHECKOUT or vault.skill_root in host.json"
    UPSTREAM_REMOTE="${OPS_FORK_SYNC_UPSTREAM_REMOTE:-upstream}"
    FORK_REMOTE="${OPS_FORK_SYNC_FORK_REMOTE:-fork}"
    if [ "$DRY" = "1" ]; then
      echo "=== dry run: would fetch $UPSTREAM_REMOTE in $FORK_CHECKOUT, merge $UPSTREAM_REMOTE/main, push $FORK_REMOTE main ==="
      JOB_EXIT=0; exit 0
    fi
    if ! with_cap git -C "$FORK_CHECKOUT" remote get-url "$UPSTREAM_REMOTE" >/dev/null 2>&1; then
      echo "=== fork-sync: $FORK_CHECKOUT has no remote named '$UPSTREAM_REMOTE' - set OPS_FORK_SYNC_UPSTREAM_REMOTE in ops.env (nothing added automatically) ==="
      JOB_EXIT=1; exit 1
    fi
    DIRTY="$(with_cap git -C "$FORK_CHECKOUT" status --porcelain 2>&1)"
    if [ -n "$DIRTY" ]; then
      echo "=== fork-sync: $FORK_CHECKOUT is dirty, refusing to merge ==="
      printf '%s\n' "$DIRTY"
      JOB_EXIT=1; exit 1
    fi
    BRANCH="$(with_cap git -C "$FORK_CHECKOUT" rev-parse --abbrev-ref HEAD 2>&1)"
    if [ "$BRANCH" != "main" ]; then
      echo "=== fork-sync: $FORK_CHECKOUT is on '$BRANCH', not main - refusing to merge ==="
      JOB_EXIT=1; exit 1
    fi
    FETCH_OUT="$(with_cap git -C "$FORK_CHECKOUT" fetch "$UPSTREAM_REMOTE" 2>&1)"; rc=$?
    printf '%s\n' "$FETCH_OUT"
    if [ $rc -ne 0 ]; then
      echo "=== fork-sync: git fetch $UPSTREAM_REMOTE failed (exit $rc) ==="
      JOB_EXIT=$rc; exit $rc
    fi
    MERGE_OUT="$(with_cap git -C "$FORK_CHECKOUT" merge --no-edit "$UPSTREAM_REMOTE/main" 2>&1)"; rc=$?
    printf '%s\n' "$MERGE_OUT"
    if [ $rc -ne 0 ]; then
      # Exit 3 only for a real conflict (unmerged paths, read before the abort
      # clears them). Any other nonzero merge - a time-cap kill (142), rc 128,
      # unrelated histories - stays a failed job.
      CONFLICTED="$(with_cap git -C "$FORK_CHECKOUT" diff --name-only --diff-filter=U 2>/dev/null)"
      with_cap git -C "$FORK_CHECKOUT" merge --abort 2>&1 || true
      if [ -n "$CONFLICTED" ]; then
        echo "=== fork-sync: merge of $UPSTREAM_REMOTE/main conflicted (exit $rc); aborted, checkout left clean - resolve by hand ==="
        JOB_EXIT=3; exit 3
      fi
      echo "=== fork-sync: merge of $UPSTREAM_REMOTE/main failed without a conflict (exit $rc); aborted, checkout left clean ==="
      JOB_EXIT=1; exit 1
    fi
    PUSH_OUT="$(with_cap git -C "$FORK_CHECKOUT" push "$FORK_REMOTE" main 2>&1)"; rc=$?
    printf '%s\n' "$PUSH_OUT"
    [ $rc -ne 0 ] && echo "=== fork-sync: git push $FORK_REMOTE main failed (exit $rc) ==="
    JOB_EXIT=$rc; exit $rc
  fi

  # ---- Claude jobs from here on -------------------------------------------

  # Session-limit stamp: a job that hit the plan's rolling limit records the
  # reset time; every Claude job started before then skips instead of burning a
  # session on the same error.
  LIMIT_STAMP="$STATE_DIR/session-limit-until"
  # A dry run executes nothing (no Claude session, nothing to protect the
  # limit from), so it must never be skipped by a real pause stamp - otherwise
  # the dry-run marker never prints while the guard is genuinely paused.
  if [ "$DRY" != "1" ] && [ -f "$LIMIT_STAMP" ]; then
    LIMIT_UNTIL="$(cat "$LIMIT_STAMP" 2>/dev/null || echo 0)"
    if [ "${LIMIT_UNTIL:-0}" -gt "$(date +%s)" ] 2>/dev/null; then
      # Row 208: a bare %H:%M reads as already past once the pause
      # crosses midnight (brief.py:292's own fix) - carry the weekday too.
      skip_job "session limit until $(date -r "$LIMIT_UNTIL" '+%a %H:%M')"
    else
      rm -f "$LIMIT_STAMP"
    fi
  fi

  # M199 (row 136): OPS_SATURDAY_ZERO=1 makes Saturday a real day off for the
  # two Saturday model jobs - architect and security - and nothing else; one
  # flag, no plist edit, revert by setting 0. Single-sourced here (not
  # duplicated per-arm below) so the condition and the message have exactly
  # one copy. Self-contained: does not touch the close wiring above.
  # RUN_SH_WEEKDAY_OVERRIDE is a test-only hatch (same family as
  # RUN_SH_DRY_RUN/RUN_SH_PRINT_ENV) so tests/test_run_sh.sh can exercise the
  # non-Saturday and Saturday branches deterministically instead of depending
  # on the day this suite happens to run; unset in production, so
  # `date +%u` (1-7, Mon-Sun) is what actually gates the real job.
  if [ "${OPS_SATURDAY_ZERO:-0}" = "1" ] && [ "${RUN_SH_WEEKDAY_OVERRIDE:-$(date +%u)}" = "6" ]; then
    case "$JOB" in
      architect|security) skip_job "Saturday zero (OPS_SATURDAY_ZERO=1)" ;;
    esac
  fi

  # Pre-checks: no Claude session starts when there is nothing to do.
  case "$JOB" in
    inbox)
      PENDING="$(python3 - "$VAULT/Inbox" <<'PY'
import sys, pathlib
n = 0
for p in pathlib.Path(sys.argv[1]).iterdir():
    if p.name.startswith(".") or p.is_dir():
        continue
    head = p.read_text(encoding="utf-8", errors="replace")[:600] if p.suffix == ".md" else ""
    if "triage: hold" in head or 'triage: "hold"' in head or "triage: needs-human" in head or 'triage: "needs-human"' in head:
        continue
    n += 1
print(n)
PY
)"
      [ "${PENDING:-0}" = "0" ] && skip_job "Inbox has nothing to triage (only .gitkeep, held, or needs-human files)"
      echo "=== inbox: $PENDING file(s) to triage ==="
      ;;
    architect)
      UNCHANGED=""
      CHANGED=""
      SCAN_REPOS="$(cd "$CAPSULE_CORP_DIR" 2>/dev/null && PYTHONPATH="$CAPSULE_CORP_DIR" python3 -P -m capsule host get --only maintenance.scan_repos)" \
        || echo "run.sh: host.json maintenance.scan_repos not readable; the architect scan walks no repos" >&2
      [ -z "$SCAN_REPOS" ] && skip_job "no scan_repos configured; skipping"
      for repo in $SCAN_REPOS; do
        head="$(git -C "$HOME/CodeBase/$repo" rev-parse --short HEAD 2>/dev/null || echo none)"
        scanned="$(grep -m1 '^scanned-commit:' "$VAULT/Projects/$repo.md" 2>/dev/null | sed 's/scanned-commit:[[:space:]]*//; s/"//g')"
        if [ -n "$scanned" ] && [ "$head" = "$scanned" ]; then UNCHANGED="$UNCHANGED $repo@$head"; else CHANGED="$CHANGED $repo($scanned->$head)"; fi
      done
      [ -z "$CHANGED" ] && skip_job "all repos unchanged since last scan:$UNCHANGED"
      echo "=== architect: changed:$CHANGED unchanged:${UNCHANGED:- none} ==="
      ;;
    sprint-plan)
      ELIGIBLE="$("$OPS_PY" "$MAINT_DIR/board_bridge.py" eligible --count 2>/dev/null | tail -n 1)"
      [ "${ELIGIBLE:-0}" = "0" ] && skip_job "no eligible Backlog card"
      echo "=== sprint-plan: $ELIGIBLE eligible card(s) ==="
      ;;
  esac

  image_gc_step
  # MEM-13 V5: nightly vault eval; vault-eval no-ops unless redesign.vault_context is report or live.
  [ "$JOB" = "nightly" ] && [ "$DRY" != "1" ] && vault_eval_nightly

  # public-ci (P10): the nightly dry-runs the public export of main into OPS_PUBLIC_DEST (ops.env)
  # and appends its one line to this log; a silent skip with no DEST; never writes, never fails.
  if [ "$JOB" = "nightly" ] && [ -n "${OPS_PUBLIC_DEST:-}" ]; then
    if [ "$DRY" = "1" ]; then
      echo "=== dry run: would run public_export.py main --dry-run into OPS_PUBLIC_DEST ==="
    else
      JOB_CAP_SECONDS=300 with_cap python3 "$MAINT_DIR/public_export.py" main "$OPS_PUBLIC_DEST" \
        --dry-run --repo "$(git -C "$MAINT_DIR" rev-parse --show-toplevel)" 2>/dev/null \
        | grep '^public would change' || echo "public dry run refused or failed"
    fi
  fi

  # The nightly prompt is private-only (not in the public export): without it only the model step is
  # skipped. The image GC, vault eval and public-ci steps above already ran. skip_job logs one line.
  [ ! -f "$PROMPT_FILE" ] && skip_job "no prompt file at $PROMPT_FILE (model step skipped; the non-prompt steps ran)"

  PROMPT_TEXT="$(cat "$PROMPT_FILE")"
  PROMPT_TEXT="${PROMPT_TEXT//__HOME__/$HOME}"
  PROMPT_TEXT="${PROMPT_TEXT//__CONFIG__/$CAPSULE_JOBS_DIR}"
  PROMPT_TEXT="${PROMPT_TEXT//__SKILL_ROOT__/$OSB_DIR}"
  PROMPT_TEXT="${PROMPT_TEXT//__VAULT__/$VAULT}"
  # __BOARD_REPOS__: the repos with a board, from `capsule host boards` (repos.json board fields, else
  # config unblocked.boards; no private names in the prompt files): "`a`, `b`, `c`".
  if [[ "$PROMPT_TEXT" == *__BOARD_REPOS__* ]]; then
    BR_NAMES="$(cd "$CAPSULE_CORP_DIR" 2>/dev/null && PYTHONPATH="$CAPSULE_CORP_DIR" python3 -P -m capsule host boards 2>/dev/null)" || BR_NAMES=""
    BR_LIST=""
    for br in ${BR_NAMES:-capsule-corp}; do BR_LIST="$BR_LIST${BR_LIST:+, }\`$br\`"; done
    PROMPT_TEXT="${PROMPT_TEXT//__BOARD_REPOS__/$BR_LIST}"
  fi

  # Role-based least privilege. Each job gets exactly the tools its prompt uses.
  # The permission-bypass flag is never used here. Unlisted tools fail closed
  # (visible in this log) - widen the one line if a run reports a denied tool.
  # Each write-capable job also names the vault paths it may write in
  # GUARD_ALLOW (see hooks/write-guard.py). Start from empty so an inherited
  # value from the caller's environment can never stand in for a job's own.
  GUARD_ALLOW=""
  case "$JOB" in
    nightly)
      ALLOWED="Read Edit Write Grep Glob Bash(uv run --directory \"$OSB_DIR\" python scripts/eval/semantic_search.py *) Bash($OPS_PY $MAINT_DIR/decision_log.py sidecar *)"
      # nightly.md phases 1-5: today's daily note, the entity/concept/decision
      # folders this vault resolves to (People/, Ideas/, Knowledge/), project
      # and dev-log notes it heals links into, index.md, and the operation log.
      # Not the boards (generated), the ledger, Specs/ or Decisions/ - NOTHING
      # under Decisions/, not even the sidecar: the sidecar
      # is written by the ALLOWED Bash verb `decision_log.py sidecar` above,
      # and hooks/write-guard.py fires on Write/Edit/MultiEdit only, so a
      # GUARD_ALLOW entry for it bought the sidecar nothing and bought the
      # nightly MODEL an Edit path into Decisions/ it never had (row 128,
      # M069/M070; the Bash-is-invisible-to-write-guard note at the inbox arm).
      GUARD_ALLOW="Daily/**:People/**:Ideas/**:Knowledge/**:Projects/**:Dev Logs/**:Logs/**:index.md:log.md" ;;
    weekly)
      ALLOWED="Read Edit Write Grep Glob Bash($OPS_PY $MAINT_DIR/resume_pointers.py *)"
      PROMPT_TEXT="${PROMPT_TEXT//__OPS_PY__/$OPS_PY}"
      PROMPT_TEXT="${PROMPT_TEXT//__MAINT_DIR__/$MAINT_DIR}"
      # Row 470/473: the runner, never the model, captures the
      # unclassified-branch section before the session starts - a plain read-only detector
      # run, no Bash verb the model itself could point anywhere else. A
      # capture that failed must never read as "none" (that says "nothing to decide" when
      # the truth is "this didn't run") - so failure is its own distinct value, and the run
      # is capped with the same perl-alarm idiom `with_cap` already uses (never unbounded).
      # "none" needs positive proof of a real report (the summary header
      # render_table always prints) AND sed's own success - a wrong dir, an import crash or a
      # missing tool all fail closed. The cap is overridable only so a test can use 1 s.
      UNCL_RAW="$(cd "$CAPSULE_CORP_DIR" 2>/dev/null && PYTHONPATH="$CAPSULE_CORP_DIR" perl -e 'alarm shift @ARGV; exec @ARGV or die "exec: $!"' \
        "${RUN_SH_UNCL_CAP_S:-120}" python3 -P -m capsule unblocked 2>&1)"; UNCL_RC=$?
      UNCLASSIFIED=""
      if [ "$UNCL_RC" -le 1 ] && printf '%s\n' "$UNCL_RAW" | grep -Eq '^UNBLOCKED [0-9]+ UNOWNED [0-9]+$'; then
        if UNCLASSIFIED="$(printf '%s\n' "$UNCL_RAW" | sed -n '/^-- unclassified/,$p')"; then
          [ -n "$UNCLASSIFIED" ] || UNCLASSIFIED="none"    # 0/1 = a real report (1 = unowned > 0)
        else
          UNCLASSIFIED=""; UNCL_RC="$UNCL_RC, sed failed"
        fi
      fi
      # No grep makes the header test fail on a healthy detector (exit 0) - name it.
      if [ -z "$UNCLASSIFIED" ] && ! command -v grep >/dev/null 2>&1; then UNCL_RC="$UNCL_RC, grep missing"; fi
      [ -n "$UNCLASSIFIED" ] || UNCLASSIFIED="capture failed (capsule unblocked exit $UNCL_RC) - run \`python3 -m capsule unblocked\` by hand"
      PROMPT_TEXT="${PROMPT_TEXT//__UNCLASSIFIED__/$UNCLASSIFIED}"
      # M201: the same YOUR TIME block `capsule report --md` prints (report._yt), captured by the
      # runner; a failed capture is its own text, never an empty block that reads as "no data".
      YT_RAW="$(cd "$CAPSULE_CORP_DIR" 2>/dev/null && PYTHONPATH="$CAPSULE_CORP_DIR" perl -e 'alarm shift @ARGV; exec @ARGV or die "exec: $!"' \
        "${RUN_SH_YT_CAP_S:-60}" python3 -P -m capsule report "${RUN_SH_YT_BOARD:-capsule-corp}" --your-time 2>&1)"; YT_RC=$?
      if [ "$YT_RC" -eq 0 ] && printf '%s\n' "$YT_RAW" | grep -q '^## YOUR TIME$'; then YOUR_TIME="$YT_RAW"
      else YOUR_TIME="capture failed (capsule report --your-time exit $YT_RC) - run \`python3 -m capsule report capsule-corp --md\` by hand"; fi
      PROMPT_TEXT="${PROMPT_TEXT//__YOUR_TIME__/$YOUR_TIME}"
      # weekly.md writes Reviews/<date> - Weekly Review.md and links it from
      # the last daily note of the week. Row 128, M085: it also runs the
      # Sunday resume-pointer sweep, marking `no-expiry`/`expired` handles in
      # place (date or strike-through only, never a rewrite) - but only in
      # `Knowledge/**` and `Debugging/**`. A handle the scan
      # finds under `Architecture/**` or `Decisions/**` is REPORTED in the
      # Weekly Review and never edited: `Architecture/**` is the one-way docs
      # mirror (row 6 / §7.5) - machine-written, never hand-edited, and
      # docs_sync.py would overwrite the mark on the next tick anyway - and
      # the decisions authority table is pipeline-owned (test_contracts.py's
      # GUARD_FORBIDDEN, M048), where no unattended job may hold a glob at
      # all. The owner marks those two by hand.
      GUARD_ALLOW="Reviews/**:Daily/**:Knowledge/**:Debugging/**" ;;
    inbox)
      ALLOWED="Read Edit Write Grep Glob Skill Bash(git add *)"
      # inbox.md ingests clipped web pages, i.e. attacker-controlled prose, so
      # its allowlist is the folder map it files into plus the capture it is
      # triaging - never the boards. `## Approvals` in the hand-written intake
      # board is a real spec approval (intake.py -> merge_gate), so a poisoned
      # clipping must not be able to reach that file at all. C3/M047: the
      # moves out of Inbox/ are now the runner's, because Bash verbs are
      # invisible to write-guard.py and `git mv Inbox/x Boards/Intake.md` was
      # therefore unguarded (row 127, M047). The job instead names the
      # captures it consumed in Inbox/.processed (inside this GUARD_ALLOW, so
      # nothing widens); the runner consumes that marker after the session
      # exits, below.
      GUARD_ALLOW="Inbox/**:Knowledge/**:Ideas/**:People/**:Projects/**:Daily/**:Logs/**:index.md:log.md" ;;
    architect)
      ALLOWED="Read Edit Write Grep Glob Skill Bash(uv run --directory \"$OSB_DIR\" scripts/architect_scan.py *) Bash(uv run --directory \"$OSB_DIR\" scripts/mine_commit_decisions.py *)"
      # architect.md refreshes the generated blocks of the architecture notes
      # under Projects/<name>/Architecture/ and logs one line per repo.
      GUARD_ALLOW="Projects/**:Architecture/**:Daily/**:log.md" ;;
    sprint-plan)
      # Row 201: the 2-card cap is enforced HERE, not
      # left to the prompt - the number the session receives is already
      # clamped, so a drifting or hijacked prompt cannot spec more than 2.
      SPRINT_MAX_CARDS_N="${SPRINT_MAX_CARDS:-3}"
      case "$SPRINT_MAX_CARDS_N" in ''|*[!0-9]*) SPRINT_MAX_CARDS_N=3 ;; esac
      [ "$SPRINT_MAX_CARDS_N" -gt 2 ] && SPRINT_MAX_CARDS_N=2
      # Row 633: --restricted fences Read to the vault, so the planner can no
      # longer open ops.env; the two size knobs it needs ride in the prompt, read
      # as 0/1 the forgiving way deploy.sh's ops_flag does (anything but 1/true/yes is 0).
      ops_01() { case "$(printf '%s' "${1:-}" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')" in 1|true|yes) echo 1 ;; *) echo 0 ;; esac; }
      PROMPT_TEXT="SPRINT_MAX_CARDS=${SPRINT_MAX_CARDS_N}
OPS_ROUTE_BIG_CARDS=$(ops_01 "${OPS_ROUTE_BIG_CARDS:-}")
OPS_EPIC_LANE=$(ops_01 "${OPS_EPIC_LANE:-}")

$PROMPT_TEXT"
      # Row 201: the planner also reads and clears `## Plan next`
      # (intake.py) to follow PICK ORDER - both new verbs are CLI reads/writes,
      # never Write/Edit, so sprint-plan-guard.py (clamped to Specs/**) is
      # unaffected. `plan-next` (no args, no wildcard - the read verb takes
      # none) is deliberately NOT `plan-next*`: a trailing
      # wildcard would also permit `plan-next --vault <anywhere>`, letting the
      # planner read a Boards/Intake.md outside its own vault. `plan-next-remove
      # *` keeps its wildcard (it needs <board> <task_id>), and `intake.py`
      # itself refuses `--vault` outside tests (AGENT_OPS_NO_SEND) as a
      # second, code-level fence - this allowlist entry is defence in depth,
      # not the only guard.
      ALLOWED="Read Write Grep Glob Bash($OPS_PY $MAINT_DIR/board_bridge.py eligible*) Bash($OPS_PY $MAINT_DIR/board_bridge.py link-spec *) Bash($OPS_PY $MAINT_DIR/intake.py plan-next) Bash($OPS_PY $MAINT_DIR/intake.py plan-next-remove *)" ;;
    security)
      # This session reads untrusted code and
      # prose, so it gets NO write access to Boards/Intake.md - not even the
      # exact-file Edit it used to have, which was enough to forge a
      # `## New work` card or a `## Approvals` spec approval. Its one door
      # into that file is intake.py's add-finding verb: one validated line
      # under `## Findings` and nothing else in the file can move.
      ALLOWED="Read Edit Write Grep Glob Bash($OPS_PY $MAINT_DIR/intake.py add-finding *)"
      # Ruling 1: the prompt names the boards the Intake grammar accepts, and
      # board.intake_board_names() is the only place that list exists. An empty
      # list is a failed job (a `capsule-corp` fallback would silently narrow every
      # finding onto one board) - the list is either
      # right or the run does not happen.
      BOARDS_ERR="$(mktemp)"
      BOARDS="$("$OPS_PY" "$MAINT_DIR/board_bridge.py" boards 2>"$BOARDS_ERR" | tail -n 1)"
      BOARDS_WHY="$(tail -n 1 "$BOARDS_ERR" 2>/dev/null)"
      rm -f "$BOARDS_ERR"
      if [ -z "$BOARDS" ]; then
        echo "=== security: board_bridge.py boards returned nothing - refusing to guess a board list ==="
        ev_log job run.sh "board list empty: security not run${BOARDS_WHY:+ ($BOARDS_WHY)}" --title "$JOB" \
          --severity warn --job "$JOB" --state open
        JOB_EXIT=1; exit 1
      fi
      PROMPT_TEXT="${PROMPT_TEXT//__BOARDS__/$BOARDS}"
      GUARD_ALLOW="Knowledge/**" ;;
    agent-review)
      # Same reasoning as security - the verb, never Edit on Intake.
      # Row 633: the board db is read through board_query.py (board NAME, one
      # SELECT, read-only, authorizer-fenced), never a raw `sqlite3` (a full escape).
      ALLOWED="Read Edit Write Grep Glob Bash($OPS_PY $MAINT_DIR/board_query.py *) Bash($OPS_PY $MAINT_DIR/intake.py add-finding *)"
      GUARD_ALLOW="Reviews/**" ;;
    *)
      ALLOWED="Read Grep Glob" ;;
  esac

  # Row 633 lockdown. --restricted: no personal settings, hooks, plugins or
  # CLAUDE.md, file tools fenced to the vault (cwd) plus --add-dir;
  # --strict-mcp-config: no MCP servers; dontAsk: anything not in --allowedTools
  # is refused (acceptEdits is never used); --tools: only the built-in tools the
  # job's ALLOWED line names (the Bash(...) entries collapse to Bash).
  TOOLS="$(printf '%s\n' "$ALLOWED" | sed 's/Bash([^)]*)/Bash/g' | tr ' ' '\n' \
    | awk '!seen[$0]++' | paste -sd' ' -)"
  # The background-agent hook of the plugin (and the project-hint switch) must never be live in a
  # scheduled session, even when a hand-run job inherits the flags from the caller's shell.
  unset OBSIDIAN_BG_AGENT_ENABLED CLAUDE_VAULT_PROPAGATION
  CLAUDE_CMD=(claude -p "$PROMPT_TEXT" --model "$MODEL" --restricted --strict-mcp-config
    --permission-mode dontAsk --tools "$TOOLS" --output-format json)
  case "$JOB" in architect|security|agent-review|sprint-plan) CLAUDE_CMD+=(--add-dir "$HOME/CodeBase") ;; esac
  # inbox reads the skill's reference notes (the write guard still denies any write outside the
  # vault); agent-review reads the job stamps, which live under STATE_DIR (maintenance/state
  # in the repo is a symlink there and the fence follows it).
  case "$JOB" in inbox) CLAUDE_CMD+=(--add-dir "$OSB_DIR/references") ;; esac
  case "$JOB" in agent-review) CLAUDE_CMD+=(--add-dir "$JOBS_DIR") ;; esac
  # inbox and architect run a user skill (/obsidian-ingest, obsidian-architect):
  # the one plugin that holds them, and nothing else of ~/.claude, comes back.
  case "$JOB" in inbox|architect) CLAUDE_CMD+=(--plugin-dir "$OSB_DIR") ;; esac
  # The write guard is ALWAYS attached: the generic one clamps Write/Edit to the
  # job's GUARD_ALLOW (hooks/write-guard.py; empty = deny every write), and the
  # Planner gets its own hook clamped to <vault>/Specs/**. --settings does not
  # merge (the last flag wins), so each job passes exactly one file. The
  # templates carry __HOME__; render per run so the repo copy stays
  # machine-independent.
  GUARD_SETTINGS=write-guard.settings.json
  [ "$JOB" = "sprint-plan" ] && GUARD_SETTINGS=sprint-plan.settings.json
  export GUARD_ALLOW
  sed -e "s#__HOME__#$HOME#g" -e "s#__CONFIG__#$CAPSULE_JOBS_DIR#g" "$MAINT_DIR/hooks/$GUARD_SETTINGS" > "$STATE_DIR/$GUARD_SETTINGS"
  CLAUDE_CMD+=(--settings "$STATE_DIR/$GUARD_SETTINGS" --allowedTools "$ALLOWED")

  # inbox: /obsidian-ingest may still delegate a long ingest to a background agent;
  # Claude Code kills those at 600 s by default (2026-09-04: lost a Fireship ingest).
  # Give the inbox job a ceiling matching its wall-clock cap.
  if [ "$JOB" = "inbox" ]; then
    export CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=$(( JOB_CAP_SECONDS * 1000 ))
  fi

  if [ "$DRY" = "1" ]; then
    echo "=== dry run: job=$JOB model=$MODEL prompt=${#PROMPT_TEXT} chars cap=${JOB_CAP_SECONDS}s bg_ceiling=${CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS:-default} ==="
    echo "=== allowedTools: $ALLOWED ==="
    echo "=== argv: ${CLAUDE_CMD[*]:0:1} ... ${CLAUDE_CMD[*]:3} ==="
    echo "=== env: OBSIDIAN_BG_AGENT_ENABLED=${OBSIDIAN_BG_AGENT_ENABLED-unset} CLAUDE_VAULT_PROPAGATION=${CLAUDE_VAULT_PROPAGATION-unset} ==="
    echo "=== dry run: nothing executed, nothing committed ==="
    JOB_EXIT=0; exit 0
  fi

  # 2026-09-01/02: nightly failed 2 nights running, both times "You've hit
  # your session limit - resets HH:MMam/pm" right at the 22:00 start (heavy
  # daytime usage exhausts the plan's rolling window). Detect that specific
  # message, stamp the reset time for the other jobs, sleep until it (+5 min,
  # capped at 2h so a parse miss can't hang the job), and retry once.
  # --output-format json extractors: RAW_OUTPUT is `claude`'s stdout+stderr
  # merged (`with_cap ... 2>&1`), so it is NOT guaranteed to be one clean JSON
  # blob - a warning line on stderr, or any text before/after the envelope,
  # breaks a whole-blob json.loads. Try the whole blob first (handles a
  # pretty-printed / multi-line envelope, which no single line parses on its
  # own); fall back to a line-by-line scan, taking the LAST
  # line that parses as a JSON object carrying the key we want (ignore every
  # other line - log noise, partial output, etc.).
  # --- json helpers start ---
  session_id_from_json() {
    python3 -c 'import json, sys
raw = sys.stdin.read()
sid = ""
try:
    d = json.loads(raw)
    if isinstance(d, dict) and "session_id" in d:
        sid = d.get("session_id") or ""
except Exception:
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        if isinstance(d, dict) and "session_id" in d:
            sid = d.get("session_id") or ""
sys.stdout.write(sid)'
  }
  # Falls back to the raw text when no line carries a truthy `result` (missing
  # key, or `"result": ""`) - a session-limit message can land with an empty
  # result, and the caller greps this output for "hit your session limit", so
  # an empty CLAUDE_OUTPUT would silently hide it from the retry logic below.
  result_from_json() {
    python3 -c 'import json, sys
raw = sys.stdin.read()
result, found = None, False
try:
    d = json.loads(raw)
    if isinstance(d, dict) and "result" in d:
        result, found = d.get("result"), True
except Exception:
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        if isinstance(d, dict) and "result" in d:
            result, found = d.get("result"), True
sys.stdout.write(str(result) if found and result else raw)'
  }
  # --- json helpers end ---

  run_claude_once() {
    if [ "${RUN_SH_FORCE_FAIL:-0}" = "1" ]; then
      CLAUDE_OUTPUT="forced failure (RUN_SH_FORCE_FAIL=1) - no Claude session was started"
      SESSION_ID=""
      printf '%s\n' "$CLAUDE_OUTPUT"; return 1
    fi
    RAW_OUTPUT="$(with_cap "${CLAUDE_CMD[@]}" 2>&1)"
    rc=$?
    printf '%s\n' "$RAW_OUTPUT"   # raw JSON (or whatever came back) goes to the log verbatim
    SESSION_ID="$(printf '%s' "$RAW_OUTPUT" | session_id_from_json)"
    CLAUDE_OUTPUT="$(printf '%s' "$RAW_OUTPUT" | result_from_json)"
    [ $rc -eq 142 ] && echo "=== wall-clock cap hit (${JOB_CAP_SECONDS}s) - claude killed ==="
    return $rc
  }

  run_claude_once
  rc=$?

  if [ $rc -ne 0 ] && printf '%s' "$CLAUDE_OUTPUT" | grep -qi "hit your session limit"; then
    RESET_STR="$(printf '%s' "$CLAUDE_OUTPUT" | grep -oE 'resets [0-9]{1,2}:[0-9]{2}(am|pm)' | head -1 | sed 's/resets //')"
    TARGET_EPOCH=""
    if [ -n "$RESET_STR" ]; then
      TARGET_EPOCH="$(date -j -f '%I:%M%p' "$(printf '%s' "$RESET_STR" | tr '[:lower:]' '[:upper:]')" +%s 2>/dev/null)"
    fi
    NOW_EPOCH="$(date +%s)"
    if [ -z "${TARGET_EPOCH:-}" ] || [ "$TARGET_EPOCH" -le "$NOW_EPOCH" ]; then
      TARGET_EPOCH=$(( NOW_EPOCH + 3600 ))   # unparsable reset: assume an hour
    fi
    echo "$(( TARGET_EPOCH + 300 ))" > "$LIMIT_STAMP"
    echo "=== session limit hit (resets ${RESET_STR:-?}) - stamped $LIMIT_STAMP so other jobs skip ==="
    WAIT=$(( TARGET_EPOCH - NOW_EPOCH + 300 ))
    if [ "$WAIT" -gt 0 ] && [ "$WAIT" -le 7200 ]; then
      echo "=== sleeping ${WAIT}s then retrying once ==="
      sleep "$WAIT"
      run_claude_once
      rc=$?
      [ $rc -eq 0 ] && rm -f "$LIMIT_STAMP"
    fi
  fi
  echo "=== claude exit $rc ==="
  [ -n "$SESSION_ID" ] && echo "↩ job $JOB · $VAULT · claude -r $SESSION_ID"

  # C3/M047: the job may no longer run `git mv`/`git rm`. It names the captures
  # it consumed in Inbox/.processed (inside its existing Inbox/** GUARD_ALLOW,
  # so no allowlist widens) and the runner removes them here - host-side, one
  # path shape, no traversal. Anything else on the line is printed and skipped.
  MARKER="$VAULT/Inbox/.processed"
  if [ "$JOB" = "inbox" ] && [ -f "$MARKER" ]; then
    while IFS= read -r line; do
      case "$line" in
        ""|"#"*) continue ;;
        Inbox/*) : ;;
        *) echo "=== inbox marker: refusing $line (not under Inbox/) ==="; continue ;;
      esac
      case "$line" in *..*) echo "=== inbox marker: refusing $line (traversal) ==="; continue ;; esac
      if [ -f "$VAULT/$line" ]; then
        git -C "$VAULT" rm -q -- "$line" && echo "=== inbox marker: removed $line ==="
      else
        echo "=== inbox marker: $line is not a file (skipped) ==="
      fi
    done < "$MARKER"
    rm -f "$MARKER"
  fi

  if [ -n "$VAULT" ] && [ -n "$(git status --porcelain)" ]; then   # never commit or push outside a vault
    git add -A
    vault_commit \
      -q -m "maint($JOB): $STAMP"
    JOB_COMMIT="$(git rev-parse --short HEAD)"
    echo "=== committed $JOB_COMMIT ==="
    ACTIVITY_LINE="$JOB: committed $JOB_COMMIT"
  else
    echo "=== no changes ==="
  fi

  # Push to the GitHub mirror if a remote is set. Never fail the job on a
  # push error (offline, auth) - the next run's push catches up.
  if [ -n "$VAULT" ] && git remote get-url origin >/dev/null 2>&1; then
    if git push -q origin HEAD 2>&1; then
      echo "=== pushed to origin ==="
    else
      echo "=== push failed (will retry next run) ==="
    fi
  fi

  # (Activity pings removed 2026-09-05: the owner found them noise. The digest's
  # JOBS line, status.sh and the team room show the same facts.)

  # A failed Claude job's own Telegram alert was removed 2026-09-06: Grafana's
  # job-failed rule (kind='job', exit<>0, last 15 min) owns this alert now -
  # see observability/grafana/provisioning/alerting/rules.yaml.

  find "$LOG_DIR" -name '*.log' -mtime +30 -delete 2>/dev/null

  echo "=== $JOB done $(date) ==="
  JOB_EXIT=$rc; exit $rc
} >"$LOG" 2>&1
