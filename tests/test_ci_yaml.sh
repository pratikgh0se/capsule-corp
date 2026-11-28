#!/usr/bin/env bash
# tests/test_ci_yaml.sh - each repo's verify.yml carries the required pieces
# from the CI spec: the ruff error-only selection, python-version 3.11 (where
# the repo is Python), the gitleaks action, and the repo's own test command.
# The sibling-repo checks run on throwaway fixture repos built in a temp dir
# (never the real checkouts), read from their ci/verify branch with `git show`.
set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fails=0
check() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; fails=$((fails+1)); fi; }
has() { grep -qF "$1" "$2" 2>/dev/null; }
# step_advisory NAME FILE - true iff the "- name: NAME" step's own block (up
# to the next "- name:"/"- uses:" step or EOF) carries continue-on-error:
# true. A file-wide grep -c would pass even if only some named steps were
# advisory (there are already two unrelated continue-on-error lines in this
# job), so this checks per step.
step_advisory() {
  awk -v n="- name: $1" '
    $0 ~ n { found=1; next }
    found && /^      - (name|uses):/ { exit }
    found && /continue-on-error: true/ { print "yes"; exit }
  ' "$2" | grep -q yes
}

AO="$REPO/.github/workflows/verify.yml"
check "capsule-corp verify.yml exists" "[ -f '$AO' ]"
check "capsule-corp: ruff error-only selection" "has 'ruff check --select E9,F63,F7,F82 .' '$AO'"
check "capsule-corp: python 3.11" "has 'python-version: \"3.11\"' '$AO'"
check "capsule-corp: gitleaks CLI v8.30.1 pinned and checksummed" "has 'gitleaks_8.30.1_linux_x64.tar.gz' '$AO' && has 'sha256sum -c -' '$AO'"
# C12 (row 140): the four advisory steps live in the security-scan job C5
# renamed - this test asserts the name, it does not own the rename.
check "capsule-corp: security-scan job (C5's rename, C12 depends on it)" "has 'security-scan:' '$AO'"
check "capsule-corp: licence scan step" "has 'maintenance/notice_gen.py --scan' '$AO'"
check "capsule-corp: NOTICE freshness step" "has 'maintenance/notice_gen.py --check' '$AO'"
check "capsule-corp: policy presence step" "has 'maintenance/policy_check.py' '$AO'"
check "capsule-corp: licences step is advisory" "step_advisory 'licences' '$AO'"
check "capsule-corp: policies step is advisory" "step_advisory 'policies' '$AO'"
check "capsule-corp: fetch-depth 0 for security-scan" "has 'fetch-depth: 0' '$AO'"
# t_30645655: a depth-1 PR checkout has an origin remote but no origin/main, and test_intent's
# cap test fails (by design) when it cannot resolve origin/main; the tests job's own checkout
# step (not the security-scan one above) must therefore fetch full history.
tests_job_fetches_all() {
  awk '/^  tests:/ { in_job=1; next } in_job && /^  [a-z]/ { exit }
       in_job && /fetch-depth: 0/ { print "yes"; exit }' "$1" | grep -q yes
}
check "capsule-corp: tests job fetch-depth 0 (test_intent)" "tests_job_fetches_all '$AO'"
check "capsule-corp: gitleaks reads the committed config" "has 'detect --config .gitleaks.toml --redact --exit-code 1' '$AO'"
check "capsule-corp: dependency audit step" "has 'pip-audit' '$AO'"
check "capsule-corp: test_report_coverage.sh" "has 'tests/test_report_coverage.sh' '$AO'"
check "capsule-corp: concurrency group per event and ref (schedule and push never cancel each other)" "has 'group: verify-\${{ github.event_name }}-\${{ github.ref }}' '$AO'"
check "capsule-corp: maintenance/tests discover" "has 'python3 -m unittest discover maintenance/tests' '$AO'"
# I3: without a Lua interpreter on the runner, test_scrub.py's LuaParity
# class skips itself and scrub.lua is never executed in CI at all.
# The `apt-get update` is not optional on a GitHub runner: without a refreshed
# index the install can 404 on a moved package version.
check "capsule-corp: lua5.4 installed for the scrub.lua parity test (I3)" "has 'sudo apt-get install -y lua5.4 && break' '$AO'"
check "capsule-corp: test_run_sh.sh" "has 'tests/test_run_sh.sh' '$AO'"
if [ -f "$REPO/release/public-paths.txt" ]; then   # the export trims this private-only line, so the public repo has none
  check "capsule-corp: test_deploy.sh" "has 'tests/test_deploy.sh' '$AO'"
