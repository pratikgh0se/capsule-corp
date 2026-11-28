#!/usr/bin/env bash
# C15b (t_ad8eadd2), Z2 second oracle: plant each invariant condition on a temp home and a temp git
# checkout, in separate processes (a real dirty tree), and run tests/invariants_probe.py
# (capsule.invariants.sweep). Each condition must open exactly its kind; a clean home opens nothing.
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
fail=0
pass() { echo "PASS $1"; }; bad() { echo "FAIL $1"; fail=1; }
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
export CAPSULE_HOME="$T/home" PYTHONPATH="$REPO" DEV="$T/dev"
mkdir -p "$CAPSULE_HOME/boards/cc" "$DEV"
G() { git -c user.name=t -c user.email=t@t -c commit.gpgSign=false -C "$DEV" "$@"; }
G init -q -b main && echo a > "$DEV/a" && G add a && G commit -q -m x
fresh() { rm -f "$CAPSULE_HOME/boards/cc/kanban.db"*; }
probe() {   # probe <label> <want> <mode>
  local out; out="$(python3 "$REPO/tests/invariants_probe.py" "$3" 2>&1 | tail -n 1)"
  [ "$out" = "$2" ] && pass "$1" || bad "$1 (got: $out, want: $2)"
}
probe clean-home-opens-nothing "none running" none
fresh; echo notes > "$DEV/untracked.md"
probe untracked-file-in-dev-checkout-is-quiet "none running" none
rm "$DEV/untracked.md"; fresh; echo changed > "$DEV/a"
probe modified-tracked-file-pages-without-a-pause "main_checkout_write running" none
echo a > "$DEV/a"; fresh
probe completed-event-but-not-done-opens-lost-card-state "lost_card_state running" lost
fresh
probe archived-after-completed-is-quiet "none running" archived
exit $fail
