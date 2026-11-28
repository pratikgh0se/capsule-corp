#!/usr/bin/env bash
# factory-autonomy Task 14 (card t_95d328af): tests/run_shell_suites.sh over a fixture dir - one
# green suite, one red, one `# needs: docker`,
# one after the red that must never run, one that hangs past the timeout.
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
fail=0; PASSED=""
pass() { echo "PASS $1"; PASSED="$PASSED|$1"; }; bad() { echo "FAIL $1"; fail=1; }
[ -x "$REPO/tests/run_shell_suites.sh" ] || { echo "FAIL runner missing"; exit 1; }
D="$T/suites"; mkdir -p "$D"
printf 'exit 0\n' > "$D/test_a_green.sh"
printf '#!/usr/bin/env bash\n# needs: docker\ntouch "%s/docker-ran"; exit 1\n' "$T" > "$D/test_b_docker.sh"
printf '#!/usr/bin/env bash\n# needs: postgres\ntouch "%s/pg-ran"; exit 1\n' "$T" > "$D/test_b2_pg.sh"
printf '#!/usr/bin/env bash\n# needs: dockerfoo\ntouch "%s/prefix-ran"\n' "$T" > "$D/test_b3_prefix.sh"
printf 'echo boom-detail; exit 1\n' > "$D/test_c_red.sh"
printf 'touch "%s/after-ran"\n' "$T" > "$D/test_d_after.sh"
out="$(bash "$REPO/tests/run_shell_suites.sh" --dir "$D" 2>&1)"; rc=$?
[ $rc -ne 0 ] && pass red-exits-non-zero || bad "red-exits-non-zero: $out"
grep -q "^FAIL .*test_c_red.sh" <<<"$out" && pass red-names-the-suite || bad "red-names-the-suite: $out"
grep -q "^PASS .*test_a_green.sh" <<<"$out" && pass green-runs || bad "green-runs: $out"
grep -q "^SKIP .*test_b_docker.sh.*needs: docker" <<<"$out" && [ ! -e "$T/docker-ran" ] \
  && grep -q "^SKIP .*test_b2_pg.sh.*needs: postgres" <<<"$out" && [ ! -e "$T/pg-ran" ] \
  && pass needs-docker-skipped || bad "needs-docker-skipped: $out"
[ -e "$T/prefix-ran" ] && pass header-regex-is-anchored || bad "header-regex-is-anchored: $out"
grep -q "boom-detail" <<<"$out" && grep -q "^FAIL .*(exit 1)$" <<<"$out" \
  && pass red-shows-log-tail-and-exit-code || bad "red-shows-log-tail-and-exit-code: $out"
grep -q "^shell suites: 2 passed, 1 failed, 2 skipped$" <<<"$out" && pass red-summary-line || bad "red-summary-line: $out"
[ ! -e "$T/after-ran" ] && pass stops-at-first-red || bad stops-at-first-red
rm "$D/test_c_red.sh"
out="$(bash "$REPO/tests/run_shell_suites.sh" --dir "$D" 2>&1)"; rc=$?
[ $rc -eq 0 ] && [ -e "$T/after-ran" ] && grep -q "^shell suites: 3 passed, 0 failed, 2 skipped$" <<<"$out" \
  && pass all-green-exits-zero || bad "all-green-exits-zero: $out"
printf 'sleep 30\n' > "$D/test_e_hang.sh"
out="$(SHELL_SUITE_TIMEOUT=2 bash "$REPO/tests/run_shell_suites.sh" --dir "$D" 2>&1)"; rc=$?
[ $rc -ne 0 ] && grep -q "^FAIL .*test_e_hang.sh.*timed out after 2s" <<<"$out" && pass hang-times-out || bad "hang-times-out: $out"
# Shards (card t_029d1b53): a `# shard: slow` header moves a suite out of --shard fast into --shard slow;
# no --shard runs everything, so no suite can fall between the two contract commands.
S="$T/shards"; mkdir -p "$S"
printf '#!/usr/bin/env bash\n# shard: slow\ntouch "%s/slow-ran"\n' "$T" > "$S/test_a_slow.sh"
printf 'touch "%s/fast-ran"\n' "$T" > "$S/test_b_fast.sh"
printf '#!/usr/bin/env bash\n# needs: docker\n# shard: slow\ntouch "%s/slow-docker-ran"\n' "$T" > "$S/test_c_slow_docker.sh"
out="$(bash "$REPO/tests/run_shell_suites.sh" --dir "$S" --shard fast 2>&1)"; rc=$?
[ $rc -eq 0 ] && [ -e "$T/fast-ran" ] && [ ! -e "$T/slow-ran" ] \
  && grep -q "^shell suites: 1 passed, 0 failed, 0 skipped$" <<<"$out" && pass shard-fast-skips-slow || bad "shard-fast-skips-slow: $out"
