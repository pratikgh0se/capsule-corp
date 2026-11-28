#!/usr/bin/env bash
# capsule-keeper-watch.sh - the keeper watchdog job: <prefix>.capsule-corp-keeper-watch (prefix: bin/lib_prefix.sh), installed by ./deploy.sh.
# Pages (outbox row, which reaches Slack and the file log; no desktop notification) when scratchpad/keeper-log.md is silent > 60 min while work waits.
# It only pages: it never drives a session, the tick or launchd. Logic: capsule/keeper_watch.py.
# Env overrides (tests): CAPSULE_HOME, CAPSULE_KEEPER_LOG, CAPSULE_KEEPER_QUEUE, CAPSULE_KEEPER_BOARD.
set -uo pipefail
REPO="$(cd -P "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
# CAPSULE_HOME rule (row 609; the same block is in every script that defaults it, = capsule/paths.py home()): explicit wins; under AGENT_OPS_NO_SEND a temp dir, never the live home.
# Known differences from tempfile.gettempdir(): TEMP/TMP are not read without TMPDIR; a missing or unwritable TMPDIR falls back to /tmp only in Python; only one trailing slash is stripped; a relative TMPDIR is not made absolute.
if [ -z "${CAPSULE_HOME:-}" ]; then
  if [ -n "${AGENT_OPS_NO_SEND:-}" ]; then CAPSULE_HOME="${TMPDIR:-/tmp}"; CAPSULE_HOME="${CAPSULE_HOME%/}/capsule-home-no-send"
  else CAPSULE_HOME="$HOME/.local/share/capsule"; fi
fi
mkdir -p "$CAPSULE_HOME/logs"
cd "$REPO" && PYTHONPATH="$REPO" exec python3 -P -m capsule.keeper_watch
