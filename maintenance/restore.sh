#!/usr/bin/env bash
# restore.sh - unpack a Capsule Corp backup tarball (see backup.sh, spec
# docs/superpowers/specs/2026-09-07-backups-design.md) into a throwaway
# directory, or --verify it end to end. Never writes to any live location
# (no AGENT_OPS_HOME, no BACKUP_DIR file is ever touched other than being
# read) - a same-machine restore into place is a manual, printed procedure.
# macOS only (BSD tar/stat, shasum, sqlite3); the scripts run only on this Mac.
#
#   restore.sh <tarball> --into <dir>   unpack the tarball under <dir>, print
#                                        the extracted file tree, exit 0
#   restore.sh <tarball> --verify       unpack into a private temp dir, run
#                                        the checks below, print
#                                        "restore-verify: ok (<n> checks; ...)"
#                                        or "restore-verify: FAILED - <what>"
#                                        (exit 1 on the first failure)
#   restore.sh <tarball>                refuse (exit 2) and print the manual
#                                        in-place restore procedure
#   restore.sh --latest [...]           use the newest *.tar.gz in BACKUP_DIR
#                                        (env, same default as backup.sh)
#                                        in place of <tarball>
#
# A <tarball> ending in .tar.gz.enc is decrypted into a private temp dir
# before anything else looks at it (row 137, M210); --verify's summary line
# reports "encrypted yes|no" accordingly.
#
# Every tarball is listed (`tar tzf`) and checked for hostile entries (a
# leading "/" or a "../" path-traversal segment) before it is ever extracted,
# in both --into and --verify.
#
# --verify checks (each counts one toward <n>, first failure stops the run).
# The final line also reports what checks 1-3 and 5 actually covered, so an
# empty tarball can't read as a clean verification:
#   restore-verify: ok (<n> checks; <k> dbs, bundle <yes|no>, manifest <m>
#   entries, infisical.env <b> bytes, infisical.sql <b> bytes|MISSING marker)
#   1. every capsule/boards/*/kanban.db present in the extracted
#      tree: `pragma integrity_check` returns "ok" (<k> = how many were
#      checked). Any such path MANIFEST.txt lists but which is missing from
#      the extracted tree fails this check by name.
#   2. if vault.bundle is present: `git clone` it and `git log -1` on the
#      clone succeeds (bundle <yes|no> in the summary). If MANIFEST.txt lists
#      vault.bundle but it is missing from the extracted tree, that fails too.
#   3. MANIFEST.txt has at least one parseable entry (<m> of them), and every
#      one's recorded sha256 AND byte count matches the extracted file - a
#      MANIFEST with only footer lines (zero parseable entries) is a failure,
#      not a vacuous pass. Also fails if the extracted tree contains a file
#      that MANIFEST.txt does not list (extra/unlisted files), and vice versa
#      (a listed file missing from the tree).
#   4. events.sql is present, non-empty, and ends with the pg_dump completion
#      marker "-- PostgreSQL database dump complete" (a truncated dump would
#      otherwise pass since its sha was computed from the same truncated
#      bytes), OR events.sql.MISSING is present (backup.sh writes one or the
#      other, never both)
#   5. the two Infisical files are in the tarball and non-empty: infisical.env
#      (required outright - it is the only copy of ENCRYPTION_KEY, and without
#      it infisical.sql is permanently undecryptable) and infisical.sql
#      (present, non-empty, ending in the pg_dump completion marker, OR
#      infisical.sql.MISSING, the same allowance check 4 makes for events.sql).
#      Both must also be listed in MANIFEST.txt. Names and byte counts only -
#      this check never reads or prints either file's contents.
#   6. (C20, only when the tarball holds capsule/boards/*): each Capsule board is
#      restored into a temp home; `capsule check --board` must show config,
#      wal_health and migrations ok. capsule/boards/*/kanban.db also joins check 1.
set -uo pipefail
# -P/pwd -P: resolve the *physical* path. launchd runs this through the
# deployed symlink <config dir>/maintenance -> <repo>/maintenance;
# a logical cd would leave REPO pointed at <config dir>,
# not the repo.
SCRIPT_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO="$(cd -P "$SCRIPT_DIR/.." && pwd -P)"
HOME_ROOT="${AGENT_OPS_HOME:-$HOME}"
BACKUP_DIR="${BACKUP_DIR:-$HOME_ROOT/Backups/capsule}"

die() { echo "restore: error: $*" >&2; exit 1; }