else
  echo "SKIP test_deploy.sh line check: public repo"
fi
check "capsule-corp: test_cleanup.sh" "has 'tests/test_cleanup.sh' '$AO'"
check "capsule-corp: test_migration_pg.sh" "has 'tests/test_migration_pg.sh' '$AO'"
check "capsule-corp: test_alert_rules_pg.sh" "has 'tests/test_alert_rules_pg.sh' '$AO'"
check "capsule-corp: test_fluent_bit_scrub.sh" "has 'tests/test_fluent_bit_scrub.sh' '$AO'"
check "capsule-corp: test_bootstrap.sh excluded (macOS-only)" "! has 'bash tests/test_bootstrap.sh' '$AO'"
check "capsule-corp: test_capsule_secret.sh" "has 'tests/test_capsule_secret.sh' '$AO'"
check "capsule-corp: test_lib_compose.sh" "has 'tests/test_lib_compose.sh' '$AO'"
check "capsule-corp: test_capsule_compose_recreate.sh" "has 'tests/test_capsule_compose_recreate.sh' '$AO'"
check "capsule-corp: test_heal.sh" "has 'tests/test_heal.sh' '$AO'"
check "capsule-corp: test_no_secret_values.sh" "has 'tests/test_no_secret_values.sh' '$AO'"
check "capsule-corp: test_report_notices.sh" "has 'tests/test_report_notices.sh' '$AO'"
check "capsule-corp: test_report_power.sh" "has 'tests/test_report_power.sh' '$AO'"
check "capsule-corp: test_report_budgets.sh" "has 'tests/test_report_budgets.sh' '$AO'"
check "capsule-corp: test_report_retrieval.sh" "has 'tests/test_report_retrieval.sh' '$AO'"
check "capsule-corp: test_report_brief.sh" "has 'tests/test_report_brief.sh' '$AO'"
check "capsule-corp: test_close.sh" "has 'tests/test_close.sh' '$AO'"
check "capsule-corp: test_blocked_tonight.sh" "has 'tests/test_blocked_tonight.sh' '$AO'"
check "capsule-corp: test_remove_card.sh" "has 'tests/test_remove_card.sh' '$AO'"
check "capsule-corp: test_status.sh" "has 'tests/test_status.sh' '$AO'"
check "capsule-corp: test_capsule_park.sh" "has 'tests/test_capsule_park.sh' '$AO'"
check "capsule-corp: verify.yml no longer exports SKIP_DOCKER_IMAGE_TEST" "! has 'SKIP_DOCKER_IMAGE_TEST' '$AO'"
check "capsule-corp: verify.yml scopes GITHUB_TOKEN to contents: read (I5)" "has 'permissions:' '$AO' && has '  contents: read' '$AO'"
check "capsule-corp: image.yml has a concurrency group (M4)" "has 'group: image' '$REPO/.github/workflows/image.yml'"
IY="$REPO/.github/workflows/image.yml"
check "capsule-corp: image.yml has no runner context in job-level env (GitHub forbids it)" "! has '\${{ runner.temp }}' '$IY'"
check "capsule-corp: image.yml pushes only from main and after the image test" "has \"github.ref == 'refs/heads/main'\" '$IY' && [ \$(grep -n 'test_docker_image.sh' '$IY' | head -1 | cut -d: -f1) -lt \$(grep -n 'docker push' '$IY' | head -1 | cut -d: -f1) ]"
check "capsule-corp: image.yml enables the containerd image store before the build (digest FROM resolves locally)" "has 'containerd-snapshotter' '$IY' && has 'jq ' '$IY' && ! grep -qE 'echo .*containerd-snapshotter.*tee' '$IY' && [ \$(grep -n 'containerd-snapshotter' '$IY' | head -1 | cut -d: -f1) -lt \$(grep -n 'for layer in' '$IY' | head -1 | cut -d: -f1) ]"
check "capsule-corp: test_ci_yaml.sh itself runs in CI (I7)" "has 'tests/test_ci_yaml.sh' '$AO'"

