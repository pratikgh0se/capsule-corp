#!/usr/bin/env bash
# backup.sh against a fake HOME: tarball content, no secrets, manifest shas,
# retention, Postgres-down path, summary line.
set -u
unset CAPSULE_JOBS_DIR   # the jobs config dir comes from each case's fake home
perm() { if stat -c %a / >/dev/null 2>&1; then stat -c %a "$1"; else stat -f %Lp "$1"; fi; }  # GNU stat first (its -f means filesystem), else BSD/macOS
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
source "$REPO/tests/lib_backup_fixture.sh"
make_backup_fixture "$T"

fails=0
check() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; fails=$((fails+1)); fi; }

out="$(bash "$REPO/maintenance/backup.sh")"; rc=$?
check "exit 0" '[ $rc -eq 0 ]'
tb="$T/backups/2026-09-07.tar.gz"
check "tarball written 0600" '[ -f "$tb" ] && [ "$(perm "$tb")" = "600" ]'
check "backup dir 0700" '[ "$(perm "$T/backups")" = "700" ]'
list="$(tar tzf "$tb")"
check "events.sql inside" 'grep -q "^events.sql$" <<<"$list"'
check "infisical.sql inside" 'grep -q "^infisical.sql$" <<<"$list"'
check "infisical.env inside" 'grep -q "^infisical.env$" <<<"$list"'
check "no .env, no logs, no skills-curated" '! grep -qE "(^|/)\.env$" <<<"$list" && ! grep -q "logs/" <<<"$list" && ! grep -q "skills-curated" <<<"$list"'
check "approvals, state, ops.env, bundle, manifest inside" 'for p in merge-gate/approvals/sample-app/t_1.json state/notify-seen.json ops.env vault.bundle MANIFEST.txt; do grep -q "$p" <<<"$list" || exit 1; done'
mkdir -p "$T/x" && tar xzf "$tb" -C "$T/x"
check "bundle restores" 'git clone -q "$T/x/vault.bundle" "$T/x/v" && [ -f "$T/x/v/a.md" ]'
check "manifest sha matches" 'sha="$(shasum -a 256 "$T/x/events.sql" | cut -d" " -f1)"; grep -q "events.sql.*$sha" "$T/x/MANIFEST.txt"'
# row 125: exactly BACKUP_KEEP (7) tarballs on disk, counting the one just
# written - not KEEP+1. The fixture seeds 9 aged tarballs, so the run that
# writes 2026-09-07 leaves 09-07 + 08-09..08-04 and removes 08-03/02/01.
check "retention keeps exactly KEEP (7) tarballs, new one included" '[ "$(ls "$T/backups"/*.tar.gz | wc -l | tr -d " ")" = "7" ] && [ -e "$tb" ] && [ ! -e "$T/backups/2026-08-03.tar.gz" ] && [ -e "$T/backups/2026-08-04.tar.gz" ]'
check "summary line" 'grep -qE "^backup: .*2026-09-07.tar.gz [0-9.]+ MB, [0-9]+ items, 3 old removed$" <<<"$out"'

# row 125: a partial backup exits 1 (so run.sh records exit 1 and Grafana's
# job-failed rule can fire) but KEEPS the tarball it wrote.
rm -f "$tb"; out2="$(FAKE_PG_DOWN=1 bash "$REPO/maintenance/backup.sh" 2>&1)"; rc=$?
check "postgres down: exit 1 with MISSING marker and warning" '[ $rc -eq 1 ] && tar tzf "$tb" | grep -q "events.sql.MISSING" && grep -q "warn" <<<"$out2"'
check "postgres down: the tarball is kept and the partial exit is explained" '[ -f "$tb" ] && [ -s "$tb" ] && grep -q "partial backup - tarball kept" <<<"$out2"'
check "postgres down: the summary line still prints before the non-zero exit" 'grep -qE "^backup: .*2026-09-07.tar.gz [0-9.]+ MB, [0-9]+ items, [0-9]+ old removed$" <<<"$out2"'

rm -f "$tb"; out2b="$(FAKE_INFISICAL_PG_DOWN=1 bash "$REPO/maintenance/backup.sh" 2>&1)"; rc=$?
check "infisical down: exit 1 with MISSING marker and warning" '[ $rc -eq 1 ] && tar tzf "$tb" | grep -q "infisical.sql.MISSING" && grep -q "warn" <<<"$out2b"'
check "infisical down: the tarball is kept" '[ -f "$tb" ] && [ -s "$tb" ]'
check "infisical down: events.sql (unaffected container) still present" 'tar tzf "$tb" | grep -q "^events.sql$"'

