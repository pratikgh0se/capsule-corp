#!/usr/bin/env bash
# Build the capsule-base layer (or, with the argument `python`, the capsule-python stack
# layer chained from the base row of the record) locally and record its digest in the image
# record, $CAPSULE_HOME/images.lock (spec "Worker images", slice S10a). The record is a file
# outside every git clone: the tick (it runs from the harness clone) and `capsule check` read
# it there for a board made by `capsule board new`. Run by the controller on the host; workers
# never run docker. Does not push.
#
# A tester's route, once, in this order (about 40 GB of disk, hours of build time):
#   docker/build-base.sh base && docker/build-base.sh python
#
#   docker/build-base.sh            build, then rewrite the capsule-base row of the record
#   docker/build-base.sh python     same for the capsule-python row (FROM is the record's base row)
#   docker/build-base.sh web        same for the capsule-web row (Node, Chromium, Playwright)
#   IMAGES_LOCK=docker/IMAGES.lock docker/build-base.sh   write the maintainer's tracked lock
#                                    instead (the file the repo commits; the record is not touched)
#   DOCKER_CONFIG=... docker/build-base.sh   (the pull of the pinned FROM may need a
#                                    config dir without the broken credential helper)
#
# Refuses to build when free disk is under docker/BUILD_MIN_FREE_BYTES (40 GB: the 25 GB floor
# plus build headroom).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOME="${HOME:-}"   # set -u: IMAGES_LOCK alone names the lock, so a missing HOME is no error here
# CAPSULE_HOME rule (row 609; the same block is in every script that defaults it, = capsule/paths.py home()): explicit wins; under AGENT_OPS_NO_SEND a temp dir, never the live home.
# Known differences from tempfile.gettempdir(): TEMP/TMP are not read without TMPDIR; a missing or unwritable TMPDIR falls back to /tmp only in Python; only one trailing slash is stripped; a relative TMPDIR is not made absolute.
if [ -z "${CAPSULE_HOME:-}" ]; then
  if [ -n "${AGENT_OPS_NO_SEND:-}" ]; then CAPSULE_HOME="${TMPDIR:-/tmp}"; CAPSULE_HOME="${CAPSULE_HOME%/}/capsule-home-no-send"
  else CAPSULE_HOME="$HOME/.local/share/capsule"; fi
