#!/usr/bin/env bash
# new-repo.sh - one command creates a product repo from the Capsule template
# (spec docs/superpowers/specs/2026-09-12-new-repo-verb-design.md, A2 row 201).
#
#   new-repo.sh <name> --license <MIT|none> [--board <name>] [--bet <slug>] [--no-github]
#   new-repo.sh --check <name> [--license <MIT|none>]   audit all seven steps, write nothing
#
# Run by the owner, never by the controller: step 3 creates a GitHub repo and
# step 5 creates a Capsule board - both his actions, and step 5 is classifier-
# blocked for the controller anyway. Each step is idempotent and printed in
# bootstrap.sh's say style (ok/WROTE/skip/MISS/NOTE/FAILED).
#
# It never pushes capsule-corp, never creates a public repo, never deletes.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"   # this capsule-corp checkout
: "${AGENT_OPS_HOME:=$HOME}"
. "$REPO/maintenance/lib_vault.sh"
vault_resolve "$REPO"      # OBSIDIAN_VAULT_PATH, else host.json vault.path, else empty: no Order Book step
: "${GH_BIN:=gh}"
REG="${AGENT_OPS_REPOS_JSON:-$REPO/maintenance/repos.json}"
# The copyright holder of a generated LICENSE: host.json `owner.copyright`, else `owner.name`,
# else "owner" (with a NOTE on stderr, so a typo in host.json is not silent).
host_get() { (cd "$REPO" && PYTHONPATH="$REPO" python3 -sP -m capsule host get "$1" --only 2>/dev/null) || true; }
HOLDER="$(host_get owner.copyright)"
[ -n "$HOLDER" ] || HOLDER="$(host_get owner.name)"
[ -n "$HOLDER" ] || HOLDER=owner
[ "$HOLDER" != owner ] || echo "NOTE   no owner.copyright or owner.name in host.json: LICENSE holder is \"owner\"" >&2

MODE=apply
NAME=""
LICENSE=""
LICENSE_GIVEN=0
BOARD=""
BET=""
NO_GITHUB=0

usage() { echo "usage: new-repo.sh [--check] <name> [--license MIT|none] [--board <name>] [--bet <slug>] [--no-github]" >&2; exit 2; }

while [ $# -gt 0 ]; do
  case "$1" in
    --check) MODE=check; shift ;;
    --license) LICENSE="$2"; LICENSE_GIVEN=1; shift 2 ;;
    --board) BOARD="$2"; shift 2 ;;
    --bet) BET="$2"; shift 2 ;;
    --no-github) NO_GITHUB=1; shift ;;
    -h|--help) sed -n 2,10p "$0"; exit 0 ;;
    --*) echo "new-repo.sh: unknown argument $1" >&2; exit 2 ;;
    *) [ -n "$NAME" ] && usage; NAME="$1"; shift ;;
  esac
