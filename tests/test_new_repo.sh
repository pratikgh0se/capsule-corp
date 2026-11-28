#!/usr/bin/env bash
# new-repo.sh against a fake gh, a temp HOME, a temp vault and a temp
# registry copy (its own tiny git repo, standing in for capsule-corp) - spec
# §5 (docs/superpowers/specs/2026-09-12-new-repo-verb-design.md). Never
# touches the real ~/CodeBase, the notes vault, or this checkout's own
# maintenance/repos.json; never calls a real gh.
set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
fails=0
check() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; fails=$((fails+1)); fi; }

mkdir -p "$T/home/CodeBase" "$T/vault/Boards" "$T/reporoot/maintenance" "$T/bin" "$T/gh-repos"

# A registry copy that stands in for capsule-corp's own maintenance/repos.json,
# inside its own throwaway git repo - so the "commit in capsule-corp" step (§3
# of the design) has something real to commit into, and the real repo is
# never touched.
cat >"$T/reporoot/maintenance/repos.json" <<'JSON'
[
  {"name": "capsule-corp", "path": "~/CodeBase/capsule-corp", "url": "https://example.test/capsule-corp.git",
   "board": "capsule-corp", "default_branch": "main", "bet": null}
]
JSON
( cd "$T/reporoot" && git init -q -b main && git config user.email t@t.example \
    && git config user.name test && git add -A && git commit -q -m init )

cat >"$T/bin/gh" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$GH_LOG"
name="$3"
case "$1 $2" in
  "repo view")
    [ -f "$GH_REPOS_DIR/$name" ] && exit 0 || exit 1 ;;
  "repo create")
    mkdir -p "$(dirname "$GH_REPOS_DIR/$name")"
    [ "${FAKE_GH_FAIL:-}" = 1 ] && exit 1
    touch "$GH_REPOS_DIR/$name"; exit 0 ;;
esac
exit 0
SH
chmod +x "$T/bin/gh"

export CAPSULE_GH_OWNER=owner AGENT_OPS_HOME="$T/home" OBSIDIAN_VAULT_PATH="$T/vault" \
       AGENT_OPS_REPOS_JSON="$T/reporoot/maintenance/repos.json" \
       GH_BIN="$T/bin/gh" CAPSULE_HOME="$T/capsule" \
       GH_LOG="$T/gh.log" GH_REPOS_DIR="$T/gh-repos"
mkdir -p "$T/capsule"
printf '{"owner": {"name": "Alex"}}\n' >"$T/capsule/host.json"
: >"$GH_LOG"

D="$T/home/CodeBase/widget"
out="$(bash "$REPO/bin/new-repo.sh" widget --license MIT --board widget-board --bet bet-slug 2>&1)"; rc=$?
check "apply exits 0" '[ $rc -eq 0 ]'

check "LICENSE has the configured owner name and the year" \
  "grep -q 'Copyright (c) $(date +%Y) Alex' \"$D/LICENSE\""
check "README has the repo name, no placeholder" \
  "grep -q '^# widget\$' \"$D/README.md\" && ! grep -q __NAME__ \"$D/README.md\""
check "README says how to run tests" "grep -q 'unittest discover tests' \"$D/README.md\""
check "CHANGELOG starts # Changelog / ## Unreleased" \
  "head -1 \"$D/CHANGELOG.md\" | grep -qx '# Changelog' && grep -qx '## Unreleased' \"$D/CHANGELOG.md\""
check "tests/test_smoke.py exists and is a real test" "grep -q 'class Smoke' \"$D/tests/test_smoke.py\""
check "verify.yml copied verbatim from the template" "cmp -s \"$REPO/maintenance/templates/verify.yml\" \"$D/.github/workflows/verify.yml\""
check "docs/CONVENTIONS.md copied verbatim from repo-conventions.md" "cmp -s \"$REPO/maintenance/templates/repo-conventions.md\" \"$D/docs/CONVENTIONS.md\""
check ".gitignore copied verbatim from the python template" "cmp -s \"$REPO/maintenance/templates/gitignore-python\" \"$D/.gitignore\""

check "git init -b main" "[ \"\$(git -C \"$D\" symbolic-ref --short HEAD)\" = main ]"
check "one commit with the exact message" \
  "[ \"\$(git -C \"$D\" log --oneline | wc -l | tr -d ' ')\" = 1 ] && git -C \"$D\" log -1 --format=%s | grep -qx 'chore: new repo from the Capsule template'"

check "gh repo create ran with --private, --source, --push" \
  "grep -qF 'repo create owner/widget --private --source . --push' \"$GH_LOG\""
check "gh was never asked to make a public repo" '! grep -q -- "--public" "$GH_LOG"'

check "registry gained widget, stays sorted by name, stays valid JSON" \
  "python3 -c \"
import json
repos = json.load(open('$T/reporoot/maintenance/repos.json'))
names = [r['name'] for r in repos]
assert names == sorted(names), names
assert 'widget' in names
w = [r for r in repos if r['name'] == 'widget'][0]
assert w['board'] == 'widget-board', w
assert w['bet'] == 'bet-slug', w
assert w['default_branch'] == 'main', w
assert w['path'] == '~/CodeBase/widget', w
assert w['url'] == 'https://github.com/owner/widget.git', w
\""
check "the registry change is committed in the registry's own repo" \
  "git -C \"$T/reporoot\" log --oneline | grep -q 'repos.json: add widget'"
check "new-repo.sh never pushes the registry repo (source has no git push)" \
  '! grep -qE "git( -C [^ ]+)? push" "'"$REPO"'/bin/new-repo.sh"'

BDB="$T/capsule/boards/widget-board/kanban.db"
check "a Capsule board db was created for the board" "[ -s \"$BDB\" ]"
check "the board db has the tasks table" \
  "sqlite3 \"$BDB\" 'select count(*) from tasks' >/dev/null"

