#!/usr/bin/env bash
# Exercises bin/capsule-secret against a fake infisical binary: get/run/check
# happy paths, argv shape sent to the real CLI (never the client secret), and
# the exit-3 failure paths (missing identity file, failed login).
set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT

fails=0
check() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; fails=$((fails+1)); fi; }

export CURL_CALLS_FILE="$T/curl_calls"

# --- fake infisical on PATH, logs its argv ---
FAKEBIN="$T/fakebin"
mkdir -p "$FAKEBIN"
ARGV_LOG="$T/argv.log"
ENV_LOG="$T/env.log"
cat >"$FAKEBIN/infisical" <<EOF
#!/usr/bin/env bash
echo "\$@" >>"$ARGV_LOG"
# What the CLI was given by environment: lets the test see that credentials
# arrive by environment, never argv.
echo "\$1 TOKEN_ENV=\${INFISICAL_TOKEN:-} CID_ENV=\${INFISICAL_UNIVERSAL_AUTH_CLIENT_ID:-} CSEC_ENV=\${INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET:-}" >>"$ENV_LOG"
case "\$1" in
  login)
    if [ "\$LOGIN_SHOULD_FAIL" = "1" ]; then
      exit 1
    fi
    echo "tok123"
    ;;
  secrets)
    if [ "\$CHECK_SHOULD_FAIL" = "1" ]; then
      echo "cannot read secret" >&2
      exit 1
    fi
    echo "value1"
    ;;
  export)
    echo "TELEGRAM_BOT_TOKEN='123:abc'"
    echo "TELEGRAM_ALLOWED_USERS='1,2'"
    ;;
  run)
    # find the args after --
    shift_to_dashdash=1
    args=("\$@")
    i=0
    for a in "\${args[@]}"; do
      i=\$((i+1))
      if [ "\$a" = "--" ]; then
        break
      fi
    done
    INJECTED=1 "\${args[@]:\$i}"
    ;;
  *)
    exit 1
    ;;
esac
EOF
chmod +x "$FAKEBIN/infisical"

cat >"$FAKEBIN/curl" <<'EOF'
#!/usr/bin/env bash
n=$(cat "$CURL_CALLS_FILE"); n=$((n+1)); echo $n >"$CURL_CALLS_FILE"
[ "$n" -le "${CURL_FAIL_TIMES:-0}" ] && exit 22
echo '{"date":"x"}'
EOF
chmod +x "$FAKEBIN/curl"

export PATH="$FAKEBIN:$PATH"

# --- config dir ---
CONF="$T/config"
mkdir -p "$CONF/identities"
echo "PROJECT_CAPSULE_CORP=proj-abc123" >"$CONF/projects.env"
cat >"$CONF/identities/bridge.env" <<EOF
INFISICAL_CLIENT_ID=client-id-xyz
INFISICAL_CLIENT_SECRET=super-secret-value
EOF

export CAPSULE_CONFIG_DIR="$CONF"
# Pin the backend and a temp CAPSULE_HOME so the suite never depends on the caller's machine.
export CAPSULE_HOME="$T/home" CAPSULE_SECRETS_BACKEND=infisical
# Important 2: CAPSULE_IDENTITY is required (no default), so every happy-path
# call below must set it explicitly - bridge is the identity fixture'd above.
export CAPSULE_IDENTITY=bridge
export CAPSULE_SECRET_WAIT_S=0
export CAPSULE_SECRET_WAIT_STEP_S=0
CS="$REPO/bin/capsule-secret"

# --- get ---
rm -f "$ARGV_LOG"
out="$("$CS" get capsule-corp CAPSULE_CHECK)"
check "get prints the secret value" '[ "$out" = "value1" ]'
check "get argv has projectId/env/domain, no token, no client secret" \
  'GET_LINE="$(grep "^secrets get" "$ARGV_LOG")"; grep -q -- "--projectId=proj-abc123 --env=prod --domain=http://localhost:8085/api --plain" <<<"$GET_LINE"'
check "no secret (client secret, token) is on argv for any call" \
  '! grep -q "super-secret-value\|tok123" "$ARGV_LOG"'
check "client credentials and token reach the CLI by environment" \
  'grep -q "^login .*CID_ENV=client-id-xyz CSEC_ENV=super-secret-value" "$ENV_LOG" && grep -q "^secrets TOKEN_ENV=tok123" "$ENV_LOG"'