fi
LOCK="${IMAGES_LOCK:-$CAPSULE_HOME/images.lock}"
case "${IMAGES_LOCK:-/}" in /*) ;; *) LOCK="$REPO/$IMAGES_LOCK" ;; esac   # docker/IMAGES.lock means the repo's, from any folder
case "${1:-base}" in
  base) NAME=capsule-base; DOCKERFILE="$REPO/docker/base/Dockerfile" ;;
  python) NAME=capsule-python; DOCKERFILE="$REPO/docker/stacks/python/Dockerfile" ;;
  web) NAME=capsule-web; DOCKERFILE="$REPO/docker/stacks/web/Dockerfile" ;;
  *) echo "usage: build-base.sh [base|python|web]" >&2; exit 64 ;;
esac
# The disk floor lives in one file, docker/BUILD_MIN_FREE_BYTES; `capsule doctor` reads the
# same one. AGENT_OPS_DOCKER_BIN (a folder holding `docker`, as in cleanup.sh) lets tests point
# at a fake.
[ -z "${AGENT_OPS_DOCKER_BIN:-}" ] || export PATH="$AGENT_OPS_DOCKER_BIN:$PATH"
# The file is read as capsule/runners/docker.py build_floor() reads it: spaces and newlines
# trimmed from both ends, then 1 to 15 digits, no leading zero (bash 3.2 reads 0-prefixed as
# octal; a CR, sign, underscore or exponent is refused).
floor_file="$REPO/docker/BUILD_MIN_FREE_BYTES"
min_free_bytes="$(cat "$floor_file" 2>/dev/null)" || min_free_bytes=""
while :; do
  case "$min_free_bytes" in
    " "*|$'\n'*) min_free_bytes="${min_free_bytes#?}" ;;
    *" "|*$'\n') min_free_bytes="${min_free_bytes%?}" ;;
    *) break ;;
  esac
done
case "$min_free_bytes" in
  ""|*[!0-9]*|0*|????????????????*)
    echo "build-base: $floor_file must hold the free-disk floor in bytes" \
         "(1 to 15 digits, no leading zero); not building" >&2
    exit 2 ;;
esac
stack_df=""; held=""   # held: the mkdir lock, set only once this process owns it
trap 'rm -f "$stack_df"; [ -z "$held" ] || rmdir "$held" 2>/dev/null' EXIT

free_kb="$(df -Pk "${DISK_PATH:-$REPO}" | awk 'NR==2{print $4}')"
if ! [ $((free_kb * 1024)) -ge "$min_free_bytes" ] 2>/dev/null; then   # any error refuses too
  echo "build-base: only $((free_kb * 1024 / 1000000000)) GB free," \
       "need $((min_free_bytes / 1000000000)) GB; not building" >&2
  exit 2
fi

# A stack chains from the base by digest. The Dockerfile pins the maintainer's base digest; a
# base built here has its own, so the FROM line is swapped for the record's base row in a
# temporary copy (the Dockerfile in the repo is never edited). No base row at all: the pinned
# base must already be here, else say what to build first.
if [ "$NAME" != capsule-base ]; then
  base="$(grep -E '^capsule-base@sha256:[0-9a-f]{64}$' "$LOCK" 2>/dev/null | tail -n 1 || true)"
  if [ -n "$base" ]; then
    stack_df="$(mktemp "${TMPDIR:-/tmp}/build-base-dockerfile.XXXXXX")"
    # The base row must be a local image: a digest-pinned FROM that is not here makes BuildKit
    # try docker.io (a local-only image is never there) and fail with a pull error.
    docker image inspect "$base" >/dev/null 2>&1 \
      || { echo "build-base: $base is not a local image; run docker/build-base.sh base first" >&2; exit 1; }
    sed -E "s|^FROM capsule-base@sha256:[0-9a-f]{64}|FROM $base|" "$DOCKERFILE" >"$stack_df"
    # Every line that chains from capsule-base must be swapped: a lowercase `from` or a tag
    # (`capsule-base:latest`) would otherwise build on an unpinned base.
    chained="$(grep -ciE '^from[[:space:]]+capsule-base' "$DOCKERFILE" || true)"
    swapped="$(grep -cE "^FROM $base( |\$)" "$stack_df" || true)"
    [ "$chained" -ge 1 ] && [ "$chained" -eq "$swapped" ] \
      || { echo "build-base: $chained FROM capsule-base line(s), $swapped swapped; not building" >&2; exit 1; }
    DOCKERFILE="$stack_df"
  else
    pinned="$(grep -E '^FROM capsule-base@' "$DOCKERFILE" | head -n 1 | sed 's/^FROM //')"
    docker image inspect "$pinned" >/dev/null 2>&1 \
      || { echo "build-base: no capsule-base row in $LOCK; run docker/build-base.sh base first" >&2; exit 1; }
    if grep -q '^capsule-base' "$LOCK" 2>/dev/null; then   # present, but not `capsule-base@sha256:<64 hex>`
      echo "build-base: warning: ignoring a malformed capsule-base row in $LOCK; building on the pinned base $pinned" >&2
    fi
  fi
fi

# --provenance=false --sbom=false: BuildKit's default attestations turn the result into an index
# (image + attestation manifests), whose digest is not the image's; the lock row must be the image's.
# BuildKit (OrbStack) cannot resolve a digest-pinned FROM of a local-only image, so the pinned base
# is mapped to a local tag of that image (capsule-base:local if it has it) with a named build
# context, found by the digest ref so a drifted tag does not matter. An untagged image gets no
# context (a note), a digest that is not local is refused. The legacy builder (no buildx)
# resolves it itself and has no such flag.
ctx=()
pinned_from="$(grep -E '^FROM capsule-base@sha256:[0-9a-f]{64}' "$DOCKERFILE" | head -n 1 | sed -E 's/^FROM ([^ ]+).*/\1/' || true)"
if [ -n "$pinned_from" ] && docker buildx version >/dev/null 2>&1; then
  have="$(docker image inspect --format '{{.Id}} {{join .RepoTags ","}}' "$pinned_from" 2>/dev/null || true)"
  id="${have%% *}"; tags=""
  case "$have" in *" "*) tags="${have#* }" ;; esac
  [ "$id" = "${pinned_from#*@}" ] \
    || { echo "build-base: ${pinned_from} is not a local image; run docker/build-base.sh base" >&2; exit 1; }
  tag="$(printf '%s\n' "$tags" | tr ',' '\n' | grep -Fx 'capsule-base:local' || true)"
  [ -n "$tag" ] || tag="${tags%%,*}"
  if [ -n "$tag" ]; then
    ctx=(--build-context "$pinned_from=docker-image://$tag")
  else
    echo "build-base: note: $pinned_from has no local tag, no build context passed" >&2
  fi
