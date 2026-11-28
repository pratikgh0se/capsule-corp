#!/usr/bin/env bash
# Daily vault digest -> Slack. Invoked by launchd at 08:30 local.
#
# Composes the digest deterministically (maintenance/vault_jobs.py digest -
# no model, 2026-09-06), then delivers it via the Slack outbox
# (`capsule outbox-send`, as heal.sh does). Read-only: never writes or commits to the vault.
#
# Lives outside the vault on purpose (see run.sh).

set -u

. "$(dirname "${BASH_SOURCE[0]}")/lib_vault.sh"
# Honour an override so tests can point at a scratch maintenance dir,
# the same idiom the interpreter variable below uses. No behaviour change in
# production, where nothing sets it.
# PATH first, before lib_config.sh: its host.json config_dir lookup needs python3 3.11+ (launchd's bare PATH has the system one).
export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.local/bin"
. "$(dirname "${BASH_SOURCE[0]}")/lib_config.sh"   # CAPSULE_JOBS_DIR
MAINT_DIR="${MAINT_DIR:-$CAPSULE_JOBS_DIR/maintenance}"
LOG_DIR="$MAINT_DIR/logs"
# Same default as run.sh: plain python3, overridable for tests.
OPS_PY="${CAPSULE_OPS_PY:-python3}"
STAMP="$(date +%Y-%m-%dT%H%M%S)"
LOG="$LOG_DIR/report-${STAMP}.log"
SUBJECT="Second Brain - $(date '+%A %d %b')"

# --close (row 136 / M188): the 18:00 evening close. Same file, same helpers,
# same sender, different composer - `run.sh close` is what launchd invokes, and
# it sets REPORT_NO_STAMP=1 because run.sh already owns state/jobs/close.json
# and the failure alert for the job it runs.
MODE=brief
case "${1:-}" in --close) MODE=close ;; --blocked-tonight) MODE=blocked-tonight ;; esac
JOB=report
if [ "$MODE" = close ]; then
  JOB=close
  STAMP="$(date +%Y-%m-%dT%H%M%S)"
  LOG="$LOG_DIR/close-${STAMP}.log"
  SUBJECT="Capsule - close $(date '+%A %d %b')"
fi
# --blocked-tonight (M190): the 20:15 line, same sender as the close; nothing is sent when
# no decision waits on the owner.
if [ "$MODE" = blocked-tonight ]; then
  JOB=blocked-tonight
  LOG="$LOG_DIR/blocked-tonight-$(date +%Y-%m-%dT%H%M%S).log"
  SUBJECT="Capsule - blocked tonight"
fi

# VAULT: env, else host.json vault.path, else empty; after the PATH rebuild (see run.sh). Only the
# blocked-tonight line works without a vault; the brief and the close read it.
vault_resolve "${REPORT_CAPSULE_CORP_DIR:-$HOME/CodeBase/capsule-corp}" "$OPS_PY"
[ -n "$VAULT" ] || [ "$MODE" = blocked-tonight ] || { echo "report.sh: no notes vault configured (OBSIDIAN_VAULT_PATH or host.json vault.path) - nothing to report"; exit 0; }
# OSB_DIR: the notes-vault skill checkout (OBSIDIAN_SKILL_ROOT, else host.json vault.skill_root, else
# empty: the search-index and retrieval lines are then left out of the digest). No default directory.
skill_root_resolve "${REPORT_CAPSULE_CORP_DIR:-$HOME/CodeBase/capsule-corp}"
export MAINT_DIR

# The timeout every brief.py invocation shares - the --brief call, the
# --close call and the --vault-copy call: "prints
# nothing" degrades correctly, but "never returns" does not - a composer
# wedged on a bad handle must not hang the launchd-driven job indefinitely.
# Same idiom the index: line already uses (`timeout 20`). Overridable so a
# test can shrink it rather than actually waiting out the default for a hang
# case.
OPS_BRIEF_TIMEOUT="${OPS_BRIEF_TIMEOUT:-30}"

# Delivery target: OPS_ALERT_TARGET from ops.env (a Slack channel id; empty means nothing is sent).
OPS_ENV="$CAPSULE_JOBS_DIR/ops.env"; [ -f "$OPS_ENV" ] && . "$OPS_ENV"
[ -n "${OPS_ACTIONABLE_MAX:-}" ] && export OPS_ACTIONABLE_MAX   # the brief child prints it
TARGET="${OPS_ALERT_TARGET:-}"
[ -n "$TARGET" ] || { echo "report.sh: no chat configured (ops.env) - nothing to deliver to"; exit 0; }

. "$(dirname "${BASH_SOURCE[0]}")/lib_state.sh"   # STATE_DIR: $AGENT_OPS_STATE_DIR or ~/.local/state/capsule-corp/maint
. "$(dirname "${BASH_SOURCE[0]}")/lib_docker_engine.sh"   # DOCKER_ENGINE*, docker_path_export
docker_path_export   # power_lines() runs docker; AGENT_OPS_DOCKER_BIN lets tests point at a fake
. "$(dirname "${BASH_SOURCE[0]}")/lib_send.sh"
mkdir -p "$LOG_DIR" "$STATE_DIR/jobs"

