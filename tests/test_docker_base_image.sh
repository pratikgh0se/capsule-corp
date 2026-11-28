#!/usr/bin/env bash
# S10a (spec "Worker images"): the slim capsule-base layer and the capsule-python stack chained from it. Static checks always run; the
# image checks are opt-in (RUN_DOCKER_TESTS=1) and inspect the image recorded in
# docker/IMAGES.lock, or build it first when it is not present locally.
# Usage: tests/test_docker_base_image.sh [image-ref]
set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DOCKERFILE="$REPO/docker/base/Dockerfile"
LOCK="${IMAGES_LOCK:-$REPO/docker/IMAGES.lock}"
fails=0
check() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; fails=$((fails+1)); fi; }

# Static, no docker.
check "base FROM is pinned by a 64-hex digest" \
  'grep -qE "^FROM [^ @]+@sha256:[0-9a-f]{64}$" "$DOCKERFILE"'
check "base FROM is not a floating tag (no :latest)" \
  '! grep -E "^FROM " "$DOCKERFILE" | grep -q ":latest"'
check "base Dockerfile has exactly one FROM" \
  '[ "$(grep -c "^FROM " "$DOCKERFILE")" -eq 1 ]'
check "apt installs come from a dated snapshot" \
  'grep -qE "snapshot\.debian\.org/archive/debian/%s/" "$DOCKERFILE" && grep -qE "^ARG SNAPSHOT=[0-9]{8}T[0-9]{6}Z$" "$DOCKERFILE"'
check "apt lists are removed in the install layer" \
  'grep -q "rm -rf /var/lib/apt/lists/\*" "$DOCKERFILE"'
check "base Dockerfile runs no pip/npm/curl-pipe installs" \
  '! grep -vE "^[[:space:]]*#" "$DOCKERFILE" | grep -qE "pip install|npm (i|install|ci)|curl[^|]*\|[[:space:]]*(ba)?sh"'
check "base drops to a non-root USER" \
  'grep -qE "^USER [a-z]" "$DOCKERFILE" && ! grep -qE "^USER root" "$DOCKERFILE"'
check "IMAGES.lock has exactly one capsule-base digest row" \
  '[ "$(grep -cE "^capsule-base@sha256:[0-9a-f]{64}$" "$LOCK")" -eq 1 ]'
check "IMAGES.lock has no tag-only rows" \
  '! grep -vE "^[[:space:]]*(#|$)" "$LOCK" | grep -qvE "^[a-z0-9._/-]+@sha256:[0-9a-f]{64}$"'
PYDF="$REPO/docker/stacks/python/Dockerfile"
check "every FROM in docker/base and docker/stacks is pinned by a 64-hex digest" \
  '! grep -hE "^FROM " "$DOCKERFILE" "$PYDF" | grep -qvE "^FROM [^ @]+@sha256:[0-9a-f]{64}$"'
check "python stack FROM is the capsule-base digest in the lock (chained by digest)" \
  '[ "$(grep -E "^FROM " "$PYDF" | sed "s/^FROM //")" = "$(grep -E "^capsule-base@sha256:" "$LOCK")" ]'
check "IMAGES.lock has exactly one capsule-python digest row" \
  '[ "$(grep -cE "^capsule-python@sha256:[0-9a-f]{64}$" "$LOCK")" -eq 1 ]'
check "the retired all-in-one image files are gone" \
  '[ ! -e "$REPO/docker/worker/Dockerfile" ] && [ ! -e "$REPO/docker/WORKER_IMAGE" ]'
