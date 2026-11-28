#!/usr/bin/env bash
# capsule/lander.py CLI (C07a, t_2216591d): the dry-run landing checks on a temp repo with a bare
# remote. Exit 0 checked, 1 red, 2 held; the repo's refs and the remote's main never move.
# capsule/landing.py CLI (C07b, t_0fc44cdb), second oracle: a real push, a real refused (non-ff) push,
# a real rejected push and a real revert, against a temp bare remote under TMPDIR (never the real
# origin; nothing here touches the network).
set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
fails=0; check() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; fails=$((fails+1)); fi; }
G="git -c user.name=t -c user.email=t@t"
# The sandbox runs on a test-local fake docker (capsule/tests/fake_docker.sh) with a pinned fake image,
# so no real worker image tag has to exist (a disk cleanup removed the old one and 13 checks failed).
mkdir "$T/fakebin" "$T/fakestate"; cp "$REPO/capsule/tests/fake_docker.sh" "$T/fakebin/docker"; chmod +x "$T/fakebin/docker"
export AGENT_OPS_DOCKER_BIN="$T/fakebin" FAKE_DOCKER_STATE="$T/fakestate" FAKE_DOCKER_TRUSTED="$REPO" \
       OPS_WORKER_IMAGE=img:test OPS_ENV_FILE=/dev/null AGENT_OPS_NO_SEND=1 CAPSULE_HOME="$T/home"

git init -q --bare -b main "$T/remote.git"
git clone -q "$T/remote.git" "$T/work" 2>/dev/null
cd "$T/work" || exit 1
git checkout -q -b main; echo a > a; git add a; $G commit -qm init; git push -q origin main
git checkout -q -b epic/x; echo b > b; git add b; $G commit -qm card
printf '{"contract_commands":["echo suite"],"lander":{"gates":["true"],"deploy_check":"true"}}' > "$T/ok.json"
printf '{"contract_commands":["echo broken; exit 1"],"lander":{"gates":["true"],"deploy_check":"true"}}' > "$T/bad.json"

snap() { echo "$(git rev-parse main epic/x) $(git -C "$T/remote.git" rev-parse main)"; }
before="$(snap)"
lander() { PYTHONPATH="$REPO" python3 -m capsule.lander --repo "$T/work" --branch epic/x --config "$1"; }

out="$(lander "$T/ok.json")"; rc=$?
check "a green branch exits 0" '[ $rc -eq 0 ]'
check "the verdict says checked" 'echo "$out" | grep -q "\"verdict\": \"checked\""'
check "refs are unmoved" '[ "$(snap)" = "$before" ]'

out="$(lander "$T/bad.json")"; rc=$?
check "a red suite exits 1" '[ $rc -eq 1 ]'
check "the suite tail is in the output" 'echo "$out" | grep -q "broken"'
check "refs are unmoved after red" '[ "$(snap)" = "$before" ]'

mkdir docs; echo z > docs/ZENKAI.md; git add docs; $G commit -qm protected
out="$(lander "$T/ok.json")"; rc=$?
check "a protected path exits 2 (held)" '[ $rc -eq 2 ]'
check "the hold is named protected" 'echo "$out" | grep -q "\"hold\": \"protected\""'
check "the work tree is clean" '[ -z "$(git status --porcelain)" ]'

# ---- C07b: land mode ----
printf '{"board":"b","caps":{},"contract_commands":["echo suite"],"lander":{"mode":"land","gates":["true"],"deploy_check":"true"}}' > "$T/land.json"
printf '{"board":"b","caps":{},"contract_commands":["exit 1"],"lander":{"mode":"land","gates":["true"],"deploy_check":"true"}}' > "$T/red.json"
git clone -q "$T/remote.git" "$T/release" 2>/dev/null
PYTHONPATH="$REPO" python3 -c "from pathlib import Path; from capsule import db; db.create_board(Path('$T/board.db'))"
card_on() {   # branch, base: a branch off base with one commit; prints its card id
  git checkout -q -b "$1" "$2"; echo "$1" > "$1.txt"; git add .; $G commit -qm "$1"
  PYTHONPATH="$REPO" python3 -c "from capsule import db; c = db.connect('$T/board.db'); print(db.new_task(c, title='t', assignee='dev', branch='$1', status='review'))"
}
land() {      # task, config, action
  PYTHONPATH="$REPO" python3 -m capsule.landing "${3:-land}" --db "$T/board.db" --task "$1" --repo "$T/work" --release "$T/release" --config "$2" --home "$T/home" --no-fetch
}
old="$(git rev-parse main)"
tid="$(card_on land-one "$old")"; one="$(git rev-parse HEAD)"
out="$(land "$tid" "$T/land.json")"; rc=$?
check "land: a real ff and push exits 0" '[ $rc -eq 0 ]'
check "land: the bare remote main is the card commit" '[ "$(git -C "$T/remote.git" rev-parse main)" = "$one" ]'
check "land: the release checkout did not move (restart does that)" '[ "$(git -C "$T/release" rev-parse HEAD)" = "$old" ]'
check "land: the dev checkout work tree is clean" '[ -z "$(git status --porcelain)" ]'

# refused ff: this branch is off the OLD main, origin is already at land-one (non-ff, never forced)
tid="$(card_on land-stale "$old")"
out="$(land "$tid" "$T/land.json")"; rc=$?
check "refused ff: exit non-zero, outcome push_rejected" '[ $rc -ne 0 ] && echo "$out" | grep -q push_rejected'
check "refused ff: remote main did not move" '[ "$(git -C "$T/remote.git" rev-parse main)" = "$one" ]'

# rejected push: a rival lands on the remote while the release checkout is behind it
git clone -q "$T/remote.git" "$T/other" 2>/dev/null
echo r > "$T/other/r"; git -C "$T/other" add r; $G -C "$T/other" commit -qm rival; git -C "$T/other" push -q origin main
rival="$(git -C "$T/remote.git" rev-parse main)"
tid="$(card_on land-late "$one")"
out="$(land "$tid" "$T/land.json")"; rc=$?
check "rejected push: exit non-zero, outcome push_rejected" '[ $rc -ne 0 ] && echo "$out" | grep -q push_rejected'
check "rejected push: the rival stays on the remote (never forced)" '[ "$(git -C "$T/remote.git" rev-parse main)" = "$rival" ]'

# red after push: land on top of the rival, then verify with failing suites -> a revert commit
git fetch -q "$T/remote.git" main 2>/dev/null
tid="$(card_on land-red "$rival")"; red="$(git rev-parse HEAD)"
land "$tid" "$T/land.json" >/dev/null 2>&1
check "revert setup: the card landed" '[ "$(git -C "$T/remote.git" rev-parse main)" = "$red" ]'
out="$(land "$tid" "$T/red.json" verify)"; rc=$?
check "revert: red after push exits 0 with outcome reverted" '[ $rc -eq 0 ] && echo "$out" | grep -q reverted'
check "revert: history kept, remote main is a new commit on top" \
  'git -C "$T/remote.git" merge-base --is-ancestor "$red" main && [ "$(git -C "$T/remote.git" rev-parse main)" != "$red" ]'
check "revert: the new commit is a Revert" 'git -C "$T/remote.git" log -1 --format=%s main | grep -q Revert'
check "revert: the card file is gone from remote main" '! git -C "$T/remote.git" cat-file -e main:land-red.txt'

[ "$fails" -eq 0 ] && echo "all passed" || { echo "$fails failed"; exit 1; }
