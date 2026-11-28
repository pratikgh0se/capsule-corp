#!/usr/bin/env bash
# cleanup.sh - the owner's rule (2026-09-06): nothing older than a week on the laptop.
# Sunday 02:00 via run.sh cleanup. --dry-run prints what it would do.
set -uo pipefail
usage() { cat <<'USAGE'
usage: cleanup.sh [--dry-run | -h | --help]
  (no flag)     run the cleanup for real (what run.sh cleanup does)
  --dry-run     print what would be done, change nothing
  -h, --help    print this usage and exit 0, doing nothing
Any other argument prints this usage to stderr and exits 2, doing nothing.
Paths and switches (environment): HOME, CLEANUP_HOME (pins the home), CAPSULE_HOME,
  CAPSULE_JOBS_DIR, AGENT_OPS_HOME, AGENT_OPS_STATE_DIR, CLEANUP_STATE_DIR,
  CLEANUP_MAINT_LOGS, CLEANUP_EVENTS_SPOOL, CLEANUP_EVENTS_OFFSET, CLEANUP_STATUTORY_DIRS,
  CLEANUP_TMPDIR, TMPDIR, CAPSULE_CLAUDE_PROJECTS_DIR, CLEANUP_TRANSCRIPT_ARCHIVE,
  OPS_ENV_FILE, OPS_WORKER_IMAGE, AGENT_OPS_DOCKER_BIN, AGENT_OPS_NO_SEND,
  CLEANUP_RETENTION_APPLY (1 = apply the retention sections), CLEANUP_NO_PG,
  CLEANUP_NO_DOCKER, CLEANUP_NO_EVENT
USAGE
}
DRY=0
case "$#:${1:-}" in
  0:) ;;
  1:--dry-run) DRY=1 ;;
  1:-h|1:--help) usage; exit 0 ;;
  *) usage >&2; exit 2 ;;
