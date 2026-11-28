# lib_docker_engine.sh - the shell twin of maintenance/docker_engine.py (change both tables together).
# Source it AFTER lib_state.sh; do not exec. Resolves the engine (env CAPSULE_DOCKER_ENGINE, else the
# one-line file $STATE_DIR/docker-engine, else orbstack; empty = unset; unknown = exit 2), sets
# DOCKER_ENGINE and DOCKER_ENGINE_{BIN,CONTEXT,TITLE,APP,APP_PATH,OPEN_HINT,CASK}, then exports PATH with
# the engine's CLI dir first (AGENT_OPS_DOCKER_BIN still wins) and DOCKER_CONTEXT pinned to the
# engine's (another engine's install rewrites ~/.docker/config.json currentContext).
# Callers that need extra PATH dirs set DOCKER_EXTRA_PATH (colon-prefixed dirs, e.g. /usr/local/go/bin).
DOCKER_ENGINE="${CAPSULE_DOCKER_ENGINE:-}"
if [ -z "$DOCKER_ENGINE" ] && [ -r "${STATE_DIR:-}/docker-engine" ]; then
  DOCKER_ENGINE="$(head -n 1 "$STATE_DIR/docker-engine" | tr -d '[:space:]')"
fi
DOCKER_ENGINE="${DOCKER_ENGINE:-orbstack}"
case "$DOCKER_ENGINE" in
  desktop)  DOCKER_ENGINE_BIN="/Applications/Docker.app/Contents/Resources/bin"; DOCKER_ENGINE_CONTEXT="desktop-linux"
            DOCKER_ENGINE_TITLE="Docker Desktop"; DOCKER_ENGINE_APP="Docker"; DOCKER_ENGINE_APP_PATH="/Applications/Docker.app"
            DOCKER_ENGINE_OPEN_HINT="open -a Docker"; DOCKER_ENGINE_CASK="docker-desktop" ;;
  orbstack) DOCKER_ENGINE_BIN="$HOME/.orbstack/bin"; DOCKER_ENGINE_CONTEXT="orbstack"
            DOCKER_ENGINE_TITLE="OrbStack"; DOCKER_ENGINE_APP="OrbStack"; DOCKER_ENGINE_APP_PATH="/Applications/OrbStack.app"
            DOCKER_ENGINE_OPEN_HINT="open -a OrbStack"; DOCKER_ENGINE_CASK="orbstack" ;;
  *) echo "unknown Docker engine '$DOCKER_ENGINE': use desktop or orbstack (CAPSULE_DOCKER_ENGINE or ${STATE_DIR:-<state dir>}/docker-engine)" >&2
     exit 2 ;;
esac
export DOCKER_CONTEXT="$DOCKER_ENGINE_CONTEXT"
# docker_path_export: call where a job needs docker; prepends the CLI dir (plus DOCKER_EXTRA_PATH) to PATH.
docker_path_export() { export PATH="${AGENT_OPS_DOCKER_BIN:-$DOCKER_ENGINE_BIN}${DOCKER_EXTRA_PATH:-}:$PATH"; }