# --- C0 item 6 / threat model 4.6: every `uses:` in this repo's own workflows is
# pinned to a full 40-hex commit SHA with the tag it was resolved from in a
# trailing comment. A mutable tag (`@v4`, `@main`, `@1.2.3`) means CI runs
# whatever that tag points at today; the comment is what keeps the pin readable
# and upgradable. Local actions (`uses: ./...`) and docker refs are exempt.
tag_pinned=""; unlabelled=""
for wf in "$REPO"/.github/workflows/*.yml; do
  [ -e "$wf" ] || continue
  while IFS= read -r line; do
    ref="${line#*uses:}"; ref="${ref#"${ref%%[![:space:]]*}"}"   # strip leading blanks
    case "$ref" in
      ./*|docker://*) continue ;;
    esac
    sha="${ref#*@}"; sha="${sha%%[[:space:]]*}"
    if ! printf '%s' "$sha" | grep -Eq '^[0-9a-f]{40}$'; then
      tag_pinned="$tag_pinned $(basename "$wf"):$ref"
    elif ! printf '%s' "$ref" | grep -Eq '#[[:space:]]*v?[0-9]'; then
      unlabelled="$unlabelled $(basename "$wf"):$ref"
    fi
  done < <(grep -h 'uses:' "$wf")
done
check "capsule-corp: no \`uses:\` is tag-pinned - every action is a 40-hex SHA (4.6)" '[ -z "$tag_pinned" ] || { echo "  tag-pinned:$tag_pinned"; false; }'
check "capsule-corp: every SHA pin carries its tag in a trailing comment" '[ -z "$unlabelled" ] || { echo "  no tag comment:$unlabelled"; false; }'

# --- O260: the public-tree job runs the baselined public lint ---
PT="$(awk '/^  public-tree:/{f=1;print;next} f&&/^  [a-z-]+:/{exit} f{print}' "$AO")"
check "capsule-corp: public-tree job exists" "[ -n \"\$PT\" ]"
check "capsule-corp: public-tree lints generic-only with an allowlist from git ls-files" "printf '%s\\n' \"\$PT\" | grep -qF 'git -c core.quotePath=false ls-files' && printf '%s\\n' \"\$PT\" | grep -qF -e 'public_lint.py --generic-only --allowlist'"
check "capsule-corp: public-tree is Python only (no docker, no secrets)" "! printf '%s\n' \"\$PT\" | grep -qiE 'docker|secrets\\.'"
# The oracle is the export's own P10 workflow check (trim `# private-only`, then _line_problems)
# over the workflows of the real extracted public tree; every workflow must be accepted.
if [ ! -f "$REPO/release/public-paths.txt" ]; then
  echo "SKIP export workflow checks: no release/public-paths.txt (public repo)"
else
CT="$(mktemp -d)"
python3 "$REPO/maintenance/public_lint.py" --repo "$REPO" --allowlist "$REPO/release/public-paths.txt" --ref HEAD --extract "$CT/tree" >/dev/null || echo "extract failed" >&2
wf_problems() {  # wf_problems TREE [WORKFLOW...]: P10 problems, one per line (a refusal or crash counts as one); no names = every workflow
  python3 -c '
import sys, pathlib
sys.path.insert(0, sys.argv[1] + "/maintenance")
import public_export as pe
repo, tree, names = sys.argv[1], sys.argv[2], sys.argv[3:]
top = set(pe._git(repo, "ls-tree", "--name-only", "HEAD").stdout.decode().split("\n"))
wfs = [pathlib.Path(tree, ".github/workflows", n) for n in names] or sorted(pathlib.Path(tree, ".github/workflows").glob("*.yml"))
for wf in wfs:
    for l in pe._trim_private(wf.name, wf.read_text().splitlines(keepends=True)):
        for p in pe._line_problems(pe._code(l), top, tree):
            print(wf.name + ": " + p)
' "$REPO" "$@" 2>&1 || echo "wf_problems: the export check refused or crashed (rc=$?)"
}
check "capsule-corp: export workflow check accepts verify.yml (incl. public-tree) and image.yml" "[ -f '$CT/tree/.github/workflows/verify.yml' ] && [ -f '$CT/tree/.github/workflows/image.yml' ] && grep -q 'public-tree:' '$CT/tree/.github/workflows/verify.yml' && [ -z \"\$(wf_problems '$CT/tree' verify.yml image.yml)\" ]"
check "capsule-corp: export workflow check accepts every extracted workflow" "[ -z \"\$(wf_problems '$CT/tree')\" ]"
# Blocker b105-1: run the public-tree job's own lint command on the extracted tree (no private
# denylist there); the personal-path line exemptions must ship, so it exits 0.
check "capsule-corp: public lint --generic-only passes on the extracted public tree (rc 0)" "[ ! -e '$CT/tree/release/private-denylist.txt' ] && (cd '$CT/tree' && git init -q . && git add -A && git -c user.name=t -c user.email=t@t.invalid -c commit.gpgsign=false commit -qm t && git -c core.quotePath=false ls-files > '$CT/allowlist.txt' && python3 maintenance/public_lint.py --generic-only --allowlist '$CT/allowlist.txt')"
printf '%s\n' '      - run: python3 release/not-shipped-planted.txt' >>"$CT/tree/.github/workflows/verify.yml"
check "capsule-corp: the workflow check refuses a planted line naming a file the export lacks" "wf_problems '$CT/tree' verify.yml | grep -q 'not-shipped-planted'"
printf '%s\n' '      - run: x   # private-only' >>"$CT/tree/.github/workflows/verify.yml"
check "capsule-corp: the workflow check reports a misplaced private-only marker (a refusal is not a pass)" "wf_problems '$CT/tree' verify.yml | grep -q 'private-only'"
rm -rf "$CT"
fi

# --- M106: the security-scan job's extra scanners (2026-09-10 correctness-over-green
# plan). Each is pinned to an exact version (no `latest`, no floating tag) and
# first-run report-only through the tool's own flag, not a job-wide
# continue-on-error. step_block prints one "- name: NAME" step's own lines so
# every check below is per step, never a file-wide grep that another step satisfies.
step_block() {
  awk -v n="- name: $1" '
    index($0, n) { found=1; print; next }
    found && /^      - (name|uses):/ { exit }
    found { print }
  ' "$2"
}
blk() { step_block "$1" "$AO" | grep -qE -e "$2"; }
check "capsule-corp: semgrep step exists" "[ -n \"\$(step_block semgrep '$AO')\" ]"
check "capsule-corp: semgrep pinned to an exact version" "blk semgrep 'pip install semgrep==[0-9]+\.[0-9]+\.[0-9]+( |\$)'"
check "capsule-corp: semgrep runs the OWASP rule pack" "blk semgrep '--config p/owasp-top-ten'"
check "capsule-corp: semgrep runs the Python rule pack" "blk semgrep '--config p/python'"
check "capsule-corp: semgrep is report-only (no --error)" "! blk semgrep '--error'"
check "capsule-corp: osv-scanner step exists" "[ -n \"\$(step_block osv-scanner '$AO')\" ]"
check "capsule-corp: osv-scanner is a release binary at an exact version" "blk osv-scanner 'releases/download/v[0-9]+\.[0-9]+\.[0-9]+/osv-scanner_linux_amd64'"
check "capsule-corp: osv-scanner binary is checked against a pinned sha256" "blk osv-scanner '[0-9a-f]{64}  ' && blk osv-scanner 'sha256sum -c'"
check "capsule-corp: osv-scanner step is advisory" "step_advisory 'osv-scanner' '$AO'"
check "capsule-corp: osv-scanner treats exit 128 (no package sources) as success" "blk osv-scanner 'scan source .*\|\| \[ \\\$\? -eq 128 \]'"
check "capsule-corp: trivy action pinned by SHA with its tag comment" "has 'aquasecurity/trivy-action@ed142fd0673e97e23eac54620cfb913e5ce36c25  # v0.36.0' '$AO'"
check "capsule-corp: trivy step exists" "[ -n \"\$(step_block trivy '$AO')\" ]"
check "capsule-corp: trivy pins its own Trivy version, not the action default" "blk trivy 'version: v[0-9]+\.[0-9]+\.[0-9]+( |\$)'"
check "capsule-corp: trivy is a filesystem scan" "blk trivy 'scan-type: fs'"
check "capsule-corp: trivy is report-only via exit-code 0" "blk trivy \"exit-code: '0'\""
check "capsule-corp: no floating scanner version (latest/main/master)" "! has 'releases/latest' '$AO' && ! grep -qE '@(main|master|latest)' '$AO'"
check "capsule-corp: semgrep and trivy are report-only by flag, not continue-on-error" "! step_advisory 'semgrep' '$AO' && ! step_advisory 'trivy' '$AO'"

# --- M106 remainder: the scan also runs nightly, and writes a Syft SBOM. Both
# stay advisory (the blocking flip is date-gated). The cron runs verify.yml, so
# the tests job is skipped on schedule events (one oracle: the `if:` is there).
check "capsule-corp: verify.yml has a nightly schedule cron" "grep -qE '^  schedule:' '$AO' && grep -qE '^    - cron: \"[0-9]+ [0-9]+ \* \* \*\"' '$AO'"
check "capsule-corp: the tests job is skipped on schedule events" "grep -qF \"if: github.event_name != 'schedule'\" '$AO'"
check "capsule-corp: syft SBOM action pinned by SHA with its tag comment" "grep -qE 'anchore/sbom-action@[0-9a-f]{40}  # v[0-9]+\.[0-9]+\.[0-9]+' '$AO'"
check "capsule-corp: syft SBOM step exists" "[ -n \"\$(step_block syft-sbom '$AO')\" ]"
check "capsule-corp: syft SBOM step writes SPDX JSON and uploads it" "blk syft-sbom 'format: spdx-json' && blk syft-sbom 'upload-artifact: true'"
check "capsule-corp: syft SBOM step is advisory" "step_advisory 'syft-sbom' '$AO'"

# G3/G4 (release gate): the vulnerability scan must really run, on the SBOM the
# syft-sbom step writes. The old `sbom` / `vulnerabilities` steps ran
# maintenance/sbom.sh on a runner with no syft, grype or osv-scanner, so both
# printed a skip line and went green having scanned nothing. CI installs none of
# those binaries, so no step may call sbom.sh (the script stays for the laptop).
check "capsule-corp: no CI step calls maintenance/sbom.sh (it skips silently on the runner)" "! has 'maintenance/sbom.sh' '$AO'"
check "capsule-corp: no dead sbom step beside syft-sbom" "[ -z \"\$(step_block sbom '$AO')\" ]"
check "capsule-corp: vulnerabilities step exists" "[ -n \"\$(step_block vulnerabilities '$AO')\" ]"
check "capsule-corp: grype scan action pinned by SHA with its tag comment" "grep -qE 'anchore/scan-action@[0-9a-f]{40}  # v[0-9]+\.[0-9]+\.[0-9]+' '$AO'"
check "capsule-corp: vulnerabilities step scans the syft SPDX file" "blk vulnerabilities 'sbom: syft-sbom.spdx.json'"
check "capsule-corp: vulnerabilities step fails on high and above" "blk vulnerabilities 'severity-cutoff: high' && ! blk vulnerabilities 'fail-build: .?false'"
check "capsule-corp: vulnerabilities step is advisory" "step_advisory 'vulnerabilities' '$AO'"
vuln_after_sbom() {  # the scan reads a file the syft-sbom step writes, so it must come second
  awk '/- name: syft-sbom/ { s=NR } /- name: vulnerabilities/ { v=NR } END { exit !(s && v && v > s) }' "$1"
}
check "capsule-corp: vulnerabilities step runs after syft-sbom" "vuln_after_sbom '$AO'"

T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT

sibling_file() {  # <repo dir> <branch> <out file> -> 0 and writes $3 if the ref resolves, else 1
  git -C "$1" show "$2:.github/workflows/verify.yml" >"$3" 2>/dev/null
}

# The sibling repos are built here as throwaway fixtures (git init in $T), never read from
# the real checkouts: tests never touch real repos. The good fixture is the real template
# (maintenance/templates/verify.yml); the `lacks` fixtures delete one line from it. A missing
# branch is a FAIL (the fixture is always built).
make_sibling() {  # <dir> <workflow yaml text> -> a repo whose ci/verify branch carries it
  # GIT_CONFIG_GLOBAL/NOSYSTEM: the real ~/.gitconfig (signing, hooks, templates) is never read.
  export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
  git init -q "$1" && git -C "$1" checkout -q -b ci/verify || return 1
  mkdir -p "$1/.github/workflows" && printf '%s\n' "$2" >"$1/.github/workflows/verify.yml"
  git -C "$1" add . && git -C "$1" -c user.name=t -c user.email=t@example.invalid commit -q -m fixture
}
# lacks <name> <yml> <line fragment to delete> <check expr using $F>: builds a fixture from the
# good yml minus the one line holding the fragment and asserts the check FAILS on it, so the
# check is shown able to fail (Z2: the passing fixture alone is text this test wrote itself).
bad_n=0
lacks() {
  local d="$T/bad-$((++bad_n))"
  if make_sibling "$d" "$(printf '%s\n' "$2" | grep -vF -- "$3")" && sibling_file "$d" ci/verify "$d.yml"; then
    F="$d.yml" check "$1" "! ( $4 )"
  else
    check "$1 (bad fixture builds)" "false"
  fi
}
I5='has "permissions:" "$F" && has "  contents: read" "$F"'
PY_YML="$(cat "$REPO/maintenance/templates/verify.yml")"
APP_DIR="$T/my-app"
POM="$T/my-app-verify.yml"
if make_sibling "$APP_DIR" "$PY_YML" && sibling_file "$APP_DIR" ci/verify "$POM"; then
  # Each expression is written once over "$F": the real check runs it on the good fixture, the
  # bad-fixture case runs the very same text on a fixture missing that one line (must fail).
  RUFF='has "ruff check --select E9,F63,F7,F82 ." "$F"'
  PYVER='has "python-version: \"3.11\"" "$F"'
  GITL='has "gitleaks_8.30.1_linux_x64.tar.gz" "$F" && has "sha256sum -c -" "$F"'
  COLL='has "pathlib.Path(\"tests\").glob(\"test_*.py\")" "$F"'
  F="$POM" check "my-app: ruff error-only selection" "$RUFF"
  F="$POM" check "my-app: python 3.11" "$PYVER"
  F="$POM" check "my-app: gitleaks CLI v8.30.1 pinned and checksummed" "$GITL"
  F="$POM" check "my-app: non-recursive collector excludes tests/ui" "$COLL"
  F="$POM" check "my-app: verify.yml scopes GITHUB_TOKEN to contents: read (I5)" "$I5"
  lacks "my-app bad fixture: no ruff line fails the ruff check" "$PY_YML" 'ruff check' "$RUFF"
  lacks "my-app bad fixture: no python-version fails the 3.11 check" "$PY_YML" 'python-version' "$PYVER"
  lacks "my-app bad fixture: no gitleaks step fails the gitleaks check" "$PY_YML" 'gitleaks' "$GITL"
  lacks "my-app bad fixture: no collector line fails the collector check" "$PY_YML" 'glob(' "$COLL"
  lacks "my-app bad fixture: no permissions: line fails the I5 check" "$PY_YML" 'permissions:' "$I5"
  lacks "my-app bad fixture: no contents: read line fails the I5 check" "$PY_YML" 'contents: read' "$I5"
else
  check "my-app fixture repo builds and its ci/verify branch resolves" "false"
fi

# --- Renovate (Capsule row 139 / M260) -------------------------------------
# Weekly, grouped, patch automerged on green CI (D239), minor and major as PRs. Two things Renovate must NOT touch: the worker images' base
# (bumped through maintenance/templates/image-rebuild-card.md, not by a bot)
# and the sibling repos (their renovate.json is a file in their own repo, so
# they get a printed card). The policy assertions live in a temp python file
# rather than inline, because `check` eval's its argument and a nested
# quoting level is how a green test stops meaning anything.
RN="$REPO/.github/renovate.json"
RC="$REPO/maintenance/templates/renovate-card.md"
cat >"$T/renovate_policy.py" <<'PY'
import json, sys
cfg = json.load(open(sys.argv[1]))
rules = cfg["packageRules"]
patch = [r for r in rules if r.get("matchUpdateTypes") == ["patch"]]
minor = [r for r in rules if r.get("matchUpdateTypes") == ["minor"]]
major = [r for r in rules if r.get("matchUpdateTypes") == ["major"]]
other = [r for r in rules if set(r.get("matchUpdateTypes", [])) & {"digest", "pin", "pinDigest", "lockFileMaintenance"}]
assert len(patch) == 1 and patch[0]["automerge"] is True, "D239: patch is exactly one rule and automerges"
assert patch[0].get("groupName"), "the patch rule needs a groupName"
assert len(minor) == 1 and minor[0]["automerge"] is False, "D239: minor is a PR for the owner, never automerged"
assert not any(["patch", "minor"] == sorted(r.get("matchUpdateTypes", [])) for r in rules), "patch and minor must not share a rule"
assert len(other) == 1 and other[0]["automerge"] is False, "digest, pin and lockfile updates are PRs"
assert len(major) == 1 and major[0]["automerge"] is False, "majors are PRs, never automerged"
assert cfg["platformAutomerge"] is False, "platform automerge needs branch protection (row 123, HTTP 403)"
assert cfg["enabledManagers"] == ["github-actions", "docker-compose", "custom.regex"], "the managers this repo uses; the dockerfile manager stays off (Dockerfile bases go through the image-rebuild card and IMAGES.lock)"
assert "ignorePaths" not in cfg and ":ignoreModulesAndTests" in cfg["ignorePresets"], "tests/ must be scanned (size_check.py); ignorePaths would replace the recommended ignore list"
cm = cfg["customManagers"]
assert len(cm) == 2 and all(m["datasourceTemplate"] == "pypi" for m in cm), "regex managers: the pip pins in verify.yml and size_check.RUFF_VERSION, both PyPI"
assert not any("fileMatch" in m for m in cm), "current Renovate uses managerFilePatterns, not fileMatch"
import os, re
root = os.path.dirname(os.path.dirname(os.path.abspath(sys.argv[1])))
for m in cm:
    target = m["managerFilePatterns"][0].strip("/").replace("\\.", ".").strip("^$")
    text = open(os.path.join(root, target)).read()
    assert any(re.search(s.replace("(?<", "(?P<"), text) for s in m["matchStrings"]), "matchStrings match nothing in " + target
assert "docker:pinDigests" in cfg["extends"], "compose image tags are digest-pinned"
infisical = [r for r in rules if "infisical/infisical" in r.get("matchPackageNames", [])]
assert len(infisical) == 1 and infisical[0]["enabled"] is False, "the infisical digest pin is bumped deliberately, never by the bot"
assert cfg["vulnerabilityAlerts"]["automerge"] is False, "a security bump is still a PR"
assert cfg["schedule"] == ["* 2-6 * * 1"], "one weekly batch, Monday early"
PY
if [ ! -f "$RN" ] && [ ! -f "$REPO/release/public-paths.txt" ]; then
  echo "SKIP renovate.json checks: no .github/renovate.json (public repo)"
else
check "capsule-corp: renovate.json exists" "[ -f '$RN' ]"
check "capsule-corp: renovate.json is valid JSON" "python3 -c 'import json,sys; json.load(open(sys.argv[1]))' '$RN'"
check "capsule-corp: renovate schema declared" "has 'renovate-schema.json' '$RN'"
check "capsule-corp: renovate policy is weekly, grouped, patch automerge, minor and major as PRs (D239)" \
  "python3 '$T/renovate_policy.py' '$RN'"
check "capsule-corp: renovate keeps the SHA pins this repo requires" \
  "has 'helpers:pinGitHubActionDigests' '$RN'"
check "capsule-corp: renovate does not enable the dockerfile manager" \
  "! has 'dockerfile' '$RN'"
fi
check "the printed card for the two product repos exists" "[ -f '$RC' ]"
for r in my-app other-app; do
  check "renovate card covers $r" "has '$r' '$RC'"
done
DL="$REPO/release/private-denylist.txt"
if [ -f "$DL" ]; then
  check "renovate card matches no private-denylist project rule (public release)" \
    "python3 -c 'import re,sys; sys.path.insert(0, sys.argv[1]); import public_lint; rules,_=public_lint._load_denylist(sys.argv[2]); t=open(sys.argv[3]).read(); sys.exit(any(re.search(r[1], t, re.I) for r in rules if r[0]==\"project\"))' '$REPO/maintenance' '$DL' '$RC'"
else
  echo "SKIP renovate card private-name check: release/private-denylist.txt absent (public checkout)"
fi
check "renovate card does not pin digests for the product repos" \
  "! has 'pinGitHubActionDigests' '$RC'"
check "renovate card points at the repo's own verify.yml pinned gitleaks step, not capsule-corp's test" \
  "has 'the repo'\\''s \`verify.yml\` shows the pinned \`gitleaks\` CLI step' '$RC' && ! has 'bash tests/test_ci_yaml.sh' '$RC'"

# RPUB-1: the release workflow emits the dependency-graph SBOM and checksums it beside the tarball.
RL="$REPO/.github/workflows/release.yml"
check "release.yml: sbom step requests a report and reads sbom_url" "step_block sbom '$RL' | grep -q 'dependency-graph/sbom/generate-report' && step_block sbom '$RL' | grep -q '\.sbom_url'"
check "release.yml: sbom step polls with a bounded loop (202 retry, timeout fails)" "step_block sbom '$RL' | grep -q 'seq 1 30' && step_block sbom '$RL' | grep -q '= 202' && step_block sbom '$RL' | grep -q 'not ready after'"
check "release.yml: sbom step writes dist/sbom.spdx.json" "step_block sbom '$RL' | grep -q 'dist/sbom.spdx.json'"
check "release.yml: sbom step is public-repo only, same condition as attest" "step_block sbom '$RL' | grep -qF 'if: \${{ !github.event.repository.private }}'"
check "release.yml: SHA256SUMS covers the SBOM only when it exists" "grep -qF '[ -f sbom.spdx.json ] && echo sbom.spdx.json' '$RL'"
check "release.yml: sbom step runs before the checksum step and the Private-repo comment stays on its step" "awk '/- name: sbom/{s=NR} /# Private repo:/{c=NR} /sha256sum/{h=NR} END{exit !(s&&c&&h&&s<c&&c<h)}' '$RL'"

# RPUB-2: a macOS job runs the installer's dry run and the CLI's --help in a temp CAPSULE_HOME.
install_smoke_job() {
  awk '/^  install-smoke:/ { in_job=1; next } in_job && /^  [a-z]/ { exit } in_job { print }' "$1"
}
check "capsule-corp: install-smoke job runs on macOS" "install_smoke_job '$AO' | grep -q 'runs-on: macos-'"
check "capsule-corp: install-smoke runs install.sh --dry-run" "install_smoke_job '$AO' | grep -qF './install.sh --dry-run'"
check "capsule-corp: install-smoke runs the CLI --help" "install_smoke_job '$AO' | grep -qF -- '-m capsule --help'"
check "capsule-corp: install-smoke uses a temp CAPSULE_HOME" "install_smoke_job '$AO' | grep -qF 'CAPSULE_HOME=\"\$(mktemp -d)'"

echo; if [ $fails -eq 0 ]; then echo "test_ci_yaml: all passed"; else echo "test_ci_yaml: $fails failed"; exit 1; fi
