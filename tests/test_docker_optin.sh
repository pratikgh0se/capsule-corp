#!/usr/bin/env bash
# Docker tests are opt-in (card t_cf6ef8d1): RUN_DOCKER_TESTS unset -> a loud `SKIP <case>: ...` line and a
# skip count, never a PASS; RUN_DOCKER_TESTS=1 -> the case is attempted. A fake docker on PATH, no real docker.
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
fail=0
pass() { echo "PASS $1"; }; bad() { echo "FAIL $1"; fail=1; }
mkdir -p "$T/bin"; ARGV="$T/argv"; : > "$ARGV"
printf '#!/bin/sh\necho "$*" >> "%s"\ncase \"$1\" in info|image) exit 0;; esac\necho fake-docker >&2\nexit 1\n' "$ARGV" > "$T/bin/docker"
chmod +x "$T/bin/docker"
runsuite() { # runsuite <suite> <RUN_DOCKER_TESTS value, may be empty>: sets out and rc; empties the argv log
  : > "$ARGV"
  out="$(env -u RUN_DOCKER_TESTS -u SKIP_DOCKER_IMAGE_TEST ${2:+RUN_DOCKER_TESTS=$2} PATH="$T/bin:$PATH" \
    bash "$REPO/tests/$1.sh" 2>&1)"; rc=$?
}
for s in test_alert_rules_pg test_migration_pg test_docker_image test_fluent_bit_scrub; do
  runsuite "$s" ""
  [ $rc -eq 77 ] && grep -q "^SKIP $s: docker tests are opt-in, set RUN_DOCKER_TESTS=1" <<<"$out" && [ ! -s "$ARGV" ] \
    && pass "$s-unset-skips-loudly" || bad "$s-unset-skips-loudly: rc=$rc $out"
  runsuite "$s" 1
  grep -q '^run ' "$ARGV" || grep -q '^build ' "$ARGV" \
    && ! grep -q "docker tests are opt-in" <<<"$out" && pass "$s-set-attempts-docker" || bad "$s-set-attempts-docker: rc=$rc $out"
done
# the tick suite's one docker case (deadman-pg) is gated by the same helper, case-level (the rest of that suite is docker-free)
grep -q '^if ! docker_optin deadman-pg; then' "$REPO/tests/test_capsule_tick_sh.sh" \
  && pass tick-sh-deadman-pg-is-gated || bad tick-sh-deadman-pg-is-gated
# opted in means must-run (card t_cf6ef8d1 fix round 1): RUN_DOCKER_TESTS=1 with docker down FAILS, never skips
mkdir -p "$T/down"; printf '#!/bin/sh\nexit 1\n' > "$T/down/docker"; chmod +x "$T/down/docker"
for s in test_alert_rules_pg test_migration_pg test_docker_image test_fluent_bit_scrub; do
  out="$(env RUN_DOCKER_TESTS=1 PATH="$T/down:$PATH" bash "$REPO/tests/$s.sh" 2>&1)"; rc=$?
  [ $rc -eq 1 ] && grep -q "^FAIL $s: RUN_DOCKER_TESTS=1 but docker unavailable" <<<"$out" \
    && pass "$s-opted-in-docker-down-fails" || bad "$s-opted-in-docker-down-fails: rc=$rc $out"
done
# the helper alone, both modes
. "$REPO/tests/lib_docker_optin.sh"
msg="$(RUN_DOCKER_TESTS= docker_optin x)"; r1=$?; RUN_DOCKER_TESTS=1 docker_optin x; r2=$?
[ $r1 -eq 1 ] && [ "$msg" = "SKIP x: docker tests are opt-in, set RUN_DOCKER_TESTS=1 to run them" ] && [ $r2 -eq 0 ] \
  && pass helper-both-modes || bad "helper-both-modes: $r1 $r2 $msg"
# L116: docker_reap_pgtest stops capsule-pgtest-* containers whose embedded pid is dead (a SIGKILLed run), nothing else
sleep 0 & dead=$!; wait "$dead"
mkdir -p "$T/reap"; : > "$T/reap/argv"
cat > "$T/reap/docker" <<EOF
#!/bin/sh
echo "\$*" >> "$T/reap/argv"
[ "\$1" = ps ] && printf 'capsule-pgtest-alert-rules-$dead\ncapsule-pgtest-other-user-1\ncapsule-pgtest-migration-$$\ncapsule-pgtest-odd-name\ncapsule-corp-postgres-$dead\n'
exit 0
EOF
chmod +x "$T/reap/docker"
PATH="$T/reap:$PATH" docker_reap_pgtest
grep -qx "stop capsule-pgtest-alert-rules-$dead" "$T/reap/argv" && pass reap-dead-pid-stopped || bad "reap-dead-pid-stopped: $(cat "$T/reap/argv")"
! grep -q "stop capsule-pgtest-migration-$$" "$T/reap/argv" && pass reap-live-pid-left || bad "reap-live-pid-left"
# pid 1 is live but another user's (kill -0 gives EPERM): its container must be left too
! grep -q "stop capsule-pgtest-other-user-1" "$T/reap/argv" && pass reap-other-users-live-pid-left || bad "reap-other-users-live-pid-left"
[ "$(grep -c '^stop ' "$T/reap/argv")" = 1 ] && ! grep -Eq '^(rm|rmi|prune|system|volume)' "$T/reap/argv" \
  && pass reap-only-that-one-stop || bad "reap-only-that-one-stop: $(cat "$T/reap/argv")"
for s in test_alert_rules_pg test_deadman_queries_pg test_migration_pg; do
  grep -qx 'docker_reap_pgtest' "$REPO/tests/$s.sh" && pass "$s-reaps-at-start" || bad "$s-reaps-at-start"
done
exit $fail