# --- job stamp + failure alert, same contract as run.sh (state/jobs/report.json)
START_EPOCH="$(date +%s)"; JOB_EXIT=""
# Event trail (row 208): the same `ev_log` run.sh's tg_send already
# calls, so a send from THIS file is no longer invisible in events.jsonl /
# Postgres. Not sourced from run.sh (they are two independent launchd jobs
# with no shared shell) - same one-line definition, kept in sync by hand.
# ev_log never fails the job over trail plumbing (spool dir missing, python
# import error, etc.) - a lost event row is not worth an alert.
EV="$OPS_PY $MAINT_DIR/events.py"
ev_log() { $EV log "$@" >/dev/null 2>&1 || true; }
# Phone messages: deliver_via_slack comes from lib_send.sh (sourced above).
tg_send() {
  local body; body="$(cat)"
  local TO="" SUBJ="" prev=""
  for a in "$@"; do
    [ "$prev" = "--to" ] && TO="$a"
    [ "$prev" = "--subject" ] && SUBJ="$a"
    prev="$a"
  done
  ev_log message report.sh "$body" --title "$JOB" ${TO:+--target "$TO"}
  if printf '%s\n' "$body" | deliver_via_slack; then return 0; fi
  # Same TG_SEND_NO_SPOOL=1 contract as run.sh's tg_send: report the failure,
  # spool nothing (heal.sh's flush sets it so a failed replay cannot
  # duplicate the file it is replaying).
  [ "${TG_SEND_NO_SPOOL:-0}" = 1 ] && return 1
  # Fail closed on the token, not on the message: spool it for heal.sh to
  # flush (spec section 6). Alert/report text only - never a secret.
  # Same three-line header as run.sh's tg_send: to=, at=, subject= (possibly
  # empty), a blank line, then the body.
  mkdir -p "$STATE_DIR/outbox"
  printf 'to=%s\nat=%s\nsubject=%s\n\n%s\n' "$TO" "$(date +%Y-%m-%dT%H:%M:%S%z)" "$SUBJ" "$body" >"$STATE_DIR/outbox/$(date +%s)-$JOB.msg"
  return 1
}
write_stamp() {
  [ "${REPORT_NO_STAMP:-0}" = "1" ] && return 0
  python3 - "$STATE_DIR/jobs/$JOB.json" "$START_EPOCH" "$(date +%s)" "$1" "$LOG" "$JOB" <<'PY'
import json, sys, datetime
path, start, end, exit_, log, job = sys.argv[1:]
iso = lambda e: datetime.datetime.fromtimestamp(int(e)).astimezone().isoformat(timespec="seconds")
json.dump({"job": job, "start": iso(start), "end": iso(end), "duration_s": int(end) - int(start),
           "exit": int(exit_), "skipped": None, "commit": None, "log": log}, open(path, "w"), indent=1)
PY
}
on_exit() { local rc=$?; rc="${JOB_EXIT:-$rc}"; write_stamp "$rc"
  # Hygiene against a SIGTERM (or any early exit) mid-run: EXTRA_FILE/MSG_FILE
  # are rm -f'd on every ordinary path already, but a signal between mktemp
  # and that rm would otherwise leave a 0600 temp file of composed digest
  # lines (no secret values) in $TMPDIR. Both are unset/empty on any path
  # that never reached them, so this is a no-op there.
  [ -n "${EXTRA_FILE:-}" ] && rm -f "$EXTRA_FILE"
  [ -n "${MSG_FILE:-}" ] && rm -f "$MSG_FILE"
  # RETRY_WHEN: report runs on a fixed 08:30 schedule, so its retry alert
  # names that concrete time (byte-equal to main's pre-C8 text); close has
  # no such fixed retry slot of its own, so it stays generic.
  RETRY_WHEN="tomorrow"; [ "$JOB" = report ] && RETRY_WHEN="tomorrow 08:30"
  if [ "$rc" != 0 ] && [ "${REPORT_NO_STAMP:-0}" != "1" ]; then printf '\xe2\x9a\xa0\xef\xb8\x8f Job failed: %s\n\n1. Exit %s\n2. Log: %s\n3. It retries %s; ops-run %s to redo it now.\n' "$JOB" "$rc" "$(basename "$LOG")" "$RETRY_WHEN" "$JOB" | MSG_CLASS=incident tg_send --to "$TARGET" -q 2>>"$LOG_DIR/legacy-send.err" || true; fi; }
trap on_exit EXIT

