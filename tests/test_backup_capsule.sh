#!/usr/bin/env bash
# C20: backup.sh carries the Capsule boards; restore.sh --verify proves the restored
# board (capsule check --board), and fails on a bad one.
set -u
unset CAPSULE_JOBS_DIR   # the jobs config dir comes from each case's fake home
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
source "$REPO/tests/lib_backup_fixture.sh"
make_backup_fixture "$T"; export BACKUP_MIN_FREE_KB=0
fails=0
check() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; fails=$((fails+1)); fi; }

CH="$T/capsule-home"; export CAPSULE_HOME="$CH"
mkdir -p "$CH/boards/pomo" "$CH/queue/repo" "$CH/human" "$CH/intake"
( cd "$REPO" && python3 - "$CH" "$T" <<'PY'
import json, sys
from pathlib import Path
from capsule import db, paths
home, tmp = Path(sys.argv[1]), sys.argv[2]
db.create_board(paths.board_db("pomo", home))
cfg = json.loads((paths.REPO / "capsule" / "config.example.json").read_text())
cfg["repo_url"] = tmp; cfg["board"] = "pomo"
(home / "boards" / "pomo" / "config.json").write_text(json.dumps(cfg))
PY
) || { echo "FAIL fixture: capsule board"; exit 1; }
cp "$CH/boards/pomo/config.json" "$CH/config.json"
echo '{"board":"pomo"}' >"$CH/handovers.jsonl"; echo '{}' >"$CH/queue/repo/owners.json"
echo h >"$CH/human/pomo.jsonl"; echo i >"$CH/intake/in.json"

bash "$REPO/maintenance/backup.sh" >/dev/null; rc=$?
TB="$T/backups/2026-09-07.tar.gz"
list="$(tar tzf "$TB")"
check "backup exits 0" '[ $rc -eq 0 ]'
for p in capsule/boards/pomo/kanban.db capsule/boards/pomo/config.json capsule/handovers.jsonl \
         capsule/config.json capsule/queue/repo/owners.json capsule/human/pomo.jsonl capsule/intake/in.json; do
  check "tarball holds $p" 'grep -qx "$p" <<<"$list"'
done

# the manual restore table must cover every top-level capsule/ path the tarball holds
refuse="$(bash "$REPO/maintenance/restore.sh" "$TB" 2>&1)"
tops="$(grep '^capsule/' <<<"$list" | cut -d/ -f2 | sort -u)"
check "tarball has capsule/ top-level paths" '[ -n "$tops" ]'
for t in $tops; do
  check "restore table covers capsule/$t" 'grep -qE "^  capsule/$t(/| |$)" <<<"$refuse"'
done

# the restored pair needs a legacy marker only inside restore; verify must pass on a good tarball
out="$(bash "$REPO/maintenance/restore.sh" "$TB" --verify)"; rc=$?
check "verify ok with Capsule board" '[ $rc -eq 0 ] && grep -qF "restore-verify: ok (6 checks;" <<<"$out"'
check "verify counts the capsule db" 'grep -qF "1 dbs," <<<"$out"'
mkdir -p "$T/cx" && tar xzf "$TB" -C "$T/cx"
check "sqlite copy of the capsule board is valid" '[ "$(sqlite3 "$T/cx/capsule/boards/pomo/kanban.db" "pragma integrity_check")" = "ok" ]'

# retar MUTATION: rebuild $T/bad.tar.gz from $TB after running MUTATION (shell) inside the extracted tree
X="$T/x"
retar() {
  mkdir -p "$X"; find "$X" -mindepth 1 -delete; tar xzf "$TB" -C "$X"
  ( cd "$X" && eval "$1" )
  ( cd "$X" && find . -type f ! -name MANIFEST.txt | sed 's#^\./##' | sort | while read -r f; do printf '%s\t%s\t%s\n' "$f" "$(stat -c %s "$f" 2>/dev/null || stat -f %z "$f")" "$(shasum -a 256 "$f" | cut -d' ' -f1)"; done ) >"$X/MANIFEST.txt"
  rm -f "$T/bad.tar.gz"; ( cd "$X" && tar czf "$T/bad.tar.gz" -- * )
}

# 2. per-board config present: strict path, no legacy WARN (the good tarball above)
check "verify strict with per-board config: no legacy WARN" '! grep -qF "served from legacy config" <<<"$out"'

# 1. my-app-style board: no per-board config, legacy config.json names this board -> passes with a WARN
retar 'rm capsule/boards/pomo/config.json'
out="$(bash "$REPO/maintenance/restore.sh" "$T/bad.tar.gz" --verify)"; rc=$?
check "verify passes on legacy config for the same board, with WARN" '[ $rc -eq 0 ] && grep -qF "WARN capsule/pomo: served from legacy config" <<<"$out"'

