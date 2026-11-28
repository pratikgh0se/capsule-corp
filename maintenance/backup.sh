#!/usr/bin/env bash
# backup.sh - nightly tarball of everything that is not on GitHub (spec
# docs/superpowers/specs/2026-09-07-backups-design.md). The owner's rule (row 35):
# to disk, keeps the 7 newest, rehearsed weekly by restore.sh --verify.
# Exit codes: 0 = everything in the tarball; 1 = either a hard die() before the
# tarball exists, or a PARTIAL backup (a pg_dump failed) whose tarball IS kept -
# see the retention/partial comments at the bottom (row 125).
# macOS only (BSD stat/find, shasum); the scripts run only on this Mac.
# Encryption (M210, row 137) is behind OPS_BACKUP_ENCRYPT, default off; the
# plaintext is retained 0 days, and the key comes from Potara or
# BACKUP_ENC_KEY_FILE (backup_key.sh).
set -uo pipefail
usage() { cat <<'USAGE'
usage: backup.sh [--dry-run | -h | --help]
  (no flag)     write the nightly tarball and prune old ones (what run.sh backup does)
  --dry-run     print the tarball path it would write, change nothing
  -h, --help    print this usage and exit 0, doing nothing
Any other argument prints this usage to stderr and exits 2, doing nothing.
Paths and switches (environment): HOME, AGENT_OPS_HOME, BACKUP_DIR, BACKUP_DATE, BACKUP_KEEP,
  BACKUP_MIN_FREE_KB, MERGE_GATE_STATE, PG_CONTAINER, INF_PG_CONTAINER, CAPSULE_HOME,
  CAPSULE_JOBS_DIR, AGENT_OPS_STATE_DIR, AGENT_OPS_NO_SEND, TMPDIR, OBSIDIAN_VAULT_PATH,
  OBSIDIAN_SKILL_ROOT, OPS_BACKUP_ENCRYPT (1 = encrypt), BACKUP_ENC_KEY_FILE, BACKUP_ENC_PROJECT,
  BACKUP_ENC_SECRET, BACKUP_ENC_ARGS, CAPSULE_SECRET_BIN, CAPSULE_IDENTITY
USAGE
}
DRY=0
case "$#:${1:-}" in
  0:) ;;
  1:--dry-run) DRY=1 ;;
  1:-h|1:--help) usage; exit 0 ;;
  *) usage >&2; exit 2 ;;
esac
# -P/pwd -P: resolve the *physical* path. launchd runs this through the
# deployed symlink <config dir>/maintenance -> <repo>/maintenance;
# a logical cd would leave SCRIPT_DIR/.. pointed at <config dir>,
# not the repo, so `git -C "$SCRIPT_DIR/.."` below would always record "none".
SCRIPT_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
die() { echo "backup: error: $*" >&2; exit 1; }
HOME_ROOT="${AGENT_OPS_HOME:-$HOME}"
# The notes vault is opt-in (D254): OBSIDIAN_VAULT_PATH, else host.json vault.path, else none (no vault bundle).
. "$SCRIPT_DIR/lib_vault.sh"
vault_resolve "$SCRIPT_DIR/.."
BACKUP_DIR="${BACKUP_DIR:-$HOME_ROOT/Backups/capsule}"
. "$SCRIPT_DIR/lib_state.sh"
. "$SCRIPT_DIR/lib_config.sh"   # CAPSULE_JOBS_DIR
GATE_STATE="${MERGE_GATE_STATE:-$HOME_ROOT/.local/state/capsule-corp/merge-gate}"
PG_CONTAINER="${PG_CONTAINER:-capsule-corp-postgres}"
INF_PG_CONTAINER="${INF_PG_CONTAINER:-capsule-corp-infisical-db}"
MIN_FREE_KB="${BACKUP_MIN_FREE_KB:-2097152}"
KEEP="${BACKUP_KEEP:-7}"
case "$KEEP" in ''|*[!0-9]*) die "BACKUP_KEEP must be a non-negative integer, got '$KEEP'" ;; esac
DATE="${BACKUP_DATE:-$(date +%F)}"
OUT="$BACKUP_DIR/$DATE.tar.gz"
WARN=0; ITEMS=0

event() {  # kind text severity
  [ "$DRY" = 1 ] && return 0
  ( cd "$SCRIPT_DIR" && python3 events.py log "$1" backup "$2" --severity "$3" ) >/dev/null 2>&1 || true
}

command -v sqlite3 >/dev/null || die "sqlite3 not found"
command -v tar >/dev/null || die "tar not found"