fi
# Keep-tag (inc_3a7d6457): `-t NAME:local` below moves the tag and leaves the previous image
# untagged, while the record may still name it by digest until the next landing syncs the row. Tag
# it NAME:prev-<12 hex of its id> first (idempotent), so it is never dangling; image_sweep.py drops
# the tag once no record names the image and it is not fresh. No :local yet: nothing to keep.
# A probe that fails (docker down, timeout) is not "absent": it stops the build (fail closed).
old="$(docker image ls --no-trunc -q "$NAME:local" 2>/dev/null)" \
  || { echo "build-base: could not look up $NAME:local; not building" >&2; exit 1; }
old="${old%%$'\n'*}"
case "$old" in
  sha256:[0-9a-f]*)
    old="${old#sha256:}"
    docker tag "sha256:$old" "$NAME:prev-${old:0:12}" 2>/dev/null \
      || { echo "build-base: could not keep-tag the current $NAME:local; not building" >&2; exit 1; } ;;
esac
docker build --pull=false --provenance=false --sbom=false --label capsule=1 --label capsule.kind=image ${ctx[@]+"${ctx[@]}"} -t "$NAME:local" -f "$DOCKERFILE" "$REPO/docker"

# With the containerd image store the image id is the manifest digest, which is what a
# digest reference resolves; a classic store has no local manifest digest, so refuse. An
# index-shaped answer (several lines, or any non-digest text) fails the checks below, never recorded.
digest="$(docker image inspect --format '{{.Id}}' "$NAME:local")"
case "$digest" in
  sha256:[0-9a-f][0-9a-f]*) [ "${#digest}" -eq 71 ] || { echo "build-base: bad digest $digest" >&2; exit 1; } ;;
  *) echo "build-base: bad digest $digest" >&2; exit 1 ;;
esac

size="$(docker image inspect --format '{{.Size}}' "$NAME:local")"
row="$NAME@$digest"
mkdir -p "$(dirname "$LOCK")"
# Two builds finishing together must not drop each other's row: the rewrite below runs under a
# lock: flock where the host has it (macOS has none by default), else a plain mkdir lock removed
# on exit, retried for 2 minutes and never taken over. BUILD_BASE_NO_FLOCK=1 forces the mkdir lock.
if [ -z "${BUILD_BASE_NO_FLOCK:-}" ] && command -v flock >/dev/null 2>&1; then
  exec 9>"$LOCK.lock"; flock 9
else
  tries=0
  until mkdir "$LOCK.lockdir" 2>/dev/null; do
    tries=$((tries + 1))
    [ "$tries" -lt 600 ] || {
      echo "build-base: $LOCK.lockdir still held after 2 minutes; the image was built," \
           "rerun this script to record it (remove that folder if no build is running)" >&2
      exit 1
    }
    sleep 0.2
  done
  held="$LOCK.lockdir"
fi
tmp="$LOCK.tmp.$$"
{ grep -v "^$NAME@" "$LOCK" 2>/dev/null || true; echo "$row"; } >"$tmp"
mv "$tmp" "$LOCK"
echo "build-base: $row ($size bytes) recorded in $LOCK"
