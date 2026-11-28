#!/usr/bin/env bash
# status.sh - one screen, no LLM: every job's last run (red when overdue or
# failed), the last sprint-sync lines.
# Usage: status.sh [--no-color]
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib_config.sh"   # CAPSULE_JOBS_DIR
MAINT_DIR="$CAPSULE_JOBS_DIR/maintenance"
. "$(dirname "${BASH_SOURCE[0]}")/lib_state.sh"   # STATE_DIR: $AGENT_OPS_STATE_DIR or ~/.local/state/capsule-corp/maint
JOBS_DIR="$STATE_DIR/jobs"
# heal_table.py is this script's sibling in the repo, so prefer that copy:
# status.sh run straight out of a checkout (or a worktree) must not depend on
# the deploy-created symlink existing. MAINT_DIR stays the fallback.
HEAL_TABLE="$(dirname "${BASH_SOURCE[0]}")/heal_table.py"
[ -f "$HEAL_TABLE" ] || HEAL_TABLE="$MAINT_DIR/heal_table.py"
export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.local/bin:$PATH"
if [ "${1:-}" = "--no-color" ] || [ ! -t 1 ]; then RED=""; GRN=""; YEL=""; RST=""; else RED=$'\e[31m'; GRN=$'\e[32m'; YEL=$'\e[33m'; RST=$'\e[0m'; fi

echo "== jobs (state/jobs/*.json; cadence = how often launchd should run it)"
python3 - "$JOBS_DIR" "$RED" "$GRN" "$YEL" "$RST" <<'PY'
import json, sys, pathlib, datetime
jobs_dir, RED, GRN, YEL, RST = sys.argv[1:]
# seconds between expected runs, plus 2h grace
CADENCE = {"sprint-sync": 600, "nightly": 86400, "morning": 86400, "inbox": 86400, "report": 86400,
           "close": 86400, "blocked-tonight": 86400, "order-book-lint": 7*86400,
           "weekly": 7*86400, "architect": 7*86400, "sprint-plan": 7*86400,
           "health-check": 31*86400, "retrieval-eval": 31*86400,
           "security": 7*86400, "agent-review": 31*86400,
           "backup": 86400, "restore-verify": 7*86400,
           "fork-sync": 7*86400, "heal": 300, "obs-probe": 600, "obs-inventory": 7*86400,
           "quality-nightly": 86400, "quality-full": 86400, "deps-sync": 86400,
           "disk-relief": 4*3600,
           "ops-preview": 86400,   # fires daily; a skip on the off day still stamps
           "remove-card": 93*86400}
# obs-probe runs every 10 min like sprint-sync, so it gets the same 20-minute
# grace instead of the 2h default - two missed ticks and it reads OVERDUE,
# matching the Probe age panel's red threshold on the Capsule dashboards.
GRACE = {"sprint-sync": 1200, "obs-probe": 1200}
now = datetime.datetime.now().astimezone()
rows = []
for job, cad in CADENCE.items():
    f = pathlib.Path(jobs_dir) / f"{job}.json"
    if not f.exists():
        rows.append((job, f"{YEL}never{RST}", "-", "-", "-", "no stamp yet")); continue
    try: d = json.load(open(f))
    except Exception as e:
        rows.append((job, f"{RED}bad stamp{RST}", "-", "-", "-", str(e))); continue
    end = datetime.datetime.fromisoformat(d["end"]); age = (now - end).total_seconds()
    if d.get("skipped"): state, note = f"{YEL}skip{RST}", d["skipped"]
    elif d.get("exit", 1) == 0: state, note = f"{GRN}ok{RST}", ""
    else: state, note = f"{RED}FAIL {d.get('exit')}{RST}", pathlib.Path(d.get("log") or "").name
    overdue = age > cad + GRACE.get(job, 7200)
    when = end.strftime("%a %d %b %H:%M") + (f"  {RED}OVERDUE{RST}" if overdue else "")
    rows.append((job, state, when, f"{d.get('duration_s', 0)}s", d.get("commit") or "-", note[:70]))
