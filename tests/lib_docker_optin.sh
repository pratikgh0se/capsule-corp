# Docker tests are opt-in (card t_cf6ef8d1, inc_ebe612a5): RUN_DOCKER_TESTS=1 runs them. Unset, a case SKIPs
# loudly: one `SKIP <case>: ...` line, never a PASS. tests/run_shell_suites.sh counts every `^SKIP ` line a
# green suite prints, and counts exit 77 (a whole suite skipped) as one skipped suite.
# Usage: . tests/lib_docker_optin.sh; docker_optin <case> || { skip the case }; a whole suite: || exit 77
docker_optin() {
  [ "${RUN_DOCKER_TESTS:-}" = 1 ] && return 0
  echo "SKIP $1: docker tests are opt-in, set RUN_DOCKER_TESTS=1 to run them"
  return 1
}
# Opted in means must-run: after docker_optin passed, a docker that is down is a FAIL (return 1), never a skip.
docker_ready() {
  command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1 && return 0
  echo "FAIL $1: RUN_DOCKER_TESTS=1 but docker unavailable"
  return 1
}
# Reap what a SIGKILLed pg suite left (L116): a container named capsule-pgtest-<kind>-<pid> whose pid is dead
# is stopped (`--rm` removes it; `ps -p`, not `kill -0`, which fails EPERM for another user's live pid), the same verb as the suites' own trap. Live pids and other names are untouched.
docker_reap_pgtest() {
  local name pid
  for name in $(docker ps --filter name=capsule-pgtest- --format '{{.Names}}' 2>/dev/null); do
    case "$name" in capsule-pgtest-*-[0-9]*) ;; *) continue ;; esac
    pid="${name##*-}"
    case "$pid" in ''|*[!0-9]*) continue ;; esac
    ps -p "$pid" >/dev/null 2>&1 || docker stop "$name" >/dev/null 2>&1 || true
  done
}
