#!/usr/bin/env python3
"""quality_nightly.py - the free layer of the testing standard (design
docs/superpowers/specs/2026-09-10-correctness-over-green-design.md section 7.4).

Four phases on ONE repo per night (the repos rotate, see `next_repo`: two repos' worth of phase
timeouts do not fit run.sh's 3600s cap, and a capped run is a non-zero exit, the one line this job
must never put on the owner's phone), no model anywhere: a rotating `mutmut` run on ONE module, the
suite five times to find flakes, a dependency audit and a secret scan. It NEVER blocks and NEVER
pages: its only outputs are lines in the launchd log and `## Findings` lines in Boards/Intake.md,
added through `intake.py add-finding`, which the owner turns into cards by moving them.

Every phase runs inside the worker image, `--network none`, on a throwaway clone of the repo's
`main` (`merge_gate.run_in_clone`, the quality gate's door). Nothing here writes to a product repo:
quarantining a flaky test is a CARD, because a worker that may edit a test to make a run green is
the configuration the tamper guard exists to prevent.

State (rotation index, flake counts, quarantine list, the 14-day record of findings already filed)
is host-side under `merge_gate.state_dir()/quality-nightly/<repo>.json`, mode 0700; no worker can
read or write it.

CLI: `quality_nightly.py [--repos a,b] [--phases mutation,flake,deps,secrets] [--dry-run]`.
`--repos` is the ROTATION, not a list to run tonight: one runs, and the index advances. `--dry-run`
prints the lines it would add and writes nothing at all. The exit code is always 0.
"""
import argparse
import datetime as _dt
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # `capsule` when run as a script

import merge_gate as mg      # noqa
import scrub                 # noqa
from capsule import host     # noqa: E402

PHASES = ("mutation", "flake", "deps", "secrets")
# Opt-in, never in the default night: the full landing suites (D273) take far longer than the
# 3600s job cap, so they run as `--phases full` from a job of their own (owner's launchd to add).
FULL_PHASE = "full"
FULL_REPOS = ("capsule-corp",)
FULL_TIMEOUT = 5400
# The board's own contract commands, resolved the way the lander gets them (capsule's config loader
# on the board's live config under the capsule home), so the nightly and the landing never
# disagree about what "green" is. The checked-in example is only the fallback.
BOARD = "capsule-corp"
EXAMPLE_CONFIG = (Path(__file__).resolve().parents[1] / "capsule"
                  / "config.capsule-corp.example.json")


TAIL_LINES = 40


def _tail(out):
    """The last TAIL_LINES lines of a red suite's output, redacted (scrub.py), indented under its
    verdict line: a verdict alone cannot be diagnosed from the launchd log."""
    rows = scrub.scrub(out or "").replace("\r", "\n").splitlines()[-TAIL_LINES:]
    return ["    " + r for r in rows]


def _suite_name(cmd):
    m = re.search(r"discover -s (\w+)/|--shard (\w+)", cmd)
    return m.group(1) or f"shell {m.group(2)}" if m else cmd[:30]


def _full_suites(home=None):
    """((name, command), ...): the board's `contract_commands`, each with stderr merged."""
    from capsule import config as capsule_config, paths
    live = paths.config_path(Path(home) if home else None, BOARD)
    try:
        cmds = capsule_config.load(live)["contract_commands"]
    except (OSError, ValueError, KeyError) as exc:
        print(f"quality-nightly: board config {live} unusable ({type(exc).__name__}); "
              "using the example config", file=sys.stderr)
        cmds = json.loads(EXAMPLE_CONFIG.read_text())["contract_commands"]
    return tuple((_suite_name(c), f"{c} 2>&1") for c in cmds)