w = max(len(r[0]) for r in rows)
for r in rows:
    print(f"  {r[0]:<{w}}  {r[1]:<18} {r[2]:<32} {r[3]:>6}  {r[4]:<8} {r[5]}")
PY

echo; echo "== last sprint-sync"
last="$(ls -t "$MAINT_DIR"/logs/sprint-sync-*.log 2>/dev/null | head -1)"
if [ -n "$last" ]; then echo "  $(basename "$last")"; grep -vE "^=== (board_bridge|sprint-sync start)" "$last" | tail -n 3 | sed 's/^/  /'; else echo "  no sprint-sync log"; fi

echo; echo "== session limit"
if [ -f "$STATE_DIR/session-limit-until" ]; then u="$(cat "$STATE_DIR/session-limit-until")"; if [ "$u" -gt "$(date +%s)" ]; then echo "  ${YEL}claude jobs paused until $(date -r "$u" '+%H:%M')${RST}"; else echo "  stale stamp (expired $(date -r "$u" '+%H:%M'))"; fi; else echo "  none"; fi

# C9/M215 (row 137): absence tier. ABSENT entirely - heading included - on
# tier 1, when the flag is off, or on any error, the same
# rule the other blocks follow. status.sh must never fail on a surface.
absence_block() {
  local line
  # Guard behind the flag BEFORE calling into anything
  # that touches the approvals tree, the same way board_bridge.py:1257 does -
  # flag off must mean zero approvals-tree IO, not just an absent line.
  line="$(python3 -c '
import sys
sys.path.insert(0, sys.argv[1])
import absence
if not absence.enabled():
    sys.exit(0)
d = absence.state()
if d.get("tier", 1) != 1:
    print("tier %s (%s), day %s, since %s" % (d.get("tier"), d.get("name"), d.get("days"), d.get("since")))
' "$MAINT_DIR" 2>/dev/null)" || line=""
  [ -n "$line" ] || return 0
  echo; echo "== absence"; echo "  $line"
}
absence_block

# C9/M216 (row 137): the park stamp. ABSENT entirely - heading included -
# when state/park.json doesn't exist, the same rule absence_block
# follows. status.sh must never fail on a surface, so a
# missing capsule-park.sh (not deployed sibling-first, or not yet synced by
# deploy.sh) degrades to no block rather than a stack trace.
CAPSULE_PARK="$(dirname "${BASH_SOURCE[0]}")/../bin/capsule-park.sh"
park_block() {
  local line
  [ -f "$STATE_DIR/park.json" ] || return 0
  line="$(AGENT_OPS_STATE_DIR="$STATE_DIR" bash "$CAPSULE_PARK" status 2>/dev/null)" || line=""
  [ -n "$line" ] || return 0
  echo; echo "== park"; echo "  $line"
}
park_block

echo; echo "== heal (state/heal.json)"
python3 "$HEAL_TABLE" "$STATE_DIR/heal.json" | sed 's/^/  /' | sed "s/FAIL/${RED}FAIL${RST}/"

