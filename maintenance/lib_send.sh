# lib_send.sh - the one shell way a maintenance script talks to Capsule and sends a phone
# message (C28). Source-safe: defines names only. The repo path is set here, once.
#   capsule_cli ARGS...   run `python3 -m capsule ARGS` from this repo (CAPSULE_PY overrides
#                         the interpreter, for tests)
#   deliver_via_slack     body on stdin -> Slack outbox row; MSG_CLASS picks the class
#                         (default blocked; incident for alerts; scheduled for the multi-line ☀ digest).
#                         MSG_V2_CLASS=scheduled: the class to send instead while the board is on
#                         slack.routing v2 (unset: nothing changes).
#                         AGENT_OPS_NO_SEND set: sends nothing, unconditionally.
CAPSULE_REPO="$(cd -P "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
capsule_cli() {
  PYTHONPATH="$CAPSULE_REPO" "${CAPSULE_PY:-python3}" -P -m capsule "$@"
}
deliver_via_slack() {
  [ -z "${AGENT_OPS_NO_SEND:-}" ] || { cat >/dev/null; return 0; }
  local digest=""; [ "${MSG_CLASS:-}" = scheduled ] || [ "${MSG_V2_CLASS:-}" = scheduled ] && digest=--digest
  capsule_cli outbox-send "${MSG_CLASS:-blocked}" "$(cat)" ${MSG_V2_CLASS:+--v2-class "$MSG_V2_CLASS"} ${digest:+"$digest"} >/dev/null
}