# --- run ---
rm -f "$ARGV_LOG"
out="$("$CS" run capsule-corp -- env)"
check "run injects env from the fake infisical" 'grep -q "^INJECTED=1$" <<<"$out"'

# --- check ---
rm -f "$ARGV_LOG"
out="$("$CS" check)"
check "check prints ok with identity" '[ "$out" = "capsule-secret: ok (bridge)" ]'

# --- check failure (Important 1): a server that answers but an identity
# that cannot read CAPSULE_CHECK from capsule-corp must be our exit 3 with
# a capsule-secret: line, not the bare Infisical CLI's exit 1 and silence.
out="$(CHECK_SHOULD_FAIL=1 "$CS" check 2>&1)"; rc=$?
check "check failure (cannot read CAPSULE_CHECK) exits 3" '[ $rc -eq 3 ]'
check "check failure message" 'grep -qF "capsule-secret: cannot read CAPSULE_CHECK from capsule-corp as bridge" <<<"$out"'

# --- CAPSULE_IDENTITY unset (Important 2: no default, fails closed) ---
out="$(env -u CAPSULE_IDENTITY "$CS" get capsule-corp CAPSULE_CHECK 2>&1)"; rc=$?
check "unset CAPSULE_IDENTITY exits 3" '[ $rc -eq 3 ]'
check "unset CAPSULE_IDENTITY message" 'grep -qF "capsule-secret: set CAPSULE_IDENTITY" <<<"$out"'

# --- missing identity ---
out="$(CAPSULE_IDENTITY=nope "$CS" get capsule-corp CAPSULE_CHECK 2>&1)"; rc=$?
check "missing identity exits 3" '[ $rc -eq 3 ]'
check "missing identity message" 'grep -q "capsule-secret: no identity file for nope" <<<"$out"'

# --- login failure ---
out="$(LOGIN_SHOULD_FAIL=1 "$CS" get capsule-corp CAPSULE_CHECK 2>&1)"; rc=$?
check "login failure exits 3" '[ $rc -eq 3 ]'
check "login failure message prefix" 'grep -q "^capsule-secret: " <<<"$out"'

# --- missing projects.env (Minor 1: must name the projects file, not
# misdiagnose it as a missing identity file) ---
NOPROJ="$T/no-projects-config"; mkdir -p "$NOPROJ/identities"
cat >"$NOPROJ/identities/bridge.env" <<EOF
INFISICAL_CLIENT_ID=client-id-xyz
INFISICAL_CLIENT_SECRET=super-secret-value
EOF
out="$(CAPSULE_SECRETS_BACKEND=infisical CAPSULE_CONFIG_DIR="$NOPROJ" "$CS" get capsule-corp CAPSULE_CHECK 2>&1)"; rc=$?
check "missing projects.env exits 3" '[ $rc -eq 3 ]'
check "missing projects.env names the projects file" 'grep -qF "capsule-secret: no projects file: $NOPROJ/projects.env" <<<"$out"'

