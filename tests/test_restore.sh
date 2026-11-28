#!/usr/bin/env bash
# restore.sh against a tarball produced by backup.sh (shared fixture with
# test_backup.sh): --into unpacks and lists the tree, --verify runs the
# integrity/bundle/manifest/events/deploy checks and reports sub-counts,
# --latest picks the newest tarball, a corrupted MANIFEST sha fails naming
# MANIFEST, an empty MANIFEST fails rather than passing vacuously, a MANIFEST
# entry naming a db/bundle that is missing from the tree fails by name, a
# hostile (path-traversal / absolute-path) tarball is refused before
# extraction, and no flag refuses an in-place restore.
set -u
unset CAPSULE_JOBS_DIR   # the jobs config dir comes from each case's fake home
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
source "$REPO/tests/lib_backup_fixture.sh"
make_backup_fixture "$T"

fails=0
check() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; fails=$((fails+1)); fi; }

out="$(bash "$REPO/maintenance/backup.sh")"; rc=$?
check "fixture: backup.sh wrote a tarball" '[ $rc -eq 0 ]'
TB="$T/backups/2026-09-07.tar.gz"
check "fixture: tarball exists" '[ -f "$TB" ]'

# --into
into="$T/into"
out="$(bash "$REPO/maintenance/restore.sh" "$TB" --into "$into")"; rc=$?
check "--into exits 0" '[ $rc -eq 0 ]'
check "--into extracted files onto disk" '[ -f "$into/MANIFEST.txt" ] && [ -f "$into/ops.env" ]'
check "--into prints the extracted tree" 'grep -q "MANIFEST.txt" <<<"$out" && grep -q "ops.env" <<<"$out"'

# --verify (good tarball)
out2="$(bash "$REPO/maintenance/restore.sh" "$TB" --verify)"; rc=$?
check "--verify exits 0" '[ $rc -eq 0 ]'
check "--verify prints restore-verify: ok (<n> checks; tarball date; sub-counts)" 'grep -qE "^restore-verify: ok \([0-9]+ checks; tarball [0-9]{4}-[0-9]{2}-[0-9]{2}; [0-9]+ dbs, bundle (yes|no), manifest [0-9]+ entries, infisical.env [0-9]+ bytes, infisical.sql ([0-9]+ bytes|MISSING marker), encrypted (yes|no)\)$" <<<"$out2"'
check "--verify sub-counts: 0 dbs, bundle yes, manifest 7 entries" 'grep -qF "0 dbs, bundle yes, manifest 7 entries" <<<"$out2"'
check "--verify runs five checks (check 5 = the two infisical files)" 'grep -qF "restore-verify: ok (5 checks;" <<<"$out2"'
check "--verify reports both infisical files by name and size, never contents" 'grep -qE "infisical.env [0-9]+ bytes, infisical.sql [0-9]+ bytes" <<<"$out2" && ! grep -q "ENCRYPTION_KEY" <<<"$out2" && ! grep -q "deadbeef" <<<"$out2"'
check "--verify reports the tarball's own date (its mtime day, not BACKUP_DATE)" 'grep -qF "tarball $(date -r "$TB" +%F)" <<<"$out2"'

# --latest --verify picks the newest tarball in BACKUP_DIR
out3="$(bash "$REPO/maintenance/restore.sh" --latest --verify)"; rc=$?
check "--latest --verify exits 0 and passes" '[ $rc -eq 0 ] && grep -q "restore-verify: ok (" <<<"$out3"'

# C1: launchd runs these scripts through the deployed symlink
# <config dir>/maintenance -> <repo>/maintenance. A logical
# `cd` (no -P) resolves SCRIPT_DIR/REPO through the symlink's parent instead
# of the repo - reproduce that layout and run restore.sh --latest --verify
# and backup.sh through it.
LINK="$T/deployed-maintenance"
ln -s "$REPO/maintenance" "$LINK"
outlink="$(bash "$LINK/restore.sh" --latest --verify)"; rc=$?
check "restore.sh --latest --verify through a symlinked maintenance dir: exits 0" '[ $rc -eq 0 ]'
check "restore.sh --latest --verify through a symlinked maintenance dir: still ok" 'grep -q "restore-verify: ok (" <<<"$outlink"'