if [ "$MODE" = close ]; then
  {
    echo "=== close start $(date) -> $TARGET ==="
    if [ "${OPS_BRIEF_V2:-0}" != "1" ]; then
      # Ruling 15: brief.py is not self-gating - report.sh is the only gate,
      # and every call into brief.py stays strictly inside this branch. Flag
      # off means brief.py is never invoked at all, not composed-and-discarded.
      echo "close: OPS_BRIEF_V2=0 - not composed, not sent"
      echo "=== close done $(date) ==="
      JOB_EXIT=0; exit 0
    fi
    CLOSE="$(timeout "$OPS_BRIEF_TIMEOUT" python3 "$MAINT_DIR/brief.py" --close 2>&1)"; rc=$?
    printf '%s\n' "$CLOSE"
    if [ "$rc" != 0 ] || ! printf '%s' "$CLOSE" | grep -q "== Capsule"; then
      echo "close: composer failed (exit $rc) - not sending"
      JOB_EXIT=1; exit 1
    fi
    printf '%s\n' "$CLOSE" | MSG_CLASS=scheduled tg_send --to "$TARGET" --subject "$SUBJECT"
    send_rc=$?
    echo "=== slack send exit $send_rc ==="
    [ "$send_rc" = 0 ] && python3 -c '
import os, sys
sys.path.insert(0, os.environ["MAINT_DIR"]); import messages; messages.record("scheduled")' 2>>"$LOG" || true
    echo "=== close done $(date) ==="
    JOB_EXIT=$send_rc; exit $send_rc
  } >"$LOG" 2>&1
fi

if [ "$MODE" = blocked-tonight ]; then
  {
    echo "=== blocked-tonight start $(date) -> $TARGET ==="
    if [ "${OPS_BRIEF_V2:-0}" != "1" ]; then   # Ruling 15: the same flag gates every brief.py call
      echo "blocked-tonight: OPS_BRIEF_V2=0 - not composed, not sent"
      JOB_EXIT=0; exit 0
    fi
    LINE="$(timeout "$OPS_BRIEF_TIMEOUT" python3 "$MAINT_DIR/brief.py" --blocked-tonight)"; rc=$?   # stderr -> the log
    if [ "$rc" != 0 ] || { [ -n "$LINE" ] && ! printf '%s' "$LINE" | grep -q "^Blocked tonight: "; }; then
      printf '%s\n' "$LINE"; echo "blocked-tonight: composer failed (exit $rc) - not sending"
      JOB_EXIT=1; exit 1
    fi
    if [ -z "$LINE" ]; then
      echo "blocked-tonight: nothing waiting on you - nothing sent"
      JOB_EXIT=0; exit 0
    fi
    printf '%s\n' "$LINE"
    printf '%s\n' "$LINE" | MSG_CLASS=blocked MSG_V2_CLASS=scheduled tg_send --to "$TARGET" --subject "$SUBJECT"
    send_rc=$?
    echo "=== slack send exit $send_rc ==="
    JOB_EXIT=$send_rc; exit $send_rc
  } >"$LOG" 2>&1
fi

# JOBS (24h) line for the digest: read from state/jobs/*.json, no LLM.
jobs_line() {
  python3 - "$STATE_DIR/jobs" <<'PY'
import json, sys, pathlib, datetime
now = datetime.datetime.now().astimezone(); parts = []
for f in sorted(pathlib.Path(sys.argv[1]).glob("*.json")):
    try: d = json.load(open(f))
    except Exception: continue
    try: end = datetime.datetime.fromisoformat(d["end"])
    except Exception: continue
    if (now - end).total_seconds() > 86400: continue
    if d.get("skipped"): state = "skip"
    elif d.get("exit", 1) == 0: state = "ok"
    else: state = "FAIL"
    parts.append(f"{d['job']} {state} {end.strftime('%H:%M')}")
print("JOBS (24h): " + (" \u00b7 ".join(parts) if parts else "no stamps yet"))
PY
}

# notices_section: currently-firing severity=notice alerts, read from
# Grafana's Alertmanager API. Only called when OPS_ALERT_SEVERITY_SPLIT=1
# (ops.env). The admin password is read through capsule-secret (identity
# compose, as the other Grafana scripts do) and handed to curl on stdin via
# `-K -` - never on argv, never printed.
notices_section() {
  local capsule_bin="${CAPSULE_SECRET_BIN:-$CAPSULE_REPO/bin/capsule-secret}"
  local pw resp rc
  pw="$(CAPSULE_IDENTITY=compose "$capsule_bin" get capsule-corp OPS_GRAFANA_PASSWORD 2>/dev/null)" || { echo "notices: unavailable (grafana api)"; return; }
  if [ -z "$pw" ]; then echo "notices: unavailable (grafana api)"; return; fi
  resp="$(printf 'user = "admin:%s"\n' "$pw" | curl -K - -fsS -m 5 'http://127.0.0.1:3000/api/alertmanager/grafana/api/v2/alerts?active=true' 2>/dev/null)"
  rc=$?
  unset pw
  if [ "$rc" -ne 0 ] || [ -z "$resp" ]; then echo "notices: unavailable (grafana api)"; return; fi
  printf '%s' "$resp" | python3 -c '
import json, sys, datetime
try:
    alerts = json.load(sys.stdin)
except Exception:
    print("notices: unavailable (grafana api)")
    sys.exit(0)
lines = []
for a in alerts:
    labels = a.get("labels", {}) or {}
    if labels.get("severity") != "notice":
        continue
    name = labels.get("alertname", "unknown")
    summary = (a.get("annotations", {}) or {}).get("summary", "")
    hhmm = ""
    started = a.get("startsAt", "")
    try:
        dt = datetime.datetime.fromisoformat(started.replace("Z", "+00:00"))
        hhmm = dt.astimezone().strftime("%H:%M")
    except Exception:
        hhmm = ""
    lines.append(f"- {name}: {summary} (since {hhmm})")
print("\n".join(lines) if lines else "notices: none")
'
}

