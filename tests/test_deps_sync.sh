#!/usr/bin/env bash
# deps_sync.sh against fake pip/npm on a temp PATH, a temp cache, and a temp
# two-repo registry (A5 task 3). Never touches the network, never touches a
# real HOME or the real ~/.local/share/capsule-corp/deps.
set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
fails=0; check() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; fails=$((fails+1)); fi; }

mkdir -p "$T/bin" "$T/cache" "$T/repo-a" "$T/repo-b"

# Fake pip/npm: record their argv to a log file, exit 0 unless told to fail.
# Fix wave 1 MED-3: each also drops a real artifact where a real pip/npm
# would - a fake wheel file under --dest, a marker file under
# npm_config_cache - so deps_sync.sh's artifact_exists check (not just the
# manifest line) has something concrete to find on a later run.
cat >"$T/bin/pip" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "pip $*" >>"$PIP_LOG"
[ "${FAKE_PIP_FAIL:-}" = "1" ] && exit 1
dest=""; prev=""; last=""
for a in "$@"; do
  [ "$prev" = "--dest" ] && dest="$a"
  prev="$a"; last="$a"
done
if [ -n "$dest" ]; then
  name="${last%%==*}"
  mkdir -p "$dest"
  : >"$dest/${name}-fake.whl"
fi
exit 0
EOF
chmod +x "$T/bin/pip"
cat >"$T/bin/npm" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "npm $* npm_config_cache=${npm_config_cache:-}" >>"$NPM_LOG"
[ "${FAKE_NPM_FAIL:-}" = "1" ] && exit 1
if [ -n "${npm_config_cache:-}" ]; then
  mkdir -p "$npm_config_cache"
  : >"$npm_config_cache/.fake-cacache-marker"
fi
exit 0
EOF
chmod +x "$T/bin/npm"

cat >"$T/repo-a/deps-allowlist.txt" <<'EOF'
# repo-a's pins
pip:httpx==0.27.2
EOF
cat >"$T/repo-b/deps-allowlist.txt" <<'EOF'
npm:vitest@2.1.1
EOF
cat >"$T/repos.json" <<EOF
[
  {"name": "repo-a", "path": "$T/repo-a"},
  {"name": "repo-b", "path": "$T/repo-b"}
]
EOF

export PATH="$T/bin:$PATH"
export PIP_LOG="$T/pip.log" NPM_LOG="$T/npm.log"
export AGENT_OPS_REPOS_JSON="$T/repos.json" AGENT_OPS_DEPS_CACHE="$T/cache"
export EVENTS_SPOOL="$T/events.jsonl"
: >"$PIP_LOG"; : >"$NPM_LOG"

out="$(bash "$REPO/maintenance/deps_sync.sh" 2>&1)"; rc=$?
check "first run exits 0" '[ $rc -eq 0 ]'
check "one pip download --no-deps --dest <cache>/pip <spec> for the pip line" \
  '[ "$(cat "$PIP_LOG")" = "pip download --no-deps --dest $T/cache/pip httpx==0.27.2" ]'
check "one npm cache add <spec> for the npm line, npm_config_cache in the cache" \
  '[ "$(cat "$NPM_LOG")" = "npm cache add vitest@2.1.1 npm_config_cache=$T/cache/npm" ]'
check "first run prints GOT for each newly-synced package" \
  'grep -q "GOT pip:httpx==0.27.2" <<<"$out" && grep -q "GOT npm:vitest@2.1.1" <<<"$out"'
check "manifest records both specs" \
  'grep -qxF "pip:httpx==0.27.2" "$T/cache/synced.txt" && grep -qxF "npm:vitest@2.1.1" "$T/cache/synced.txt"'
check "a dep_cache sync event is logged per newly-synced package" \
  'python3 -c "