BACKUP_DATE=2026-09-08 bash "$LINK/backup.sh" >/dev/null
linktb="$T/backups/2026-09-08.tar.gz"
check "backup.sh through a symlinked maintenance dir wrote a tarball" '[ -f "$linktb" ]'
mkdir -p "$T/link-check"; tar xzf "$linktb" -C "$T/link-check" MANIFEST.txt
check "backup.sh through a symlink: MANIFEST records the real capsule-corp commit, not none" 'grep -qE "^# capsule-corp: [0-9a-f]{7,}$" "$T/link-check/MANIFEST.txt" && ! grep -q "^# capsule-corp: none$" "$T/link-check/MANIFEST.txt"'
rm -f "$linktb"

# no flag: refuse an in-place restore
out4="$(bash "$REPO/maintenance/restore.sh" "$TB" 2>&1)"; rc=$?
check "no flag refuses in place, exit 2" '[ $rc -eq 2 ]'
check "refusal names 'refuse' and gives manual steps" 'grep -qi "refuse" <<<"$out4" && grep -q "Manual in-place restore procedure" <<<"$out4"'

# corrupt one sha in MANIFEST inside a re-packed copy
bad="$T/bad-extract"; mkdir -p "$bad"; tar xzf "$TB" -C "$bad"
python3 - "$bad/MANIFEST.txt" <<'PY'
import sys
path = sys.argv[1]
lines = open(path).read().splitlines()
out = []
touched = False
for line in lines:
    if not touched and line.startswith("events.sql\t"):
        f, size, sha = line.split("\t")
        sha = ("f" if sha[0] != "f" else "e") + sha[1:]
        line = "\t".join([f, size, sha])
        touched = True
    out.append(line)
assert touched, "did not find events.sql line in MANIFEST.txt"
open(path, "w").write("\n".join(out) + "\n")
PY
BADTB="$T/bad.tar.gz"
( cd "$bad" && tar czf "$BADTB" -- * )
out5="$(bash "$REPO/maintenance/restore.sh" "$BADTB" --verify 2>&1)"; rc=$?
check "corrupted MANIFEST sha: --verify fails" '[ $rc -eq 1 ]'
check "corrupted MANIFEST sha: failure names MANIFEST" 'grep -q "FAILED.*MANIFEST" <<<"$out5"'

# MANIFEST.txt with only footer lines (zero parseable entries) must fail,
# not pass vacuously
empty="$T/empty-manifest"; mkdir -p "$empty"; tar xzf "$TB" -C "$empty"
printf '# tool: sqlite3 3.51.0\n# vault: deadbee\n# capsule-corp: deadbee\n' >"$empty/MANIFEST.txt"
EMPTYTB="$T/empty-manifest.tar.gz"
( cd "$empty" && tar czf "$EMPTYTB" -- * )
out6="$(bash "$REPO/maintenance/restore.sh" "$EMPTYTB" --verify 2>&1)"; rc=$?
check "MANIFEST with only footer lines: --verify fails" '[ $rc -eq 1 ]'
check "MANIFEST with only footer lines: failure names MANIFEST" 'grep -q "FAILED.*MANIFEST" <<<"$out6"'

# MANIFEST names a kanban db that is missing from the extracted tree - checks
# 1/2 must not no-op silently past this
missdb="$T/missing-db"; mkdir -p "$missdb"; tar xzf "$TB" -C "$missdb"
printf 'capsule/boards/sample-app/kanban.db\t0\te3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855\n' >>"$missdb/MANIFEST.txt"
MISSDBTB="$T/missing-db.tar.gz"
( cd "$missdb" && tar czf "$MISSDBTB" -- * )
out7="$(bash "$REPO/maintenance/restore.sh" "$MISSDBTB" --verify 2>&1)"; rc=$?
check "MANIFEST names a capsule board db but it's missing: --verify fails" '[ $rc -eq 1 ]'
check "failure names the missing db path" 'grep -qF "MANIFEST names capsule/boards/sample-app/kanban.db but it is missing" <<<"$out7"'