# C9/M215 (row 137): one absence-tier line in the digest body, from
# absence.py's brief_line - ABSENT on tier 1 or the flag off, the same
# off-changes-nothing rule the other digest lines follow. It does not send a
# message on its own; it is one line inside the digest that already goes out.
absence_line() {
  local ab="$MAINT_DIR/absence.py"
  [ -f "$ab" ] || return 0
  # Guard behind the flag BEFORE calling into anything
  # that touches the approvals tree, the same way board_bridge.py:1257 does -
  # flag off must mean zero approvals-tree IO, not just an absent line.
  python3 -c '
import sys
sys.path.insert(0, sys.argv[1])
import absence
if not absence.enabled():
    sys.exit(0)
line = absence.brief_line()
if line:
    print(line)
' "$MAINT_DIR" 2>/dev/null || true
}

# Power Level (design 2026-09-09-power-level section 7.7). One psql read of
# the `samples` table the obs-probe already fills, through the same docker exec
# door events.py uses. Every line is ABSENT rather than blank when its numbers
# are zero, so an idle day adds nothing to the digest.
power_lines() {
  local csv
  csv="$(docker exec -i capsule-corp-postgres psql -U ops -d ops -t -A --csv \
      -v ON_ERROR_STOP=1 -c "
with newest as (
  select distinct on (name, labels) name, labels, value
  from samples where service='capsule' and ts > now() - interval '30 minutes'
  order by name, labels, ts desc)
select 'rulings', coalesce(max(value),0)::int::text from newest
  where name='capsule.delegation.rulings.7d'
union all select 'decisions', coalesce(max(value),0)::int::text from newest
  where name='capsule.delegation.decisions.7d'
union all select 'triage', (labels->>'board') || '/' || (labels->>'task_id') || ',' || value::int::text
  from newest where name='capsule.card.error_repeats' and value >= 3
  order by 1;" 2>/dev/null)" || return 0
  [ -n "${csv:-}" ] || return 0
  printf '%s\n' "$csv" | python3 -c '
import csv, sys
# psql --csv, so the rows are CSV, not "split on the first comma": the triage
# row carries `<board>/<task_id>,<n>` as ONE column, which psql therefore
# quotes. Reading it by hand kept the quotes and split on the comma INSIDE the
# field, so every triage line came out mangled. csv.reader is
# the same door obs_probe.collect_power reads its own psql --csv through.
d, triage = {}, []
for row in csv.reader(sys.stdin):
    if len(row) != 2:
        continue
    k, v = row
    if k == "triage":
        # Only the card/count pair is packed into the one column the UNION can
        # carry; the board and task_id themselves never contain a comma.
        card, _, n = v.rpartition(",")
        triage.append((card, n))
    else:
        d[k] = v
r, dc = int(float(d.get("rulings") or 0)), int(float(d.get("decisions") or 0))
if r or dc:
    print(f"delegation: {r} machine rulings : {dc} your decisions (7 d)")
for card, n in triage[:3]:
    print(f"triage: {card} failed {int(float(n))}x with the same error")
'
  if [ "${OPS_WINDOW_PREFLIGHT:-0}" = "1" ]; then
    python3 -c '
import json, sys, pathlib
try:
    rows = json.loads(pathlib.Path(sys.argv[1]).read_text())
except Exception:
    sys.exit(0)
if isinstance(rows, list) and rows:
    pct = (rows[-1].get("reason") or "").split("%")[0].split()[-1]
    print(f"window: queued {len(rows)} card(s), headroom {pct}%")
' "$STATE_DIR/window-queue.json" 2>/dev/null
  fi
}