esac
H="${CLEANUP_HOME:-$HOME}"
[ -n "${CLEANUP_HOME:-}" ] && : "${CAPSULE_JOBS_DIR:=$H/.config/capsule-corp}"   # tests pin the home
. "$(dirname "$0")/lib_config.sh"   # CAPSULE_JOBS_DIR
ML="${CLEANUP_MAINT_LOGS:-$CAPSULE_JOBS_DIR/maintenance/logs}"
DAYS=7; MAXMB=50; KEEP=10000; freed=0
# The engine's docker CLI is not on a launchd job's PATH; prepend it the same way
# run.sh's heal branch does (AGENT_OPS_DOCKER_BIN lets tests point at a fake).
. "$(dirname "$0")/lib_state.sh"   # STATE_DIR: $AGENT_OPS_STATE_DIR or ~/.local/state/capsule-corp/maint
. "$(dirname "$0")/lib_docker_engine.sh"   # DOCKER_ENGINE*, DOCKER_CONTEXT, docker_path_export
docker_path_export
SPOOL_DEFAULT="$STATE_DIR/events.jsonl"   # twin of events.py, before the CLEANUP_STATE_DIR override
STATE_DIR="${CLEANUP_STATE_DIR:-$STATE_DIR}"
act() { if [ "$DRY" = 1 ]; then echo "would $*"; else echo "$*"; fi; }
filesize() { stat -c %s "$1" 2>/dev/null || stat -f %z "$1"; }  # GNU then BSD
# Keep the last N lines of a log IN PLACE (same inode): launchd holds these files open, so
# swapping in a new file would leave the daemon writing to the unlinked old one and free nothing.
trim_inplace() {  # file keep
  tail -n "$2" "$1" > "$1.tmp" && cat "$1.tmp" > "$1"; rc=$?; rm -f "$1.tmp"; return $rc
}
# C12 / M269 (decision row 140), gate G10: two stores are never deleted by this
# job, and the refusal is PRINTED every run including --dry-run - the study's
# G10 requires the dry run to say so. Invoices and receipts carry a five-year
# statutory floor (Thai Revenue Code s.87/3 for a VAT registrant, s.105 bis for
# a non-VAT business); the app user data a product app writes into the vault
# (Tasks/Focus Log.md, Tasks/Todo.md) is user-authored personal data, not
# Capsule telemetry. See docs/RETENTION.md "Statutory floors". There is
# deliberately NO flag: a guard with an off switch is not a guard, so
# CLEANUP_STATUTORY_DIRS can only ADD roots, never remove the built-in names.
# Matching is case-insensitive and symlink-safe - every candidate
# and every configured root is resolved to its real path first (os.path.realpath,
# same portable pattern backup.sh's resolve() already uses; it needs no target
# to exist). A path that fails to resolve is treated as statutory - fail closed,
# refuse the delete - rather than falling through and being swept for real.
resolve_path() {
  python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" "$1" 2>/dev/null
}
STATUTORY="${CLEANUP_STATUTORY_DIRS-$H/Documents/Capsule/invoices:$H/.local/share/capsule-corp/invoices}"
STAT_SKIPPED=0
is_statutory() {
  local rp lp d rd rl
  rp="$(resolve_path "$1")"
  [ -n "$rp" ] || return 0
  lp="$(printf '%s' "$rp" | tr '[:upper:]' '[:lower:]')"
  case "$lp" in
    */invoices|*/invoices/*|*/receipts|*/receipts/*|*/focus\ log.md|*/todo.md) return 0 ;;
  esac
  local IFS=:
  for d in $STATUTORY; do
    [ -n "$d" ] || continue
    rd="$(resolve_path "$d")"
    [ -n "$rd" ] || continue
    rl="$(printf '%s' "$rd" | tr '[:upper:]' '[:lower:]')"
    case "$lp" in "$rl"|"$rl"/*) return 0 ;; esac
  done
  return 1
}
skip_statutory() { if is_statutory "$1"; then STAT_SKIPPED=$((STAT_SKIPPED+1)); return 0; fi; return 1; }
# Retention flag (OPS_RETENTION_APPLY, ops.env; CLEANUP_RETENTION_APPLY overrides for
# tests): gates the sessions/snapshots/samples sections below. Trimmed "1" = apply;
# anything else, or a real --dry-run of the whole script, = dry-run for these sections.
if [ -n "${CLEANUP_RETENTION_APPLY+x}" ]; then
  RA="$CLEANUP_RETENTION_APPLY"
else
  ENVF="$CAPSULE_JOBS_DIR/ops.env"
  RA=""
  [ -f "$ENVF" ] && RA="$(grep '^OPS_RETENTION_APPLY=' "$ENVF" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d "'\"")"
fi
RA="$(printf '%s' "$RA" | tr -d '[:space:]')"
RETENTION_DRY=1; [ "$DRY" != 1 ] && [ "$RA" = "1" ] && RETENTION_DRY=0
ract() { if [ "$RETENTION_DRY" = 1 ]; then echo "would $*"; else echo "$*"; fi; }
# A full `--dry-run` invokes nothing at all, docker included, so the pending
# samples row count cannot be read: it prints as "?" rather than a made-up 0.
RET_FILES=0; RET_ROWS=0; [ "$DRY" = 1 ] && RET_ROWS="?"
del_old() {  # dir pattern
  while IFS= read -r -d '' f; do
    skip_statutory "$f" && continue
    sz=$(filesize "$f" 2>/dev/null || echo 0); act "delete $f ($sz b)"; [ "$DRY" = 1 ] || { rm -f "$f" && freed=$((freed+sz)); }
  done < <(find "$1" -type f -name "$2" -mtime +$DAYS -print0 2>/dev/null)
}
del_old "$ML" "*.log"
while IFS= read -r -d '' f; do            # live logs over 50 MB -> last 10k lines
  skip_statutory "$f" && continue
  sz=$(filesize "$f" 2>/dev/null || echo 0); act "truncate $f ($sz b) to $KEEP lines"
  [ "$DRY" = 1 ] || { trim_inplace "$f" $KEEP && freed=$((freed+sz-$(filesize "$f"))); }
done < <(
  find "$ML" -type f -name "*.log" -size +${MAXMB}M -print0 2>/dev/null
)
# Event spool: everything before the shipped offset is already in Postgres, so drop it
# and rewind the offset to 0. A missing or unparseable offset means we cannot tell what
# has shipped - then we trim nothing rather than risk losing unshipped rows.
SPOOL="${CLEANUP_EVENTS_SPOOL:-$SPOOL_DEFAULT}"
OFFS="${CLEANUP_EVENTS_OFFSET:-${SPOOL%.jsonl}.offset}"
if [ -f "$SPOOL" ]; then
  python3 - "$SPOOL" "$OFFS" "$DRY" <<'EOF'
import os, sys
spool, offs, dry = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
try:
    off = int(open(offs).read().strip())
except Exception as exc:
    print(f"spool not trimmed: offset unreadable ({exc})"); raise SystemExit(0)
data = open(spool, "rb").read()
if off <= 0 or off > len(data):
    print(f"spool not trimmed: offset {off} outside 1..{len(data)}"); raise SystemExit(0)
kept = data[off:]
if dry:
    print(f"would trim spool from {len(data)} to {len(kept)} bytes"); raise SystemExit(0)
tmp = spool + ".trim"
with open(tmp, "wb") as f:
    f.write(kept)
# Guard against a concurrent log()/ship(): if the spool grew or the offset moved since we
# read them, another process is mid-write - skip this week rather than lose or re-ship rows.
try:
    off_now = int(open(offs).read().strip())
except Exception:
    off_now = -1
if os.path.getsize(spool) != len(data) or off_now != off:
    os.remove(tmp); print("spool not trimmed: changed underneath us, try next week"); raise SystemExit(0)
os.replace(tmp, spool)
with open(offs, "w") as f:
    f.write("0")
print(f"trim spool from {len(data)} to {len(kept)} bytes")
EOF
fi
if [ -z "${CLEANUP_NO_PG:-}" ]; then
  act "apply events retention (card 180 d, health 14 d, other 30 d; maintenance/retention.sql)"; [ "$DRY" = 1 ] || docker exec -i capsule-corp-postgres psql -U ops -d ops -q -v ON_ERROR_STOP=1 -f - < "$(dirname "$0")/retention.sql" >/dev/null 2>&1 || true
  # Read-only count for the retention report/job event - safe to run regardless of
  # RETENTION_DRY (so "N rows would go" reflects the live count while the flag is 0),
  # but NOT in a full --dry-run of the script: that mode's whole contract is that it
  # invokes nothing. The count is then unknown and prints as "?".
  if [ "$DRY" != 1 ]; then
    RET_ROWS="$(docker exec capsule-corp-postgres psql -U ops -d ops -tAc "select count(*) from samples where ts < now() - interval '90 days'" 2>/dev/null | tr -d '[:space:]')"
    case "$RET_ROWS" in ''|*[!0-9]*) RET_ROWS=0 ;; esac
  fi
  ract "delete samples older than 90 days"; [ "$RETENTION_DRY" = 1 ] || docker exec capsule-corp-postgres psql -U ops -d ops -qc "delete from samples where ts < now() - interval '90 days'" >/dev/null 2>&1 || true
  # D255: the hourly rollup (009-samples-rollup.sql) outlives the raw rows by two years.
  ract "delete samples_hourly older than 730 days"; [ "$RETENTION_DRY" = 1 ] || docker exec capsule-corp-postgres psql -U ops -d ops -qc "delete from samples_hourly where bucket < now() - interval '730 days'" >/dev/null 2>&1 || true
fi
# CAPSULE_HOME rule (row 609; the same block is in every script that defaults it, = capsule/paths.py home()): explicit wins; under AGENT_OPS_NO_SEND a temp dir, never the live home.
# Known differences from tempfile.gettempdir(): TEMP/TMP are not read without TMPDIR; a missing or unwritable TMPDIR falls back to /tmp only in Python; only one trailing slash is stripped; a relative TMPDIR is not made absolute.
if [ -z "${CAPSULE_HOME:-}" ]; then
  if [ -n "${AGENT_OPS_NO_SEND:-}" ]; then CAPSULE_HOME="${TMPDIR:-/tmp}"; CAPSULE_HOME="${CAPSULE_HOME%/}/capsule-home-no-send"
  else CAPSULE_HOME="$H/.local/share/capsule"; fi
fi
for lf in "$CAPSULE_HOME"/lookout/ship.log "$CAPSULE_HOME"/lookout/publish.log; do
  if [ -f "$lf" ] && [ "$(wc -c < "$lf")" -gt 5242880 ]; then
    act "trim $lf to its last 10000 lines"; [ "$DRY" = 1 ] || trim_inplace "$lf" 10000
  fi
done
# The tick logs (launchd StandardOut/ErrorPath, logs/tick*.log): nothing else rotates them.
for lf in "$CAPSULE_HOME"/logs/tick*.log; do
  if [ -f "$lf" ] && [ "$(wc -c < "$lf")" -gt 5242880 ]; then
    act "trim $lf to its last 10000 lines"; [ "$DRY" = 1 ] || trim_inplace "$lf" 10000
  fi
done
# Test temp dirs (t_dd689940): suites that leaked 17 GB of tmp* into TMPDIR. Only top-level dirs
# in TMPDIR, only our uid's, only older than 1 day, only the names the suites create (mktemp -d
# `tmp.XXXXXXXXXX`, mkdtemp `tmpXXXXXXXX`, tmpclean `capsule-suite-*`, mutation_check
# `mutation-check-*`, lander sandbox `lander-box-*`). find -type d never matches a symlink; rmtree never follows one.
# Gated by OPS_RETENTION_APPLY like the sections above.
TMPD="${CLEANUP_TMPDIR:-${TMPDIR:-/tmp}}"
if [ -n "$TMPD" ] && [ "$TMPD" != "/" ] && [ -d "$TMPD" ]; then
  while IFS= read -r -d '' f; do
    # The top dir's mtime only moves on direct entries: anything modified inside it within the
    # day means the dir is live, keep it.
    [ -z "$(find "$f" -mindepth 1 -mmin -1440 -print -quit 2>/dev/null)" ] || continue
    ract "delete test tmp dir $f"
    if [ "$RETENTION_DRY" = 1 ]; then RET_FILES=$((RET_FILES+1))
    elif python3 -c "
import os, shutil, sys
t = sys.argv[1]; os.path.islink(t) and sys.exit(1); os.chmod(t, 0o700)
for here, dirs, _ in os.walk(t):
    for d in dirs:
        p = os.path.join(here, d)
        if not os.path.islink(p): os.chmod(p, 0o700)
shutil.rmtree(t, ignore_errors=True); sys.exit(os.path.lexists(t))" "$f" 2>/dev/null; then RET_FILES=$((RET_FILES+1)); fi
  done < <(find "$TMPD" -mindepth 1 -maxdepth 1 -type d -user "$(id -u)" -mmin +1440 \
    \( -name 'tmp.??????????' -o -name 'tmp????????' -o -name 'capsule-suite-*' -o -name 'mutation-check-*' -o -name 'lander-box-*' \) -print0 2>/dev/null)
fi
# Worker session transcripts (retention): compress after 14 d once the tokens oracle has
# recorded their week, verify, delete the original; archives go after 30 d. Worker sessions
# only (ids named in spool run.json files). Gated by OPS_RETENTION_APPLY.
CH="$CAPSULE_HOME"
TR_ARGS=("$CH" "${CAPSULE_CLAUDE_PROJECTS_DIR:-$H/.claude/projects}" "${CLEANUP_TRANSCRIPT_ARCHIVE:-$CH/transcript-archive}")
TR_OUT="$(python3 "$(dirname "$0")/retention_transcripts.py" "${TR_ARGS[@]}" "$((1-RETENTION_DRY))" 2>&1)"
printf '%s\n' "$TR_OUT" | grep -v '^RETENTION_TRANSCRIPTS '
TR_N="$(printf '%s\n' "$TR_OUT" | sed -n 's/^RETENTION_TRANSCRIPTS files=\([0-9]*\) .*/\1/p' | tail -1)"
RET_FILES=$((RET_FILES+${TR_N:-0}))
# Unused images: `docker image prune -a` cannot exclude anything, so unused images are listed and
# removed one by one (never -f: docker itself refuses an image a container uses), skipping the
# keep-list: every image in observability/docker-compose.yml, the worker pin (OPS_WORKER_IMAGE
# env, ops.env), the images.lock rows (docker/IMAGES.lock and the record under $CAPSULE_HOME)
# and every image a
# container (running or not) uses, and every image labelled capsule-layer (maintenance/image_gc.py
# owns those: it keeps the lock, the previous lock and anything under 14 days old).
# The listing is read-only, so it also runs in a full --dry-run, to print exactly what would go.
prune_images() {
  local d keep rows id repo tag dig ref pin envf layerids keepids=""
  d="$(dirname "$0")"; envf="${OPS_ENV_FILE:-$CAPSULE_JOBS_DIR/ops.env}"
  pin="${OPS_WORKER_IMAGE:-}"   # env, then ops.env; the lock rows are kept separately below
  [ -n "$pin" ] || pin="$(grep '^OPS_WORKER_IMAGE=' "$envf" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d "'\"")"
  # every ref also as repo:tag without a digest, without docker.io/ and library/, and with :latest
  keep="$({ sed -n 's/^[[:space:]]*image:[[:space:]]*\([^[:space:]#]*\).*/\1/p' "$d"/../observability/docker-compose*.yml 2>/dev/null
      echo "$pin"
      # IMAGES.lock rows are local-only builds (no registry copy): keep each as its digest ref, its :local tag and its short id
      grep -E '^capsule-[a-z]+@sha256:[0-9a-f]{64}$' "$d/../docker/IMAGES.lock" 2>/dev/null | while read -r r; do
        echo "$r"; echo "${r%%@*}:local"; echo "${r##*sha256:}" | cut -c1-12; done
      # the FROM refs of docker/base and docker/stacks/*: the build bases, pulled once and pinned
      awk 'toupper($1) == "FROM" { for (i = 2; i <= NF; i++) if ($i !~ /^--/) { print $i; break } }' \
        "$d"/../docker/base/Dockerfile "$d"/../docker/stacks/*/Dockerfile 2>/dev/null
      # and the rows of the image record (what `capsule image build` made on this machine)
      grep -E '^capsule-[a-z0-9-]+@sha256:[0-9a-f]{64}$' "$CAPSULE_HOME/images.lock" 2>/dev/null | while read -r r; do
        echo "$r"; echo "${r%%@*}:local"; echo "${r##*sha256:}" | cut -c1-12; done
      docker ps -a --format '{{.Image}}' 2>/dev/null; } | awk 'NF {
    r = $1; sub(/^docker\.io\//, "", r); print r; w = r; sub(/^library\//, "", w); print w
    dg = ""; if (match(w, /@sha256:.*/)) { dg = substr(w, RSTART); w = substr(w, 1, RSTART - 1) }
    base = w; if (match(w, /:[^\/:]*$/)) base = substr(w, 1, RSTART - 1); else print w ":latest"
    print w; if (dg != "") print base dg }')"
  rows="$(docker image ls -a --format '{{.ID}}|{{.Repository}}|{{.Tag}}|{{.Digest}}' 2>/dev/null)" || { echo "images: listing failed, none removed"; return 0; }
  layerids="$(docker image ls -a -q --filter label=capsule-layer 2>/dev/null)" || { echo "images: capsule-layer listing failed, none removed"; return 0; }
  while IFS='|' read -r id repo tag dig; do  # pass 1: ids the keep-list protects
    [ -n "$id" ] || continue
    if printf '%s\n' "$keep" | grep -Fxq -e "$repo:$tag" -e "$repo@$dig" -e "$id" \
       || printf '%s\n' "$layerids" | grep -Fxq -e "$id"; then keepids="$keepids $id"; fi
  done <<<"$rows"
  while IFS='|' read -r id repo tag dig; do  # pass 2: the rest
    [ -n "$id" ] || continue
    case " $keepids " in *" $id "*) continue ;; esac
    if [ "$tag" != "<none>" ]; then ref="$repo:$tag"; elif [ "$dig" != "<none>" ]; then ref="$repo@$dig"; else continue; fi  # dangling: image prune -f
    ract "docker image rm $ref ($id)"
    if [ "$RETENTION_DRY" = 1 ]; then RET_FILES=$((RET_FILES+1))
    elif docker image rm "$ref" >/dev/null 2>&1; then RET_FILES=$((RET_FILES+1)); fi
  done <<<"$rows"
}
if [ -z "${CLEANUP_NO_DOCKER:-}" ]; then
  # the dangling prune spares capsule-layer images: a rebuild leaves the previous image dangling and
  # that is the rollback image (image_gc.py decides when those go)
  act "docker image prune -f --filter 'label!=capsule-layer'; docker builder prune -f"; [ "$DRY" = 1 ] || { docker image prune -f --filter 'label!=capsule-layer' >/dev/null 2>&1; docker builder prune -f >/dev/null 2>&1; }
  prune_images