command -v tar >/dev/null || die "tar not found"
command -v sqlite3 >/dev/null || die "sqlite3 not found"
command -v shasum >/dev/null || die "shasum not found"
command -v git >/dev/null || die "git not found"

# refuse to extract a tarball carrying an absolute path or a path-traversal
# ("../") segment, before any tar xzf ever runs
refuse_hostile_entries() {
  local tb="$1" entry listing
  listing="$(tar tzf "$tb")" || die "cannot list $tb"
  while IFS= read -r entry; do
    [ -n "$entry" ] || continue
    case "$entry" in
      /*) die "hostile tarball entry (absolute path): $entry" ;;
    esac
    case "$entry" in
      ..|*/..|../*|*/../*) die "hostile tarball entry (path traversal): $entry" ;;
    esac
  done <<<"$listing"
}

TARBALL=""
LATEST=0
if [ "${1:-}" = "--latest" ]; then
  LATEST=1
  TARBALL="$(ls -t "$BACKUP_DIR"/*.tar.gz "$BACKUP_DIR"/*.tar.gz.enc 2>/dev/null | head -n1)"
  [ -n "$TARBALL" ] || die "no *.tar.gz or *.tar.gz.enc found in $BACKUP_DIR"
  shift
elif [ $# -ge 1 ]; then
  TARBALL="$1"; shift
fi
[ -n "$TARBALL" ] || { echo "usage: restore.sh (<tarball>|--latest) [--into <dir> | --verify]" >&2; exit 2; }
[ -f "$TARBALL" ] || die "tarball not found: $TARBALL"

MODE=""; INTO_DIR=""
case "${1:-}" in
  --into) INTO_DIR="${2:-}"; [ -n "$INTO_DIR" ] || die "--into requires a directory"; MODE=into ;;
  --verify) MODE=verify ;;
  "") MODE=refuse ;;
  *) echo "restore.sh: unknown argument $1" >&2; exit 2 ;;
esac

# --- M210 (row 137, spec section 7.2): a .tar.gz.enc is decrypted into a
# private temp dir before anything else looks at it. The checks below are
# untouched and run over the same bytes they always did; nothing ever writes a
# plaintext tarball next to the ciphertext in BACKUP_DIR. --into is the one
# exception, and only because the operator asked for the contents.
ENCRYPTED=no
if [ "$MODE" != refuse ] && case "$TARBALL" in *.tar.gz.enc) true ;; *) false ;; esac; then
  ENCRYPTED=yes
  SRC_TARBALL="$TARBALL"
  command -v openssl >/dev/null || die "openssl not found (needed for $TARBALL)"
  # shellcheck source=/dev/null
  . "$SCRIPT_DIR/backup_key.sh" || die "cannot load backup_key.sh"
  # A failing mktemp must die before anything is
  # written, not fall through with an empty/garbage $KD that a later
  # `rm -rf "$KD"` (the EXIT trap) or a write could touch outside a real
  # private temp dir.
  KD="$(mktemp -d)" || die "mktemp failed"
  [ -n "$KD" ] || die "mktemp failed"
  chmod 700 "$KD"; trap 'rm -rf "$KD"' EXIT
  PLAIN="$KD/$(basename "${TARBALL%.enc}")"
  if ! backup_key_write "$KD/enc.key"; then
    [ "$MODE" = verify ] && { echo "restore-verify: FAILED - cannot decrypt $TARBALL (encryption key unavailable)"; exit 1; }
    die "encryption key unavailable"
  fi
  if ! ( umask 077; backup_dec "$TARBALL" "$PLAIN" "$KD/enc.key" ); then
    rm -f "$PLAIN"
    [ "$MODE" = verify ] && { echo "restore-verify: FAILED - cannot decrypt $TARBALL"; exit 1; }
    die "cannot decrypt $TARBALL"
  fi
  TARBALL="$PLAIN"
fi

if [ "$MODE" = refuse ]; then
  cat <<EOF
restore: refuse to restore $TARBALL in place. Use --into <dir> to unpack it
somewhere throwaway, or --verify to check it, then copy pieces back by hand:

If the file is <date>.tar.gz.enc, decrypt it first (row 137, M210). The key is
in Potara (capsule-corp / BACKUP_ENC_KEY) and on the printed key sheet - put
one line of it in a scratch file first, mode 0600:
  openssl enc -d -aes-256-cbc -md sha512 -pbkdf2 -iter 600000 \\
    -in <date>.tar.gz.enc -out <scratch>/<date>.tar.gz -pass file:<keyfile>

Manual in-place restore procedure:

These are WAL databases: remove the live -wal/-shm sidecars before copying
the restored .db over, or sqlite may replay the stale WAL onto the fresh
file, e.g. \$CAPSULE_HOME/boards/<board>/kanban.db-wal and -shm.

Then, having extracted the tarball to a scratch dir (restore.sh <tarball> --into <scratch>):
  merge-gate/approvals/*        -> \$MERGE_GATE_STATE/approvals/  (default
                                     ~/.local/state/capsule-corp/merge-gate/approvals/)
  state/*                       -> the state root (AGENT_OPS_STATE_DIR, default
                                     ~/.local/state/capsule-corp/maint): notify-seen.json,
                                     spec-grades.json, usage-guard.json,
                                     events.jsonl, events.offset, escalated.json)
  capsule/boards/<b>/kanban.db  -> \$CAPSULE_HOME/boards/<b>/kanban.db (stop the tick,
                                     rm -wal/-shm first)
  capsule/boards/<b>/config.json -> \$CAPSULE_HOME/boards/<b>/config.json
  capsule/config.json           -> \$CAPSULE_HOME/config.json
  capsule/handovers.jsonl       -> \$CAPSULE_HOME/handovers.jsonl
  capsule/queue/*/owners.json   -> \$CAPSULE_HOME/queue/*/owners.json
  capsule/human/                -> \$CAPSULE_HOME/human/
  capsule/intake/               -> \$CAPSULE_HOME/intake/
  ops.env                       -> <config dir>/ops.env
  vault.bundle                  -> git -C <vault> pull <scratch>/vault.bundle main
  events.sql                    -> psql -U ops -d ops < <scratch>/events.sql
                                     (only if events.sql.MISSING is absent)

Restart the gateways with ./deploy.sh once files are back in place.
EOF
  exit 2
fi

if [ "$MODE" = into ]; then
  refuse_hostile_entries "$TARBALL"
  mkdir -p "$INTO_DIR" || die "cannot create $INTO_DIR"
  tar xzf "$TARBALL" -C "$INTO_DIR" || die "tar extraction failed"
  ( cd "$INTO_DIR" && find . -type f | sed 's#^\./##' | sort )
  exit 0
fi

# --verify
n=0
fail() { echo "restore-verify: FAILED - $*"; exit 1; }

tb_mtime="$(stat -f %m "${SRC_TARBALL:-$TARBALL}")"
tb_date="$(date -r "$tb_mtime" +%F)"
if [ "$LATEST" = 1 ]; then
  age_s=$(( $(date +%s) - tb_mtime ))
  [ "$age_s" -le $((48*3600)) ] || fail "newest tarball $tb_date is older than 48h"
fi

refuse_hostile_entries "$TARBALL"
W="$(mktemp -d)"; DH="$(mktemp -d)"
trap 'rm -rf "$W" "$DH" "${KD:-}"' EXIT
tar xzf "$TARBALL" -C "$W" || die "tar extraction failed"

# Parse MANIFEST.txt up front (checks 1-3 all consult it): entry lines are
# "<path>\t<bytes>\t<sha256>"; anything else (the "# tool: ..." / "# vault:
# ..." footer, and any stray non-tab-delimited line a tool's captured
# stdout/stderr might contribute there) is not a manifest entry and is
# skipped - matched strictly so footer noise can never be mistaken for a
# missing restored file, and never counted toward <m>.
[ -f "$W/MANIFEST.txt" ] || fail "MANIFEST.txt missing from tarball"
ENTRIES="$DH/manifest-entries.tsv"
grep -E $'^[^\t]+\t[0-9]+\t[0-9a-f]{64}$' "$W/MANIFEST.txt" >"$ENTRIES" || true
m="$(wc -l <"$ENTRIES" | tr -d ' ')"
[ "${m:-0}" -gt 0 ] || fail "MANIFEST.txt has no parseable entries"

# 1. sqlite integrity: every capsule/boards/*/kanban.db present
# in the extracted tree, plus: any such path MANIFEST names must actually be
# present (a listed-but-missing db is a failure, not a silently skipped one).
k=0
for db in "$W"/capsule/boards/*/kanban.db; do
  [ -f "$db" ] || continue
  res="$(sqlite3 "$db" "pragma integrity_check" 2>&1)"
  [ "$res" = "ok" ] || fail "sqlite integrity_check failed for ${db#"$W"/}: $res"
  k=$((k+1))
done
while IFS= read -r path; do
  [ -n "$path" ] || continue
  [ -f "$W/$path" ] || fail "MANIFEST names $path but it is missing"
done < <(cut -f1 "$ENTRIES" | grep -E '^capsule/boards/[^/]+/kanban\.db$')
n=$((n+1))

# 2. vault bundle: if present, it clones and has history; if MANIFEST names
# it but it is missing from the extracted tree, that is a failure too.
bundle=no
if [ -f "$W/vault.bundle" ]; then
  git clone -q "$W/vault.bundle" "$DH/.vclone" >/dev/null 2>&1 || fail "vault.bundle: git clone failed"
  git -C "$DH/.vclone" log -1 >/dev/null 2>&1 || fail "vault.bundle: git log -1 failed"
  bundle=yes
elif grep -qxF vault.bundle <(cut -f1 "$ENTRIES"); then
  fail "MANIFEST names vault.bundle but it is missing"
fi
n=$((n+1))

# 3. MANIFEST parsed above (>=1 entry, or we already failed); every entry's
# recorded sha256 AND byte count matches the extracted file, and there is no
# extracted file MANIFEST.txt does not list (extra/unlisted files) - the
# converse (a listed file missing from the tree) is caught in the same loop.
while IFS=$'\t' read -r path bytes sha; do
  f="$W/$path"
  [ -f "$f" ] || fail "MANIFEST: $path listed but missing from tarball"
  actual_bytes="$(stat -f %z "$f")"
  [ "$actual_bytes" = "$bytes" ] || fail "MANIFEST: byte count mismatch for $path (recorded $bytes, actual $actual_bytes)"
  actual="$(shasum -a 256 "$f" | cut -d' ' -f1)"
  [ "$actual" = "$sha" ] || fail "MANIFEST: sha256 mismatch for $path"
done <"$ENTRIES"
EXTRACTED="$DH/extracted-files.txt"
# sqlite sidecars (-wal/-shm/-journal) are created by our own integrity_check on
# WAL-mode databases (seen on the first live rehearsal, 2026-09-07); backup.sh
# never ships them (it uses .backup), so they are not manifest entries.
( cd "$W" && find . -type f ! -name MANIFEST.txt ! -name '*-wal' ! -name '*-shm' ! -name '*-journal' | sed 's#^\./##' | sort ) >"$EXTRACTED"
LISTED="$DH/listed-files.txt"
cut -f1 "$ENTRIES" | sort >"$LISTED"
extra="$(comm -23 "$EXTRACTED" "$LISTED")"
[ -z "$extra" ] || fail "extracted file(s) not listed in MANIFEST: $(echo "$extra" | tr '\n' ' ')"
n=$((n+1))

# 4. events.sql present, non-empty, and ends with pg_dump's completion
# marker (a truncated dump would otherwise pass, since its sha was computed
# from the same truncated bytes), or its MISSING marker
if [ -f "$W/events.sql" ]; then
  [ -s "$W/events.sql" ] || fail "events.sql present but empty"
  tail -c 200 "$W/events.sql" | grep -qF -- '-- PostgreSQL database dump complete' \
    || fail "events.sql does not end with the pg_dump completion marker (truncated dump?)"
elif [ -f "$W/events.sql.MISSING" ]; then
  :
else
  fail "neither events.sql nor events.sql.MISSING present"
fi
n=$((n+1))

# 5. the two Infisical files are actually in the tarball and are not empty
# (row 125). Without infisical.env's ENCRYPTION_KEY the infisical.sql dump is
# permanently undecryptable, and backup.sh copies infisical.env with a bare
# `[ -f ... ] && cp` - if the file is ever missing or renamed on the host, the
# tarball is silently useless for a secrets restore and every other check
# here still passes. Names and byte counts only: this never reads, prints or
# hashes either file's contents beyond the sha256 check 3 already did.
# infisical.sql may legitimately be absent behind its MISSING marker (the
# container was down), same allowance check 4 makes for events.sql - that
# night's backup exits 1 and job-failed owns the alert; infisical.env has no
# such marker path, so it is required outright.
inf_env_bytes=""
if [ -f "$W/infisical.env" ]; then
  [ -s "$W/infisical.env" ] || fail "infisical.env is present but empty - the ENCRYPTION_KEY is gone"
  inf_env_bytes="$(stat -f %z "$W/infisical.env")"
else
  fail "infisical.env missing from tarball - without it infisical.sql cannot be decrypted"
fi
grep -qxF infisical.env "$LISTED" || fail "infisical.env is not listed in MANIFEST.txt"
inf_sql="MISSING marker"
if [ -f "$W/infisical.sql" ]; then
  [ -s "$W/infisical.sql" ] || fail "infisical.sql is present but empty"
  tail -c 200 "$W/infisical.sql" | grep -qF -- '-- PostgreSQL database dump complete' \
    || fail "infisical.sql does not end with the pg_dump completion marker (truncated dump?)"
  grep -qxF infisical.sql "$LISTED" || fail "infisical.sql is not listed in MANIFEST.txt"
  inf_sql="$(stat -f %z "$W/infisical.sql") bytes"
elif [ -f "$W/infisical.sql.MISSING" ]; then
  :
else
  fail "neither infisical.sql nor infisical.sql.MISSING present"
fi
n=$((n+1))

# 6. (C20) each Capsule board in the tarball restores into a temp home and passes
# `capsule check --board` (config, db opens, migrations - the host rows docker,
# claude and the worker image say nothing about a backup).
# Skipped, and not counted, when the tarball has no Capsule boards.
cb=0
for bdb in "$W"/capsule/boards/*/kanban.db; do
  [ -f "$bdb" ] || continue
  b="$(basename "$(dirname "$bdb")")"
  CH="$DH/capsule-home"
  rm -rf "$CH"; mkdir -p "$CH"
  cp -R "$W/capsule/." "$CH/" || fail "capsule/$b: cannot copy into the temp home"
  chk="$(cd "$REPO" && AGENT_OPS_NO_DOCKER=1 PYTHONPATH="$REPO" python3 -P -m capsule check --capsule-home "$CH" --board "$b" 2>&1)"
  # config comes from boards/$b/config.json when the tarball has one (strict). A board run
  # without --board (the legacy board) has no per-board config and is served from the legacy
  # $CH/config.json (paths.resolve_config); accept that only when its `board` is $b.
  if [ -f "$W/capsule/boards/$b/config.json" ]; then
    grep -qE "^ok config: .*/boards/$b/config\.json:" <<<"$chk" \
      || fail "capsule/$b: capsule check row 'config' is not ok: $(grep -E "^[A-Za-z]+ config:" <<<"$chk" | head -n1)"
  else
    # No per-board config.json: legitimate only for a board the legacy $CH/config.json names
    # (sample-app). A board this host does not serve is retired: WARN and skip. A served board (or a
    # failed lookup: fail closed) keeps the strict failure. `check` says `FAIL config: no such board`
    # when the legacy file does not name $b; a legacy file naming another board gets the sharper message.
    grep -qE "^ok config: .*/config\.json: board $b," <<<"$chk" || {
      if served="$(cd "$REPO" && PYTHONPATH="$REPO" python3 -P -c 'import sys; from capsule import host_registry; print("\n".join(host_registry.boards_served(sys.argv[1])))' "$CH" 2>/dev/null)" \
         && ! grep -qxF "$b" <<<"$served"; then
        echo "restore-verify: WARN board $b: no config.json, skipped (retired or incomplete board)"
        continue
      fi
      { grep -E '"board"[[:space:]]*:' "$W/capsule/config.json" 2>/dev/null \
          | grep -qvE "\"board\"[[:space:]]*:[[:space:]]*\"$b\""; } \
        && fail "capsule/$b: no boards/$b/config.json and the legacy config.json is not for board $b"
      fail "capsule/$b: capsule check row 'config' is not ok: $(grep -E "^[A-Za-z]+ config:" <<<"$chk" | head -n1)"
    }
    echo "restore-verify: WARN capsule/$b: served from legacy config"
  fi
  for row in wal_health migrations; do
    grep -qE "^ok $row:" <<<"$chk" || fail "capsule/$b: capsule check row '$row' is not ok: $(grep -E "^[A-Za-z]+ $row:" <<<"$chk" | head -n1)"
  done
  cb=$((cb+1))
done
[ "$cb" -gt 0 ] && n=$((n+1))
rm -rf "$DH/capsule-home"

echo "restore-verify: ok ($n checks; tarball $tb_date; $k dbs, bundle $bundle, manifest $m entries, infisical.env $inf_env_bytes bytes, infisical.sql $inf_sql, encrypted $ENCRYPTED)"
exit 0