# refuse a degenerate BACKUP_DIR before any mkdir/chmod/rm touches it. Resolve
# without requiring the dir to exist yet (realpath, not cd+pwd -P) since the
# common case is a first run where BACKUP_DIR hasn't been created.
resolve() { python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" "$1"; }
_resolved_backup_dir="$(resolve "$BACKUP_DIR")"
_resolved_home="$(resolve "$HOME")"
_resolved_home_root="$(resolve "$HOME_ROOT")"
_comp_count="$(printf '%s' "$_resolved_backup_dir" | awk -F/ '{c=0; for(i=1;i<=NF;i++) if($i!="") c++; print c}')"
if [ "$_resolved_backup_dir" = "/" ] || [ "$_resolved_backup_dir" = "$_resolved_home" ] \
   || [ "$_resolved_backup_dir" = "$_resolved_home_root" ] || [ "${_comp_count:-0}" -lt 3 ]; then
  die "refusing BACKUP_DIR=$BACKUP_DIR"
fi

if [ "$DRY" = 1 ]; then echo "backup: would write $OUT"; exit 0; fi
mkdir -p "$BACKUP_DIR" || die "cannot create $BACKUP_DIR"
chmod 700 "$BACKUP_DIR" || die "cannot chmod $BACKUP_DIR"
free_kb="$(df -k "$BACKUP_DIR" | awk 'NR==2{print $4}')"
[ "${free_kb:-0}" -ge "$MIN_FREE_KB" ] || die "only ${free_kb} KB free under $BACKUP_DIR (need $MIN_FREE_KB)"

W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT
add() { ITEMS=$((ITEMS+1)); }

# Capsule boards (C20, row 478): the online-backup copy of each board db, plus the
# small files a give-back needs beside it.
# CAPSULE_HOME rule (row 609; the same block is in every script that defaults it, = capsule/paths.py home()): explicit wins; under AGENT_OPS_NO_SEND a temp dir, never the live home.
# Known differences from tempfile.gettempdir(): TEMP/TMP are not read without TMPDIR; a missing or unwritable TMPDIR falls back to /tmp only in Python; only one trailing slash is stripped; a relative TMPDIR is not made absolute.
if [ -z "${CAPSULE_HOME:-}" ]; then
  if [ -n "${AGENT_OPS_NO_SEND:-}" ]; then CAPSULE_HOME="${TMPDIR:-/tmp}"; CAPSULE_HOME="${CAPSULE_HOME%/}/capsule-home-no-send"
  else CAPSULE_HOME="$HOME_ROOT/.local/share/capsule"; fi
fi
# Weekly WAL checkpoint (stall unit 4b): TRUNCATE folds the -wal file into the db; no row is deleted.
# A stamp under STATE_DIR keeps it to once in 7 days (167 h); a busy board is retried next night.
WAL_STAMP="$STATE_DIR/wal-checkpoint.stamp"
[ -z "$(find "$WAL_STAMP" -mmin -10020 2>/dev/null)" ] && WAL_DUE=1 || WAL_DUE=0
for db in "$CAPSULE_HOME"/boards/*/kanban.db; do
  [ -f "$db" ] || continue; b="$(basename "$(dirname "$db")")"; mkdir -p "$W/capsule/boards/$b"
  if [ "$WAL_DUE" = 1 ]; then
    [ "$(sqlite3 "$db" "PRAGMA wal_checkpoint(TRUNCATE);" 2>/dev/null | cut -d'|' -f1)" = 0 ] || WAL_DUE=2
  fi
  sqlite3 "$db" ".backup '$W/capsule/boards/$b/kanban.db'" || die "sqlite backup of $db failed"; add
  [ -f "$(dirname "$db")/config.json" ] && cp "$(dirname "$db")/config.json" "$W/capsule/boards/$b/config.json"
done
[ "$WAL_DUE" = 1 ] && { mkdir -p "$STATE_DIR"; touch "$WAL_STAMP"; }
[ -f "$CAPSULE_HOME/handovers.jsonl" ] && { mkdir -p "$W/capsule"; cp "$CAPSULE_HOME/handovers.jsonl" "$W/capsule/"; add; }
[ -f "$CAPSULE_HOME/config.json" ] && { mkdir -p "$W/capsule"; cp "$CAPSULE_HOME/config.json" "$W/capsule/"; add; }
for o in "$CAPSULE_HOME"/queue/*/owners.json; do
  [ -f "$o" ] || continue; q="$(basename "$(dirname "$o")")"; mkdir -p "$W/capsule/queue/$q"; cp "$o" "$W/capsule/queue/$q/owners.json"; add
done
for d in human intake; do
  [ -d "$CAPSULE_HOME/$d" ] && { mkdir -p "$W/capsule"; cp -R "$CAPSULE_HOME/$d" "$W/capsule/"; add; }
done
# Postgres events
if docker exec "$PG_CONTAINER" pg_dump -U ops --no-owner ops >"$W/events.sql" 2>"$W/.pgerr"; then add
else rm -f "$W/events.sql"; { echo "MISSING: pg_dump failed at $(date +%FT%T)"; cat "$W/.pgerr"; } >"$W/events.sql.MISSING"; WARN=1
     echo "backup: warn: postgres dump missing ($(head -c 120 "$W/.pgerr" | tr '\n' ' '))"; fi
rm -f "$W/.pgerr"
# Infisical: pg_dump of infisical-db, and the infisical.env file it needs to
# be readable (ENCRYPTION_KEY lives only there - lose it and the dump is
# permanently undecryptable), same MISSING-marker shape as events.sql above.
if docker exec "$INF_PG_CONTAINER" pg_dump -U infisical --no-owner infisical >"$W/infisical.sql" 2>"$W/.inferr"; then add
else rm -f "$W/infisical.sql"; { echo "MISSING: infisical pg_dump failed at $(date +%FT%T)"; cat "$W/.inferr"; } >"$W/infisical.sql.MISSING"; WARN=1
     echo "backup: warn: infisical dump missing ($(head -c 120 "$W/.inferr" | tr '\n' ' '))"; fi
rm -f "$W/.inferr"
[ -f "$CAPSULE_JOBS_DIR/infisical.env" ] && { cp "$CAPSULE_JOBS_DIR/infisical.env" "$W/infisical.env"; add; }
# merge-gate approvals, bridge state, ops.env
[ -d "$GATE_STATE/approvals" ] && { mkdir -p "$W/merge-gate"; cp -R "$GATE_STATE/approvals" "$W/merge-gate/"; add; }
if [ -d "$STATE_DIR" ]; then
  state_copied=0
  for f in notify-seen.json spec-grades.json usage-guard.json events.jsonl events.offset escalated.json; do
    [ -f "$STATE_DIR/$f" ] && { mkdir -p "$W/state"; cp "$STATE_DIR/$f" "$W/state/$f"; state_copied=1; }
  done
  [ "$state_copied" = 1 ] && add
fi
[ -f "$CAPSULE_JOBS_DIR/ops.env" ] && { cp "$CAPSULE_JOBS_DIR/ops.env" "$W/ops.env"; add; }
# vault bundle
[ -n "$VAULT" ] && [ -d "$VAULT/.git" ] && { git -C "$VAULT" bundle create "$W/vault.bundle" --all >/dev/null || die "vault bundle failed"; add; }
# manifest
( cd "$W" && find . -type f ! -name MANIFEST.txt | sed 's#^\./##' | sort | while read -r f; do printf '%s\t%s\t%s\n' "$f" "$(stat -f %z "$f")" "$(shasum -a 256 "$f" | cut -d' ' -f1)"; done ) >"$W/MANIFEST.txt"
{ echo "# tool: sqlite3 $(sqlite3 --version | cut -d' ' -f1)"; echo "# tool: pg_dump $(docker exec "$PG_CONTAINER" pg_dump --version 2>/dev/null | awk '{print $3}' || echo MISSING)"
  echo "# vault: $(git -C "$VAULT" rev-parse --short HEAD 2>/dev/null || echo none)"; echo "# capsule-corp: $(git -C "$SCRIPT_DIR/.." rev-parse --short HEAD 2>/dev/null || echo none)"; } >>"$W/MANIFEST.txt"
# write (no leading ./ prefix in the tarball: expand the workdir's entries explicitly)
( cd "$W" && tar czf "$OUT.tmp" -- * ) || die "tar failed"
mv "$OUT.tmp" "$OUT" || die "mv $OUT.tmp -> $OUT failed"
chmod 600 "$OUT" || die "chmod 600 $OUT failed"
[ -f "$OUT" ] && [ -s "$OUT" ] || die "no tarball at $OUT after mv"
# --- encryption (M210, row 137, spec section 7.1), behind OPS_BACKUP_ENCRYPT,
# default OFF. Deliberately AFTER the tarball is complete and verified
# present: an encryption failure must never be the reason there is no backup.
# The plaintext is deleted only once the ciphertext has been decrypted back
# and hashed to the same bytes - a backup nothing can decrypt is worse than a
# plaintext one, and this is the only moment the machine can prove otherwise
# without a human. "Plaintext retained 0 days" is literal: it exists only
# between `tar czf` and the `rm -f` below, in this one process.
SELF="$OUT"; ENCRYPTED=no
_enc_flag="$(printf '%s' "${OPS_BACKUP_ENCRYPT:-0}" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')"
case "$_enc_flag" in 1|true|yes) _enc_flag=1 ;; *) _enc_flag=0 ;; esac
if [ "$_enc_flag" = 1 ]; then
  command -v openssl >/dev/null || die "OPS_BACKUP_ENCRYPT=1 but openssl not found"
  # shellcheck source=/dev/null
  . "$SCRIPT_DIR/backup_key.sh" || die "cannot load backup_key.sh"
  ENC="$BACKUP_DIR/$DATE.tar.gz.enc"
  plain_sha="$(shasum -a 256 "$OUT" | cut -d' ' -f1)"
  # $W's `trap rm -rf` (line 58) already covers the key file on every exit
  # path, die() included - that is why the key goes there and nowhere else.
  backup_key_write "$W/enc.key" || die "encryption key unavailable"
  backup_enc "$OUT" "$ENC.tmp" "$W/enc.key" || { rm -f "$ENC.tmp"; die "openssl encrypt failed - plaintext kept at $OUT"; }
  backup_dec "$ENC.tmp" "$W/roundtrip.tar.gz" "$W/enc.key" || { rm -f "$ENC.tmp"; die "encrypted tarball does not decrypt - plaintext kept at $OUT"; }
  rt_sha="$(shasum -a 256 "$W/roundtrip.tar.gz" | cut -d' ' -f1)"
  rm -f "$W/roundtrip.tar.gz"
  [ "$rt_sha" = "$plain_sha" ] || { rm -f "$ENC.tmp"; die "round trip sha mismatch - plaintext kept at $OUT"; }
  mv "$ENC.tmp" "$ENC" || { rm -f "$ENC.tmp"; die "mv $ENC.tmp -> $ENC failed - plaintext kept at $OUT"; }
  chmod 600 "$ENC" || die "chmod 600 $ENC failed"
  rm -f "$OUT" || die "cannot remove plaintext $OUT"
  SELF="$ENC"; ENCRYPTED=yes