# row 125 (C4): semantic_search.py --stats has been printing coverage since
# 2026-09-09 but nowhere except the nightly job's own log - surface it here
# too. Read-only, cheap (no embedding backend needed - index_stats() just
# walks the vault and diffs it against the index file), capped at 20s so a
# stuck `uv` (cold venv resolve, missing notes-vault skill checkout,
# hung network) can never hang status.sh; any failure degrades to one line
# and this block never causes a non-zero exit. A function (like report.sh's
# power_lines()) so tests can extract and drive it in isolation with a fake
# `uv` on PATH, instead of fighting this script's own PATH override above.
vault_index_block() {
SB_REPO="${OBSIDIAN_SECOND_BRAIN_REPO:-${OBSIDIAN_SKILL_ROOT:-}}"
if [ -z "$SB_REPO" ] || [ -z "${OBSIDIAN_VAULT_PATH:-}" ]; then
  echo "  ${YEL}index: skipped (no notes vault or skill checkout configured)${RST}"; return 0
fi
python3 - "$SB_REPO" "$OBSIDIAN_VAULT_PATH" "$YEL" "$RST" <<'PY'
import json, subprocess, sys, time

sb_repo, vault, YEL, RST = sys.argv[1:]

def unavailable(reason):
    print(f"  {YEL}index: unavailable ({reason}){RST}")

try:
    proc = subprocess.run(
        ["uv", "run", "--directory", sb_repo, "python", "scripts/eval/semantic_search.py",
         "--path", vault, "--stats", "--json"],
        capture_output=True, text=True, timeout=20,
    )
except subprocess.TimeoutExpired:
    unavailable("timeout")
    sys.exit(0)
except FileNotFoundError:
    unavailable("uv not found")
    sys.exit(0)
except Exception as e:
    unavailable(str(e)[:60] or type(e).__name__)
    sys.exit(0)

out = (proc.stdout or "").strip()
if not out:
    reason = (proc.stderr or "").strip().splitlines()[-1][:60] if proc.stderr else f"exit {proc.returncode}"
    unavailable(reason)
    sys.exit(0)

try:
    s = json.loads(out)
except Exception:
    unavailable("bad json")
    sys.exit(0)

if not s.get("exists"):
    unavailable("no index built")
    sys.exit(0)

indexed, eligible = s.get("notes_indexed", 0), s.get("notes_eligible", 0)
print(f"  indexed {indexed}/{eligible} eligible")

newest = s.get("newest_write")
if newest:
    days = (time.time() - newest) / 86400.0
    print(f"  newest write {days:.1f} days ago")
else:
    print("  newest write unknown")

excluded = s.get("excluded", {}) or {}
mirror = sum(n for reason, n in excluded.items() if "mirror" in reason)
copilot = sum(n for reason, n in excluded.items() if reason.startswith("denied dir: copilot"))
print(f"  excluded {s.get('excluded_total', 0)} (mirror {mirror}, copilot {copilot})")
PY
}

# The notes vault and its skill checkout are opt-in (D254): env, else host.json, else unset.
. "$(dirname "${BASH_SOURCE[0]}")/lib_vault.sh"
_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
vault_resolve "$_REPO_ROOT"; skill_root_resolve "$_REPO_ROOT"
echo; echo "== vault search index"
vault_index_block

# Decision budget. One read of budgets.py - no docker, no network,
# no board. A function, like vault_index_block(), so
# tests can drive it with a scratch MAINT_DIR and vault instead of fighting
# this script's PATH override. OPS_DECISION_BUDGET off (the
# default) means the budget itself never counts or holds anything (decisions.
# py:enabled), so a status line about it would be noise - absent entirely,
# heading included, the same rule vault_index_block() follows.
decision_budget_block() {
  [ "${OPS_DECISION_BUDGET:-}" = "1" ] || return 0
  local line
  line="$(python3 "$MAINT_DIR/budgets.py" --decisions 2>/dev/null)"; local rc=$?
  echo; echo "== decision budget"
  if [ -z "$line" ]; then echo "  ${YEL}decisions: unavailable${RST}"; return; fi
  if [ "$rc" -ne 0 ]; then echo "  ${YEL}${line}${RST}"; else echo "  $line"; fi
}

decision_budget_block

echo; echo "== retention (state/retention.json)"
python3 - "$STATE_DIR/retention.json" "$YEL" "$RST" <<'PY'
import json, sys, pathlib
path, YEL, RST = sys.argv[1:]
try:
    d = json.loads(pathlib.Path(path).read_text())
except Exception:
    print(f"  {YEL}retention: no state yet{RST}"); raise SystemExit(0)
mode, files, rows = d.get("mode"), d.get("files", 0), d.get("rows", 0)
if mode == "dry-run":
    print(f"  {YEL}retention: dry-run ({files} files, {rows} rows pending){RST}")
elif mode == "applied":
    print(f"  retention: applied ({files} files, {rows} rows)")
else:
    print(f"  {YEL}retention: no state yet{RST}")
PY
