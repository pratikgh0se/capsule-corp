#!/usr/bin/env bash
# Row 633 (scheduled job lockdown): every scheduled `claude -p` job starts --restricted, with no
# MCP, dontAsk (never acceptEdits), an explicit --tools set, and the write guard ALWAYS attached.
# Dry-runs all seven jobs (RUN_SH_DRY_RUN=1 prints the argv, runs nothing) and drives the guard
# hooks with the payloads Claude Code sends.
set -u
unset CAPSULE_JOBS_DIR   # the jobs config dir comes from each case's fake home
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
T="$(mktemp -d "${TMPDIR:-/tmp}/job-lockdown.XXXXXX")"; trap 'rm -rf "$T"' EXIT
export EVENTS_SPOOL="$T/events.jsonl" AGENT_OPS_STATE_DIR="$T/state"
export RUN_SH_MAINT_DIR="$REPO/maintenance" RUN_SH_DRY_RUN=1 RUN_SH_UNCL_CAP_S=1
export RUN_SH_CAPSULE_CORP_DIR="$REPO"
# The architect job reads its repo list from host.json: name one repo so it reaches the claude command.
export CAPSULE_HOME="$T/capsule-home"; mkdir -p "$CAPSULE_HOME"
echo '{"maintenance": {"scan_repos": ["my-app"]}}' > "$CAPSULE_HOME/host.json"
export OBSIDIAN_VAULT_PATH="$T/vault"
# The notes-vault skill checkout is opt-in (no default directory): a scratch one, never the real checkout.
export OBSIDIAN_SKILL_ROOT="$T/skill"; mkdir -p "$OBSIDIAN_SKILL_ROOT/references"
mkdir -p "$T/vault/Inbox" "$T/vault/Specs" "$T/vault/Knowledge"
echo "a capture" > "$T/vault/Inbox/capture.md"
unset AGENT_OPS_NO_SEND
# A python override that answers the three pre-checks (board list, eligible card, absence tier) so every
# job reaches the claude command; everything else is plain python3.
cat > "$T/fakepy" <<'EOF'
#!/bin/sh
case "$1" in
  */board_bridge.py) case "$2" in boards) echo capsule-corp; exit 0 ;; eligible) echo 1; exit 0 ;; esac ;;
  */absence.py) echo 1; exit 0 ;;
esac
exec python3 "$@"
EOF
chmod +x "$T/fakepy"
export CAPSULE_OPS_PY="$T/fakepy"

fails=0; PASSED=""
check() {
  if eval "$2"; then echo "PASS $1"; PASSED="$PASSED|$1"; else echo "FAIL $1"; fails=$((fails+1)); fi
}
argv_of() { bash "$REPO/maintenance/run.sh" "$1" 2>&1 | grep '^=== argv:'; }
JOBS="nightly weekly inbox architect sprint-plan security agent-review"

for job in $JOBS; do
  argv="$(argv_of "$job")"
  check "$job is started --restricted" 'grep -q -- " --restricted " <<<"$argv"'
  check "$job ignores every MCP server" 'grep -q -- " --strict-mcp-config " <<<"$argv"'
  check "$job runs under dontAsk" 'grep -q -- "--permission-mode dontAsk " <<<"$argv"'
  check "$job never uses acceptEdits" '! grep -q acceptEdits <<<"$argv"'
  check "$job names its built-in tools" 'grep -q -- " --tools Read " <<<"$argv"'
  check "$job carries an --allowedTools list" 'grep -q -- " --allowedTools " <<<"$argv"'
  settings="write-guard.settings.json"; [ "$job" = sprint-plan ] && settings="sprint-plan.settings.json"
  check "$job attaches its write guard settings" \
    'grep -qF -- "--settings $T/state/$settings " <<<"$argv"'
  check "$job passes exactly one --settings file (the last one would win)" \
    '[ "$(grep -o -- "--settings" <<<"$argv" | wc -l | tr -d " ")" = 1 ]'
  case "$job" in inbox|architect) want=1 ;; *) want=0 ;; esac
  check "$job passes --plugin-dir $want time(s)" \
    '[ "$(grep -o -- "--plugin-dir" <<<"$argv" | wc -l | tr -d " ")" = $want ]'
done
check "--tools is the ALLOWED names with each Bash(...) collapsed to one Bash" \
  'grep -q -- "--tools Read Edit Write Grep Glob Bash --output-format" <<<"$(argv_of nightly)"'
check "nightly holds no Skill tool (its prompt runs no skill)" '! grep -q "Skill" <<<"$(argv_of nightly)"'
check "the inbox and architect plugin is the notes-vault skill checkout only" \
  'grep -q -- "--plugin-dir $T/skill " <<<"$(argv_of inbox)"'