# build-base.sh with a fake docker (AGENT_OPS_DOCKER_BIN is a folder holding `docker`; no PATH
# shim), a temp CAPSULE_HOME and a temp copy of the repo's docker folder; the real docker, home
# and images are never reached.
BT="$(mktemp -d)"; trap 'rm -rf "$BT"' EXIT
FD="$BT/fakebin"; BR="$BT/repo"; BH="$BT/home"
mkdir -p "$FD" "$BR/docker/base" "$BR/docker/stacks/python" "$BH"
cp "$REPO/docker/build-base.sh" "$BR/docker/"; cp "$REPO/docker/base/Dockerfile" "$BR/docker/base/"
cp "$REPO/docker/stacks/python/Dockerfile" "$BR/docker/stacks/python/"
cat >"$FD/docker" <<'FAKE'
#!/bin/sh
[ "$1 $2" = "image inspect" ] || exit 0
for a; do last="$a"; done
case "$*" in
  *'{{.Size}}'*) echo 123 ;;
  *) case "$last" in
       capsule-base:local) printf 'sha256:%064d\n' 1 ;;
       capsule-base@*) case "$*" in   # a local digest ref answers with its own id (and its tag)
         *'{{join'*) printf '%s capsule-base:local\n' "${last#*@}" ;;
         *) printf '%s\n' "${last#*@}" ;;
       esac ;;
       *) printf 'sha256:%064d\n' 2 ;;
     esac ;;
esac
FAKE
chmod +x "$FD/docker"
bb() {
  env -u AGENT_OPS_NO_SEND HOME="$BT" CAPSULE_HOME="$BH" AGENT_OPS_DOCKER_BIN="$FD" "$@" \
    bash "$BR/docker/build-base.sh" "${BB_ARG:-base}"
}
seed() { printf 'capsule-base@sha256:%064d\n' 3 >"$BH/images.lock"; }
has_python() { grep -q "^capsule-python@" "$BH/images.lock"; }
seed

# One floor, one file: doctor and build-base.sh both read docker/BUILD_MIN_FREE_BYTES and print
# the same GB figure.
FLOOR="$(tr -dc 0-9 <"$REPO/docker/BUILD_MIN_FREE_BYTES" 2>/dev/null)"
check "docker/BUILD_MIN_FREE_BYTES holds one whole number of bytes" '[ -n "$FLOOR" ]'
DOCTOR_FLOOR='from capsule import doctor; print(doctor.BUILD_FREE_BYTES)'
check "doctor's build floor is that file's figure" \
  '[ "$(cd "$REPO" && python3 -B -c "$DOCTOR_FLOOR")" = "$FLOOR" ]'
# floor_case FORMAT: the floor file holds printf FORMAT; run build-base.sh (rc, err) and ask
# docker.build_floor() (py) to read the same file: the two must agree on every input.
PY_FLOOR='import sys, pathlib; from capsule.runners import docker
docker.BUILD_FLOOR_FILE = pathlib.Path(sys.argv[1]); print(docker.build_floor())'
floor_case() {
  printf -- "$1" >"$BR/docker/BUILD_MIN_FREE_BYTES"
  err="$(bb 2>&1 >/dev/null)"; rc=$?
  py="$(cd "$REPO" && python3 -B -c "$PY_FLOOR" "$BR/docker/BUILD_MIN_FREE_BYTES")"
}
# A well-formed floor above the free disk: refused for lack of disk, GB figure from the file.
for fmt in '999999999999999\n' '  999999999999999 \n' '\n 999999999999999\n\n'; do
  floor_case "$fmt"
  check "floor '$fmt' is read as 999999999999999: refused (exit 2) for need 999999 GB" \
    '[ $rc -eq 2 ] && grep -q "need 999999 GB; not building" <<<"$err" &&
     [ "$py" = 999999999999999 ]'
done
check "a refusal leaves the record alone" '[ "$(wc -l <"$BH/images.lock" | tr -d " ")" = 1 ]'
# A floor file that is not 1 to 15 plain digits stops the build (exit 2) naming the file, before
# any build: empty, blank, exponent, leading zero, inner space, signs, underscores, CRLF, tab, 16
# and 20 digits (a 20-digit number overflows the shell's arithmetic), a second line, zero.
for fmt in '' '   \n' '4e10\n' '040000000000\n' '40 000\n' '-5\n' '+4\n' '40_000_000_000\n' \
           '40000000000\r\n' '40000000000\t\n' '1000000000000000\n' '12345678901234567890\n' \
           '40000000000\nx\n' '0\n'; do
  floor_case "$fmt"
  check "floor '$fmt' refuses with exit 2 naming the file, and docker.build_floor() says None" \
    '[ $rc -eq 2 ] && grep -q "BUILD_MIN_FREE_BYTES must hold" <<<"$err" && [ "$py" = None ]'
