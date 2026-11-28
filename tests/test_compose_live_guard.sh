#!/usr/bin/env bash
# compose_live_guard (maintenance/lib_compose.sh, inc_02b0bd3a): the live `capsule-corp`
# compose project must never be driven with a non-live OBS_ENV (it decides which data dirs
# compose mounts). A test that ran deploy under a temp HOME with real docker recreated all
# 8 live containers on empty data dirs, twice. Every case here uses a FAKE docker that
# records its argv; real docker is never called.
set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
fails=0; check() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; fails=$((fails+1)); fi; }
FAKEBIN="$T/bin"; mkdir -p "$FAKEBIN" "$T/home" "$T/other"; ARGV="$T/argv.log"
REAL="$T/realhome"; REALDIR="$REAL/.config/capsule-corp"; mkdir -p "$REALDIR"
LIVE_OBS="$REALDIR/observability.env"; TEMP_OBS="$T/other/observability.env"
cat >"$FAKEBIN/docker" <<EOF
#!/usr/bin/env bash
echo "docker \$*" >>"$ARGV"
case "\$*" in *"config --services"*) printf 'grafana\n' ;; esac
exit 0
EOF
cat >"$FAKEBIN/capsule-secret" <<EOF
#!/usr/bin/env bash
shift 2; [ "\$1" = "--" ] && shift; "\$@"
EOF
chmod +x "$FAKEBIN"/*
: >"$LIVE_OBS"; : >"$TEMP_OBS"; : >"$T/dc.yml"

# run_lib - $FN [$ARG] from lib_compose.sh under the caller's env overrides, in a scrubbed shell.
run_lib() {
  env -u CAPSULE_JOBS_DIR -u AGENT_OPS_HOME -u COMPOSE_PROJECT_NAME -u CAPSULE_TEST_COMPOSE_REAL_HOME -u AGENT_OPS_NO_SEND "$@" \
    bash -c ". '$REPO/maintenance/lib_compose.sh'; \"\$0\" \"\$@\"" "$FN" ${ARG:+"$ARG"}
}
LIBENV=(COMPOSE_FILE="$T/dc.yml" DOCKER_BIN="$FAKEBIN/docker" CAPSULE_SECRET_BIN="$FAKEBIN/capsule-secret" COMPOSE_PROFILES=)
SEAM=(AGENT_OPS_NO_SEND=1 CAPSULE_TEST_COMPOSE_REAL_HOME="$REAL")
MSG="FAILED compose: refusing to run the live capsule-corp stack with a non-live OBS_ENV"
UPRE="^docker compose .* up -d"

for FN in compose_up_infisical compose_up_rest compose_recreate; do
  ARG=""; [ "$FN" = compose_recreate ] && ARG=grafana
  : >"$ARGV"; out="$(run_lib "${LIBENV[@]}" "${SEAM[@]}" HOME="$REAL" OBS_ENV="$TEMP_OBS" 2>&1)"; rc=$?
  check "$FN: temp OBS_ENV with the real HOME refuses" '[ $rc -ne 0 ] && grep -qF "$MSG ($TEMP_OBS)" <<<"$out" && [ ! -s "$ARGV" ]'
  : >"$ARGV"; out="$(run_lib "${LIBENV[@]}" "${SEAM[@]}" HOME="$REAL" OBS_ENV="$T/nodir/observability.env" 2>&1)"; rc=$?
  check "$FN: an OBS_ENV whose directory is missing refuses" '[ $rc -ne 0 ] && [ ! -s "$ARGV" ]'
  : >"$ARGV"; out="$(run_lib "${LIBENV[@]}" "${SEAM[@]}" HOME="$REAL" OBS_ENV="$TEMP_OBS" COMPOSE_PROJECT_NAME=capsule-corp 2>&1)"; rc=$?
  check "$FN: explicit live project name with a temp OBS_ENV refuses" '[ $rc -ne 0 ] && [ ! -s "$ARGV" ]'
  : >"$ARGV"; out="$(run_lib "${LIBENV[@]}" "${SEAM[@]}" HOME="$T/home" OBS_ENV="$LIVE_OBS" 2>&1)"; rc=$?
  check "$FN: the live OBS_ENV runs compose (HOME is irrelevant)" '[ $rc -eq 0 ] && grep -q "$UPRE" "$ARGV"'
  ln -sfn "$REALDIR" "$T/obslink"
  : >"$ARGV"; out="$(run_lib "${LIBENV[@]}" "${SEAM[@]}" HOME="$T/home" OBS_ENV="$T/obslink/observability.env" 2>&1)"; rc=$?
  check "$FN: a symlinked spelling of the live OBS_ENV dir runs compose" '[ $rc -eq 0 ] && grep -q "$UPRE" "$ARGV"'
  : >"$ARGV"; out="$(run_lib "${LIBENV[@]}" HOME="$REAL" OBS_ENV="$LIVE_OBS" 2>&1)"; rc=$?
  check "$FN: the seam is ignored without AGENT_OPS_NO_SEND (real passwd home differs, refuses)" '[ $rc -ne 0 ] && [ ! -s "$ARGV" ]'
  : >"$ARGV"; out="$(run_lib "${LIBENV[@]}" HOME="$T/home" OBS_ENV="$TEMP_OBS" COMPOSE_PROJECT_NAME=capsule-corp-test 2>&1)"; rc=$?
  check "$FN: fake-docker bypass: other project name runs compose from a temp OBS_ENV" '[ $rc -eq 0 ] && grep -q "$UPRE" "$ARGV"'
  : >"$ARGV"; out="$(run_lib "${LIBENV[@]}" DOCKER_BIN=/usr/bin/true HOME="$T/home" OBS_ENV="$TEMP_OBS" COMPOSE_PROJECT_NAME=capsule-corp-test 2>&1)"; rc=$?
  check "$FN: bypass is refused when docker is not a temp-root fake" '[ $rc -ne 0 ] && grep -qF "$MSG" <<<"$out"'
done

# Bypass hardening. A symlink to "real docker" (a temp file stands in for it; its calls would
# land in $ARGV) placed in a temp dir is not a fake.
mkdir -p "$T/linkbin"; cp "$FAKEBIN/docker" "$T/standin-docker"; ln -sfn "$T/standin-docker" "$T/linkbin/docker"
for FN in compose_up_infisical compose_up_rest compose_recreate; do
  ARG=""; [ "$FN" = compose_recreate ] && ARG=grafana
  : >"$ARGV"; out="$(run_lib "${LIBENV[@]}" DOCKER_BIN="$T/linkbin/docker" HOME="$T/home" OBS_ENV="$TEMP_OBS" COMPOSE_PROJECT_NAME=capsule-corp-test 2>&1)"; rc=$?
  check "$FN: a symlinked docker never counts as a fake: refused, docker log empty" '[ $rc -ne 0 ] && grep -qF "$MSG ($TEMP_OBS)" <<<"$out" && [ ! -s "$ARGV" ]'
done
# A temp root that is the passwd home, or inside it, never counts (Docker Desktop's ~/.docker/bin/docker).
# Skipped where the scratch dir itself sits under /tmp, because /tmp is the other accepted temp root.
case "$T" in
  /tmp/*|/private/tmp/*) echo "SKIP temp root inside the passwd home (scratch dir is under /tmp)" ;;
  *)
    mkdir -p "$REAL/tmp"; cp "$FAKEBIN/docker" "$REAL/tmp/docker"; cp "$FAKEBIN/docker" "$REAL/docker"
    FN=compose_up_rest ARG=""
    : >"$ARGV"; out="$(run_lib "${LIBENV[@]}" "${SEAM[@]}" TMPDIR="$REAL/tmp" DOCKER_BIN="$REAL/tmp/docker" HOME="$T/home" OBS_ENV="$TEMP_OBS" COMPOSE_PROJECT_NAME=capsule-corp-test 2>&1)"; rc=$?
    check "TMPDIR inside the passwd home: the bypass is refused" '[ $rc -ne 0 ] && grep -qF "$MSG" <<<"$out" && [ ! -s "$ARGV" ]'
    : >"$ARGV"; out="$(run_lib "${LIBENV[@]}" "${SEAM[@]}" TMPDIR="$REAL" DOCKER_BIN="$REAL/docker" HOME="$T/home" OBS_ENV="$TEMP_OBS" COMPOSE_PROJECT_NAME=capsule-corp-test 2>&1)"; rc=$?
    check "TMPDIR equal to the passwd home: the bypass is refused" '[ $rc -ne 0 ] && grep -qF "$MSG" <<<"$out" && [ ! -s "$ARGV" ]'
    ;;
esac

# Read-only calls stay open (compose_services is `config --services`).
: >"$ARGV"; out="$(FN=compose_services ARG="" run_lib "${LIBENV[@]}" HOME="$T/home" OBS_ENV="$TEMP_OBS" 2>&1)"; rc=$?
check "compose_services (read-only config) is not guarded" '[ $rc -eq 0 ] && [ "$out" = grafana ] && grep -q "config --services" "$ARGV"'

# The real-home lookup comes from the OS, never from $HOME.
real="$(FN=compose_real_home ARG="" run_lib "${LIBENV[@]}" HOME="$T/home")"
check "compose_real_home ignores a HOME override" '[ -n "$real" ] && [ "$real" != "$T/home" ]'
check "compose_real_home is the passwd home" '[ "$real" = "$(eval echo "~$(id -un)")" ]'

# Lookup failure: an unknown user has no passwd home, so even the live-looking OBS_ENV refuses.
mkdir -p "$T/idbin"; printf '#!/bin/sh\necho nosuchuser-zz9\n' >"$T/idbin/id"; chmod +x "$T/idbin/id"
real="$(FN=compose_real_home ARG="" run_lib "${LIBENV[@]}" PATH="$T/idbin:$PATH" HOME="$T/home")"
check "an unknown user has no real home (empty answer, not ~user)" '[ -z "$real" ]'
: >"$ARGV"; out="$(FN=compose_up_rest ARG="" run_lib "${LIBENV[@]}" PATH="$T/idbin:$PATH" HOME="$T/home" OBS_ENV="$LIVE_OBS" 2>&1)"; rc=$?
check "a failed real-home lookup refuses (fail closed)" '[ $rc -ne 0 ] && grep -qF "$MSG" <<<"$out" && [ ! -s "$ARGV" ]'

echo "---"; [ "$fails" -eq 0 ] && echo "all passed" || { echo "$fails failed"; exit 1; }