check "sprint-plan reads source repos through --add-dir, as it did unscoped before" \
  'grep -q -- "--add-dir $HOME/CodeBase " <<<"$(argv_of sprint-plan)"'

# --- --add-dir per job: the only paths a job's file tools reach beyond the vault.
add_dirs() { grep -o -- "--add-dir [^ ]*" <<<"$(argv_of "$1")" | cut -d' ' -f2 | paste -sd' ' -; }
check "inbox gets --add-dir on the skill references only" \
  '[ "$(add_dirs inbox)" = "$T/skill/references" ]'
check "agent-review gets the code folder and the job-stamp folder" \
  '[ "$(add_dirs agent-review)" = "$HOME/CodeBase $T/state/jobs" ]'
check "every job's command confines file tools: --restricted and only its own --add-dir" '
  ok=1
  for j in $JOBS; do
    case "$j" in
      inbox) want="$T/skill/references" ;;
      agent-review) want="$HOME/CodeBase $T/state/jobs" ;;
      architect|security|sprint-plan) want="$HOME/CodeBase" ;;
      *) want="" ;;
    esac
    [ "$(add_dirs "$j")" = "$want" ] && grep -q -- " --restricted " <<<"$(argv_of "$j")" || ok=0
  done
  [ $ok = 1 ]'

# --- the background-agent flags never reach a session, even when the caller has them set.
bg_ok=1
for j in $JOBS; do
  bg_out="$(OBSIDIAN_BG_AGENT_ENABLED=1 CLAUDE_VAULT_PROPAGATION=1 bash "$REPO/maintenance/run.sh" "$j" 2>&1)"
  grep -q "^=== env: OBSIDIAN_BG_AGENT_ENABLED=unset CLAUDE_VAULT_PROPAGATION=unset ===" <<<"$bg_out" || bg_ok=0
done
check "a job started from a shell that sets the bg-agent flags runs with both unset" '[ $bg_ok = 1 ]'
check "every job's command has --restricted + --strict-mcp-config and the bg flags are unset" '
  ok=$bg_ok
  for j in $JOBS; do
    a="$(argv_of "$j")"
    grep -q -- " --restricted " <<<"$a" && grep -q -- " --strict-mcp-config " <<<"$a" || ok=0
  done
  [ $ok = 1 ]'

# --- agent-review reads the board db through board_query.py, never a raw sqlite3 or a bare git rule.
ar="$(argv_of agent-review)"
check "agent-review allows board_query.py, not sqlite3" \
  'grep -qF "Bash($T/fakepy $REPO/maintenance/board_query.py *)" <<<"$ar" && ! grep -qi sqlite <<<"$ar"'
check "agent-review holds no Bash(git ...) allow rule (git log is read-only, --output is refused)" \
  '! grep -qF "Bash(git" <<<"$ar"'
AR_PROMPT="$(sed -e "s#__HOME__#$HOME#g" -e "s#__CONFIG__#$HOME/.config/capsule-corp#g" "$REPO/maintenance/agent-review.md")"
check "agent-review's prompt command renders to the board_query.py allow prefix" \
  'grep -qF "python3 $HOME/.config/capsule-corp/maintenance/board_query.py <board>" <<<"$AR_PROMPT" && ! grep -qi "sqlite3 -readonly\|git -C" <<<"$AR_PROMPT"'
check "architect takes the commit from the scan, not from git in the target repo" \
  '! grep -q "git rev-parse" "$REPO/maintenance/architect.md"'

# --- sprint-plan's size knobs ride in the prompt as 0/1 (it cannot read ops.env).
eval "$(grep -m1 '^ *ops_01()' "$REPO/maintenance/run.sh")"
check "the planner's size knobs are normalised to 0 or 1 like deploy.sh ops_flag" \
  '[ "$(ops_01 " Yes ")$(ops_01 TRUE)$(ops_01 1)$(ops_01 2)$(ops_01 "")$(ops_01 "0; rm")" = 111000 ] && ! grep -q "/ops.env" "$REPO/maintenance/sprint-plan.md"'