# MANIFEST names vault.bundle but it is missing from the extracted tree
missbundle="$T/missing-bundle"; mkdir -p "$missbundle"; tar xzf "$TB" -C "$missbundle"
rm -f "$missbundle/vault.bundle"
MISSBUNTB="$T/missing-bundle.tar.gz"
( cd "$missbundle" && tar czf "$MISSBUNTB" -- * )
out8="$(bash "$REPO/maintenance/restore.sh" "$MISSBUNTB" --verify 2>&1)"; rc=$?
check "MANIFEST names vault.bundle but it's missing: --verify fails" '[ $rc -eq 1 ]'
check "failure names the missing bundle path" 'grep -qF "MANIFEST names vault.bundle but it is missing" <<<"$out8"'

# I4: --latest refuses a tarball whose mtime is older than 48h - a dead
# nightly backup must not read as "verified". Use a dedicated BACKUP_DIR
# holding only the stale tarball so it is unambiguously "the newest".
staledir="$T/backups-stale"; mkdir -p "$staledir"
stale="$staledir/2026-08-20.tar.gz"; cp "$TB" "$stale"; touch -t 202608200000 "$stale"
out_stale="$(BACKUP_DIR="$staledir" bash "$REPO/maintenance/restore.sh" --latest --verify 2>&1)"; rc=$?
check "--latest refuses a >48h old newest tarball, exit 1" '[ $rc -eq 1 ]'
check "--latest failure names the staleness and the tarball date" 'grep -qE "FAILED - newest tarball 2026-08-20 is older than 48h" <<<"$out_stale"'

# check 3: an extracted file MANIFEST.txt does not list must also fail
extrafile="$T/extra-file"; mkdir -p "$extrafile"; tar xzf "$TB" -C "$extrafile"
printf 'not tracked\n' >"$extrafile/state/untracked.txt"
EXTRATB="$T/extra-file.tar.gz"
( cd "$extrafile" && tar czf "$EXTRATB" -- * )
out_extra="$(bash "$REPO/maintenance/restore.sh" "$EXTRATB" --verify 2>&1)"; rc=$?
check "extracted file not listed in MANIFEST: --verify fails" '[ $rc -eq 1 ]'
check "failure names the unlisted file" 'grep -qF "not listed in MANIFEST" <<<"$out_extra" && grep -qF "state/untracked.txt" <<<"$out_extra"'

# check 5 (row 125): the two Infisical files. Each case re-packs the good
# tarball with the MANIFEST kept consistent, so checks 1-4 all pass and only
# check 5 can be what fails.
# manifest_drop <root> <path...>: remove those paths' MANIFEST.txt entries
manifest_drop() {
  local root="$1"; shift
  python3 - "$root/MANIFEST.txt" "$@" <<'PY'
import sys
mpath, drops = sys.argv[1], set(sys.argv[2:])
lines = [l for l in open(mpath).read().splitlines()
         if l.split("\t")[0] not in drops]
open(mpath, "w").write("\n".join(lines) + "\n")
PY
}
# manifest_resize <root> <path>: rewrite that path's bytes+sha from the file
manifest_resize() {
  python3 - "$1" "$2" <<'PY'
import hashlib, os, sys
root, target = sys.argv[1], sys.argv[2]
data = open(os.path.join(root, target), "rb").read()
line = f"{target}\t{len(data)}\t{hashlib.sha256(data).hexdigest()}"
mpath = os.path.join(root, "MANIFEST.txt")
out, touched = [], False
for l in open(mpath).read().splitlines():
    if l.startswith(target + "\t"):
        out.append(line); touched = True
    else:
        out.append(l)
assert touched, f"no MANIFEST line for {target}"
open(mpath, "w").write("\n".join(out) + "\n")
PY
}

noenv="$T/no-infisical-env"; mkdir -p "$noenv"; tar xzf "$TB" -C "$noenv"
rm -f "$noenv/infisical.env"; manifest_drop "$noenv" infisical.env
NOENVTB="$T/no-infisical-env.tar.gz"; ( cd "$noenv" && tar czf "$NOENVTB" -- * )
out_noenv="$(bash "$REPO/maintenance/restore.sh" "$NOENVTB" --verify 2>&1)"; rc=$?
check "infisical.env missing from the tarball: --verify fails" '[ $rc -eq 1 ]'
check "failure names infisical.env and why it matters" 'grep -qF "infisical.env missing from tarball" <<<"$out_noenv" && grep -qF "infisical.sql cannot be decrypted" <<<"$out_noenv"'

