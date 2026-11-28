#!/usr/bin/env bash
# bin/capsule-secret run (infisical backend): the child sees the injected secrets only,
# never the machine-identity credentials the helper used to log in. Fake infisical on
# PATH, fake values, temp config dir; no network and no real config is touched.
set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT

fails=0
check() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; fails=$((fails+1)); fi; }

FAKEBIN="$T/fakebin"; mkdir -p "$FAKEBIN"
cat >"$FAKEBIN/infisical" <<'FAKE'
#!/usr/bin/env bash
case "$1" in
  login) echo "fake-token" ;;
  run)
    shift; while [ "${1:-}" != "--" ]; do [ $# -gt 0 ] || exit 1; shift; done; shift
    INJECTED_SECRET=fake-injected "$@" ;;
  *) exit 1 ;;
esac
FAKE
chmod +x "$FAKEBIN/infisical"
export PATH="$FAKEBIN:$PATH"

CONF="$T/config"; mkdir -p "$CONF/identities"
echo "PROJECT_DEMO=proj-fake" >"$CONF/projects.env"
# The identity file exports its variables (the shape that leaks them to the child).
cat >"$CONF/identities/demo.env" <<'ID'
export INFISICAL_CLIENT_ID=fake-client-id
export INFISICAL_CLIENT_SECRET=fake-client-secret
ID

export HOME="$T/home"; mkdir -p "$HOME"
unset INFISICAL_BIN INFISICAL_API_URL
export CAPSULE_CONFIG_DIR="$CONF" CAPSULE_HOME="$T/home" CAPSULE_SECRETS_BACKEND=infisical
export CAPSULE_IDENTITY=demo CAPSULE_SECRET_WAIT_S=0 CAPSULE_SECRET_WAIT_STEP_S=0
CS="$REPO/bin/capsule-secret"

# The caller may also have the login variables in its own environment.
out="$(INFISICAL_UNIVERSAL_AUTH_CLIENT_ID=caller-id INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET=caller-secret \
  "$CS" run demo -- env)"
check "run: the injected secret reaches the child" 'grep -q "^INJECTED_SECRET=fake-injected$" <<<"$out"'
check "run: INFISICAL_CLIENT_ID is absent from the child" '! grep -q "^INFISICAL_CLIENT_ID=" <<<"$out"'
check "run: INFISICAL_CLIENT_SECRET is absent from the child" '! grep -q "^INFISICAL_CLIENT_SECRET=" <<<"$out"'
check "run: INFISICAL_UNIVERSAL_AUTH_* are absent from the child" '! grep -q "^INFISICAL_UNIVERSAL_AUTH_" <<<"$out"'
check "run: INFISICAL_TOKEN is absent from the child" '! grep -q "^INFISICAL_TOKEN=" <<<"$out"'
check "run: no fake credential value appears anywhere in the child env" \
  '! grep -q "fake-client\|caller-id\|caller-secret\|fake-token" <<<"$out"'

echo; if [ $fails -eq 0 ]; then echo "test_capsule_secret_env_scrub: all passed"; else echo "test_capsule_secret_env_scrub: $fails failed"; exit 1; fi