# --- an unknown job (the run.sh `*` arm) has an empty GUARD_ALLOW: it still gets the guard.
MC="$T/maint-copy"; mkdir -p "$MC"
cp -R "$REPO/maintenance/." "$MC/"
printf 'Read _CLAUDE.md. Probe.\n' > "$MC/probe.md"
probe_out="$(RUN_SH_MAINT_DIR="$MC" bash "$MC/run.sh" probe 2>&1)"
deny_rc() {  # deny_rc <tool> <path-key> <path>: the hook's exit code under an empty GUARD_ALLOW
  printf '{"tool_name":"%s","tool_input":{"%s":"%s"}}' "$1" "$2" "$3" \
    | OBSIDIAN_VAULT_PATH="$T/vault" GUARD_ALLOW="" python3 "$REPO/maintenance/hooks/write-guard.py" 2>/dev/null
  echo $?
}
check "an empty-allowlist job still gets the write guard, which denies Write and NotebookEdit" '
  grep -qF -- "--settings $T/state/write-guard.settings.json " <<<"$probe_out" &&
  grep -q "\"matcher\": \"Write|Edit|MultiEdit|NotebookEdit\"" "$T/state/write-guard.settings.json" &&
  [ "$(deny_rc Write file_path "$T/vault/Knowledge/x.md")" = 2 ] &&
  [ "$(deny_rc NotebookEdit notebook_path "$T/vault/Knowledge/x.ipynb")" = 2 ]'
check "run.sh attaches the generic guard unconditionally (no GUARD_ALLOW condition around it)" \
  '! grep -q "if \[ -n \"\${GUARD_ALLOW" "$REPO/maintenance/run.sh" && grep -q "^  CLAUDE_CMD+=(--settings \"\$STATE_DIR/\$GUARD_SETTINGS\"" "$REPO/maintenance/run.sh"'

# --- the guard hook: empty allowlist writes nothing; NotebookEdit is guarded.
guard() {  # guard <tool> <path-key> <path> <GUARD_ALLOW>; returns the hook's exit code
  printf '{"tool_name":"%s","tool_input":{"%s":"%s"}}' "$1" "$2" "$3" \
    | OBSIDIAN_VAULT_PATH="$T/vault" GUARD_ALLOW="$4" python3 "$REPO/maintenance/hooks/write-guard.py" 2>/dev/null
}
guard Write file_path "$T/vault/Knowledge/n.md" ""; rc=$?
check "an empty GUARD_ALLOW denies a Write inside the vault" '[ $rc -eq 2 ]'
guard Edit file_path "$T/vault/Knowledge/n.md" ":"; rc=$?
check "a GUARD_ALLOW of only separators denies too" '[ $rc -eq 2 ]'
guard Write file_path "$T/vault/Knowledge/n.md" "Knowledge/**"; rc=$?
check "a listed path is still allowed" '[ $rc -eq 0 ]'
guard NotebookEdit notebook_path "$T/vault/Specs/n.ipynb" "Knowledge/**"; rc=$?
check "NotebookEdit outside GUARD_ALLOW is denied" '[ $rc -eq 2 ]'
guard NotebookEdit notebook_path "$T/vault/Knowledge/n.ipynb" "Knowledge/**"; rc=$?
check "NotebookEdit inside GUARD_ALLOW is allowed" '[ $rc -eq 0 ]'
guard NotebookEdit notebook_path "$T/vault/Knowledge/n.ipynb" ""; rc=$?
check "NotebookEdit with an empty GUARD_ALLOW is denied" '[ $rc -eq 2 ]'
printf '{"tool_name":"NotebookEdit","tool_input":{"notebook_path":"%s"}}' "$T/vault/Knowledge/n.ipynb" \
  | OBSIDIAN_VAULT_PATH="$T/vault" python3 "$REPO/maintenance/hooks/sprint-plan-guard.py" 2>/dev/null; rc=$?
check "the planner's own hook denies a NotebookEdit outside Specs/" '[ $rc -eq 2 ]'
printf '{"tool_name":"NotebookEdit","tool_input":{"notebook_path":"%s"}}' "$T/vault/Specs/n.ipynb" \
  | OBSIDIAN_VAULT_PATH="$T/vault" python3 "$REPO/maintenance/hooks/sprint-plan-guard.py" 2>/dev/null; rc=$?
check "the planner's own hook allows a NotebookEdit inside Specs/" '[ $rc -eq 0 ]'
for f in write-guard sprint-plan; do
  check "$f settings match NotebookEdit" \
    'grep -q "\"matcher\": \"Write|Edit|MultiEdit|NotebookEdit\"" "$REPO/maintenance/hooks/$f.settings.json"'
done

# Z1: contracts/intent/job_lockdown.jsonl replayed here - each example names a check above that
# passed. A missing file fails (open() raises), it is never skipped.
INTENT="$REPO/contracts/intent/job_lockdown.jsonl"
check "intent examples all name a passing check" \
  'python3 -c '"'"'import json,sys; rows=[json.loads(l) for l in open(sys.argv[1]) if l.strip()]; got=sys.argv[2].split("|"); sys.exit(0 if rows and all(r["expect"]["pass"] in got for r in rows) else 1)'"'"' "$INTENT" "$PASSED"'

echo; if [ $fails -eq 0 ]; then echo "test_job_lockdown: all passed"; else echo "test_job_lockdown: $fails failed"; exit 1; fi