emptyenv="$T/empty-infisical-env"; mkdir -p "$emptyenv"; tar xzf "$TB" -C "$emptyenv"
: >"$emptyenv/infisical.env"; manifest_resize "$emptyenv" infisical.env
EMPTYENVTB="$T/empty-infisical-env.tar.gz"; ( cd "$emptyenv" && tar czf "$EMPTYENVTB" -- * )
out_emptyenv="$(bash "$REPO/maintenance/restore.sh" "$EMPTYENVTB" --verify 2>&1)"; rc=$?
check "infisical.env present but empty (sha consistent): --verify fails" '[ $rc -eq 1 ]'
check "failure says infisical.env is empty" 'grep -qF "infisical.env is present but empty" <<<"$out_emptyenv"'

nosql="$T/no-infisical-sql"; mkdir -p "$nosql"; tar xzf "$TB" -C "$nosql"
rm -f "$nosql/infisical.sql"; manifest_drop "$nosql" infisical.sql
NOSQLTB="$T/no-infisical-sql.tar.gz"; ( cd "$nosql" && tar czf "$NOSQLTB" -- * )
out_nosql="$(bash "$REPO/maintenance/restore.sh" "$NOSQLTB" --verify 2>&1)"; rc=$?
check "infisical.sql gone with no MISSING marker: --verify fails" '[ $rc -eq 1 ]'
check "failure names infisical.sql and its MISSING marker" 'grep -qF "neither infisical.sql nor infisical.sql.MISSING present" <<<"$out_nosql"'

trunc="$T/truncated-infisical-sql"; mkdir -p "$trunc"; tar xzf "$TB" -C "$trunc"
printf -- '-- PostgreSQL database dump\nCREATE TABLE secrets (id int);\n' >"$trunc/infisical.sql"
manifest_resize "$trunc" infisical.sql
TRUNCTB="$T/truncated-infisical-sql.tar.gz"; ( cd "$trunc" && tar czf "$TRUNCTB" -- * )
out_trunc="$(bash "$REPO/maintenance/restore.sh" "$TRUNCTB" --verify 2>&1)"; rc=$?
check "truncated infisical.sql (sha consistent): --verify fails" '[ $rc -eq 1 ]'
check "failure names the missing pg_dump completion marker" 'grep -qF "infisical.sql does not end with the pg_dump completion marker" <<<"$out_trunc"'

# a backup taken while infisical-db was down is an honest partial: the
# MISSING marker is accepted (that night's backup.sh exits 1 and job-failed
# owns the alert), and the summary says so instead of a byte count.
export FAKE_INFISICAL_PG_DOWN=1
BACKUP_DATE=2026-09-06 bash "$REPO/maintenance/backup.sh" >/dev/null 2>&1
unset FAKE_INFISICAL_PG_DOWN
PARTTB="$T/backups/2026-09-06.tar.gz"
out_part="$(bash "$REPO/maintenance/restore.sh" "$PARTTB" --verify 2>&1)"; rc=$?
check "infisical.sql.MISSING marker: --verify still passes" '[ $rc -eq 0 ]'
check "summary reports the MISSING marker, not a byte count" 'grep -qF "infisical.sql MISSING marker" <<<"$out_part"'
rm -f "$PARTTB"

# an archive made while the old store existed still holds kanban/ and profiles/ entries
# (listed in its MANIFEST): --verify must pass and --into must extract them.
oldx="$T/old-store"; mkdir -p "$oldx"; tar xzf "$TB" -C "$oldx"
mkdir -p "$oldx/kanban/sample-app" "$oldx/profiles/dev"
sqlite3 "$oldx/kanban/sample-app/kanban.db" "create table tasks(id text);"
printf 'soul\n' >"$oldx/profiles/dev/SOUL.md"; printf 'not yaml\n' >"$oldx/profiles/dev/config.yaml"
for f in kanban/sample-app/kanban.db profiles/dev/SOUL.md profiles/dev/config.yaml; do
  printf '%s\t%s\t%s\n' "$f" "$(wc -c <"$oldx/$f" | tr -d ' ')" "$(shasum -a 256 "$oldx/$f" | cut -d' ' -f1)" >>"$oldx/MANIFEST.txt"
