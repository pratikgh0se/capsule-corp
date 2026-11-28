#!/usr/bin/env bash
# Runs scrub.lua through a real Fluent Bit container (fluent/fluent-bit:5.1.5,
# the image the compose stack uses) and checks the redaction it produces on
# stdout - the same C1-shaped risk test_migration_pg.sh guards against for
# psql: a hand-traced Lua pattern agreeing with itself proves nothing about
# what the real interpreter does with it.
#
# Opt-in: skips (exit 77, prints SKIP) unless RUN_DOCKER_TESTS=1; with it set and docker down it FAILS - like tests/test_docker_image.sh
# and tests/test_migration_pg.sh. Never touches the live capsule-corp-fluent-bit
# container: its own throwaway container name, removed on exit.
set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
. "$REPO/tests/lib_docker_optin.sh"
docker_optin test_fluent_bit_scrub || exit 77
docker_ready test_fluent_bit_scrub || exit 1

CT="capsule-corp-fluent-bit-scrub-test-$$"
TMP="$(mktemp -d)"
cleanup() { docker rm -f "$CT" >/dev/null 2>&1 || true; rm -rf "$TMP"; }
trap cleanup EXIT

fails=0
check() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; fails=$((fails+1)); fi; }

# One line carrying every shape this filter must redact and every shape it
# must leave alone: the C1 negatives (ordinary text the frontier-less
# patterns used to eat - "ri[sk-]free", "ho[st.d]ocker.internal", since [%x]
# accepts "d") and the I1 env-var-shaped keys.
SK="sk-$(printf 'a%.0s' $(seq 24))"                     # >= 20 after "sk-"
JWT="eyJdddddddddd.eeeeeeeeeeee.ffffffffffff"           # >= 10 per part
LINE="token 123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA password=hunter2 ok telegram:-1001234567890:10 x-api-key=abcdefghij auth-token=klmnopqrst INFISICAL_CLIENT_SECRET=abc TELEGRAM_BOT_TOKEN=xyz LEGACY_API_KEY=k key=$SK auth=$JWT ledger pushed: 3 entries task-list risk-free capsule.host.disk.pct host.docker.internal test.db manifest.cafe latest.bad"
cat >"$TMP/test.conf" <<EOF
[SERVICE]
    Flush        1
    Log_Level    error

[INPUT]
    Name   dummy
    Dummy  {"log":"$LINE"}

[FILTER]
    Name    lua
    Match   *
    script  /fluent-bit/etc/scrub.lua
    call    scrub_log

[OUTPUT]
    Name         stdout
    Match        *
    Format       json_lines
EOF

# macOS has no `timeout`; a background `docker run` (dummy input free-runs
# forever) killed after a fixed wait is the portable equivalent.
OUT="$TMP/out.log"
docker run --rm --label capsule=1 --label capsule.kind=test --name "$CT" \
  -v "$TMP/test.conf:/fluent-bit/etc/test.conf:ro" \
  -v "$REPO/observability/fluent-bit/scrub.lua:/fluent-bit/etc/scrub.lua:ro" \
  fluent/fluent-bit:5.1.5 /fluent-bit/bin/fluent-bit -c /fluent-bit/etc/test.conf \
  >"$OUT" 2>&1 &
PID=$!
( sleep 15; kill "$PID" >/dev/null 2>&1; docker rm -f "$CT" >/dev/null 2>&1 ) &
WATCHDOG=$!
wait "$PID" 2>/dev/null
kill "$WATCHDOG" >/dev/null 2>&1
wait "$WATCHDOG" 2>/dev/null

out="$(cat "$OUT")"
check "fluent-bit produced at least one output line" '[ -n "$out" ]'
check "telegram-token redacted" 'grep -q "<redacted:telegram-token>" <<<"$out"'
check "kv-secret redacted, key kept" 'grep -q "password=<redacted:kv-secret>" <<<"$out"'
check "the raw telegram token never appears" '! grep -q "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" <<<"$out"'
check "the raw password value never appears" '! grep -q "hunter2" <<<"$out"'
check "the telegram negative fixture (chat id) survives intact" 'grep -q "telegram:-1001234567890:10" <<<"$out"'
check "hyphenated api-key kv redacted, hyphen prefix kept" 'grep -q "x-api-key=<redacted:kv-secret>" <<<"$out"'
check "hyphenated token kv redacted, hyphen prefix kept" 'grep -q "auth-token=<redacted:kv-secret>" <<<"$out"'
check "the raw x-api-key value never appears" '! grep -q "abcdefghij" <<<"$out"'
check "the raw auth-token value never appears" '! grep -q "klmnopqrst" <<<"$out"'
# C1: the two rows whose floor+frontier changed most - a real secret of at
# least the Python minimum length must still be redacted, so the frontier and
# the string.rep floor cannot have been made so strict they under-redact.
check "openai-key redacted (C1)" 'grep -q "key=<redacted:openai-key>" <<<"$out"'
check "the raw sk- key never appears (C1)" '! grep -q "$SK" <<<"$out"'
check "jwt redacted (C1)" 'grep -q "auth=<redacted:jwt>" <<<"$out"'
check "the raw jwt never appears (C1)" '! grep -q "$JWT" <<<"$out"'
# I1: env-var-shaped keys keep the whole key, lose the value.
check "INFISICAL_CLIENT_SECRET redacted, whole key kept (I1)" 'grep -q "INFISICAL_CLIENT_SECRET=<redacted:kv-secret>" <<<"$out"'
check "TELEGRAM_BOT_TOKEN redacted, whole key kept (I1)" 'grep -q "TELEGRAM_BOT_TOKEN=<redacted:kv-secret>" <<<"$out"'
check "LEGACY_API_KEY redacted, whole key kept (I1)" 'grep -q "LEGACY_API_KEY=<redacted:kv-secret>" <<<"$out"'
# C1: ordinary text must survive byte-for-byte - no frontier, no floor, and
# the real Lua interpreter redacts every one of these.
for neg in task-list risk-free capsule.host.disk.pct host.docker.internal test.db manifest.cafe latest.bad "ledger pushed: 3 entries"; do
  check "negative fixture survives intact: $neg (C1)" 'grep -qF "$neg" <<<"$out"'
done

if [ $fails -ne 0 ]; then
  echo "---- fluent-bit output ----"
  echo "$out"
  echo "----------------------------"
fi

[ $fails -eq 0 ] && echo "test_fluent_bit_scrub: all passed" || { echo "test_fluent_bit_scrub: $fails failed"; exit 1; }