rm -f "$T/fast-ran"
out="$(bash "$REPO/tests/run_shell_suites.sh" --dir "$S" --shard slow 2>&1)"; rc=$?
[ $rc -eq 0 ] && [ -e "$T/slow-ran" ] && [ ! -e "$T/fast-ran" ] \
  && grep -q "^SKIP .*test_c_slow_docker.sh.*needs: docker" <<<"$out" \
  && grep -q "^shell suites: 1 passed, 0 failed, 1 skipped$" <<<"$out" && pass shard-slow-runs-only-slow || bad "shard-slow-runs-only-slow: $out"
rm -f "$T/slow-ran"
out="$(bash "$REPO/tests/run_shell_suites.sh" --dir "$S" 2>&1)"; rc=$?
[ $rc -eq 0 ] && [ -e "$T/slow-ran" ] && [ -e "$T/fast-ran" ] && pass no-shard-runs-all || bad "no-shard-runs-all: $out"
out="$(bash "$REPO/tests/run_shell_suites.sh" --dir "$S" --shard bogus 2>&1)"; rc=$?
[ $rc -eq 2 ] && pass bad-shard-rejected || bad "bad-shard-rejected: rc=$rc $out"
# the real tree: the two shards partition every suite (slow-marked ones are the budget split).
slow=""; for f in "$REPO"/tests/test_*.sh; do head -n 10 "$f" | grep -Eq '^# shard: slow$' && slow="$slow $(basename "$f")"; done
[ "$slow" = " test_deploy.sh test_capsule_state_migrate.sh" ] || [ "$slow" = " test_capsule_state_migrate.sh test_deploy.sh" ] \
  && pass slow-shard-pins-the-heavy-suites || bad "slow-shard-pins-the-heavy-suites: [$slow]"
mkdir "$T/self"; printf 'exit 1\n' > "$T/self/test_run_shell_suites.sh"
out="$(bash "$REPO/tests/run_shell_suites.sh" --dir "$T/self" 2>&1)"
grep -q "^FAIL .*test_run_shell_suites.sh" <<<"$out" \
  && pass runner-test-is-not-excluded || bad runner-test-is-not-excluded
# Fix 2 (review): only the pinned suites may carry a `# needs:` header; the pin is the protected jsonl.
pinned="$(python3 -c 'import json,sys; print(json.loads(open(sys.argv[1]).readline())["expect"]["skipped"])' \
  "$REPO/contracts/intent/run_shell_suites.jsonl")"
actual=""
for f in "$REPO"/tests/test_*.sh; do
  head -n 10 "$f" | grep -Eq '^# needs: *(docker|host|postgres)( |$)' && actual="${actual:+$actual,}$(basename "$f")"
done
[ "$actual" = "$pinned" ] && pass needs-header-only-on-pinned-suites || bad "needs-header-only-on-pinned-suites: headers=[$actual] pinned=[$pinned]"
# Docker opt-in (row 615, t_cf6ef8d1): exit 77 is a skip ONLY with a matching `SKIP <case>:` line, never a pass; a green
# suite's `SKIP` lines count as skipped cases; exit 77 with no SKIP line is a failure.
Q="$T/optin"; mkdir -p "$Q"
printf 'echo "SKIP whole: docker tests are opt-in"; exit 77\n' > "$Q/test_a_whole.sh"
printf 'echo "SKIP case-b: docker tests are opt-in"\n' > "$Q/test_b_case.sh"
out="$(bash "$REPO/tests/run_shell_suites.sh" --dir "$Q" 2>&1)"; rc=$?
[ $rc -eq 0 ] && grep -q "^SKIP .*test_a_whole.sh" <<<"$out" && ! grep -q "^PASS .*test_a_whole.sh" <<<"$out" \
  && grep -q "^shell suites: 1 passed, 0 failed, 2 skipped$" <<<"$out" \
  && pass exit-77-suite-is-skipped-never-passed || bad "exit-77-suite-is-skipped-never-passed: rc=$rc $out"
