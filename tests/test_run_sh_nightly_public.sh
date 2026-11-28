#!/usr/bin/env bash
# Public edition: maintenance/nightly.md is private-only. `run.sh nightly` without it must skip only the
# model step and still run the image GC (shown here by the dry-run line), not stop at "no prompt file".
set -u
unset CAPSULE_JOBS_DIR
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
T="$(mktemp -d "${TMPDIR:-/tmp}/run-sh-pub.XXXXXX")"; trap 'rm -rf "$T"' EXIT
export EVENTS_SPOOL="$T/events.jsonl" AGENT_OPS_STATE_DIR="$T/state-root"
export CAPSULE_OPS_PY=python3 OBSIDIAN_VAULT_PATH="$T/vault" OBSIDIAN_SKILL_ROOT="$T/skill"
export RUN_SH_CAPSULE_CORP_DIR="$T/no-such-checkout" HOME="$T/home"
unset AGENT_OPS_NO_SEND
mkdir -p "$T/vault" "$T/skill" "$T/home" "$T/maint"
for f in "$REPO"/maintenance/*; do
  case "$(basename "$f")" in nightly.md|inbox.md) ;; *) ln -s "$f" "$T/maint/$(basename "$f")" ;; esac
done
export RUN_SH_MAINT_DIR="$T/maint"

fails=0
check() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; fails=$((fails+1)); fi; }

out="$(RUN_SH_DRY_RUN=1 bash "$REPO/maintenance/run.sh" nightly 2>&1)"; rc=$?
check "no nightly prompt: exits 0" '[ $rc -eq 0 ]'
check "no nightly prompt: the image GC step still runs" 'grep -q "would run image_gc.py (OPS_IMAGE_GC_APPLY=0)" <<<"$out"'
check "no nightly prompt: one clear skip line names the prompt file" \
  '[ "$(grep -c "nightly skipped: no prompt file at $T/maint/nightly.md" <<<"$out")" = 1 ]'
check "no nightly prompt: no claude command is built" '! grep -q "argv:" <<<"$out"'
# the default public install has no notes vault: the image GC still runs, once, before that skip
out3="$(env -u OBSIDIAN_VAULT_PATH RUN_SH_DRY_RUN=1 bash "$REPO/maintenance/run.sh" nightly 2>&1)"; rc=$?
check "no vault: exits 0, skips for the vault, image GC ran exactly once" \
  '[ $rc -eq 0 ] && grep -q "nightly skipped: no notes vault configured" <<<"$out3" && [ "$(grep -c "would run image_gc.py" <<<"$out3")" = 1 ]'
out4="$(env -u OBSIDIAN_SKILL_ROOT RUN_SH_DRY_RUN=1 bash "$REPO/maintenance/run.sh" nightly 2>&1)"; rc=$?
check "no skill checkout: image GC ran once, then the skill skip" \
  '[ $rc -eq 0 ] && grep -q "no notes-vault skill checkout" <<<"$out4" && [ "$(grep -c "would run image_gc.py" <<<"$out4")" = 1 ]'
# the private tree (marker present): a lost nightly prompt stays a failed job
mkdir -p "$T/release"; : >"$T/release/private-denylist.txt"
out5="$(RUN_SH_DRY_RUN=1 bash "$REPO/maintenance/run.sh" nightly 2>&1)"; rc=$?
check "private marker present: a missing nightly prompt exits 1 with the no-prompt line" \
  '[ $rc -eq 1 ] && grep -q "run.sh: no prompt file at $T/maint/nightly.md" <<<"$out5"'
rm -f "$T/release/private-denylist.txt"
out2="$(RUN_SH_DRY_RUN=1 bash "$REPO/maintenance/run.sh" inbox 2>&1)"; rc=$?
check "another job with no prompt still stops with rc 1" '[ $rc -eq 1 ] && grep -q "no prompt file at" <<<"$out2"'

echo; if [ $fails -eq 0 ]; then echo "test_run_sh_nightly_public: all passed"; else echo "test_run_sh_nightly_public: $fails failed"; exit 1; fi