ROTATION_STATE = "_rotation"
# ONE repo per night, rotated: the four phases' worst case is 1500 + 5*300 + 2*150 = 3300s, which
# fits run.sh's 3600s cap with 300s of margin for the eight clone/`docker run` set-ups. Two repos
# would be 6600s, and a capped run is a non-zero exit (`- failed jobs: quality-nightly` in the daily
# digest), which this job promises never to cause. A test pins the per-night sum below the cap.
JOB_CAP_SECONDS = 3600
JOB_BUDGET_MARGIN = 300
MUTATION_TIMEOUT = 1500
FLAKE_TIMEOUT = 300
# The two scans are seconds' work (pip-audit fails fast with no network, gitleaks reads a small
# history), so they get the smallest slice; the margin above comes out of them, not mutation.
SCAN_TIMEOUT = 150
FLAKE_RUNS = 5
MAX_SECRET_FINDINGS = 3
FLAKE_MEMORY_DAYS = 30
FINDING_MEMORY_DAYS = 14
MAIN_REF = "refs/remotes/origin/main"

_MUTMUT_RE = re.compile(r"(\d+)\s*/\s*(\d+)")
_KILLED_RE = re.compile(r"🎉\s*(\d+)")
_SURVIVED_RE = re.compile(r"🙁\s*(\d+)")
# Defensive only: mutmut 3.7's print_stats renders the emoji line above (the fixture is that line
# verbatim). If a build prints an ASCII summary, this reads it; if it prints something else, the
# phase says "unparseable" and opens no card, so guessing here is safe.
_ASCII_STATS_RE = re.compile(r"killed\D{0,3}(\d+).*?survived\D{0,3}(\d+)", re.I | re.S)
_ABSENT_RE = re.compile(r"^(?:\S*sh|mutmut|python3?):.*not found|^not found|^No module named mutmut",
                        re.M)
_UNITTEST_FAIL_RE = re.compile(r"^(?:FAIL|ERROR):\s+(\w+)\s+\(([\w.]+)\)")
# Evidence that a suite actually ran, rather than dying before the first test: unittest prints
# `Ran <n> tests` and then `OK` or `FAILED (...)`. Without one the run is a skip, never "unanimous".
_SUITE_RAN_RE = re.compile(r"^Ran \d+ tests?\b|^OK\b|^FAILED \(", re.M)
# The directory a `python3 -m unittest discover <dir>` command hands to
# unittest, which is also what mutmut's pytest run has to be pointed at.
_DISCOVER_RE = re.compile(r"unittest\s+discover\s+(?:-s\s+|--start-directory[= ])?([^\s-]\S*)")


# ------------------------------------------------------------------ state --
def state_path(repo):
    d = mg.state_dir(create=True) / "quality-nightly"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d / f"{repo}.json"