# Retrieval health (design 2026-09-10-memory-and-retrieval section 7.4, M068).
# nightly.md Phase 6 has printed --stats since row 124, but into the job log,
# where nothing reads it. Row 128 routed the alert here. ABSENT when healthy,
# on the power_lines() pattern, so a good day adds nothing to the digest.
retrieval_lines() {
  local json
  [ -n "$OSB_DIR" ] || return 0
  # `timeout 60` for the same reason the index: line's identical call carries
  # one (a cold `uv` sync of the fork's venv, or a wedged
  # Ollama, would otherwise block the 08:30 brief entirely - `|| return 0`
  # only helps once the command actually returns.
  json="$(timeout 60 uv run --directory "$OSB_DIR" python scripts/eval/semantic_search.py \
            --path "$VAULT" --stats --json 2>/dev/null)" || json=""
  # The floor regression (row 132): the monthly eval writes
  # `last_regression` into state/retrieval_floor-<cases>.json when measured
  # recall@5 falls below the stored floor. Without this the only brief-visible
  # signal is the generic "retrieval-eval failed" job name, with no number -
  # and §9 calls this "the one place a red is the point".
  python3 -c '
import glob, json, sys
newest = None
for p in sorted(glob.glob(sys.argv[1] + "/retrieval_floor-*.json")):
    try:
        d = json.load(open(p, encoding="utf-8"))
    except Exception:
        continue
    if not isinstance(d, dict):
        continue
    reg = d.get("last_regression") or {}
    when, measured, floor = reg.get("when"), reg.get("measured"), reg.get("floor")
    if not when or measured is None or floor is None:
        continue
    if newest is None or when > newest[0]:
        newest = (when, measured, floor, d.get("cases_file") or "?")
if newest:
    when, measured, floor, cases = newest
    print("retrieval: recall@5 {:.1f}% is below the {:.1f}% floor "
          "({}), measured {}".format(measured * 100, floor * 100, cases, when))
' "$STATE_DIR" 2>/dev/null
  [ -n "${json:-}" ] || return 0
  printf '%s' "$json" | python3 -c '
import json, sys, time
try:
    s = json.load(sys.stdin)
except Exception:
    sys.exit(0)
if not s.get("exists"):
    print("retrieval: no semantic index - nightly Phase 6 has not built one")
    sys.exit(0)
bits = []
cov = s.get("coverage_pct")
if cov is not None and cov < 98:
    bits.append(f"coverage {cov:.0f}% (<98%)")
oldest = s.get("oldest_write") or 0
if oldest:
    hours = (time.time() - oldest) / 3600.0
    if hours > 36:
        bits.append(f"index age {hours:.0f}h (>36h)")
ceiling = s.get("at_ceiling") or 0
if ceiling:
    bits.append(f"{ceiling} notes at the chunk ceiling")
if bits:
    print("retrieval: " + " · ".join(bits))'
}

# Budgets and stop conditions (design 2026-09-10-budgets-and-stops section 7.7).
# Everything a budget suppressed in the last 24 h, one line each. No docker, no
# Postgres - just state/brief-batch.json. Absent entirely when nothing was
# suppressed, which is always true while the three flags are 0.
budget_lines() {
  python3 "$MAINT_DIR/budgets.py" --brief 2>/dev/null
}

# The v2 brief (design 2026-09-10-communication-and-rhythm section 7.2, row 136).
# One call into maintenance/brief.py - the ONE composer for both scheduled
# messages, no model, no docker. Prints NOTHING when OPS_BRIEF_V2 is off or when
# the composer fails, so the caller falls back to the v1 digest assembly below
# and a bad composer can never send half a message.
brief_v2() {
  BRIEF_V2_REASON=""
  [ "${OPS_BRIEF_V2:-0}" = "1" ] || return 0
  local extra="${1:-}" out rc err last_err
  # hotfix 13 (2026-09-12): the first live v2 run fell back to the digest with
  # no trace because stderr went to /dev/null and every failure was `return 0`.
  # Still fail soft (the digest is the fallback), but say why on stderr, which
  # the caller's log redirect captures.
  err="$(mktemp)"
  if [ -n "$extra" ]; then
    out="$(timeout "$OPS_BRIEF_TIMEOUT" python3 "$MAINT_DIR/brief.py" --brief --extra-file "$extra" 2>"$err")"; rc=$?
  else
    out="$(timeout "$OPS_BRIEF_TIMEOUT" python3 "$MAINT_DIR/brief.py" --brief 2>"$err")"; rc=$?
  fi
  if [ "$rc" != 0 ]; then
    last_err="$(tail -n 1 "$err" | cut -c1-200)"
    echo "brief_v2: brief.py exit $rc - falling back to the digest: $last_err" >&2
    # A3 (row 201): the same reason, minus the stderr framing, for the
    # caller's verdict table - kept as a variable, not a second stderr line,
    # so hotfix 13's stderr text stays byte-identical.
    BRIEF_V2_REASON="brief.py exit $rc - $last_err"
    rm -f "$err"; return 0
  fi
  rm -f "$err"
  if ! printf '%s' "$out" | grep -q "== Capsule"; then
    echo "brief_v2: no '== Capsule' header in ${#out} bytes - falling back to the digest" >&2
    BRIEF_V2_REASON="no \"== Capsule\" header in ${#out} bytes"
    return 0
  fi
  printf '%s\n' "$out"
}

# --- Insight resurfacing rotation state. report.sh stays read-only w.r.t. the
# vault (see header), so rotation state lives in this external file instead of
# a vault note. Holds one resurfaced note path per line, newest last, trimmed
# to the most recent 15 so the exclusion list can't grow unbounded.
RESURFACE_HISTORY="$STATE_DIR/insight-resurface-history.txt"
mkdir -p "$STATE_DIR"
touch "$RESURFACE_HISTORY"
# Passed to vault_jobs.py as --exclude-file: vault paths contain commas, so
# the file (one path per line) is read directly rather than flattened.