done
OLDTB="$T/old-store.tar.gz"; ( cd "$oldx" && tar czf "$OLDTB" -- * )
out_old="$(bash "$REPO/maintenance/restore.sh" "$OLDTB" --verify 2>&1)"; rc=$?
check "old archive with kanban/ and profiles/ entries: --verify still passes" '[ $rc -eq 0 ] && grep -q "restore-verify: ok (" <<<"$out_old"'
out_oldi="$(bash "$REPO/maintenance/restore.sh" "$OLDTB" --into "$T/old-into" 2>&1)"; rc=$?
check "old archive with kanban/ and profiles/ entries: --into exits 0" '[ $rc -eq 0 ] && [ -f "$T/old-into/profiles/dev/SOUL.md" ]'

# hostile tarball: a path-traversal entry must be refused before extraction,
# for both --into and --verify
hostile_trav="$T/hostile-traversal.tar.gz"
python3 - "$hostile_trav" <<'PY'
import sys, tarfile, io
data = b"pwned\n"
with tarfile.open(sys.argv[1], "w:gz") as tf:
    info = tarfile.TarInfo(name="../evil.txt")
    info.size = len(data)
    tf.addfile(info, io.BytesIO(data))
PY
outdir="$T/hostile-out"
out9="$(bash "$REPO/maintenance/restore.sh" "$hostile_trav" --into "$outdir" 2>&1)"; rc=$?
check "path-traversal tarball --into refused, exit 1" '[ $rc -eq 1 ] && ! [ -e "$T/evil.txt" ]'
check "path-traversal tarball: error names the entry" 'grep -qi "hostile tarball entry" <<<"$out9" && grep -q "\.\./evil.txt" <<<"$out9"'
out10="$(bash "$REPO/maintenance/restore.sh" "$hostile_trav" --verify 2>&1)"; rc=$?
check "path-traversal tarball --verify refused, exit 1" '[ $rc -eq 1 ]'

# hostile tarball: an absolute-path entry must also be refused
hostile_abs="$T/hostile-abs.tar.gz"
python3 - "$hostile_abs" <<'PY'
import sys, tarfile, io
data = b"pwned\n"
with tarfile.open(sys.argv[1], "w:gz") as tf:
    info = tarfile.TarInfo(name="/tmp/evil.txt")
    info.size = len(data)
    tf.addfile(info, io.BytesIO(data))
PY
out11="$(bash "$REPO/maintenance/restore.sh" "$hostile_abs" --into "$T/hostile-abs-out" 2>&1)"; rc=$?
check "absolute-path tarball --into refused, exit 1" '[ $rc -eq 1 ]'
check "absolute-path tarball: error names the entry" 'grep -qi "hostile tarball entry" <<<"$out11" && grep -q "/tmp/evil.txt" <<<"$out11"'