# --- malformed identity file (fails closed, not just silently sourced) ---
cat >"$CONF/identities/broken.env" <<'EOF'
this is not valid shell (
EOF
out="$(CAPSULE_IDENTITY=broken "$CS" get capsule-corp CAPSULE_CHECK 2>&1)"; rc=$?
check "malformed identity file exits 3" '[ $rc -eq 3 ]'
check "malformed identity file message" 'grep -q "capsule-secret: malformed config:.*broken.env" <<<"$out"'

# --- run: the child must not inherit the access token ---
out="$("$CS" run capsule-corp -- sh -c 'echo "tok=${INFISICAL_TOKEN:-none}"')"
check "run scrubs INFISICAL_TOKEN from the child" '[ "$out" = "tok=none" ]'

# Script-owned names must not leak into, or clobber, the caller's environment.
out="$(TOKEN=x CMD=mine PID=p NAME=n "$CS" run capsule-corp -- sh -c 'echo "$TOKEN|$CMD|$PID|$NAME|${INFISICAL_TOKEN:-none}"')"
check "run: caller's TOKEN/CMD/PID/NAME survive, the access token never reaches the child" '[ "$out" = "x|mine|p|n|none" ]'

# --- export ---
rm -f "$ARGV_LOG"
out="$("$CS" export capsule-corp)"
check "export passes dotenv lines through" '[ "$out" = "$(printf "TELEGRAM_BOT_TOKEN='"'"'123:abc'"'"'\nTELEGRAM_ALLOWED_USERS='"'"'1,2'"'"'")" ]'
check "export argv shape" 'grep -q -- "^export --format=dotenv --projectId=proj-abc123 --env=prod --domain=http://localhost:8085/api$" "$ARGV_LOG"'

# --- wait loop ---
echo 0 >"$T/curl_calls"
out="$(CURL_FAIL_TIMES=2 CAPSULE_SECRET_WAIT_S=10 "$CS" get capsule-corp CAPSULE_CHECK 2>&1)"; rc=$?
check "wait loop retries until /status answers" '[ $rc -eq 0 ] && [ "$(cat "$T/curl_calls")" = "3" ]'
echo 0 >"$T/curl_calls"
out="$(CURL_FAIL_TIMES=99 CAPSULE_SECRET_WAIT_S=0 "$CS" get capsule-corp CAPSULE_CHECK 2>&1)"; rc=$?
check "CAPSULE_SECRET_WAIT_S=0 skips the wait entirely" '[ $rc -eq 0 ] && [ "$(cat "$T/curl_calls")" = "0" ]'
echo 0 >"$T/curl_calls"
out="$(CURL_FAIL_TIMES=99 CAPSULE_SECRET_WAIT_S=3 "$CS" get capsule-corp CAPSULE_CHECK 2>&1)"; rc=$?
check "wait timeout exits 3 with the message" '[ $rc -eq 3 ] && grep -qF "capsule-secret: infisical not answering after 3s" <<<"$out"'
echo 0 >"$T/curl_calls"
out="$(CURL_FAIL_TIMES=99 CAPSULE_SECRET_WAIT_STEP_S=0 CAPSULE_SECRET_WAIT_S=3 "$CS" get capsule-corp CAPSULE_CHECK 2>&1)"; rc=$?
check "wait step=0 does not busy-loop forever, bounded by wait_s polls" '[ $rc -eq 3 ] && [ "$(cat "$T/curl_calls")" = "4" ]'

# --- check --identity-all ---
echo "PROJECT_AGENT_OPS=proj-ops" >>"$CONF/projects.env"
printf 'INFISICAL_CLIENT_ID=a\nINFISICAL_CLIENT_SECRET=b\n' >"$CONF/identities/compose.env"
rm -f "$CONF/identities/broken.env"
rm -f "$ARGV_LOG"
out="$("$CS" check --identity-all)"; rc=$?
check "identity-all prints one ok line per identity" '[ $rc -eq 0 ] && grep -q "capsule-secret: ok (bridge)" <<<"$out" && grep -q "capsule-secret: ok (compose)" <<<"$out"'
# "agent-ops" here names a live Infisical project distinct from this repo's
# name (rename/capsule-corp inventory item B7) - not renamed with the repo.
check "identity-all: bridge reads agent-ops, compose reads capsule-corp" 'grep -q -- "--projectId=proj-ops" "$ARGV_LOG" && grep -q -- "--projectId=proj-abc123" "$ARGV_LOG"'
out="$(CHECK_SHOULD_FAIL=1 "$CS" check --identity-all 2>&1)"; rc=$?
check "identity-all exits 3 when one identity fails" '[ $rc -eq 3 ]'
out="$("$CS" check --identity-all foo 2>&1)"; rc=$?
check "identity-all with a trailing arg hits usage" '[ $rc -eq 2 ]'
# An empty identities/ dir must say so and fail closed, not iterate the
# literal `*.env` glob and then complain about an identity named `*`.
# The caller's own identity file is checked before the subcommand runs, so it
# is parked one level up (CAPSULE_IDENTITY=../bridge) to leave identities/
# itself genuinely empty.
EMPTY_CONF="$T/empty-conf"; mkdir -p "$EMPTY_CONF/identities"
cp "$CONF/projects.env" "$EMPTY_CONF/projects.env"
printf 'INFISICAL_CLIENT_ID=a\nINFISICAL_CLIENT_SECRET=b\n' >"$EMPTY_CONF/bridge.env"
out="$(CAPSULE_CONFIG_DIR="$EMPTY_CONF" CAPSULE_IDENTITY=../bridge "$CS" check --identity-all 2>&1)"; rc=$?
check "identity-all on an empty identities dir exits 3 naming the dir" \
  '[ $rc -eq 3 ] && grep -qF "capsule-secret: no identity files under $EMPTY_CONF/identities" <<<"$out" && ! grep -q "no identity file for \*" <<<"$out"'

echo; if [ $fails -eq 0 ]; then echo "test_capsule_secret: all passed"; else echo "test_capsule_secret: $fails failed"; exit 1; fi