# --- Milestone trigger: enable semantic search once the vault passes 50 notes.
# Runs at most once (sentinel), before the digest. setup-semantic.sh is
# idempotent and sends its own Telegram progress lines.
SEMANTIC_SENTINEL="$MAINT_DIR/.semantic-setup-done"
if [ ! -f "$SEMANTIC_SENTINEL" ]; then
  NOTE_COUNT=$(find "$VAULT" -name '*.md' \
      -not -path '*/.*' -not -path '*/Templates/*' -not -path '*/_trash/*' \
      -not -path '*/copilot/*' -not -path '*/_export/*' 2>/dev/null | wc -l | tr -d ' ')
  if [ "${NOTE_COUNT:-0}" -ge 50 ]; then
    echo "=== vault at $NOTE_COUNT notes - running setup-semantic.sh ===" >>"$LOG_DIR/report-${STAMP}.log"
    bash "$MAINT_DIR/setup-semantic.sh" && touch "$SEMANTIC_SENTINEL"
  fi
fi

{
  echo "=== report start $(date) -> $TARGET ==="
  cd "$VAULT" || exit 1

  # Deterministic digest (2026-09-06): vault_jobs.py renders the board standup,
  # the vault section, the resurfacing line and the retrieval line from vault
  # text and git history. No model, nothing to ask permission for.
  DIGEST="$(python3 "$MAINT_DIR/vault_jobs.py" digest --exclude-file "$RESURFACE_HISTORY")"
  rc=$?
  echo "--- digest (vault_jobs.py exit $rc) ---"
  printf '%s\n' "$DIGEST"
  echo "--- end digest ---"

  if [ -z "${DIGEST// /}" ]; then
    echo "empty digest - not sending"
    JOB_EXIT=1; exit 1
  fi

  # Shape guard: vault_jobs.py digest() always opens with "BOARD STANDUP".
  # Kept as a sanity check even now that a model can't produce something else
  # here - fail the job (Grafana's job-failed rule tells the owner once) instead
  # of ever pushing something malformed into Standup.
  if ! printf '%s' "$DIGEST" | grep -q "BOARD STANDUP"; then
    echo "digest has no BOARD STANDUP section - not a digest, not sending"
    JOB_EXIT=1; exit 1
  fi

  # Record which note (if any) was resurfaced this run, so tomorrow's exclusion
  # list skips it. Silently no-ops if the digest omitted the line. Extracted
  # BEFORE the [[wikilink]] -> (plain) rewrite below, so history tracking is
  # unaffected by the display-only text change.
  RESURFACED_PATH="$(printf '%s\n' "$DIGEST" | grep "Resurfacing:" | grep -oE '\[\[[^]]+\]\]' | head -1 | tr -d '[]')"
  if [ -n "${RESURFACED_PATH:-}" ]; then
    echo "$RESURFACED_PATH" >>"$RESURFACE_HISTORY"
    tail -n 15 "$RESURFACE_HISTORY" >"$RESURFACE_HISTORY.tmp" && mv "$RESURFACE_HISTORY.tmp" "$RESURFACE_HISTORY"
    echo "=== resurfaced: $RESURFACED_PATH (added to rotation history) ==="
  fi

  # "Since yesterday": a factual events.py summary of the last 24h (merged
  # cards, failed jobs, event counts), independent of the LLM digest above.
  # Guarded so a missing/empty summary changes nothing.
  SINCE="$("$OPS_PY" "$MAINT_DIR/events.py" tail --since 24h --summary 2>/dev/null)"
  if [ -n "$SINCE" ]; then
    DIGEST="$DIGEST

## Since yesterday
$(printf '%s' "$SINCE" | python3 -c '
import json,sys; s=json.load(sys.stdin)
print("- merged: " + (", ".join(s["merged"]) or "nothing"))
print("- failed jobs: " + (", ".join(s["failed"]) or "none"))
print("- events: " + ", ".join(f"{k} {v}" for k,v in sorted(s["counts"].items())))')"
  fi

  # Heal table: "all ok" one-liner unless something is failing, or heal
  # resumed something in the last 24h (worth surfacing even if
  # heal.json itself is currently all-clear again).
  HEAL_STATE="$STATE_DIR/heal.json"
  if python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import heal_table; sys.exit(0 if heal_table.all_ok(sys.argv[2]) else 1)' "$MAINT_DIR" "$HEAL_STATE" \
     && ! "$OPS_PY" "$MAINT_DIR/events.py" tail --since 24h 2>/dev/null | grep -qE ' infra heal +\| [a-z-]+ resumed:'; then
    DIGEST="$DIGEST
heal: all ok"
  else
    DIGEST="$DIGEST

## Heal
$(python3 "$MAINT_DIR/heal_table.py" "$HEAL_STATE")"
  fi

  # Retention: dry-run flag reminder (spec 2026-09-09 section 3) - one line,
  # only while OPS_RETENTION_APPLY is still 0, so the switch is not forgotten.
  # Silent once the flag is flipped to 1 (mode "applied").
  RETENTION_LINE="$(python3 -c '
import json, sys, pathlib
try:
    d = json.loads(pathlib.Path(sys.argv[1]).read_text())
except Exception:
    sys.exit(0)
if d.get("mode") == "dry-run":
    n = d.get("files", 0) + d.get("rows", 0)
    print(f"retention: dry-run ({n} pending)")
' "$STATE_DIR/retention.json" 2>/dev/null)"
  [ -n "$RETENTION_LINE" ] && DIGEST="$DIGEST
$RETENTION_LINE"

  # Vault search index coverage (row 125 / C4): the same --stats --json status.sh
  # now shows, condensed to one line - only when the call succeeds and an index
  # exists, so a cold `uv`, a missing notes-vault skill checkout, or no
  # index yet just omits the line rather than putting "unavailable" noise in
  # the digest (status.sh is where that detail belongs).
  INDEX_LINE="$(timeout 20 uv run --directory "$OSB_DIR" python \
      scripts/eval/semantic_search.py --path "$VAULT" --stats --json 2>/dev/null | python3 -c '
import json, sys
try:
    s = json.load(sys.stdin)
except Exception:
    sys.exit(0)
if s.get("exists"):
    print(f"index: {s.get(\"notes_indexed\", 0)}/{s.get(\"notes_eligible\", 0)} eligible ({s.get(\"coverage_pct\", 0)}%)")
' 2>/dev/null)"
  [ -n "$INDEX_LINE" ] && DIGEST="$DIGEST
$INDEX_LINE"

  # Absence: DRAIN/PARK tier, absent entirely on tier 1 or the flag off.
  ABSENCE_LINE="$(absence_line)"
  [ -n "$ABSENCE_LINE" ] && DIGEST="$DIGEST
$ABSENCE_LINE"

  # Power Level: merged/delegation/triage/window lines (spec 2026-09-09-power-level
  # section 7.7), absent entirely when nothing crosses the line above.
  POWER_LINES="$(power_lines)"
  [ -n "$POWER_LINES" ] && DIGEST="$DIGEST
$POWER_LINES"

  # Budgets: parks, warns, reclassify notes, routing and deferred DECISIONs.
  BUDGET_LINES="$(budget_lines)"
  [ -n "$BUDGET_LINES" ] && DIGEST="$DIGEST
$BUDGET_LINES"

  # Retrieval health: coverage/age/chunk-ceiling alerts (row 128, M068),
  # absent entirely when the semantic index is healthy.
  RETRIEVAL_LINES="$(retrieval_lines)"
  [ -n "$RETRIEVAL_LINES" ] && DIGEST="$DIGEST
$RETRIEVAL_LINES"

  # Notices: flagged (Build 1 spec section 7) - severity=notice alerts fire
  # muted (no Telegram message per-alert) so this section is the only place
  # they surface. Absent entirely when the flag is off.
  if [ "${OPS_ALERT_SEVERITY_SPLIT:-0}" = "1" ]; then
    DIGEST="$DIGEST

## Notices
$(notices_section)"
  fi

  # Telegram doesn't render notes-app [[wikilinks]] - it just shows literal
  # brackets. vault_jobs.py's resurfacing line already wraps the link in
  # "(...)", so strip just the inner [[ ]] for display, after the parse above.
  DIGEST_DISPLAY="$(printf '%s\n' "$DIGEST" | sed -E 's/\[\[([^]]+)\]\]/\1/g')"
  DIGEST_DISPLAY="$DIGEST_DISPLAY
$(jobs_line)"

  # v2 (row 136): the same readers, restructured - waiting block first, the
  # overnight section folded in, the closing line last. The lines report.sh
  # composes itself (Power Level, retrieval, notices, retention, index,
  # the deferral batch) are handed to the composer verbatim
  # through a temp file, so each keeps its single home. Flag off -> BRIEF is
  # empty and the v1 bytes below are sent unchanged.
  #
  # The whole EXTRA_FILE build stays INSIDE the flag check:
  # building it unconditionally re-invoked notices_section() - a
  # second capsule-secret fetch and a second Grafana curl - on every flag-off
  # run, which is a duplicated side effect a flag exists to prevent, even
  # though the composed message stayed byte-identical either way.
  # A3 (row 201): six-line verdict block, the last thing before "=== report
  # done" on every exit path past this point, so a job that died before
  # composing prints no block - itself a verdict (design section 1/3).
  # Defaults assume the earliest possible exit (the shape guard below); each
  # decision point overwrites the variables it owns as the run reaches it.
  V_COMPOSER="v1"; V_COMPOSER_WHY=""
  V_GUARD="ok"
  V_SEND="skipped (guard)"; V_SEND_WHY=""
  V_VAULT="skipped (guard)"; V_VAULT_WHY=""
  V_BUDGET="skipped"; V_BUDGET_WHY=""
  verdict_table() {
    printf 'report: verdicts %s\n' "$(date +"%Y-%m-%d %H:%M")"
    printf '%-11s %-9s %s\n' "composer" "$V_COMPOSER" "$V_COMPOSER_WHY"
    printf '%-11s %-9s %s\n' "guard" "$V_GUARD" "$GUARD"
    printf '%-11s %-9s %s\n' "send" "$V_SEND" "$V_SEND_WHY"
    printf '%-11s %-9s %s\n' "vault-copy" "$V_VAULT" "$V_VAULT_WHY"
    printf '%-11s %-9s %s\n' "budget" "$V_BUDGET" "$V_BUDGET_WHY"
  }

  BRIEF=""
  if [ "${OPS_BRIEF_V2:-0}" = "1" ]; then
    EXTRA_FILE="$(mktemp)"
    printf '%s\n' "$POWER_LINES" "$RETRIEVAL_LINES" "$RETENTION_LINE" "$INDEX_LINE" >"$EXTRA_FILE"
    if [ "${OPS_ALERT_SEVERITY_SPLIT:-0}" = "1" ]; then
      printf '\n## Notices\n%s\n' "$(notices_section)" >>"$EXTRA_FILE"
    fi
    # A3: brief_v2 is called directly (not via `BRIEF="$(brief_v2 ...)"`) so
    # the BRIEF_V2_REASON it sets lands in THIS shell, not a subshell that
    # command substitution would otherwise fork and discard it in - only its
    # stdout goes through the file.
    BRIEF_V2_OUT="$(mktemp)"
    brief_v2 "$EXTRA_FILE" >"$BRIEF_V2_OUT"
    BRIEF="$(cat "$BRIEF_V2_OUT")"
    rm -f "$EXTRA_FILE" "$BRIEF_V2_OUT"
  else
    V_COMPOSER_WHY="OPS_BRIEF_V2=0"
  fi
  if [ -n "$BRIEF" ]; then
    MESSAGE="$BRIEF"
    GUARD="== Capsule"
    V_COMPOSER="v2"
    V_COMPOSER_WHY='brief.py exit 0, "== Capsule" header present'
  else
    MESSAGE="$DIGEST_DISPLAY"
    GUARD="BOARD STANDUP"
    V_COMPOSER="v1"
    [ "${OPS_BRIEF_V2:-0}" = "1" ] && V_COMPOSER_WHY="$BRIEF_V2_REASON"
  fi

  # Shape guard, per mode (v1: the 2026-09-06 digest incident's fix; v2: the
  # header brief.py always emits). Fail the job rather than ever pushing
  # something malformed into the chat.
  if ! printf '%s' "$MESSAGE" | grep -q "$GUARD"; then
    echo "message has no $GUARD - not a brief, not sending"
    V_GUARD="FAIL"
    verdict_table
    JOB_EXIT=1; exit 1
  fi

  # Delivery goes through tg_send -> deliver_via_slack (lib_send.sh).
  printf '%s\n' "$MESSAGE" | MSG_CLASS=scheduled tg_send --to "$TARGET" --subject "$SUBJECT"
  send_rc=$?
  echo "=== slack send exit $send_rc ==="
  if [ "$send_rc" = 0 ]; then V_SEND="ok"; else V_SEND="FAIL"; fi
  V_SEND_WHY="target $TARGET, slack send exit $send_rc"
  # The vault copy (M197): the brief REPLACES the hand-written
  # Knowledge/Morning report note. Written only under the flag, only after a
  # successful send, exactly one path, best-effort - report.sh stays read-only
  # w.r.t. the vault with the flag off (see this file's header).
  if [ -n "$BRIEF" ] && [ "$send_rc" = 0 ]; then
    # A FILE, not a pipe: `python3 - <<'PY'` already owns stdin, so a piped
    # message would never reach the script (review fix).
    MSG_FILE="$(mktemp)"; printf '%s\n' "$MESSAGE" >"$MSG_FILE"
    VCOUT="$(timeout "$OPS_BRIEF_TIMEOUT" python3 "$MAINT_DIR/brief.py" --vault-copy --file "$MSG_FILE" 2>&1)"; VC_RC=$?
    VC_LAST="$(printf '%s\n' "$VCOUT" | tail -1)"
    echo "=== vault copy: $VC_LAST ==="
    if [ "$VC_RC" = 0 ]; then V_VAULT="ok"; else V_VAULT="FAIL"; fi
    V_VAULT_WHY="$VC_LAST"
    python3 -c 'import os, sys; sys.path.insert(0, os.environ["MAINT_DIR"]); import messages; messages.record("scheduled")' || true
    rm -f "$MSG_FILE"
    V_BUDGET="counted"; V_BUDGET_WHY="scheduled"
  elif [ -z "$BRIEF" ]; then
    V_VAULT="skipped (v1)"; V_VAULT_WHY=""
    V_BUDGET="skipped"; V_BUDGET_WHY=""
  else
    V_VAULT="skipped (send failed)"; V_VAULT_WHY=""
    V_BUDGET="skipped"; V_BUDGET_WHY=""
  fi
  verdict_table
  echo "=== report done $(date) ==="
  JOB_EXIT=$send_rc; exit $send_rc
} >"$LOG" 2>&1