printf 'exit 77\n' > "$Q/test_a_whole.sh"
out="$(bash "$REPO/tests/run_shell_suites.sh" --dir "$Q" 2>&1)"; rc=$?
[ $rc -ne 0 ] && grep -q "^FAIL .*test_a_whole.sh (exit 77, no SKIP line)" <<<"$out" \
  && pass exit-77-without-skip-line-is-red || bad "exit-77-without-skip-line-is-red: rc=$rc $out"
# ui_check's skip is loud and counted: with no playwright interpreter (fake python3, empty HOME, no env gate) the real
# suite skips whole by itself. Where /opt/playwright exists (the worker image) it runs for real and must not fail.
U="$T/ui"; mkdir -p "$U" "$T/nopw/bin" "$T/nopw/home"; ln -s "$REPO/tests/test_ui_check.sh" "$U/test_ui_check.sh"
printf '#!/bin/sh\nexit 1\n' > "$T/nopw/bin/python3"; chmod +x "$T/nopw/bin/python3"
if [ -x /opt/playwright/bin/python ]; then
  out="$(bash "$REPO/tests/run_shell_suites.sh" --dir "$U" 2>&1)"; rc=$?
  [ $rc -eq 0 ] && pass ui-check-skip-is-counted || bad "ui-check-skip-is-counted: rc=$rc $out"
else
  out="$(env -u SKIP_DOCKER_IMAGE_TEST HOME="$T/nopw/home" PATH="$T/nopw/bin:$PATH" \
    bash "$REPO/tests/run_shell_suites.sh" --dir "$U" 2>&1)"; rc=$?
  [ $rc -eq 0 ] && grep -q "^SKIP .*test_ui_check.sh (whole suite skipped)$" <<<"$out" \
    && grep -q "^SKIP test_ui_check: no interpreter with playwright" <<<"$out" \
    && grep -q "^shell suites: 0 passed, 0 failed, 1 skipped$" <<<"$out" \
    && pass ui-check-skip-is-counted || bad "ui-check-skip-is-counted: rc=$rc $out"
fi
# --docker-auto <base> (row 615): the runner turns RUN_DOCKER_TESTS=1 on itself when the diff base...HEAD touches a docker path.
G="$T/auto"; mkdir -p "$G/tests" "$G/suites"; cp "$REPO/tests/run_shell_suites.sh" "$G/tests/"
printf 'echo "docker=${RUN_DOCKER_TESTS:-unset}"; exit 1\n' > "$G/suites/test_a_env.sh"
g() { git -C "$G" -c user.name=t -c user.email=t@t "$@" >/dev/null 2>&1; }
g init -q; g add -A; g commit -qm base; g tag base
run_auto() { env -u RUN_DOCKER_TESTS "$@" bash "$G/tests/run_shell_suites.sh" --dir "$G/suites" --docker-auto base 2>&1; }
out="$(run_auto)"; grep -q "docker=unset" <<<"$out" && pass docker-auto-untouched-leaves-off || bad "docker-auto-untouched-leaves-off: $out"
mkdir -p "$G/docs"; printf x > "$G/docs/a.md"; g add -A; g commit -qm docs
out="$(run_auto)"; grep -q "docker=unset" <<<"$out" && pass docker-auto-unrelated-path-leaves-off || bad "docker-auto-unrelated-path-leaves-off: $out"
mkdir -p "$G/observability"; printf x > "$G/observability/a.yml"; g add -A; g commit -qm obs
out="$(run_auto)"; grep -q "docker=1" <<<"$out" && grep -q "^DOCKER-AUTO: RUN_DOCKER_TESTS=1 because observability/a.yml " <<<"$out" \
  && pass docker-auto-touched-path-turns-on || bad "docker-auto-touched-path-turns-on: $out"
out="$(run_auto RUN_DOCKER_TESTS=0)"; grep -q "docker=0" <<<"$out" && ! grep -q "^DOCKER-AUTO" <<<"$out" \
  && pass docker-auto-explicit-zero-wins || bad "docker-auto-explicit-zero-wins: $out"
