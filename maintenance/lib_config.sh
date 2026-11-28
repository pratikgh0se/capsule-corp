# lib_config.sh - the one shell spelling of the jobs' runtime config dir (ops.env, observability.env,
# infisical.env, maintenance/ ...). Source it; do not exec. Same precedence as host.config_dir():
# a non-empty $CAPSULE_JOBS_DIR; else host.json `config_dir` (read through `capsule host get`,
# which owns the validation); else ${AGENT_OPS_HOME:-$HOME}/.config/capsule-corp (AGENT_OPS_HOME is the
# deploy/test home override and always decides which home's host.json is read, even over an inherited
# CAPSULE_HOME). Empty means unset. When the lookup itself fails (python3 missing or too old, a bad
# host.json) one line goes to stderr and the default is used; deploy.sh pins the resolved value into
# every plist, so a launchd job never depends on this lookup. Sets and exports CAPSULE_JOBS_DIR so
# Python children and later sourcings resolve the same dir without another host.json read.
_cfg_legacy_rel=".config/obsidian-second-brain"
_cfg_home="${AGENT_OPS_HOME:-$HOME}"
CAPSULE_LEGACY_JOBS_DIR="$_cfg_home/$_cfg_legacy_rel"   # the pre-rename dir (not exported); deploy.sh guards on it
unset _cfg_legacy_rel
if [ -z "${CAPSULE_JOBS_DIR:-}" ]; then
  _cfg_repo="$(cd -P "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd -P)"
  _cfg_args=(); _cfg_rc=0
  if [ -n "${AGENT_OPS_HOME:-}" ]; then
    _cfg_args=(--capsule-home "$AGENT_OPS_HOME/.local/share/capsule")
  fi
  CAPSULE_JOBS_DIR="$(cd "$_cfg_repo" 2>/dev/null && PYTHONPATH="$_cfg_repo" python3 -sP -m capsule host get --only config_dir ${_cfg_args[@]+"${_cfg_args[@]}"} 2>/dev/null)" || _cfg_rc=$?
  if [ "$_cfg_rc" -ne 0 ]; then
    echo "lib_config: the host.json config_dir lookup failed (python3 missing or too old, or a bad host.json); using the default jobs config dir" >&2
    CAPSULE_JOBS_DIR=""
  fi
  if [ "$_cfg_rc" -ne 0 ]; then CAPSULE_JOBS_DIR_SRC=lookup-failed
  elif [ -n "$CAPSULE_JOBS_DIR" ]; then CAPSULE_JOBS_DIR_SRC=host
  else CAPSULE_JOBS_DIR_SRC=default
  fi
  if [ -z "$CAPSULE_JOBS_DIR" ]; then
    # Default order mirrors capsule/jobs_dir.py (the source): the new dir if it exists, else the legacy one
    # if it holds a Capsule env file (observability.env or ops.env), else the new one.
    CAPSULE_JOBS_DIR="$_cfg_home/.config/capsule-corp"
    if [ ! -d "$CAPSULE_JOBS_DIR" ] && { [ -f "$CAPSULE_LEGACY_JOBS_DIR/observability.env" ] || [ -f "$CAPSULE_LEGACY_JOBS_DIR/ops.env" ]; }; then
      CAPSULE_JOBS_DIR="$CAPSULE_LEGACY_JOBS_DIR"
    fi
  fi
  unset _cfg_repo _cfg_args _cfg_rc
else
  # Set by the caller: the source is kept only when it was recorded for this very value, so a
  # dir someone typed in later (a typo) is never treated as the `default`.
  if [ "${CAPSULE_JOBS_DIR_SRC_DIR:-}" = "$CAPSULE_JOBS_DIR" ]; then CAPSULE_JOBS_DIR_SRC="${CAPSULE_JOBS_DIR_SRC:-env}"
  else CAPSULE_JOBS_DIR_SRC=env
  fi
fi
unset _cfg_home
# CAPSULE_JOBS_DIR_SRC says where the value came from: env | host | default | lookup-failed. deploy.sh
# creates observability.env / infisical.env (fresh passwords) only for `default`.
CAPSULE_JOBS_DIR_SRC_DIR="$CAPSULE_JOBS_DIR"
export CAPSULE_JOBS_DIR CAPSULE_JOBS_DIR_SRC CAPSULE_JOBS_DIR_SRC_DIR
