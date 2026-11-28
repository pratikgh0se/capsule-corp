# lib_state.sh - the one shell spelling of the maintenance state root.
# Source it; do not exec. Same precedence as events.state_root() (U1): a non-empty
# $AGENT_OPS_STATE_DIR; else, under a non-empty AGENT_OPS_NO_SEND (tests), a temp dir that is never
# the real root; else ~/.local/state/capsule-corp/maint. Empty means unset. Sets STATE_DIR only and
# exports nothing: Python children re-resolve the same root from the same env, and an exported
# AGENT_OPS_STATE_DIR would beat the NO_SEND temp dir of a child started under NO_SEND.
# bin/capsule-park.sh inlines these lines (no sibling lib).
STATE_DIR="${AGENT_OPS_STATE_DIR:-}"
if [ -z "$STATE_DIR" ]; then
  if [ -n "${AGENT_OPS_NO_SEND:-}" ]; then STATE_DIR="${TMPDIR:-/tmp}"; STATE_DIR="${STATE_DIR%/}/capsule-corp-state-no-send"
  else STATE_DIR="$HOME/.local/state/capsule-corp/maint"; fi
fi