# --- M210 (row 137): an encrypted tarball verifies exactly like a plain one --
KEYF="$T/enc.key"; printf 'fixture-not-a-real-key-0000000000000000000000\n' >"$KEYF"; chmod 600 "$KEYF"
rm -f "$T/backups"/*.tar.gz*
BACKUP_ENC_KEY_FILE="$KEYF" OPS_BACKUP_ENCRYPT=1 bash "$REPO/maintenance/backup.sh" >/dev/null 2>&1
enc="$T/backups/2026-09-07.tar.gz.enc"
check "the encrypted run left only a ciphertext" '[ -f "$enc" ] && [ -z "$(ls "$T/backups"/*.tar.gz 2>/dev/null)" ]'

# C9 fix wave 1 item 3: a failing mktemp must die before anything is written,
# not fall through with an empty/garbage $KD.
mkdir -p "$T/fakebin-mktemp" "$T/mktemp-fail-cwd"
cat >"$T/fakebin-mktemp/mktemp" <<'EOF'
#!/usr/bin/env bash
exit 1
EOF
chmod +x "$T/fakebin-mktemp/mktemp"
before_mk="$(find "$T/mktemp-fail-cwd" -mindepth 1 2>/dev/null | wc -l | tr -d ' ')"
outMK="$(cd "$T/mktemp-fail-cwd" && PATH="$T/fakebin-mktemp:$PATH" BACKUP_ENC_KEY_FILE="$KEYF" bash "$REPO/maintenance/restore.sh" "$enc" --verify 2>&1)"; rc=$?
after_mk="$(find "$T/mktemp-fail-cwd" -mindepth 1 2>/dev/null | wc -l | tr -d ' ')"
check "a failing mktemp dies before writing anything, cwd untouched" \
  '[ $rc -ne 0 ] && [ "$before_mk" = "$after_mk" ] && grep -qiF "mktemp failed" <<<"$outMK"'

outE="$(BACKUP_ENC_KEY_FILE="$KEYF" bash "$REPO/maintenance/restore.sh" "$enc" --verify 2>&1)"; rc=$?
check "verify of a .enc: ok, five checks, encrypted yes" '[ $rc -eq 0 ] && grep -qE "^restore-verify: ok \(5 checks;.*encrypted yes" <<<"$outE"'
check "verify of a .enc printed no key material" '! grep -qF "fixture-not-a-real-key" <<<"$outE"'
check "verify of a .enc left no plaintext beside the ciphertext" '[ -z "$(ls "$T/backups"/*.tar.gz 2>/dev/null)" ]'

outL="$(BACKUP_ENC_KEY_FILE="$KEYF" bash "$REPO/maintenance/restore.sh" --latest --verify 2>&1)"; rc=$?
check "--latest finds the .enc when it is the newest file" '[ $rc -eq 0 ] && grep -q "encrypted yes" <<<"$outL"'

# a plaintext tarball still says "encrypted no"
BACKUP_DATE=2026-09-08 bash "$REPO/maintenance/backup.sh" >/dev/null 2>&1
outP="$(bash "$REPO/maintenance/restore.sh" "$T/backups/2026-09-08.tar.gz" --verify 2>&1)"; rc=$?
check "verify of a plaintext tarball: ok, encrypted no" '[ $rc -eq 0 ] && grep -q "encrypted no" <<<"$outP"'

outW="$(BACKUP_ENC_KEY_FILE="$T/wrong.key" bash "$REPO/maintenance/restore.sh" "$enc" --verify 2>&1)"; rc=$?
check "verify with no usable key: FAILED, exit 1, no ok line" '[ $rc -eq 1 ] && grep -qF "restore-verify: FAILED - cannot decrypt" <<<"$outW" && ! grep -q "restore-verify: ok" <<<"$outW"'

mkdir -p "$T/into-enc" "$T/scratch-tmpdir"
# scope TMPDIR to a private, otherwise-empty dir for this one invocation so
# any leftover restore.sh mktemp -d can be detected without noise from other
# concurrent processes' own tmp.* dirs sharing the system TMPDIR
outI="$(TMPDIR="$T/scratch-tmpdir" BACKUP_ENC_KEY_FILE="$KEYF" bash "$REPO/maintenance/restore.sh" "$enc" --into "$T/into-enc" 2>&1)"; rc=$?
check "--into a .enc decrypts and extracts into the operator's dir" '[ $rc -eq 0 ] && [ -f "$T/into-enc/MANIFEST.txt" ] && grep -q "^MANIFEST.txt$" <<<"$outI"'
check "--into a .enc leaves no plaintext tarball in the operator's dir" '[ -z "$(find "$T/into-enc" -maxdepth 1 \( -name "*.tar.gz" -o -name "*.tar.gz.enc" \) 2>/dev/null)" ]'
check "--into a .enc leaves no leftover private temp dir behind" '[ -z "$(find "$T/scratch-tmpdir" -mindepth 1 2>/dev/null)" ]'

outR="$(bash "$REPO/maintenance/restore.sh" "$enc" 2>&1)"; rc=$?
check "the refusal text carries the openssl decrypt one-liner and names the printed sheet" \
  '[ $rc -eq 2 ] && grep -qF "openssl enc -d -aes-256-cbc -md sha512 -pbkdf2 -iter 600000" <<<"$outR" && grep -qiF "printed key" <<<"$outR"'

[ $fails -eq 0 ] && echo "test_restore: all passed" || { echo "test_restore: $fails failed"; exit 1; }