# a healthy run must stay exit 0 with no partial marker in its output
rm -f "$tb"; out2c="$(bash "$REPO/maintenance/backup.sh" 2>&1)"; rc=$?
check "healthy run: exit 0, no partial line" '[ $rc -eq 0 ] && ! grep -q "partial backup" <<<"$out2c"'

out3="$(bash "$REPO/maintenance/backup.sh" --dry-run)"; rc=$?
check "dry run writes nothing" '[ $rc -eq 0 ] && grep -q "would write" <<<"$out3"'

out4="$(BACKUP_DIR=/ bash "$REPO/maintenance/backup.sh" 2>&1)"; rc=$?
check "refuses BACKUP_DIR=/" '[ $rc -eq 1 ] && grep -qF "backup: error: refusing BACKUP_DIR=/" <<<"$out4"'

out5="$(BACKUP_DIR="$H" bash "$REPO/maintenance/backup.sh" 2>&1)"; rc=$?
check "refuses BACKUP_DIR=AGENT_OPS_HOME" '[ $rc -eq 1 ] && grep -qF "backup: error: refusing BACKUP_DIR=$H" <<<"$out5" && [ ! -e "$H/2026-09-07.tar.gz" ]'

D="$T/does-not-exist/backups"
out6="$(BACKUP_DIR="$D" bash "$REPO/maintenance/backup.sh" --dry-run 2>&1)"; rc=$?
check "dry run against missing BACKUP_DIR leaves it absent" '[ $rc -eq 0 ] && [ ! -e "$D" ] && [ ! -e "$T/does-not-exist" ]'

out7="$(BACKUP_KEEP=-1 bash "$REPO/maintenance/backup.sh" 2>&1)"; rc=$?
check "refuses BACKUP_KEEP=-1, exit 1" "[ \$rc -eq 1 ] && grep -qF \"backup: error: BACKUP_KEEP must be a non-negative integer, got '-1'\" <<<\"\$out7\""

out8="$(BACKUP_KEEP=abc7 bash "$REPO/maintenance/backup.sh" 2>&1)"; rc=$?
check "refuses BACKUP_KEEP=abc7, exit 1" "[ \$rc -eq 1 ] && grep -qF \"backup: error: BACKUP_KEEP must be a non-negative integer, got 'abc7'\" <<<\"\$out8\""

# row 125: the kept count is exactly BACKUP_KEEP for any value, and
# BACKUP_KEEP=0 must still never delete the tarball this run just wrote
# (the new tarball is not a deletion candidate).
kd="$T/backups-keep"; mkdir -p "$kd"
for d in 01 02 03 04 05; do f="$kd/2026-07-$d.tar.gz"; echo x >"$f"; touch -t "202607${d}0000" "$f"; done
BACKUP_DIR="$kd" BACKUP_KEEP=3 bash "$REPO/maintenance/backup.sh" >/dev/null 2>&1; rc=$?
check "BACKUP_KEEP=3 leaves exactly 3 tarballs, the new one among them" '[ $rc -eq 0 ] && [ "$(ls "$kd"/*.tar.gz | wc -l | tr -d " ")" = "3" ] && [ -f "$kd/2026-09-07.tar.gz" ]'
kd0="$T/backups-keep0"; mkdir -p "$kd0"
echo x >"$kd0/2026-07-01.tar.gz"; touch -t 202607010000 "$kd0/2026-07-01.tar.gz"
BACKUP_DIR="$kd0" BACKUP_KEEP=0 bash "$REPO/maintenance/backup.sh" >/dev/null 2>&1; rc=$?
check "BACKUP_KEEP=0 keeps the just-written tarball and nothing else" '[ $rc -eq 0 ] && [ -f "$kd0/2026-09-07.tar.gz" ] && [ "$(ls "$kd0"/*.tar.gz | wc -l | tr -d " ")" = "1" ]'

