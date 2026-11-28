# backup_key.sh - the one door to the nightly tarball's encryption key (spec
# docs/superpowers/specs/2026-09-10-continuity-and-data-safety-design.md
# section 7.1, decision row 137). SOURCED by backup.sh and restore.sh; it has
# no top-level side effect and is not meant to be executed.
#
# The key is a secret and is treated like one (row 99): read at run time
# through Potara, written straight into a mode-0600 file inside the caller's
# own `mktemp -d`, and never turned into a shell variable, a log line, an
# event payload or a committed fixture. Nothing here ever prints the key, its
# length, a prefix or a hash of it - the only failure message is
# "encryption key unavailable".
#
# BACKUP_ENC_KEY_FILE is the break-glass door: the printed key sheet retyped
# into a scratch file. It is also the only door the tests use, and it is why a
# restore never requires Infisical to be up.
#
# openssl `enc` has no authenticated mode worth using, so integrity does not
# come from a tag: it comes from backup.sh's decrypt-and-compare round trip
# before the plaintext is deleted, and from MANIFEST.txt *inside* the tarball.
# An `age` swap (row 137: "a later swap") replaces backup_enc/backup_dec and
# the file extension, and nothing else.

BACKUP_ENC_ARGS=(-aes-256-cbc -md sha512 -pbkdf2 -iter 600000 -salt)
BACKUP_ENC_PROJECT="${BACKUP_ENC_PROJECT:-capsule-corp}"
BACKUP_ENC_SECRET="${BACKUP_ENC_SECRET:-BACKUP_ENC_KEY}"
# -P: backup.sh and restore.sh are both reached through the deployed symlink
# <config dir>/maintenance -> <repo>/maintenance, so a
# logical .. would miss the repo's bin/ entirely (backup.sh:11-15).
: "${CAPSULE_SECRET_BIN:=$(cd -P "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd -P)/bin/capsule-secret}"

backup_key_write() {  # <dest>: 0 and a 0600 single-line key file, or 1
  local dest="$1" src="${BACKUP_ENC_KEY_FILE:-}" n
  ( umask 077; : >"$dest" ) || return 1
  if [ -n "$src" ]; then
    [ -f "$src" ] || return 1
    ( umask 077; head -n1 "$src" >"$dest" ) || return 1
  else
    [ -x "$CAPSULE_SECRET_BIN" ] || return 1
    # stdout straight into the file: the value never passes through a variable
    # or a command substitution, so it cannot reach a log or a `set -x` trace.
    ( umask 077; CAPSULE_IDENTITY="${CAPSULE_IDENTITY:-bridge}" \
        "$CAPSULE_SECRET_BIN" get "$BACKUP_ENC_PROJECT" "$BACKUP_ENC_SECRET" >"$dest" ) || return 1
  fi
  chmod 600 "$dest" || return 1
  # >=32 characters. A one-line typo of a passphrase must not silently become
  # the thing standing between the store and the disk.
  n="$(tr -d '\r\n' <"$dest" | wc -c | tr -d ' ')"
  [ "${n:-0}" -ge 32 ] || return 1
  return 0
}

# openssl's own stderr is passed through for a human to read, never captured
# into an event (bin/capsule-secret:6-8's rule: it can echo the invocation).
backup_enc() { openssl enc "${BACKUP_ENC_ARGS[@]}" -in "$1" -out "$2" -pass file:"$3"; }
backup_dec() { openssl enc -d "${BACKUP_ENC_ARGS[@]}" -in "$1" -out "$2" -pass file:"$3"; }