done
rm -f "$BR/docker/BUILD_MIN_FREE_BYTES"
err="$(bb 2>&1 >/dev/null)"; rc=$?
check "a missing floor file refuses with exit 2 naming the file" \
  '[ $rc -eq 2 ] && grep -q "BUILD_MIN_FREE_BYTES must hold" <<<"$err"'
check "no refusal touched the record" '[ "$(wc -l <"$BH/images.lock" | tr -d " ")" = 1 ]'
echo 1 >"$BR/docker/BUILD_MIN_FREE_BYTES"

# The record: a finished build rewrites its own row under a lock; two builds finishing together
# keep both rows.
bb >/dev/null 2>&1; rc=$?
check "a build records its row (exit 0)" \
  '[ $rc -eq 0 ] && grep -q "^capsule-base@sha256:0*1$" "$BH/images.lock"'
check "no mkdir lock or temp file is left behind after a build" \
  '! ls "$BH" | grep -qE "lockdir|tmp"'
both_rows() {
  [ "$(grep -cE "^capsule-(base|python)@sha256:" "$BH/images.lock")" = 2 ] && has_python
}
# up to 30 s: the host may be loaded
wait_for() { local i; for i in $(seq 1 150); do "$@" && return 0; sleep 0.2; done; return 1; }
lock_modes="NO_FLOCK=1"   # the mkdir lock, always available
command -v flock >/dev/null 2>&1 && lock_modes="$lock_modes NO_FLOCK="   # macOS has none by default
for mode in $lock_modes; do
  for round in 1 2 3; do
    seed
    (BB_ARG=base bb BUILD_BASE_$mode >/dev/null 2>&1 &)
    BB_ARG=python bb BUILD_BASE_$mode >/dev/null 2>&1; wait_for both_rows
    check "BUILD_BASE_$mode: two builds finishing together keep both rows (round $round)" \
      'both_rows'
  done
done
# A held lock makes the second writer wait, and it writes (and drops its lock) once released.
# mkdir: the lock is a folder; never taken over, even when it holds a dead pid.
lock_done() { has_python && [ ! -e "$BH/images.lock.lockdir" ]; }
seed
sh -c 'exit 0' & dead=$!; wait "$dead"
mkdir "$BH/images.lock.lockdir"; echo "$dead" >"$BH/images.lock.lockdir/pid"
(BB_ARG=python bb BUILD_BASE_NO_FLOCK=1 >/dev/null 2>&1 &); sleep 2
check "mkdir lock: a held folder (even with a dead pid in it) keeps the record unwritten" \
  '! has_python'
rm -f "$BH/images.lock.lockdir/pid"; rmdir "$BH/images.lock.lockdir"
wait_for lock_done
check "mkdir lock: it writes once the folder is gone, and removes its own" 'lock_done'
if command -v flock >/dev/null 2>&1; then
  seed
  (flock "$BH/images.lock.lock" sleep 6 &); sleep 0.5
  (BB_ARG=python bb >/dev/null 2>&1 &); sleep 2
  check "flock: a held flock keeps the record unwritten" '! has_python'
  wait_for has_python
  check "flock: it writes once the flock is released" 'has_python'
fi