OB="$T/vault/Boards/Order Book.md"
check "Order Book gained one opportunity line, dictated: new-repo" \
  "grep -qF '**widget** (bet: bet-slug)' \"\$OB\" && grep -qF '(dictated: new-repo)' \"\$OB\""

check "step 7 prints both follow-ups" \
  'grep -qF "./deploy.sh" <<<"$out" && grep -qF "bootstrap.sh --check" <<<"$out"'

# --- idempotency: a second run changes nothing and exits 0 --------------
snapshot() { find "$D" -type f | sort | while IFS= read -r f; do cksum "$f"; done; }
before="$(snapshot)"
gh_calls_before="$(grep -c 'repo create' "$GH_LOG")"
out2="$(bash "$REPO/bin/new-repo.sh" widget --license MIT --board widget-board --bet bet-slug 2>&1)"; rc2=$?
after="$(snapshot)"
check "second run exits 0" '[ $rc2 -eq 0 ]'
check "second run changes no file under the repo" '[ "$before" = "$after" ]'
check "second run does not call gh repo create again" \
  '[ "$(grep -c "repo create" "$GH_LOG")" = "'"$gh_calls_before"'" ]'
check "second run does not duplicate the registry entry" \
  "[ \"\$(python3 -c \"import json; print(sum(1 for r in json.load(open('$T/reporoot/maintenance/repos.json')) if r['name']=='widget'))\")\" = 1 ]"
check "second run does not duplicate the board" "[ \"\$(ls \"$T/capsule/boards\" | wc -l | tr -d ' ')\" = 1 ]"
check "second run does not duplicate the Order Book line" "[ \"\$(grep -c 'widget.*dictated: new-repo' \"\$OB\")\" = 1 ]"

# --- a --board that climbs out of the boards directory is refused ---------
out4="$(bash "$REPO/bin/new-repo.sh" evil --license none --no-github --board ../../x 2>&1)"; rc4=$?
check "bad --board exits 2" '[ $rc4 -eq 2 ]'
check "bad --board wrote nothing outside boards" '[ ! -e "$T/x" ] && [ ! -e "$T/home/CodeBase/evil" ]'

# --- --license none writes no LICENSE ------------------------------------
D2="$T/home/CodeBase/nolicense"
out3="$(bash "$REPO/bin/new-repo.sh" nolicense --license none --no-github 2>&1)"; rc3=$?
check "--license none run exits 0" '[ $rc3 -eq 0 ]'
check "--license none writes no LICENSE" '[ ! -e "'"$D2"'/LICENSE" ]'
check "--license none still writes the other template files" '[ -f "'"$D2"'/README.md" ] && [ -f "'"$D2"'/CHANGELOG.md" ]'

# --- --no-github prints the command and calls no gh ----------------------
: >"$GH_LOG"
check "--no-github called no gh" '[ ! -s "$GH_LOG" ]'
check "--no-github printed the command as the next step" \
  'grep -qF "gh repo create owner/nolicense --private --source" <<<"$out3" && grep -qF -- "--no-github: run this yourself" <<<"$out3"'

# --- --check on a hand-made repo missing CI ------------------------------
D3="$T/home/CodeBase/handmade"
mkdir -p "$D3"
printf '# handmade\n' >"$D3/README.md"
out4="$(bash "$REPO/bin/new-repo.sh" --check handmade --license MIT 2>&1)"; rc4=$?
check "--check on an incomplete repo exits 1" '[ $rc4 -eq 1 ]'
check "--check reports MISS for the missing CI workflow" \
  'grep -qF "MISS   '"$D3"'/.github/workflows/verify.yml" <<<"$out4"'
check "--check writes nothing" '[ ! -e "'"$D3"'/.git" ] && [ ! -f "'"$D3"'/.github/workflows/verify.yml" ]'

# --- --check with no --license skips the LICENSE audit --------------------
out5="$(bash "$REPO/bin/new-repo.sh" --check widget --board widget-board 2>&1)"; rc5=$?
check "--check without --license does not require it" '[ $rc5 -eq 0 ]'
check "--check without --license skips the LICENSE audit" \
  'grep -qF "skip   $D/LICENSE audit (no --license given)" <<<"$out5"'

# --- the LICENSE holder: owner.copyright, else owner.name, else "owner" -----
holder_run() {   # holder_run <repo name> <host.json text, or empty for none>
  mkdir -p "$T/holder-$1"
  [ -z "$2" ] || printf '%s\n' "$2" >"$T/holder-$1/host.json"
  CAPSULE_HOME="$T/holder-$1" bash "$REPO/bin/new-repo.sh" "$1" --license MIT --no-github >"$T/holder-$1.out" 2>&1
}
holder_run held '{"owner": {"name": "Alex", "copyright": "Alex Example"}}'
check "owner.copyright wins over owner.name" \
  "grep -q 'Copyright (c) $(date +%Y) Alex Example\$' \"$T/home/CodeBase/held/LICENSE\""
holder_run named '{"owner": {"name": "Alex"}}'
check "only owner.name set: the name is the holder" \
  "grep -q 'Copyright (c) $(date +%Y) Alex\$' \"$T/home/CodeBase/named/LICENSE\""
holder_run bare ''
check "neither set: the holder is owner, with a NOTE" \
  "grep -q 'Copyright (c) $(date +%Y) owner\$' \"$T/home/CodeBase/bare/LICENSE\" && grep -q '^NOTE.*owner.copyright' \"$T/holder-bare.out\""

echo; if [ $fails -eq 0 ]; then echo "test_new_repo: all passed"; else echo "test_new_repo: $fails failed"; exit 1; fi