# 3. no usable per-board config and pomo is SERVED (AGENT_OPS_REPOS_JSON lists it): still fails
echo '[{"board":"pomo"}]' >"$T/repos.json"; echo '[{"board":"other"}]' >"$T/repos-other.json"
export AGENT_OPS_REPOS_JSON="$T/repos.json"
retar 'rm capsule/boards/pomo/config.json; sed -i.bak "s/\"board\": *\"pomo\"/\"board\": \"other\"/" capsule/config.json; rm capsule/config.json.bak; grep -q "\"other\"" capsule/config.json'
out="$(bash "$REPO/maintenance/restore.sh" "$T/bad.tar.gz" --verify)"; rc=$?
check "verify fails when legacy config is for a different board" '[ $rc -eq 1 ] && grep -qF "the legacy config.json is not for board pomo" <<<"$out" && ! grep -qF "served from legacy config" <<<"$out"'

# 4. no config anywhere / corrupt config, board served -> a FAIL config row, still fails
retar 'rm capsule/boards/pomo/config.json capsule/config.json'
out="$(bash "$REPO/maintenance/restore.sh" "$T/bad.tar.gz" --verify)"; rc=$?
check "verify fails when no config exists (FAIL config row)" '[ $rc -eq 1 ] && grep -qE "capsule/pomo: capsule check row .config. is not ok" <<<"$out"'
retar 'rm capsule/boards/pomo/config.json; printf "not json" >capsule/config.json'
out="$(bash "$REPO/maintenance/restore.sh" "$T/bad.tar.gz" --verify)"; rc=$?
check "verify fails on a corrupt config (FAIL config row)" '[ $rc -eq 1 ] && grep -qE "capsule/pomo: capsule check row .config. is not ok" <<<"$out"'

# 4b. same corrupt-config tarball but pomo NOT served: WARN skip, rc 0
SKIPW="restore-verify: WARN board pomo: no config.json, skipped (retired or incomplete board)"
AGENT_OPS_REPOS_JSON="$T/repos-other.json" bash "$REPO/maintenance/restore.sh" "$T/bad.tar.gz" --verify >"$T/o.txt" 2>&1; rc=$?
check "unserved board with no config: WARN skip, rc 0" '[ $rc -eq 0 ] && grep -qF "$SKIPW" "$T/o.txt"'

# 4c. the served-board lookup errors (python3 -c fails): fail closed
mkdir -p "$T/shim"; REALPY="$(command -v python3)"
printf '#!/bin/sh\ncase "$*" in *"-c"*) exit 1;; esac\nexec "%s" "$@"\n' "$REALPY" >"$T/shim/python3"; chmod +x "$T/shim/python3"
PATH="$T/shim:$PATH" AGENT_OPS_REPOS_JSON="$T/repos-other.json" bash "$REPO/maintenance/restore.sh" "$T/bad.tar.gz" --verify >"$T/o.txt" 2>&1; rc=$?
check "lookup error fails closed" '[ $rc -eq 1 ] && grep -qE "capsule/pomo: capsule check row .config. is not ok" "$T/o.txt"'

# (a) one valid board + a retired board folder (pomo served, gone not) (kanban.db, no config.json): rc 0 and the WARN line
retar 'mkdir -p capsule/boards/gone && cp capsule/boards/pomo/kanban.db capsule/boards/gone/kanban.db'
bash "$REPO/maintenance/restore.sh" "$T/bad.tar.gz" --verify >"$T/o.txt" 2>&1; rc=$?
check "valid board + retired folder: verify rc 0" '[ $rc -eq 0 ]'
check "valid board + retired folder: WARN line for gone" 'grep -qxF "restore-verify: WARN board gone: no config.json, skipped (retired or incomplete board)" "$T/o.txt"'
check "valid board + retired folder: pomo still checked" '! grep -qF "board pomo: no config.json" "$T/o.txt"'

# (b) a board WITH config.json whose check fails still fails
retar 'mkdir -p capsule/boards/gone && cp capsule/boards/pomo/kanban.db capsule/boards/gone/kanban.db; printf "not json" >capsule/boards/pomo/config.json'
bash "$REPO/maintenance/restore.sh" "$T/bad.tar.gz" --verify >"$T/o.txt" 2>&1; rc=$?
check "board with bad config.json: verify rc 1" '[ $rc -eq 1 ]'
check "board with bad config.json: names the config row" 'grep -qE "capsule/pomo: capsule check row .config. is not ok" "$T/o.txt"'

# no Capsule boards -> check 6 skipped, 5 checks
export CAPSULE_HOME="$T/none"
rm -f "$T/backups/2026-09-07.tar.gz"; bash "$REPO/maintenance/backup.sh" >/dev/null
out="$(bash "$REPO/maintenance/restore.sh" "$TB" --verify)"
check "verify skips check 6 without a Capsule board" 'grep -qF "restore-verify: ok (5 checks;" <<<"$out"'

[ "$fails" -eq 0 ] || { echo "$fails check(s) failed"; exit 1; }
echo "all passed"