out="$(env -u RUN_DOCKER_TESTS bash "$G/tests/run_shell_suites.sh" --dir "$G/suites" 2>&1)"; grep -q "docker=unset" <<<"$out" \
  && pass docker-auto-absent-is-unchanged || bad "docker-auto-absent-is-unchanged: $out"
out="$(env -u RUN_DOCKER_TESTS bash "$G/tests/run_shell_suites.sh" --dir "$G/suites" --docker-auto nosuchref 2>&1)"; rc=$?
[ $rc -eq 2 ] && grep -q "^DOCKER-AUTO: cannot diff nosuchref...HEAD" <<<"$out" && ! grep -q "docker=" <<<"$out" \
  && pass docker-auto-bad-base-fails-loud || bad "docker-auto-bad-base-fails-loud: rc=$rc $out"
out="$(env -u RUN_DOCKER_TESTS bash "$G/tests/run_shell_suites.sh" --dir "$G/suites" --docker-auto "" 2>&1)"; rc=$?
[ $rc -eq 2 ] && grep -q "^DOCKER-AUTO: cannot diff" <<<"$out" && pass docker-auto-empty-base-fails-loud || bad "docker-auto-empty-base-fails-loud: rc=$rc $out"
out="$(run_auto RUN_DOCKER_TESTS=)"; grep -q "docker=1" <<<"$out" && pass docker-auto-empty-env-is-unset || bad "docker-auto-empty-env-is-unset: $out"
g tag base2; mkdir -p "$G/docker"; printf x > "$G/docker/run.sh"; g add -A; g commit -qm dk; g tag base3
g mv docker/run.sh moved_run.sh; g commit -qm mv
out="$(env -u RUN_DOCKER_TESTS bash "$G/tests/run_shell_suites.sh" --dir "$G/suites" --docker-auto base3 2>&1)"
grep -q "^DOCKER-AUTO: .*because docker/run.sh " <<<"$out" && pass docker-auto-rename-out-turns-on || bad "docker-auto-rename-out-turns-on: $out"
g checkout -q -b gated base2; printf x > "$G/tests/test_docker_image.sh"; g add -A; g commit -qm gated
out="$(env -u RUN_DOCKER_TESTS bash "$G/tests/run_shell_suites.sh" --dir "$G/suites" --docker-auto base2 2>&1)"
grep -q "^DOCKER-AUTO: .*because tests/test_docker_image.sh " <<<"$out" && pass docker-auto-gated-suite-turns-on || bad "docker-auto-gated-suite-turns-on: $out"
out="$(env -u RUN_DOCKER_TESTS CAPSULE_DOCKER_AUTO_BASE=base2 bash "$G/tests/run_shell_suites.sh" --dir "$G/suites" 2>&1)"
grep -q "^DOCKER-AUTO: .*because tests/test_docker_image.sh " <<<"$out" && pass docker-auto-env-only-turns-on || bad "docker-auto-env-only-turns-on: $out"
out="$(env -u RUN_DOCKER_TESTS CAPSULE_DOCKER_AUTO_BASE=nosuchref bash "$G/tests/run_shell_suites.sh" --dir "$G/suites" --docker-auto base2 2>&1)"
grep -q "^DOCKER-AUTO: .*because tests/test_docker_image.sh " <<<"$out" && pass docker-auto-flag-overrides-env || bad "docker-auto-flag-overrides-env: $out"
printf 'echo "base=${CAPSULE_DOCKER_AUTO_BASE:-unset}"; exit 1\n' > "$G/suites/test_a_env.sh"
out="$(env -u RUN_DOCKER_TESTS CAPSULE_DOCKER_AUTO_BASE=base2 bash "$G/tests/run_shell_suites.sh" --dir "$G/suites" 2>&1)"
grep -q "base=unset" <<<"$out" && pass docker-auto-env-not-passed-to-suites || bad "docker-auto-env-not-passed-to-suites: $out"
# Z1 (row 455): contracts/intent/run_shell_suites.jsonl replayed here, no extra run - each example names a check above that passed.
python3 -c 'import json,sys; rows=[json.loads(l) for l in open(sys.argv[1]) if l.strip()]; got=sys.argv[2].split("|"); sys.exit(0 if rows and all(r["expect"]["pass"] in got for r in rows) else 1)' \
  "$REPO/contracts/intent/run_shell_suites.jsonl" "$PASSED" && pass intent-examples || bad intent-examples
exit $fail