fi
# drop stale *.tar.gz.tmp left by a crashed run before retention runs, so
# they don't accumulate silently inside the directory whose whole point is
# bounded disk use
find "$BACKUP_DIR" -maxdepth 1 \( -name '*.tar.gz.tmp' -o -name '*.tar.gz.enc.tmp' \) -mtime +0 -delete 2>/dev/null || true
# retention: keep exactly KEEP tarballs on disk, newest first, counting the
# one this run just wrote. Until 2026-09-09 the loop kept KEEP+1 ("the 7
# newest plus the one just written" = 8 files), which contradicted both this
# script's own header and docs/RETENTION.md; no reason for the +1 was ever
# recorded, so the count was made true rather than the doc (row 125).
# The tarball just written is never a deletion candidate, so BACKUP_KEEP=0
# floors at "only today's" instead of deleting this run's own output.
removed=0; i=0
while IFS= read -r old; do
  [ "$old" = "$SELF" ] && { i=$((i+1)); continue; }
  i=$((i+1)); [ "$i" -gt "$KEEP" ] && rm -f "$old" && removed=$((removed+1))
done < <(ls -t "$BACKUP_DIR"/*.tar.gz "$BACKUP_DIR"/*.tar.gz.enc 2>/dev/null)
mb="$(awk "BEGIN{printf \"%.1f\", $(stat -f %z "$SELF")/1048576}")"
if [ "$ENCRYPTED" = yes ]; then
  echo "backup: $SELF $mb MB (encrypted), $ITEMS items, $removed old removed"
else
  echo "backup: $SELF $mb MB, $ITEMS items, $removed old removed"
fi
# A partial backup exits 1 (row 125). The tarball is kept - a backup missing
# one dump is still worth far more than none - but the run must not report
# success: until 2026-09-09 it exited 0, so run.sh recorded exit 0 on its
# kind=job event, Grafana's job-failed rule could not fire, and the
# backup_partial event had no rule watching it. The exit code is the only
# signal the alerting path already reads.
if [ "$WARN" = 1 ]; then
  event backup_partial "backup partial: postgres dump missing" warn
  echo "backup: error: partial backup - tarball kept at $SELF, exiting 1" >&2
  exit 1
fi
event backup_done "backup ok: $SELF" info
exit 0
