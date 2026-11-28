#!/usr/bin/env bash
# bin/capsule-secret keychain source (D227): on the infisical backend, allow-listed keys for
# project capsule-corp are read from the Keychain after the identity checks and before login.
# A fake `security` (via SECURITY_BIN) stands in; the real Keychain is never touched.
set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT

fails=0
check() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; fails=$((fails+1)); fi; }

export HOME="$T/home"; mkdir -p "$HOME"
export CAPSULE_HOME="$T/capsule"; mkdir -p "$CAPSULE_HOME"
CONF="$T/config"; mkdir -p "$CONF/identities"
echo "PROJECT_CAPSULE_CORP=proj-abc123" >"$CONF/projects.env"
echo "PROJECT_OTHER=proj-other" >>"$CONF/projects.env"
printf 'INFISICAL_CLIENT_ID=cid\nINFISICAL_CLIENT_SECRET=csec\n' >"$CONF/identities/bridge.env"
export CAPSULE_CONFIG_DIR="$CONF" CAPSULE_SECRETS_BACKEND=infisical CAPSULE_IDENTITY=bridge
export CAPSULE_SECRET_WAIT_S=0 CAPSULE_SECRET_WAIT_STEP_S=0
unset SECURITY_BIN CAPSULE_KEYCHAIN_WAIT_S
CS="$REPO/bin/capsule-secret"
CALLS="$T/security.calls"; ICALLS="$T/infisical.calls"

mkdir -p "$T/bin"
cat >"$T/bin/security" <<EOF
#!/usr/bin/env bash
echo "\$*" >>"$CALLS"
[ -f "$T/kc.sleep" ] && sleep 5
key=""; while [ \$# -gt 0 ]; do [ "\$1" = -a ] && key="\$2"; shift; done
f="$T/kc.\$key"
[ -f "\$f" ] || { echo "security: item not found" >&2; exit 44; }
cat "\$f"
EOF
cat >"$T/bin/infisical" <<EOF
#!/usr/bin/env bash
echo "\$1" >>"$ICALLS"
case "\$1" in login) echo tok;; secrets) echo fake-infisical-token;; esac
EOF
cat >"$T/bin/curl" <<EOF
#!/usr/bin/env bash
echo curl >>"$ICALLS"; echo '{}'
EOF
chmod +x "$T/bin/"*
export SECURITY_BIN="$T/bin/security" INFISICAL_BIN="$T/bin/infisical" CURL_BIN="$T/bin/curl"
printf 'fake-keychain-token\n' >"$T/kc.SLACK_BOT_TOKEN"
printf 'fake-keychain-other\n' >"$T/kc.OTHER_KEY"

# hit: no Infisical, no curl
: >"$CALLS"; : >"$ICALLS"
check "keychain hit wins and Infisical/curl are never called" \
  '[ "$("$CS" get capsule-corp SLACK_BOT_TOKEN)" = "fake-keychain-token" ] && [ ! -s "$ICALLS" ]'
check "lookup asks service capsule-corp, account = key, -w last" \
  'grep -qx "find-generic-password -s capsule-corp -a SLACK_BOT_TOKEN -w" "$CALLS"'
check "the value never reached security argv" '! grep -q "fake-" "$CALLS"'
check "SLACK_APP_TOKEN is allow-listed" \
  'printf x >"$T/kc.SLACK_APP_TOKEN"; [ "$("$CS" get capsule-corp SLACK_APP_TOKEN)" = x ]'

# identity still fail-closed
out="$(env -u CAPSULE_IDENTITY "$CS" get capsule-corp SLACK_BOT_TOKEN 2>&1)"; rc=$?
check "no identity is refused even with a keychain hit" '[ $rc -eq 3 ] && grep -q "set CAPSULE_IDENTITY" <<<"$out" && ! grep -q "fake-" <<<"$out"'
out="$(CAPSULE_IDENTITY=nope "$CS" get capsule-corp SLACK_BOT_TOKEN 2>&1)"; rc=$?
check "unknown identity is refused" '[ $rc -eq 3 ]'

# wrong project: no keychain lookup
: >"$CALLS"; : >"$ICALLS"
out="$("$CS" get other SLACK_BOT_TOKEN)"
check "wrong project goes to Infisical, no keychain lookup" \
  '[ "$out" = fake-infisical-token ] && [ ! -s "$CALLS" ] && grep -q login "$ICALLS"'