# Keep-tag (inc_3a7d6457): a rebuild must not leave the previous NAME:local image untagged.
# A second fake logs every call; its :local exists before the first `build` only when asked.
F2="$BT/fake2"; mkdir -p "$F2"
cat >"$F2/docker" <<'FAKE'
#!/bin/sh
d="$(dirname "$0")"
echo "$*" >>"$d/calls"
[ "$1" != build ] || : >"$d/built"
[ "$1" != buildx ] || exit 1
[ "$1" != tag ] || [ ! -e "$d/tag_fails" ] || exit 1
[ "$1 $2" != "image ls" ] || [ ! -e "$d/ls_fails" ] || exit 1
[ "$1 $2" != "image ls" ] || { [ ! -e "$d/has_local" ] || printf 'sha256:abcdef012345%052d\n' 0; exit 0; }
[ "$1 $2" = "image inspect" ] || exit 0
case "$*" in *'{{.Size}}'*) echo 123; exit 0 ;; esac
printf 'sha256:%064d\n' 1
FAKE
chmod +x "$F2/docker"
bb2() {
  rm -f "$F2/calls" "$F2/built"; seed
  env -u AGENT_OPS_NO_SEND HOME="$BT" CAPSULE_HOME="$BH" AGENT_OPS_DOCKER_BIN="$F2" \
    bash "$BR/docker/build-base.sh" base >/dev/null 2>&1
}
bb2_err() {
  env -u AGENT_OPS_NO_SEND HOME="$BT" CAPSULE_HOME="$BH" AGENT_OPS_DOCKER_BIN="$F2" \
    bash "$BR/docker/build-base.sh" base 2>&1 >/dev/null
}
OLDID="sha256:abcdef012345$(printf '%052d' 0)"
tag_line() { grep -n "^tag $OLDID capsule-base:prev-abcdef012345\$" "$F2/calls" | cut -d: -f1; }
build_line() { grep -n "^build " "$F2/calls" | head -n 1 | cut -d: -f1; }
: >"$F2/has_local"; bb2; rc=$?
check "keep-tag: an existing :local is tagged prev-<12 hex> before the build runs" \
  '[ $rc -eq 0 ] && [ -n "$(tag_line)" ] && [ "$(tag_line)" -lt "$(build_line)" ]'
bb2
check "keep-tag: running again tags the same name, still exit 0" \
  '[ "$(grep -c "capsule-base:prev-abcdef012345\$" "$F2/calls")" = 1 ] && [ -n "$(build_line)" ]'
rm -f "$F2/has_local"; bb2; rc=$?
check "keep-tag: no :local yet means no tag call and the build still runs" \
  '[ $rc -eq 0 ] && ! grep -q "^tag " "$F2/calls" && [ -n "$(build_line)" ]'

: >"$F2/has_local"; : >"$F2/tag_fails"; bb2; rc=$?
check "keep-tag: a failing docker tag stops with exit 1 and runs no build" \
  '[ $rc -eq 1 ] && [ -z "$(build_line)" ] && bb2_err | grep -q "not building"'
rm -f "$F2/tag_fails"; : >"$F2/ls_fails"; bb2; rc=$?
check "keep-tag: a failing :local lookup stops with exit 1 and runs no build" \
  '[ $rc -eq 1 ] && [ -z "$(build_line)" ] && ! grep -q "^tag " "$F2/calls"'
rm -f "$F2/ls_fails"

[ "$fails" -eq 0 ] || { echo "test_docker_base_image: static check failed"; exit 1; }

. "$REPO/tests/lib_docker_optin.sh"
docker_optin test_docker_base_image || exit 77
docker_ready test_docker_base_image || exit 1

IMG="${1:-$(grep -E '^capsule-base@sha256:' "$LOCK" | head -n 1)}"
if ! docker image inspect "$IMG" >/dev/null 2>&1; then
  IMAGES_LOCK="$LOCK" "$REPO/docker/build-base.sh" >/dev/null || { echo "FAIL build"; exit 1; }
  IMG="$(grep -E '^capsule-base@sha256:' "$LOCK" | head -n 1)"
fi
run() { docker run --rm --label capsule=1 --label capsule.kind=test --network none --entrypoint sh "$IMG" -c "$1" 2>&1; }

out="$(run 'git --version && sqlite3 --version && jq --version && curl --version | head -n 1 && ps --version && pkill --version')"
check "git, sqlite3, jq, curl, ps and pkill are present" \
  'grep -q "^git version" <<<"$out" && grep -q "^jq-" <<<"$out" && grep -q "^curl " <<<"$out" && grep -q procps <<<"$out"'

out="$(run 'id -u')"
check "default user is non-root" '[ -n "$out" ] && [ "$out" != 0 ]'