done
[ -n "$NAME" ] || usage
case "$NAME" in */*|"") echo "new-repo.sh: bad name $NAME" >&2; exit 2 ;; esac
if [ "$LICENSE_GIVEN" = 1 ]; then
  case "$LICENSE" in MIT|none) ;; *) echo "new-repo.sh: --license must be MIT or none" >&2; exit 2 ;; esac
elif [ "$MODE" = apply ]; then
  # repo-conventions.md: the licence is the owner's decision, never a default.
  echo "new-repo.sh: --license MIT|none is required" >&2; exit 2
fi
[ -n "$BOARD" ] || BOARD="$NAME"
[[ "$BOARD" =~ ^[a-z0-9][a-z0-9-]*$ ]] || { echo "new-repo.sh: bad board $BOARD" >&2; exit 2; }

# need_owner: sets OWNER from CAPSULE_GH_OWNER, else the account `gh` is signed in as (not
# under --no-github: no gh call); fails naming CAPSULE_GH_OWNER. Never hard-coded, and only
# called right before a GitHub repo is created or the registry url is written.
OWNER=""
need_owner() {
  [ -n "$OWNER" ] && return 0
  OWNER="${CAPSULE_GH_OWNER:-}"
  [ -n "$OWNER" ] || [ "$NO_GITHUB" = 1 ] || OWNER="$("$GH_BIN" api user -q .login 2>/dev/null)"
  [ -n "$OWNER" ] || { echo "new-repo.sh: set CAPSULE_GH_OWNER (no login)" >&2; exit 1; }
}

FAIL=0
say() { printf '%-6s %s\n' "$1" "$2"; }
step() { echo; echo "== $1"; }

# write_content <dst>: content on stdin. ok (unchanged) / WROTE (apply) / MISS (check, sets FAIL)
write_content() {
  local dst="$1" tmp
  tmp="$(mktemp)"; cat >"$tmp"
  if [ -f "$dst" ] && cmp -s "$tmp" "$dst"; then say ok "$dst"; rm -f "$tmp"; return 0; fi
  if [ "$MODE" = check ]; then say MISS "$dst"; rm -f "$tmp"; FAIL=1; return 1; fi
  mkdir -p "$(dirname "$dst")"; mv "$tmp" "$dst"; say WROTE "$dst"; return 0
}

# sync_copy <src> <dst>: same tri-state, copying a template file verbatim.
sync_copy() {
  local src="$1" dst="$2"
  if [ -f "$dst" ] && cmp -s "$src" "$dst"; then say ok "$dst"; return 0; fi
  if [ "$MODE" = check ]; then say MISS "$dst"; FAIL=1; return 1; fi
  mkdir -p "$(dirname "$dst")"; cp "$src" "$dst"; say WROTE "$dst"
}

D="$AGENT_OPS_HOME/CodeBase/$NAME"

# ---------------------------------------------------------------- 1. template
step "1. template: $D"
if [ "$LICENSE" = MIT ]; then
  write_content "$D/LICENSE" <<EOF
MIT License

Copyright (c) $(date +%Y) $HOLDER

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
EOF
elif [ "$LICENSE" = none ]; then
  say skip "$D/LICENSE (--license none)"
else
  say skip "$D/LICENSE audit (no --license given)"
fi

sed "s/__NAME__/$NAME/g" "$REPO/maintenance/templates/README.md" | write_content "$D/README.md"

write_content "$D/CHANGELOG.md" <<'EOF'
# Changelog

## Unreleased
EOF

write_content "$D/tests/test_smoke.py" <<'EOF'
import unittest


class Smoke(unittest.TestCase):
    def test_true(self):
        self.assertTrue(True)


if __name__ == "__main__":
    unittest.main()
EOF

sync_copy "$REPO/maintenance/templates/verify.yml" "$D/.github/workflows/verify.yml"
sync_copy "$REPO/maintenance/templates/repo-conventions.md" "$D/docs/CONVENTIONS.md"
sync_copy "$REPO/maintenance/templates/gitignore-python" "$D/.gitignore"

# ---------------------------------------------------------------- 2. git init
step "2. git init"
if [ -d "$D/.git" ]; then
  say ok "$D/.git"
else
  if [ "$MODE" = check ]; then
    say MISS "$D/.git"; FAIL=1
  else
    ( cd "$D" && git init -q -b main && git add -A && git commit -q -m "chore: new repo from the Capsule template" )
    say WROTE "$D/.git (initial commit $(git -C "$D" rev-parse --short HEAD))"
  fi
fi

# ---------------------------------------------------------------- 3. github repo
step "3. GitHub repo"
if [ "$NO_GITHUB" = 1 ]; then
  CREATE="gh repo create ${CAPSULE_GH_OWNER:-<owner>}/$NAME --private --source $D --push"
  say skip "$CREATE (--no-github: run this yourself)"
elif [ "$MODE" = check ]; then
  say skip "github repo (--check makes no gh or network call)"
else
  need_owner
  if "$GH_BIN" repo view "$OWNER/$NAME" >/dev/null 2>&1; then
    say ok "github repo $OWNER/$NAME"
  else
    if [ "$MODE" = check ]; then
      say MISS "github repo $OWNER/$NAME"; FAIL=1
    else
      if ( cd "$D" && "$GH_BIN" repo create "$OWNER/$NAME" --private --source . --push ); then
        say WROTE "github repo $OWNER/$NAME"
      else
        say FAILED "gh repo create $OWNER/$NAME"; FAIL=1
      fi
    fi
  fi
fi

# ---------------------------------------------------------------- 4. registry
step "4. registry: $REG"
REG_UPDATED=0
if NAME="$NAME" REG="$REG" python3 -c '
import json, os, sys
with open(os.environ["REG"]) as f:
    repos = json.load(f)
sys.exit(0 if any(r["name"] == os.environ["NAME"] for r in repos) else 1)
'; then
  say ok "$REG (already has $NAME)"
else
  if [ "$MODE" = check ]; then
    say MISS "$REG: $NAME"; FAIL=1
  else
    need_owner
    NAME="$NAME" REG="$REG" BOARD="$BOARD" BET="$BET" OWNER="$OWNER" python3 -c '
import json, os
path = os.environ["REG"]
with open(path) as f:
    repos = json.load(f)
name = os.environ["NAME"]
bet = os.environ.get("BET") or None
repos.append({
    "name": name,
    "path": "~/CodeBase/%s" % name,
    "url": "https://github.com/%s/%s.git" % (os.environ["OWNER"], name),
    "board": os.environ["BOARD"],
    "default_branch": "main",
    "bet": bet,
})
repos.sort(key=lambda r: r["name"])
with open(path, "w") as f:
    json.dump(repos, f, indent=2)
    f.write("\n")
'
    say WROTE "$REG ($NAME added)"
    REG_UPDATED=1
  fi
fi
if [ "$REG_UPDATED" = 1 ]; then
  REG_DIR="$(cd "$(dirname "$REG")" && pwd -P)"
  REG_REPO_ROOT="$(git -C "$REG_DIR" rev-parse --show-toplevel 2>/dev/null || true)"
  if [ -n "$REG_REPO_ROOT" ]; then
    git -C "$REG_REPO_ROOT" --no-pager diff -- "$REG" | sed 's/^/       /'
    git -C "$REG_REPO_ROOT" add "$REG"
    git -C "$REG_REPO_ROOT" commit -q -m "repos.json: add $NAME (new-repo.sh)"
    say WROTE "committed in $(basename "$REG_REPO_ROOT") ($(git -C "$REG_REPO_ROOT" rev-parse --short HEAD)) - not pushed"
  else
    say NOTE "$REG is not inside a git checkout - commit it yourself"
  fi
fi

# ---------------------------------------------------------------- 5. board
step "5. board: $BOARD"
# capsule.paths owns the board location (CAPSULE_HOME or ~/.local/share/capsule).
board_py() { PYTHONPATH="$REPO" python3 -c 'import sys; from capsule import db, paths
p = paths.board_db(sys.argv[2])
if sys.argv[1] == "create" and not p.is_file():
    db.create_board(p)
sys.exit(0 if p.is_file() else 1)' "$1" "$BOARD"; }
if board_py check; then
  say ok "board $BOARD"
else
  if [ "$MODE" = check ]; then
    say MISS "board $BOARD"; FAIL=1
  else
    if board_py create; then
      say WROTE "board $BOARD"
    else
      say FAILED "capsule create_board $BOARD"; FAIL=1
    fi
  fi
fi

# ---------------------------------------------------------------- 6. order book
step "6. Order Book"
OB="$VAULT/Boards/Order Book.md"
if [ -z "$VAULT" ]; then
  say skip "Order Book opportunity line (no notes vault configured: OBSIDIAN_VAULT_PATH or host.json vault.path)"
elif [ -z "$BET" ]; then
  say skip "Order Book opportunity line (no --bet)"
else
  LINE="- **$NAME** (bet: $BET) - new repo created (dictated: new-repo)"
  if [ -f "$OB" ] && grep -qF -- "$LINE" "$OB"; then
    say ok "$OB"
  else
    if [ "$MODE" = check ]; then
      say MISS "$OB: $NAME opportunity line"; FAIL=1
    else
      if [ ! -f "$OB" ]; then mkdir -p "$(dirname "$OB")"; printf '# Order Book\n\n## Opportunities\n' >"$OB"; fi
      awk -v line="$LINE" '
        { print }
        /^## Opportunities$/ && !done { print line; done=1 }
      ' "$OB" >"$OB.tmp" && mv "$OB.tmp" "$OB"
      say WROTE "$OB"
    fi
  fi
fi

# ---------------------------------------------------------------- 7. next
step "7. next"
say NEXT "./deploy.sh (renders the new mount into every profile; --check will show it as DRIFT until then)"
say NEXT "bootstrap.sh --check (now lists the $NAME clone)"

[ "$FAIL" = 1 ] && exit 1
exit 0