# miss -> Infisical
: >"$CALLS"; : >"$ICALLS"; unlink "$T/kc.SLACK_BOT_TOKEN"
check "keychain miss falls back to Infisical" '[ "$("$CS" get capsule-corp SLACK_BOT_TOKEN)" = fake-infisical-token ] && grep -q login "$ICALLS"'
: >"$T/kc.SLACK_BOT_TOKEN"; : >"$ICALLS"
check "empty keychain value falls back" '[ "$("$CS" get capsule-corp SLACK_BOT_TOKEN)" = fake-infisical-token ]'

# both missing: clear error, no leak
cat >"$T/bin/infisical" <<EOF
#!/usr/bin/env bash
case "\$1" in login) echo tok;; *) echo "cannot read" >&2; exit 1;; esac
EOF
unlink "$T/kc.SLACK_BOT_TOKEN"
out="$("$CS" get capsule-corp SLACK_BOT_TOKEN 2>&1)"; rc=$?
check "both missing: nonzero, no value in output" '[ $rc -ne 0 ] && ! grep -q "fake-" <<<"$out"'

# non-allow-listed key never consults keychain
: >"$CALLS"
"$CS" get capsule-corp OTHER_KEY >/dev/null 2>&1
check "non-allow-listed key never calls security" '[ ! -s "$CALLS" ]'

# timeout = miss
printf 'fake-keychain-token\n' >"$T/kc.SLACK_BOT_TOKEN"; : >"$T/kc.sleep"
cat >"$T/bin/infisical" <<EOF
#!/usr/bin/env bash
case "\$1" in login) echo tok;; secrets) echo fake-infisical-token;; esac
EOF
out="$(CAPSULE_KEYCHAIN_WAIT_S=1 "$CS" get capsule-corp SLACK_BOT_TOKEN 2>&1)"
check "a blocked security call times out and falls back" '[ "$out" = fake-infisical-token ]'
unlink "$T/kc.sleep"

# file and env backends never consult keychain
: >"$CALLS"
printf 'SLACK_BOT_TOKEN=fake-file-token\n' >"$CAPSULE_HOME/secrets.env"; chmod 600 "$CAPSULE_HOME/secrets.env"
check "file backend ignores the keychain" \
  '[ "$(CAPSULE_SECRETS_BACKEND=file "$CS" get capsule-corp SLACK_BOT_TOKEN)" = fake-file-token ] && [ ! -s "$CALLS" ]'
check "env backend ignores the keychain" \
  '[ "$(CAPSULE_SECRETS_BACKEND=env SLACK_BOT_TOKEN=fake-env-token "$CS" get capsule-corp SLACK_BOT_TOKEN)" = fake-env-token ] && [ ! -s "$CALLS" ]'

# keychain-check: presence only
out="$("$CS" keychain-check SLACK_BOT_TOKEN 2>&1)"; rc=$?
check "keychain-check ok exits 0, no value" '[ $rc -eq 0 ] && ! grep -q "fake-" <<<"$out"'
unlink "$T/kc.SLACK_BOT_TOKEN"
out="$("$CS" keychain-check SLACK_BOT_TOKEN 2>&1)"; rc=$?
check "keychain-check missing exits 3" '[ $rc -eq 3 ] && grep -q "not in keychain: SLACK_BOT_TOKEN" <<<"$out"'
: >"$CALLS"
"$CS" keychain-check OTHER_KEY >/dev/null 2>&1; rc=$?
check "keychain-check refuses a non-allow-listed key without calling security" '[ $rc -eq 3 ] && [ ! -s "$CALLS" ]'
out="$("$CS" keychain-check 'A B' 2>&1)"; rc=$?
check "keychain-check validates the key name" '[ $rc -eq 3 ] && grep -q "bad secret name" <<<"$out"'
SECURITY_BIN="$T/bin/none" "$CS" keychain-check SLACK_BOT_TOKEN >/dev/null 2>&1; rc=$?
check "keychain-check without a security binary exits 4" '[ $rc -eq 4 ]'

echo; if [ $fails -eq 0 ]; then echo "test_capsule_secret_keychain: all passed"; else echo "test_capsule_secret_keychain: $fails failed"; exit 1; fi