fi
echo "statutory: never deleting invoices or app user data (Thai Revenue Code s.87/3 / s.105 bis, 5 y floor); $STAT_SKIPPED paths skipped this run"
sizes="$(python3 - "$H" "$ML" <<'EOF'
import json, os, sys
def du(p):
    t = 0
    for r, _, fs in os.walk(p):
        for f in fs:
            try: t += os.path.getsize(os.path.join(r, f))
            except OSError: pass
    return t
H, ML = sys.argv[1], sys.argv[2]
print(json.dumps({k: du(v) for k, v in {
  "job_logs": ML,
  "postgres": f"{H}/.local/share/capsule-corp/postgres", "loki": f"{H}/.local/share/capsule-corp/loki"
  }.items() if os.path.isdir(v)}))
EOF
)"
echo "freed $freed bytes; sizes $sizes"
if [ "$RETENTION_DRY" = 1 ]; then
  RET_MODE="dry-run"; RET_MSG="retention: dry-run, $RET_FILES files / $RET_ROWS rows would go"
else
  RET_MODE="applied"; RET_MSG="retention: applied, $RET_FILES files / $RET_ROWS rows"
fi
echo "$RET_MSG"
if [ "$DRY" != 1 ]; then
  mkdir -p "$STATE_DIR"
  printf '{"mode": "%s", "files": %s, "rows": %s, "at": "%s"}\n' \
    "$RET_MODE" "$RET_FILES" "$RET_ROWS" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >"$STATE_DIR/retention.json.tmp" \
    && mv "$STATE_DIR/retention.json.tmp" "$STATE_DIR/retention.json"
fi
[ -n "${CLEANUP_NO_EVENT:-}" ] || [ "$DRY" = 1 ] || python3 "$(dirname "$0")/events.py" log job cleanup.sh "freed $freed bytes; $RET_MSG" --job cleanup --phase end --exit 0 --payload "{\"freed_bytes\": $freed, \"sizes\": $sizes, \"retention_files\": $RET_FILES, \"retention_rows\": $RET_ROWS}" >/dev/null 2>&1 || true
