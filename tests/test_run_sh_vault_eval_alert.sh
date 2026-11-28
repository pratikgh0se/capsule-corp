#!/usr/bin/env bash
# The nightly vault eval's failure is no longer silent (spec vault-eval section 6): a non-zero exit
# or a cap kill raises one notice-severity audit event naming vault-eval and the exit code, and the
# nightly goes on. Extracts with_cap / ev_log / vault_eval_nightly from run.sh (same sed pattern as
# test_run_sh.sh uses for tg_send); a fake interpreter stands in for bin/vault-eval. Nothing is sent.
set -u
unset CAPSULE_JOBS_DIR   # the jobs config dir comes from each case's fake home
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
T="$(mktemp -d "${TMPDIR:-/tmp}/ve-alert.XXXXXX")"; trap 'rm -rf "$T"' EXIT
export AGENT_OPS_NO_SEND=1 EVENTS_SPOOL="$T/events.jsonl" AGENT_OPS_STATE_DIR="$T/state"
fails=0
check() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; fails=$((fails+1)); fi; }

printf '#!/usr/bin/env bash\nexit "${FAKE_VE_RC:-0}"\n' >"$T/fake-py"; chmod +x "$T/fake-py"
printf '#!/usr/bin/env bash\ncase "$*" in *vault-eval*) : >"%s/marker"; exit 0;; esac\nexec python3 "$@"\n' "$T" >"$T/marker-py"
chmod +x "$T/marker-py"
EV="python3 $REPO/maintenance/events.py"
OPS_PY="$T/fake-py"; CAPSULE_CORP_DIR="$T/cc"; OSB_DIR="$T/osb"; VAULT="$T/vault"; JOB_CAP_SECONDS=900
eval "$(sed -n '/^ev_log()/p;/^with_cap()/p;/^vault_eval_nightly()/,/^}/p' "$REPO/maintenance/run.sh")"

FAKE_VE_RC=7 vault_eval_nightly; rc=$?
check "a failing eval does not fail the caller" '[ $rc -eq 0 ]'
check "a failing eval raises one notice naming vault-eval and the exit code" \
  '[ "$(grep -c "vault-eval" "$EVENTS_SPOOL")" = 1 ] && grep -q "\"severity\": \"notice\"" "$EVENTS_SPOOL" && grep -q "exit 7" "$EVENTS_SPOOL"'

: >"$EVENTS_SPOOL"
FAKE_VE_RC=0 vault_eval_nightly; rc=$?
check "a clean eval returns 0" '[ $rc -eq 0 ]'
check "a clean eval raises no alert" '[ ! -s "$EVENTS_SPOOL" ]'

# The step after the eval still runs: the call site is not chained to the eval's exit.
check "the nightly call site cannot stop the run" 'grep -q "&& vault_eval_nightly$" "$REPO/maintenance/run.sh"'
mkdir -p "$T/vault"
out="$(RUN_SH_DRY_RUN=1 EVENTS_SPOOL="$T/dry.jsonl" RUN_SH_MAINT_DIR="$REPO/maintenance" \
  OBSIDIAN_VAULT_PATH="$T/vault" CAPSULE_OPS_PY="$T/marker-py" bash "$REPO/maintenance/run.sh" nightly 2>&1)"
check "dry run never runs the eval or alerts" \
  '[ ! -e "$T/marker" ] && ! grep -q "vault-eval" <<<"$out" && ! grep -q "notice" "$T/dry.jsonl"'

[ "$fails" -eq 0 ] && echo "ALL PASS" || { echo "$fails FAILED"; exit 1; }