# mv/chmod guard: pre-create $OUT as a directory so `mv "$OUT.tmp" "$OUT"`
# fails - must exit 1 with a message and never log backup_done, not report
# "backup ok" with no tarball on disk
rm -f "$tb"; mkdir -p "$tb"
out6b="$(bash "$REPO/maintenance/backup.sh" 2>&1)"; rc=$?
check "OUT pre-created as a directory: exit 1" '[ $rc -eq 1 ]'
check "OUT pre-created as a directory: error message, dir untouched" '[ -d "$tb" ] && grep -qi "backup: error:" <<<"$out6b"'
check "OUT pre-created as a directory: no summary/backup_done, die() exited first" '! grep -q "^backup: .*MB" <<<"$out6b" && ! grep -qi "backup ok" <<<"$out6b"'
chmod -R 700 "$tb" 2>/dev/null; rm -rf "$tb"

# stale *.tar.gz.tmp left by a crashed run: cleaned before retention, not
# left to accumulate
rm -f "$T/backups"/*.tar.gz.tmp
old_tmp="$T/backups/2026-08-01.tar.gz.tmp"; echo x >"$old_tmp"; touch -t 202608010000 "$old_tmp"
out9="$(bash "$REPO/maintenance/backup.sh" 2>&1)"; rc=$?
check "stale .tar.gz.tmp removed by the next run" '[ $rc -eq 0 ] && [ ! -e "$old_tmp" ]'

# --- M210 (row 137): the encryption step, behind OPS_BACKUP_ENCRYPT ---------
# The fixture key is 48 obviously-fake bytes generated here, inside $T: no real
# key, no committed key, and nothing in this file that resembles one.
KEYF="$T/enc.key"; printf 'fixture-not-a-real-key-0000000000000000000000\n' >"$KEYF"; chmod 600 "$KEYF"
enc="$T/backups/2026-09-07.tar.gz.enc"

rm -f "$tb" "$enc"
out9="$(BACKUP_ENC_KEY_FILE="$KEYF" OPS_BACKUP_ENCRYPT=1 bash "$REPO/maintenance/backup.sh" 2>&1)"; rc=$?
check "flag on: exit 0, ciphertext at 0600, plaintext gone" '[ $rc -eq 0 ] && [ -f "$enc" ] && [ "$(perm "$enc")" = "600" ] && [ ! -e "$tb" ]'
check "flag on: the summary line says encrypted and names the .enc" 'grep -qE "^backup: .*2026-09-07.tar.gz.enc [0-9.]+ MB \(encrypted\), [0-9]+ items, [0-9]+ old removed$" <<<"$out9"'
check "flag on: the ciphertext is not a tarball" '! tar tzf "$enc" >/dev/null 2>&1'
check "flag on: it decrypts back to a valid tarball with the manifest inside" \
  'openssl enc -d -aes-256-cbc -md sha512 -pbkdf2 -iter 600000 -in "$enc" -out "$T/rt.tar.gz" -pass file:"$KEYF" && tar tzf "$T/rt.tar.gz" | grep -q "^MANIFEST.txt$"'
check "flag on: a wrong key cannot decrypt it" \
  'printf "fixture-wrong-key-111111111111111111111111\n" >"$T/wrong.key"; ! openssl enc -d -aes-256-cbc -md sha512 -pbkdf2 -iter 600000 -in "$enc" -out /dev/null -pass file:"$T/wrong.key" 2>/dev/null'
check "flag on: no output line contains the fixture key" '! grep -qF "fixture-not-a-real-key" <<<"$out9"'

# retention counts BOTH extensions: the fixture seeds 9 aged plaintext
# tarballs, so the encrypted run must still leave exactly KEEP (7) files.
check "flag on: retention keeps exactly KEEP across both extensions" \
  '[ "$(ls "$T/backups"/*.tar.gz "$T/backups"/*.tar.gz.enc 2>/dev/null | wc -l | tr -d " ")" = "7" ] && [ -e "$enc" ]'

# a missing/short key must never cost us the plaintext
rm -f "$enc"; out10="$(BACKUP_ENC_KEY_FILE="$T/nope.key" OPS_BACKUP_ENCRYPT=1 bash "$REPO/maintenance/backup.sh" 2>&1)"; rc=$?
check "no key: exit 1, says the key is unavailable, plaintext kept, no .enc" \
  '[ $rc -eq 1 ] && grep -qF "encryption key unavailable" <<<"$out10" && [ -f "$tb" ] && [ ! -e "$enc" ]'
printf 'short\n' >"$T/short.key"
rm -f "$tb" "$enc"; out11="$(BACKUP_ENC_KEY_FILE="$T/short.key" OPS_BACKUP_ENCRYPT=1 bash "$REPO/maintenance/backup.sh" 2>&1)"; rc=$?
check "short key: refused the same way, plaintext kept" '[ $rc -eq 1 ] && grep -qF "encryption key unavailable" <<<"$out11" && [ -f "$tb" ]'

# a broken openssl (round trip cannot hold) must keep the plaintext
cat >"$T/bin/openssl" <<'EOF'
#!/usr/bin/env bash
# fake openssl: encrypts by copying, "decrypts" to different bytes so the
# round-trip check must fail
for a in "$@"; do [ "$a" = "-d" ] && D=1; done
prev=""; for a in "$@"; do
  [ "$prev" = "-in" ] && IN="$a"; [ "$prev" = "-out" ] && OUT="$a"; prev="$a"
done
if [ "${D:-0}" = 1 ]; then printf 'corrupted\n' >"$OUT"; else cp "$IN" "$OUT"; fi
EOF
chmod +x "$T/bin/openssl"
rm -f "$tb" "$enc"; out12="$(BACKUP_ENC_KEY_FILE="$KEYF" OPS_BACKUP_ENCRYPT=1 bash "$REPO/maintenance/backup.sh" 2>&1)"; rc=$?
check "round-trip mismatch: exit 1, plaintext kept, no .enc, no .enc.tmp" \
  '[ $rc -eq 1 ] && grep -qF "plaintext kept" <<<"$out12" && [ -f "$tb" ] && [ ! -e "$enc" ] && [ -z "$(ls "$T/backups"/*.enc.tmp 2>/dev/null)" ]'
rm -f "$T/bin/openssl"

# and the partial-backup contract survives encryption
rm -f "$tb" "$enc"; out13="$(FAKE_PG_DOWN=1 BACKUP_ENC_KEY_FILE="$KEYF" OPS_BACKUP_ENCRYPT=1 bash "$REPO/maintenance/backup.sh" 2>&1)"; rc=$?
check "flag on + partial: exit 1, ciphertext kept, marker inside it" \
  '[ $rc -eq 1 ] && [ -f "$enc" ] && grep -qF "partial backup - tarball kept" <<<"$out13" && openssl enc -d -aes-256-cbc -md sha512 -pbkdf2 -iter 600000 -in "$enc" -out "$T/rt2.tar.gz" -pass file:"$KEYF" && tar tzf "$T/rt2.tar.gz" | grep -q "events.sql.MISSING"'

check "run.sh exports OPS_BACKUP_ENCRYPT so it crosses into backup.sh" \
  'grep -qF "export OPS_BACKUP_ENCRYPT" "$REPO/maintenance/run.sh"'

# The old store is gone (D198/D202): even with a leftover legacy-home tree, nothing of it is backed up,
# and the restore help names no gateway and no legacy-home step. The old name comes from the
# private denylist; without it (public checkout) the guard is skipped.
OLD="$(python3 "$REPO/tests/denylist_term.py" retired)" || { echo "FAIL: denylist_term: release/private-denylist.txt is unusable"; exit 1; }
if [ -n "$OLD" ]; then
mkdir -p "$H/.$OLD/profiles/dev" "$H/.$OLD/kanban/boards/sample-app"
printf 'soul\n' >"$H/.$OLD/profiles/dev/SOUL.md"
sqlite3 "$H/.$OLD/kanban/boards/sample-app/kanban.db" "create table tasks(id text);"
fi
rm -f "$tb"; bash "$REPO/maintenance/backup.sh" >/dev/null 2>&1
list0="$(tar tzf "$tb")"
check "the tarball has a MANIFEST" 'grep -q "MANIFEST" <<<"$list0"'
if [ -n "$OLD" ]; then check "no kanban/ or profiles/ in the tarball even with a leftover legacy tree" '! grep -qE "^(kanban|profiles)/" <<<"$list0"'
else echo "SKIP no kanban/ or profiles/ in the tarball even with a leftover legacy tree: release/private-denylist.txt absent"; fi
rr="$(bash "$REPO/maintenance/restore.sh" "$tb" 2>&1)"
check "restore help has the manual in-place restore" 'grep -q "Manual in-place restore" <<<"$rr"'
if [ -n "$OLD" ]; then check "restore help names no gateway and no legacy-home step" '! grep -qE "ai\.$OLD|\.$OLD/" <<<"$rr"'
else echo "SKIP restore help names no gateway and no legacy-home step: release/private-denylist.txt absent"; fi

[ $fails -eq 0 ] && echo "test_backup: all passed" || { echo "test_backup: $fails failed"; exit 1; }