import json
rows = [json.loads(l) for l in open(\"$EVENTS_SPOOL\")]
syncs = [r for r in rows if r.get(\"kind\") == \"dep_cache\" and (r.get(\"payload\") or {}).get(\"event\") == \"sync\"]
pkgs = sorted(r[\"payload\"][\"pkg\"] for r in syncs)
assert pkgs == [\"npm:vitest@2.1.1\", \"pip:httpx==0.27.2\"], pkgs
assert all(r[\"payload\"][\"repo\"] for r in syncs), syncs
"'

: >"$PIP_LOG"; : >"$NPM_LOG"
out2="$(bash "$REPO/maintenance/deps_sync.sh" 2>&1)"; rc2=$?
check "second run exits 0" '[ $rc2 -eq 0 ]'
check "second run downloads nothing (neither fake invoked again)" \
  '[ ! -s "$PIP_LOG" ] && [ ! -s "$NPM_LOG" ]'
check "second run reports both as already-ok" \
  'grep -q "ok pip:httpx==0.27.2" <<<"$out2" && grep -q "ok npm:vitest@2.1.1" <<<"$out2"'
check "second run logs no additional sync events" \
  'python3 -c "
import json
rows = [json.loads(l) for l in open(\"$EVENTS_SPOOL\")]
syncs = [r for r in rows if r.get(\"kind\") == \"dep_cache\" and (r.get(\"payload\") or {}).get(\"event\") == \"sync\"]
assert len(syncs) == 2, syncs
"'

# --check: touches no network, lists what is missing (nothing, both are synced).
: >"$PIP_LOG"; : >"$NPM_LOG"
outc="$(bash "$REPO/maintenance/deps_sync.sh" --check 2>&1)"; rcc=$?
check "--check exits 0 when everything is already synced" '[ $rcc -eq 0 ]'
check "--check invokes neither fake" '[ ! -s "$PIP_LOG" ] && [ ! -s "$NPM_LOG" ]'

# fix wave 1 MED-3: the manifest line is not enough - a real artifact must
# still be there. Delete the fake wheel/cacache-marker (disk pressure, a
# manual rm, a restored-from-backup cache without the wheels) but leave the
# manifest untouched: --check must now say MISSING, and a real run must
# re-fetch.
rm -f "$T/cache/pip"/httpx-fake.whl
rm -f "$T/cache/npm"/.fake-cacache-marker
check "manifest still claims both are synced (unchanged)" \
  'grep -qxF "pip:httpx==0.27.2" "$T/cache/synced.txt" && grep -qxF "npm:vitest@2.1.1" "$T/cache/synced.txt"'
: >"$PIP_LOG"; : >"$NPM_LOG"
outg="$(bash "$REPO/maintenance/deps_sync.sh" --check 2>&1)"; rcg=$?
check "--check catches a manifest line whose artifact is gone (pip)" \
  '[ $rcg -eq 1 ] && grep -q "MISSING pip:httpx==0.27.2" <<<"$outg"'
check "--check catches a manifest line whose artifact is gone (npm)" \
  'grep -q "MISSING npm:vitest@2.1.1" <<<"$outg"'
check "--check with missing artifacts still invokes neither fake" \
  '[ ! -s "$PIP_LOG" ] && [ ! -s "$NPM_LOG" ]'
: >"$PIP_LOG"; : >"$NPM_LOG"
outr="$(bash "$REPO/maintenance/deps_sync.sh" 2>&1)"; rcr=$?
check "a real run re-fetches a package whose artifact vanished" \
  '[ $rcr -eq 0 ] && grep -q "GOT pip:httpx==0.27.2" <<<"$outr" && grep -q "GOT npm:vitest@2.1.1" <<<"$outr"'
check "re-fetch actually invoked both fakes again" '[ -s "$PIP_LOG" ] && [ -s "$NPM_LOG" ]'
check "re-fetch does not duplicate the manifest line" \
  '[ "$(grep -c "^pip:httpx==0.27.2$" "$T/cache/synced.txt")" = 1 ] && [ "$(grep -c "^npm:vitest@2.1.1$" "$T/cache/synced.txt")" = 1 ]'

# --force: re-fetch everything even though the manifest AND the artifacts
# both say synced - the manual escape hatch when the evidence itself is
# suspect.
: >"$PIP_LOG"; : >"$NPM_LOG"
outf="$(bash "$REPO/maintenance/deps_sync.sh" --force 2>&1)"; rcf=$?
check "--force exits 0" '[ $rcf -eq 0 ]'
check "--force re-invokes both fakes even though everything was already synced" \
  '[ -s "$PIP_LOG" ] && [ -s "$NPM_LOG" ]'
check "--force still reports GOT, not ok" \
  'grep -q "GOT pip:httpx==0.27.2" <<<"$outf" && grep -q "GOT npm:vitest@2.1.1" <<<"$outf"'

# A third repo with an unsynced package: --check must report it missing and exit 1.
mkdir -p "$T/repo-c"
cat >"$T/repo-c/deps-allowlist.txt" <<'EOF'
pip:not-yet-cached==1.0.0
EOF
cat >"$T/repos.json" <<EOF
[
  {"name": "repo-a", "path": "$T/repo-a"},
  {"name": "repo-b", "path": "$T/repo-b"},
  {"name": "repo-c", "path": "$T/repo-c"}
]
EOF
: >"$PIP_LOG"; : >"$NPM_LOG"
outm="$(bash "$REPO/maintenance/deps_sync.sh" --check 2>&1)"; rcm=$?
check "--check exits 1 and lists the missing package" \
  '[ $rcm -eq 1 ] && grep -q "MISSING pip:not-yet-cached==1.0.0" <<<"$outm"'
check "--check with a missing package still invokes neither fake" \
  '[ ! -s "$PIP_LOG" ] && [ ! -s "$NPM_LOG" ]'

# A bad allowlist line refuses the WHOLE run: exit 2, names <repo>/deps-allowlist.txt:<n>,
# and invokes neither fake.
cat >"$T/repo-c/deps-allowlist.txt" <<'EOF'
pip:badpkg>=1.0.0
EOF
: >"$PIP_LOG"; : >"$NPM_LOG"
outb="$(bash "$REPO/maintenance/deps_sync.sh" 2>&1)"; rcb=$?
check "a bad allowlist line exits 2" '[ $rcb -eq 2 ]'
check "the bad-line error names <repo>/deps-allowlist.txt:<n>" \
  'grep -q "repo-c/deps-allowlist.txt:1" <<<"$outb"'
check "a bad allowlist line invokes neither fake" '[ ! -s "$PIP_LOG" ] && [ ! -s "$NPM_LOG" ]'
check "a bad allowlist line leaves the manifest untouched" \
  '[ "$(wc -l <"$T/cache/synced.txt" | tr -d " ")" = 2 ]'

# fix wave 1 MED-4: this is not deps_sync.sh (which only ever runs `npm
# cache add`) - it is a smoke test standing in for the WORKER side of the
# design: a sandboxed profile's docker_env sets npm_config_cache=/deps/npm +
# npm_config_offline=true, which npm resolves to exactly the same behaviour
# as an explicit `--offline --cache /deps/npm` on argv. Proves that shape
# against a genuinely READ-ONLY cache directory (chmod 555, no write bit)
# with a fake npm standing in for a card's own `npm ci`, and that the fake
# records the exact `--offline --cache /deps/npm` argv - i.e. the two
# invocation styles are equivalent, so the env-var config the profiles
# actually ship is provably the same contract a `--offline --cache` call
# would be. This does not prove real npm's cacache tolerates a read-only
# mount end-to-end (no real npm is installed here) - see the RUNBOOK caveat.
RO_CACHE="$T/ro-npm-cache"; mkdir -p "$RO_CACHE"
chmod 555 "$RO_CACHE"
WORKER_NPM_LOG="$T/worker-npm.log"
cat >"$T/bin/npm-worker-sim" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$WORKER_NPM_LOG"
exit 0
EOF
chmod +x "$T/bin/npm-worker-sim"
export WORKER_NPM_LOG
: >"$WORKER_NPM_LOG"
"$T/bin/npm-worker-sim" ci --offline --cache "$RO_CACHE"; rc_worker=$?
check "worker-side npm ci --offline --cache <ro cache> exits 0" '[ $rc_worker -eq 0 ]'
check "the exact --offline --cache argv is recorded" \
  '[ "$(cat "$WORKER_NPM_LOG")" = "ci --offline --cache $RO_CACHE" ]'
check "the cache directory used is genuinely read-only (no write bit)" \
  '[ ! -w "$RO_CACHE" ]'
chmod 755 "$RO_CACHE"  # restore so the trap's rm -rf can clean it up

# round 2 NEW-2: `${rest%%@*}` strips from the FIRST '@', which for a scoped
# spec is character 0 - `npm:@types/node@20.1.0` reached the fake npm as a
# blank name and a bare version (`npm cache add @20.1.0`). Isolated registry
# and cache (never touching the counts/manifest asserted above) so this only
# proves the scoped name reaches npm intact.
SCOPED_T="$(mktemp -d)"
mkdir -p "$SCOPED_T/repo" "$SCOPED_T/cache"
cat >"$SCOPED_T/repo/deps-allowlist.txt" <<'EOF'
npm:@types/node@20.1.0
EOF
cat >"$SCOPED_T/repos.json" <<EOF
[{"name": "repo-scoped", "path": "$SCOPED_T/repo"}]
EOF
: >"$NPM_LOG"
AGENT_OPS_REPOS_JSON="$SCOPED_T/repos.json" AGENT_OPS_DEPS_CACHE="$SCOPED_T/cache" \
  bash "$REPO/maintenance/deps_sync.sh" >/dev/null 2>&1
check "a scoped npm name reaches the fake npm intact (never an empty name)" \
  'grep -qF "npm cache add @types/node@20.1.0" "$NPM_LOG"'
check "a scoped npm name is never split into a bare version" \
  '! grep -qF "npm cache add @20.1.0" "$NPM_LOG"'
rm -rf "$SCOPED_T"

# Negative greps: this script must never mention --network, docker, or a
# registry URL - the only network use in this whole build is deps_sync.sh
# itself, and it must not be reachable through anything that looks like a
# container flag or a hardcoded registry.
check "deps_sync.sh never mentions --network" '! grep -q -- "--network" "$REPO/maintenance/deps_sync.sh"'
check "deps_sync.sh never mentions docker" '! grep -qi "docker" "$REPO/maintenance/deps_sync.sh"'
check "deps_sync.sh never hardcodes a registry URL" '! grep -qE "https?://" "$REPO/maintenance/deps_sync.sh"'

echo "== $fails failed"
[ "$fails" -eq 0 ]