out="$(run 'command -v pip pip3 python3 npm node; echo done')"
check "no pip, python or npm at runtime (language toolchains belong to stack layers)" '[ "$out" = "done" ]'

out="$(run 'ls /var/lib/apt/lists | wc -l' | tr -d ' ')"
check "no apt package lists left in the image (nothing to install from)" '[ "$out" = 0 ]'

out="$(run 'apt-get install -y --no-install-recommends cowsay >/dev/null 2>&1; echo rc=$?')"
check "apt-get install fails for the default user with no network" '[ "$out" != "rc=0" ]'

out="$(run 'echo "$TMPDIR"; test -x /usr/local/bin/worker-entrypoint && test -f /usr/local/bin/ui_guard.py && echo files')"
check "TMPDIR, entrypoint and ui_guard.py are in place" '[ "$out" = "/workspace/.worker-exec
files" ]'

out="$(docker image inspect --format '{{index .Config.Labels "capsule-layer"}}' "$IMG" 2>&1)"
check "image carries the capsule-layer=base label (GC key)" '[ "$out" = base ]'

kb="$(run 'du -sxk / 2>/dev/null' | awk 'NR==1{print $1}')"
check "base is within the 300 MB budget (${kb:-?} KB on disk)" '[ -n "$kb" ] && [ "$kb" -le 307200 ]'

PY="$(grep -E '^capsule-python@sha256:' "$LOCK" | head -n 1)"
if docker image inspect "$PY" >/dev/null 2>&1 || IMAGES_LOCK="$LOCK" "$REPO/docker/build-base.sh" python >/dev/null; then
  PY="$(grep -E '^capsule-python@sha256:' "$LOCK" | head -n 1)"
  out="$(docker image inspect --format '{{.Id}}' "$PY" 2>&1)"
  check "python image id equals the lock digest (second oracle)" '[ "capsule-python@$out" = "$PY" ]'
  out="$(docker run --rm --label capsule=1 --label capsule.kind=test --network none "$PY" sh -c 'python3 -c "import coverage, diff_cover, hypothesis, pytest, ui_guard; print(1)" && ruff --version && mutmut --version && id -u' 2>&1 | tr '\n' ' ')"
  check "python stack has the tooling and runs non-root" 'grep -q "^1 ruff " <<<"$out" && ! grep -q " 0 $" <<<"$out"'
  # The runner's shape (run_args flags, entrypoint kept, `exec sh -lc`): a login shell resets PATH, so
  # the venv must survive it; the `sh -c` check above cannot see that. CAPSULE_PYTHON_IMAGE aims this
  # one check at a locally built candidate before it is locked. mutmut is only located: its
  # --version loads project config and fails in the empty /workspace.
  LOGIN_IMG="${CAPSULE_PYTHON_IMAGE:-$PY}"
  cname="capsule-loginpath-probe-$$"
  docker run -d --init --label capsule=1 --label capsule.kind=test --network none --user "$(id -u):$(id -g)" -e HOME=/tmp --name "$cname" \
    "$LOGIN_IMG" sleep infinity >/dev/null 2>&1
  out="$(docker exec -w /workspace "$cname" sh -lc 'python3 -c "import pytest, hypothesis, coverage; print(1)" && ruff --version && command -v mutmut' 2>&1 | tr '\n' ' ')"
  { docker stop "$cname" && docker rm "$cname"; } >/dev/null 2>&1 || true
  check "python tooling is on PATH in the runner's login shell, exec sh -lc ($LOGIN_IMG)" \
    'grep -q "^1 ruff " <<<"$out" && ! grep -qiE "error|not found" <<<"$out"'
  size="$(docker image inspect --format '{{.Size}}' "$PY")"
  check "python stack is within the 1.5 GB budget ($size bytes)" '[ "$size" -le 1500000000 ]'
else
  echo "FAIL python stack build"; fails=$((fails+1))
fi

[ $fails -eq 0 ] && echo "test_docker_base_image: all passed" || { echo "test_docker_base_image: $fails failed"; exit 1; }