def load_state(repo):
    """A malformed or missing state file reads as empty state, never a crash: a nightly job that
    dies on its own bookkeeping is worse than one that restarts the rotation."""
    try:
        d = json.loads(state_path(repo).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def save_state(repo, state):
    p = state_path(repo)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(p)


# --------------------------------------------------------------- rotation --
def next_module(repo, modules, state):
    """One module per night, sorted so the rotation is reproducible, index kept
    in state. Returns None when the repo has no non-exempt module."""
    mods = sorted(modules or [])
    if not mods:
        return None
    i = int(state.get("mutation_index") or 0) % len(mods)
    state["mutation_index"] = (i + 1) % len(mods)
    return mods[i]


def next_repo(repos, state):
    """The ONE repo tonight, rotated in the order given, index kept in its own state file
    (`_rotation.json`). Two repos in one night is 6600s of phase timeouts under a 3600s cap; one
    per night gives every phase its full timeout. Returns None when the list is empty."""
    rs = [r for r in (repos or []) if r]
    if not rs:
        return None
    try:
        i = int(state.get("repo_index") or 0) % len(rs)
    except (TypeError, ValueError):      # a hand-edited or corrupt index starts over
        i = 0
    state["repo_index"] = (i + 1) % len(rs)
    return rs[i]


EXCLUDED_DIRS = ("tests", ".venv", "venv", "node_modules", "build", "dist",
                 ".git", "__pycache__", ".worktrees")


def repo_modules(repo_root):
    """Every mutatable .py under the repo, sorted, repo-relative. It walks `**/*.py` rather than
    the top level, since a repo whose code lives in a package would otherwise rotate over nothing.
    Tests are not mutated, and `__init__.py`/dunder files are skipped: almost always import glue
    with no behaviour to kill."""
    root = Path(repo_root)
    out = []
    for p in root.rglob("*.py"):
        rel = p.relative_to(root)
        if any(part in EXCLUDED_DIRS for part in rel.parts[:-1]):
            continue
        name = rel.name
        if name.startswith("test_") or (name.startswith("__") and name.endswith("__.py")):
            continue
        out.append(rel.as_posix())
    return sorted(out)


# --------------------------------------------------------------- mutation --
def parse_mutmut(text):
    """(killed, survived, total) from `mutmut run`'s own output. `mutmut results` prints a list,
    not counts: the numbers are in the stats line `mutmut run` renders (`N/M  🎉 killed 🫥 ⏰ 🤔 🙁
    survived 🔇`), rewritten in place with a carriage return, so the LAST such line is the final
    tally. (0, 0, 0) means unparseable, and the phase turns that into a skip line.

    The 🎉 group is REQUIRED: the first number of `N/M` is mutants *processed*, not killed, so a
    progress line without the emoji summary (a truncated tail, a non-UTF-8 locale, a `41/57` in a
    traceback) would otherwise be reported as a tally nobody can reproduce.
    """
    for line in reversed([ln for ln in (text or "").replace("\r", "\n").splitlines() if ln.strip()]):
        m = _MUTMUT_RE.search(line)
        if not m:
            continue
        total = int(m.group(2))
        k = _KILLED_RE.search(line)
        if not k:
            continue
        killed = int(k.group(1))
        s = _SURVIVED_RE.search(line)
        return killed, (int(s.group(1)) if s else max(total - killed, 0)), total
    a = _ASCII_STATS_RE.search(text or "")
    if a:
        killed, survived = int(a.group(1)), int(a.group(2))
        return killed, survived, killed + survived
    return 0, 0, 0


def test_selection(test_cmd):
    """The test directory out of the repo's own test command, or "". `mg.test_cmd_for` returns
    `python3 -m unittest discover maintenance/tests` for capsule-corp and `... discover tests` for
    other repos; the directory is the only part mutmut's runner needs (see mutmut_config_cmd)."""
    m = _DISCOVER_RE.search(test_cmd or "")
    return m.group(1) if m else ""


def mutmut_config_cmd(module, test_cmd=None):
    """mutmut 3.7 has no `--paths-to-mutate`: `source_paths` comes from config, and so does the
    test run. There is no `runner` key in mutmut 3.x, which always drives **pytest** in-process;
    the keys it documents for the test run are `source_paths` and
    `pytest_add_cli_args_test_selection` (plus `pytest_add_cli_args`). So `pytest` is pinned beside
    `mutmut` in the python stack Dockerfile, and the directory from the repo's own `python3 -m
    unittest discover <dir>` goes into `pytest_add_cli_args_test_selection` so pytest collects that
    repo's unittest TestCases natively.

    The config is written INSIDE the throwaway clone, right before the run, never in a product
    repo. setup.cfg is the default; if the repo already carries a `[mutmut]` section there, a second
    would be a duplicate, so the override goes to pyproject.toml's `[tool.mutmut]`. Values go
    through printf's `%s` args, never its format string, so a `%` in a path cannot corrupt the file.
    """
    sel = test_selection(test_cmd)
    ini, toml = "[mutmut]\\nsource_paths = %s\\n", "[tool.mutmut]\\nsource_paths = [\"%s\"]\\n"
    args = f"'{module}'"
    if sel:
        ini += "pytest_add_cli_args_test_selection = %s\\n"
        toml += "pytest_add_cli_args_test_selection = [\"%s\"]\\n"
        args += f" '{sel}'"
    return (f"if [ ! -f setup.cfg ]; then "
            f"printf '{ini}' {args} > setup.cfg; "
            f"elif ! grep -q '^\\[mutmut\\]' setup.cfg; then "
            f"printf '\\n{ini}' {args} >> setup.cfg; "
            f"else printf '\\n{toml}' {args} >> pyproject.toml; fi")


def _mutmut_missing(rc, out):
    """The deployed worker image predates the mutmut pin in the Dockerfile, so until the owner
    rebuilds it the runner reports the tool absent: `sh -c` exits 127 and says "not found". That
    is a skip line and NO finding; a missing tool is never a quality signal about the repo.
    Matched at line start only, so a mutmut summary that contains the words is still a real result.
    Both arms are live: the command writes mutmut's output to a file and re-exits with its rc
    (never `| tail`, which would own the exit status), so `rc == 127` really is mutmut's."""
    return rc == 127 or bool(_ABSENT_RE.search(out or ""))


def _hold(state, index):
    """Put the rotation index back: a night that never ran mutmut must not cost a module its turn,
    or a fortnight of a missing tool silently skips the whole repo. Tomorrow tries the same
    module."""
    if index is None:
        state.pop("mutation_index", None)
    else:
        state["mutation_index"] = index


def phase_mutation(repo, repo_root, *, modules, state, test_cmd=None, runner=None,
                   today=None, image=None):
    runner = runner or mg.run_in_clone
    before = state.get("mutation_index")
    module = next_module(repo, modules, state)
    if module is None:
        return [f"mutation: {repo} - skipped (no module to mutate)"], []
    # File first, THEN tail, then re-exit with mutmut's own rc: a pipeline's exit status is the
    # tail's, which would hide every mutmut crash (127 included) behind "rc 0 + unparseable output".
    cmd = (f"{mutmut_config_cmd(module, test_cmd)}; "
           f"mutmut run >/tmp/mutmut.log 2>&1; rc=$?; tail -c 600 /tmp/mutmut.log; exit $rc")
    try:
        rc, out = runner(repo_root, MAIN_REF, cmd, timeout=MUTATION_TIMEOUT, image=image)
    except mg.InfraError as exc:
        _hold(state, before)
        return [f"mutation: {repo} - skipped ({exc})"], []
    if rc == 124:
        _hold(state, before)
        return [f"mutation: {repo} {module} - skipped (timed out after {MUTATION_TIMEOUT}s)"], []
    if _mutmut_missing(rc, out):
        _hold(state, before)
        return [f"mutation: {repo} {module} - skipped (mutmut not in the worker image)"], []
    killed, survived, total = parse_mutmut(out)
    if total <= 0:
        # A run we cannot read is a run that did not happen: hold the rotation
        # and say so, rather than reporting a fictional "0/0 killed".
        _hold(state, before)
        return [f"mutation: {repo} {module} - skipped (unparseable mutmut output)"], []
    line = f"mutation: {repo} {module} - {killed}/{total} killed, {survived} survived"
    if not survived:
        return [line], []
    # ONE finding, never one per mutant: a nightly job that files sixteen cards gets turned off
    # on day two.
    return [line], [(repo, "low", f"add tests that kill the {survived} surviving mutants in {module}")]


# ------------------------------------------------------------------ flake --
def _failed_tests(text):
    """{test_id} from one unittest run's output."""
    out = set()
    for line in (text or "").splitlines():
        m = _UNITTEST_FAIL_RE.match(line.strip())
        if m:
            out.add(f"{m.group(2)}.{m.group(1)}")
    return out


def parse_flakes(run_results):
    """`{test_id: fail_count}` for tests that were NOT unanimous across the runs. A test that
    failed every time is a genuine red, not a flake; the merge gate already owns it."""
    seen = {}
    n = len(run_results or [])
    for run in run_results or []:
        for test_id, passed in run.items():
            seen.setdefault(test_id, []).append(bool(passed))
    return {t: r.count(False) for t, r in seen.items()
            if 0 < r.count(False) < n}


def _prune_flake_nights(counts, today):
    """Forget nights older than 30 days before counting: two flakes a quarter apart are two
    different bugs, and unbounded state grows a card for a test fixed last month."""
    try:
        cut = (_dt.date.fromisoformat(today) - _dt.timedelta(days=FLAKE_MEMORY_DAYS)).isoformat()
    except (TypeError, ValueError):
        return counts
    for test_id, nights in list(counts.items()):
        kept = sorted(n for n in (nights or []) if str(n) >= cut)
        if kept:
            counts[test_id] = kept
        else:
            counts.pop(test_id)
    return counts


def quarantine(state, test_id, today):
    """True the SECOND night a test flakes (quarantine on the second flake, with a card). The count
    is per test id and the quarantine list is recorded so the same card is not filed every night."""
    counts = _prune_flake_nights(state.setdefault("flake_nights", {}), today)
    already = state.setdefault("quarantined", [])
    if test_id in already:
        return False
    nights = set(counts.get(test_id) or [])
    nights.add(today)
    counts[test_id] = sorted(nights)
    if len(nights) >= 2:
        already.append(test_id)
        return True
    return False


def phase_flake(repo, repo_root, *, test_cmd, state, runner=None, today=None, image=None):
    runner = runner or mg.run_in_clone
    today = today or _dt.date.today().isoformat()
    runs, lines = [], []
    for _ in range(FLAKE_RUNS):
        try:
            rc, out = runner(repo_root, MAIN_REF, f"{test_cmd} 2>&1", timeout=FLAKE_TIMEOUT, image=image)
        except mg.InfraError as exc:
            return [f"flake: {repo} - skipped ({exc})"], []
        if rc == 124:
            return [f"flake: {repo} - skipped (a run timed out after {FLAKE_TIMEOUT}s)"], []
        # A run that died before the first test (import error, bad test_cmd, rc 2, a traceback)
        # parses as "nothing failed", and five of those would read as `unanimous over 5 runs`.
        # Demand evidence the suite ran: unittest's `Ran <n> tests` / `OK` / `FAILED (` line, and
        # an rc a real suite can produce (0 pass, 1 fail).
        if rc not in (0, 1) or not _SUITE_RAN_RE.search(out or ""):
            return [f"flake: {repo} - skipped (suite did not run, rc {rc})"], []
        failed = _failed_tests(out)
        runs.append({t: False for t in failed})
    for run in runs:
        for t in {t for r in runs for t in r}:
            run.setdefault(t, True)
    flaky = parse_flakes(runs)
    if not flaky:
        return [f"flake: {repo} - unanimous over {FLAKE_RUNS} runs"], []
    findings = []
    named = ", ".join(f"{t} ({n}/{FLAKE_RUNS} failed)" for t, n in sorted(flaky.items()))
    lines.append(f"flake: {repo} - {len(flaky)} not unanimous over {FLAKE_RUNS} runs: {named}")
    for test_id in sorted(flaky):
        if quarantine(state, test_id, today):
            findings.append((repo, "med", f"quarantine {test_id} - flaky 2 nights"))
    return lines, findings


def _phase_full(repo, repo_root, *, runner=None, today=None, image=None):
    """The whole suite a selected landing skipped (D273): capsule, maintenance and the shell suites
    in the worker clone of main. A red suite is one finding (a missed break shows up in Intake the
    next morning); a suite that could not run is a skip line, never a pass or a finding. The blind
    suites need the lander's docker and a card, so they stay with the lander and its blind step."""
    if repo not in FULL_REPOS:
        return [f"full: {repo} - skipped (no full-suite recipe)"], []
    runner = runner or mg.run_in_clone
    lines, findings, verdicts = [], [], 0
    try:
        suites = _full_suites()
    except Exception as exc:                  # no suite list to run: say so, never a quiet skip
        return ([f"full: {repo} - skipped (board config unusable: {type(exc).__name__})"],
                [(repo, "medium", "nightly full suite could not read the board config "
                  f"({type(exc).__name__}) - selected landings are unchecked")])
    for name, cmd in suites:
        try:
            rc, out = runner(repo_root, MAIN_REF, cmd, timeout=FULL_TIMEOUT, image=image)
        except mg.InfraError as exc:
            lines.append(f"full: {repo} {name} - skipped ({exc})")
        else:
            verdicts += rc != 124
            word = "green" if rc == 0 else "timed out" if rc == 124 else f"red (rc {rc})"
            lines.append(f"full: {repo} {name} - {word}")
            if rc == 124:                     # no verdict: a skip line, never a finding
                continue
            if rc != 0:
                lines.extend(_tail(out))
                findings.append((repo, "high", f"full {name} suite red on main (rc {rc}) - "
                                 "a selected landing missed a break"))
    if not verdicts:                          # all skipped or timed out: never a silent pass
        findings.append((repo, "medium", "nightly full suite produced no verdict (docker down or "
                         "timeout) - selected landings are unchecked"))
    return lines, findings


# ------------------------------------------------------------------- deps --
_VULN_RE = re.compile(r"Found (\d+) known vulnerabilit")
# The ONLY two things pip-audit prints when it completed a clean audit. Anything else (a
# connection error, a traceback, "Max retries exceeded") is a scan that did not happen.
_AUDIT_CLEAN_RE = re.compile(r"No known vulnerabilities found|Found 0 known vulnerabilit")


def phase_deps(repo, repo_root, *, runner=None, today=None, image=None):
    """pip-audit, or an honest skip line. ONE tool on purpose: an osv-scanner fallback would need a
    second output parser, and every extra parser is another way to mis-read a scan as clean.

    pip-audit queries PyPI's advisory service and the container runs `--network none`, so on a
    machine without a local database this is a skip; the real audit lives in CI, and a fictional
    "clean" line would be worse than an honest skip."""
    runner = runner or mg.run_in_clone
    cmd = ("if command -v pip-audit >/dev/null 2>&1; then pip-audit --progress-spinner=off 2>&1; "
           "else echo 'pip-audit: not found'; exit 127; fi")
    try:
        rc, out = runner(repo_root, MAIN_REF, cmd, timeout=SCAN_TIMEOUT, image=image)
    except mg.InfraError as exc:
        return [f"deps: {repo} - skipped ({exc})"], []
    if rc in (124, 127) or "not found" in (out or ""):
        return [f"deps: {repo} - skipped (pip-audit not in the worker image)"], []
    m = _VULN_RE.search(out or "")
    n = int(m.group(1)) if m else 0
    if n == 0:
        # Clean requires BOTH a zero exit and pip-audit's own success line. The container runs
        # --network none, so a real run fails with whatever its HTTP stack says, none of which say
        # "not found". Reporting that as "no known vulnerabilities" would be a scanner that never
        # scanned calling itself clean.
        if rc == 0 and _AUDIT_CLEAN_RE.search(out or ""):
            return [f"deps: {repo} - no known vulnerabilities"], []
        return [f"deps: {repo} - skipped (audit failed, rc {rc})"], []
    return ([f"deps: {repo} - {n} known vulnerabilities"],
            [(repo, "high", f"upgrade the {n} dependencies with known vulnerabilities")])


# ---------------------------------------------------------------- secrets --
def phase_secrets(repo, repo_root, *, runner=None, today=None, image=None):
    """gitleaks with the repo's own .gitleaks.toml (the config CI's security-scan job passes through
    GITLEAKS_CONFIG) or, in a repo with no such file, gitleaks' default ruleset. `--config` is
    conditional because only capsule-corp carries a `.gitleaks.toml`: hard-coding it made a repo
    without one's scan exit non-zero on a config error and print
    `skipped (unreadable gitleaks report)` nightly.
    `--redact` is passed either way, so no matched value can reach a line or a card."""
    runner = runner or mg.run_in_clone
    cmd = ("if command -v gitleaks >/dev/null 2>&1; then "
           "if [ -f .gitleaks.toml ]; then cfg='--config .gitleaks.toml'; else cfg=''; fi; "
           "gitleaks git --no-banner --redact --report-format json --report-path /dev/stdout "
           "$cfg 2>/dev/null; else echo 'gitleaks: not found'; exit 127; fi")
    try:
        rc, out = runner(repo_root, MAIN_REF, cmd, timeout=SCAN_TIMEOUT, image=image)
    except mg.InfraError as exc:
        return [f"secrets: {repo} - skipped ({exc})"], []
    if rc in (124, 127) or "not found" in (out or ""):
        return [f"secrets: {repo} - skipped (gitleaks not on PATH)"], []
    if rc == 0:
        # gitleaks exits 0 iff it found nothing, whatever it printed - trusting
        # the exit code here means a banner or an empty report is still "clean".
        return [f"secrets: {repo} - gitleaks clean (0 findings)"], []
    # rc != 0 means gitleaks claims leaks, so the report is the whole signal: empty, truncated or
    # not a JSON list is a SKIP, never "clean". Calling a failed scan clean is the one outcome a
    # secret scanner must not produce; rc is in the line so the log says which failure it was.
    unreadable = [f"secrets: {repo} - skipped (unreadable gitleaks report, rc {rc})"]
    try:
        report = json.loads((out or "").strip())
    except ValueError:
        return unreadable, []
    if not isinstance(report, list) or not report:
        return unreadable, []
    lines = [f"secrets: {repo} - gitleaks {len(report)} findings"]
    findings = []
    # Capped at three: the finding carries the FILE AND LINE, never the secret, and a night that
    # leaks fifty lines into Intake.md is a night nobody reads.
    for hit in report[:MAX_SECRET_FINDINGS]:
        where = f"{hit.get('File', '?')}:{hit.get('StartLine', '?')}"
        findings.append((repo, "high", f"fix the gitleaks finding in {where}"))
    return lines, findings


# --------------------------------------------------------------- findings --
def _filed_key(phase, repo, title):
    """`|` cannot appear in a title (intake.py refuses one), so it is a safe
    separator and the key stays readable in the state file."""
    return f"{phase}|{repo}|{title}"


def _prune_filed(filed, today):
    """Forget filings older than 14 days, so the memory stays bounded and a finding the owner never
    actioned comes back after a fortnight rather than never."""
    try:
        cut = (_dt.date.fromisoformat(today) - _dt.timedelta(days=FINDING_MEMORY_DAYS)).isoformat()
    except (TypeError, ValueError):
        return filed
    for key, when in list(filed.items()):
        if not isinstance(when, str) or when < cut:
            filed.pop(key)
    return filed


def filed_recently(state, phase, repo, title, today):
    """True when this exact `(repo, phase, title)` already went to Intake in the last 14 days.
    Without this the deps and mutation phases re-file the same line every night until `##
    Findings` is unreadable. The flake phase has a stronger memory (`quarantined` is permanent);
    this is the floor under all four."""
    return _filed_key(phase, repo, title) in _prune_filed(state.setdefault("filed", {}), today)


def mark_filed(state, phase, repo, title, today):
    state.setdefault("filed", {})[_filed_key(phase, repo, title)] = today


def add_finding(board, risk, title, today, runner=None):
    """One `## Findings` line through intake.py's add-finding verb, the door the security and
    agent-review jobs use and the only write this job makes. A refused line (bad board, '|' in
    the title) is reported and dropped, never retried."""
    argv = [sys.executable, str(Path(__file__).resolve().parent / "intake.py"),
            "add-finding", board, risk, title, "--source", f"quality-nightly {today}"]
    run = runner or (lambda a, **kw: mg._default_sh(a, timeout=60))
    rc, out = run(argv)
    return rc == 0, (out or "").strip()


# -------------------------------------------------------------------- run --
def run_repo(repo, *, state=None, runner=None, today=None, phases=PHASES, image=None):
    """Every phase for one repo. Each phase is wrapped: one failure is one line and the rest still
    run. Returns (lines, findings). A finding this repo/phase filed in the last 14 days is dropped
    to a log line. The filing is recorded HERE, before intake sees it, so a line intake refuses is
    also suppressed for a fortnight ("reported and dropped, never retried", applied to the loop)."""
    today = today or _dt.date.today().isoformat()
    state = load_state(repo) if state is None else state
    bb = mg._bridge()          # lazy, like merge_gate: board_bridge imports it back
    root = bb.REPO_PATHS.get(repo)
    if not root or not Path(root).exists():
        return [f"{repo}: skipped (no checkout at {root})"], []
    # Only the flake and mutation phases need the operator env (the repo's test command), so a
    # deps/secrets-only run reads nothing from ~/.config.
    needs_cmd = bool({"flake", "mutation"} & set(phases))
    env = bb._ops_env() if needs_cmd else {}
    test_cmd = mg.test_cmd_for(repo, env) if needs_cmd else ""
    lines, findings = [], []
    for phase in phases:
        try:
            if phase == "mutation":
                got, f = phase_mutation(repo, root, modules=repo_modules(root), state=state,
                                      test_cmd=test_cmd, runner=runner, today=today, image=image)
            elif phase == "flake":
                got, f = phase_flake(repo, root, test_cmd=test_cmd, state=state,
                                   runner=runner, today=today, image=image)
            elif phase == FULL_PHASE:
                got, f = _phase_full(repo, root, runner=runner, today=today, image=image)
            elif phase == "deps":
                got, f = phase_deps(repo, root, runner=runner, today=today, image=image)
            elif phase == "secrets":
                got, f = phase_secrets(repo, root, runner=runner, today=today, image=image)
            else:
                raise ValueError(f"unknown phase {phase!r}")
        except Exception as exc:                      # never let one phase kill the night
            got, f = [f"{phase}: {repo} - skipped ({exc})"], []
        lines.extend(got)
        # One dedupe gate for all four phases, here rather than inside each: the phases stay pure
        # functions of their tool's output and this is the only place that knows phase and state.
        for finding in f:
            board, _risk, title = finding
            if filed_recently(state, phase, board, title, today):
                lines.append(f"{phase}: {repo} - already filed within "
                             f"{FINDING_MEMORY_DAYS} days: {title}")
                continue
            mark_filed(state, phase, board, title, today)
            findings.append(finding)
    return lines, findings


def main(argv=None, *, runner=None, finding_runner=None):
    """`runner`/`finding_runner` exist for the suite only: the default is the real worker clone and
    the real intake.py, and the tests inject fakes so no test starts docker or writes to a vault."""
    ap = argparse.ArgumentParser(prog="quality_nightly.py", description="the nightly testing layer")
    ap.add_argument("--repos", default=",".join(host.get_or_default("quality.repos")))
    ap.add_argument("--phases", default=",".join(PHASES))
    ap.add_argument("--dry-run", action="store_true", help="print the lines, add no findings, save no state")
    ns = ap.parse_args(argv)
    today = _dt.date.today().isoformat()
    phases = tuple(p.strip() for p in ns.phases.split(",") if p.strip() in PHASES + (FULL_PHASE,))
    repos = tuple(r.strip() for r in ns.repos.split(",") if r.strip())
    # ONE repo per night, rotated: see next_repo and JOB_BUDGET_MARGIN. With
    # `--repos <one>` the rotation is a no-op and that repo runs.
    full_only = phases == (FULL_PHASE,)       # its own job: names its repo, leaves the rotation be
    rotation = load_state(ROTATION_STATE)
    repo = (repos[0] if repos else None) if full_only else next_repo(repos, rotation)
    if repo is None:
        print("quality-nightly: no repo to run")
        return 0
    print(f"quality-nightly: {repo} tonight ({len(repos)} in the rotation)")
    state = load_state(repo)
    lines, findings = run_repo(repo, state=state, runner=runner, today=today, phases=phases)
    for line in lines:
        print(line)
    for board, risk, title in findings:
        if ns.dry_run:
            print(f"would add: - repo: {board} | risk: {risk} | {title} (quality-nightly {today})")
            continue
        ok, msg = add_finding(board, risk, title, today, runner=finding_runner)
        print(f"finding: {'added' if ok else 'refused'} - {title}" + (f" ({msg})" if not ok else ""))
    if not ns.dry_run:
        save_state(repo, state)
        if not full_only:
            save_state(ROTATION_STATE, rotation)
    return 0        # this job never blocks and never pages


if __name__ == "__main__":
    sys.exit(main())
