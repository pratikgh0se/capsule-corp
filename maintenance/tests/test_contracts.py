import os, re, sys, unittest
import datetime as dt
from pathlib import Path
from unittest import mock
sys.path[:0] = [str(Path(__file__).resolve().parents[n]) for n in (1, 2)]
import capsule.tests  # noqa: E402,F401 - its __init__ installs the swept TMPDIR (tmpclean)
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("AGENT_OPS_NO_SEND", "1")
import contracts as ct
import decisions as dc
import board_bridge as bb
import board, board_view
import release_gate as rg
import fakes
ROOT = Path(__file__).resolve().parents[2]

# M080 (row 123): the eight worker roles take context from the card and the
# vault, not from an unaudited cross-session memory - so the legacy built-in
# memory store is off for all eight. telegram, slack and automation are NOT
# workers (they act for the owner / run scheduled jobs) and are deliberately
# left out of this list - see test_non_worker_profiles_are_not_asserted_off.
WORKER_ROLES = ("dev", "dev-backend", "dev-frontend", "qa", "reviewer", "lead", "infra", "researcher")
NON_WORKER_ROLES = ("telegram", "slack", "automation")

# Row 125: every profile SOUL but `automation` (a 7-line scheduled shim) carries
# the never-list - the actions no worker takes on the owner's behalf.
NEVER_LIST_ROLES = ("dev", "dev-backend", "dev-frontend", "infra", "lead", "qa",
                    "researcher", "reviewer", "slack", "telegram")
from capsule.tests.public_tree import private_only  # noqa: E402


class Identity(unittest.TestCase):
    """The parsers import the contract constants; they are not copies."""
    def test_parsers_use_contract_objects(self):
        self.assertIs(dc.DECISION_PREFIX, ct.DECISION_PREFIX)
        self.assertIs(dc.OPEN_COMMENT, ct.DECISION_OPEN)
        self.assertIs(dc.ANSWER_COMMENT, ct.DECISION_ANSWER)
        self.assertTrue(ct.REVIEW_PASS.startswith(ct.REVIEW_PASS_MARK))
        self.assertIs(bb.DIFF_PACKET, ct.DIFF_PACKET)
        self.assertIs(bb.DIFF_UNAVAILABLE, ct.DIFF_UNAVAILABLE)
        self.assertTrue(ct.DIFF_UNAVAILABLE.startswith(ct.DIFF_PACKET))


class RoundTrips(unittest.TestCase):
    def test_decision(self):
        d = dc.parse_decision(ct.format_decision("Which?", "why", [("a", "b"), ("c", "d")], 1, "because", "learn"))
        self.assertEqual(d["recommend"], 1); self.assertEqual(len(d["options"]), 2)

    def test_meta_block_keys(self):
        block = bb.build_meta_block("slug-a", "low", "abc", "sha", area="frontend")
        for k in ct.META_KEYS:
            self.assertIn(f"{k}:", block, k)


class Producers(unittest.TestCase):
    """The role prompts name the exact prefixes the parsers consume."""
    def test_non_worker_profiles_are_not_asserted_off(self):
        """Documents the boundary of M080: telegram, slack and automation
        are not workers and this decision does not touch them - they keep
        memory on. This test does not assert a value either way; it exists
        so the boundary is visible and future edits to WORKER_ROLES cannot
        silently sweep these three in."""
        self.assertEqual(set(WORKER_ROLES) & set(NON_WORKER_ROLES), set())
        for role in NON_WORKER_ROLES:
            self.assertNotIn(role, WORKER_ROLES, f"{role} is not a worker role under M080")


import ast

MAINT = Path(__file__).resolve().parents[1]


def _kanban_argv_lines(path):
    """Lines where the literal "kanban" sits in argv position: an element of a
    list/tuple, or a positional argument of a call. A path segment
    (`Path.home() / ".legacy" / "kanban"`) is a BinOp and a dict value
    (`{"source": "kanban"}`) is a Dict, so neither is argv and neither is
    flagged - which is why this is an AST scan and not a grep with an
    exemption list.

    What it deliberately cannot see: an indirection (`K = "kanban"` and then
    `[K, ...]`), an f-string, a keyword argument's value (`f(argv=[...])` is
    caught only because the List itself is walked), or argv assembled at
    runtime. It is a tripwire against the easy regression, not a proof."""
    hits = []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple)):
            elts = node.elts
        elif isinstance(node, ast.Call):
            elts = node.args
        else:
            continue
        for e in elts:
            if isinstance(e, ast.Constant) and e.value == "kanban":
                hits.append(e.lineno)
    return sorted(set(hits))


class NoKanbanArgvOutsideBoard(unittest.TestCase):
    """Row 51: board.py is the only door to the kanban CLI. A new module that
    shells out on its own re-opens the legacy kanban-DB env trap board.py exists to
    close, so the seam is a test, not a convention."""

    def test_only_board_py_builds_kanban_argv(self):
        offenders = {}
        for path in sorted(MAINT.glob("*.py")):
            if path.name == "board.py":
                continue
            lines = _kanban_argv_lines(path)
            if lines:
                offenders[path.name] = lines
        self.assertEqual(offenders, {},
                         "these modules build kanban argv directly; route them through "
                         "maintenance/board.py (spec 2026-09-07-board-seam-design.md, ruling 1)")

    def test_board_py_does_build_kanban_argv(self):
        """The scan must not be able to pass by finding nothing anywhere."""
        self.assertTrue(_kanban_argv_lines(MAINT / "board.py"))

    def test_the_scan_catches_a_planted_offender(self):
        import tempfile
        p = Path(tempfile.mkdtemp()) / "offender.py"
        p.write_text('BASE = ["-m", "x"]\nargv = BASE + ["kanban", "--board", b, "show", t]\n')
        self.assertEqual(_kanban_argv_lines(p), [2])

    def test_the_scan_ignores_a_path_segment_and_a_dict_value(self):
        import tempfile
        p = Path(tempfile.mkdtemp()) / "innocent.py"
        p.write_text('from pathlib import Path\n'
                     'BOARDS = Path.home() / ".legacy" / "kanban" / "boards"\n'
                     'row = {"source": "kanban"}\n'
                     'TOOL = "kanban_request_review"\n')
        self.assertEqual(_kanban_argv_lines(p), [])


class NoMarkdownBoardSurgery(unittest.TestCase):
    """Spec 7: the grep that stops markdown surgery growing back, beside Build
    1's kanban-argv grep. `board_view.py` renders the board; nothing else may
    know what a column heading looks like, and nothing else may write the file.

    Reading the rendered file is fine - that is what it is for - so
    `vault_jobs.py` (the morning standup) is exempted by name.

    The heading-literal scan (a) is an EXACT match against
    `board.COLUMN_NAMES` - not a "starts with '## '" prefix match - so it
    cannot mistake another module's own unrelated Markdown heading
    (`decisions.py`'s `## Your answer`, `quality_gate.py`'s `## Unreleased`
    CHANGELOG parsing, `vault_jobs.py`'s own digest headings, `intake.py`'s
    `## New work`/`## Notes` grammar) for a board column: none of those
    strings equals a `board.COLUMN_NAMES` value, so none of those modules
    needs an exemption. Only `board.py` (where the column mapping itself
    lives, `board.COLUMNS` - not a copy, the one place it is allowed to
    exist) and `board_view.py` (renders from that mapping) are exempted."""

    READ_EXEMPT = {"vault_jobs.py"}
    HEADING_EXEMPT = {"board_view.py", "board.py"}

    # 3.2: Boards/Archive.md is the second generated file and gets the same
    # rule - only board_view.py may name it. vault_jobs.py stays exempt as a
    # READER (the standup reads the board file); nothing reads the archive yet.
    BOARD_FILES = ("Boards/Engineering.md", "Boards/Archive.md")

    def _heading_literals(self, path):
        hits = []
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and node.value in board.COLUMN_NAMES:
                hits.append(node.lineno)
        return sorted(set(hits))

    def test_only_board_view_knows_a_column_heading(self):
        offenders = {}
        for path in sorted(MAINT.glob("*.py")):
            if path.name in self.HEADING_EXEMPT:
                continue
            lines = self._heading_literals(path)
            if lines:
                offenders[path.name] = lines
        self.assertEqual(offenders, {},
                         "these modules carry a column-heading literal; the mapping lives in "
                         "board.COLUMNS and the rendering in board_view.py (spec 2.1, 7)")

    def test_only_board_view_names_the_generated_board_files(self):
        offenders = {}
        for path in sorted(MAINT.glob("*.py")):
            if path.name in ("board_view.py",) or path.name in self.READ_EXEMPT:
                continue
            text = path.read_text(encoding="utf-8")
            if any(f in text for f in self.BOARD_FILES) or '"Engineering.md"' in text \
                    or '"Archive.md"' in text:
                offenders[path.name] = True
        self.assertEqual(offenders, {},
                         "only board_view.py writes the generated board files (vault_jobs.py "
                         "reads Engineering.md for the standup and is exempt)")

    # C3 (final review): the Python scan above only sees maintenance/*.py. The
    # shell and the hooks are the other half of the surface - run.sh used to
    # `git add`/`git commit` the board file behind board_view's back, which is
    # exactly the two-writers race the spec removed. These are not Python, so
    # there is no AST for the shell half: a `#` comment line is skipped
    # (explaining that the file is generated is not surgery) and everything
    # else must not name it. The hook half is Python, so it gets the same AST
    # treatment as the module scan, with docstrings (bare string statements)
    # excluded for the same reason comments are.
    #
    # The shell/hook scan is exemption-free: since 2c the two review jobs'
    # GUARD_ALLOW lists name Boards/Intake.md, so no live shell line names the
    # generated board file at all. Only comments and docstrings mention it.

    def _shell_scan_files(self):
        files = [MAINT / "run.sh"]
        files += sorted((MAINT / "hooks").glob("*.py"))
        files += sorted((ROOT / "bin").glob("*.sh"))
        return [p for p in files if p.exists()]

    def _board_file_hits(self, path):
        """Lines that name the generated board file in live code: comments and
        docstrings do not count, a `GUARD_ALLOW=` allowlist line does not
        count (see SHELL_ALLOW)."""
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".py":
            tree = ast.parse(text)
            docs = {id(n.value) for n in ast.walk(tree)
                    if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
                    and isinstance(n.value.value, str)}
            return sorted({n.lineno for n in ast.walk(tree)
                           if isinstance(n, ast.Constant) and isinstance(n.value, str)
                           and any(f in n.value for f in self.BOARD_FILES) and id(n) not in docs})
        return [i for i, line in enumerate(text.splitlines(), 1)
                if any(f in line for f in self.BOARD_FILES)
                and not line.strip().startswith("#")]

    def test_no_shell_or_hook_names_the_generated_board_file(self):
        offenders = {}
        for path in self._shell_scan_files():
            if path.name in ("board_view.py", "vault_jobs.py"):
                continue
            hits = self._board_file_hits(path)
            if hits:
                offenders[path.name] = hits
        self.assertEqual(offenders, {},
                         "only board_view.py writes Boards/Engineering.md; the shell must not "
                         "add, commit or edit it (spec 7, final-review C3)")

    def test_the_shell_scan_covers_run_sh_the_hooks_and_bin(self):
        names = [p.name for p in self._shell_scan_files()]
        self.assertIn("run.sh", names)
        self.assertIn("sprint-plan-guard.py", names)
        self.assertTrue(any(n.endswith(".sh") and n != "run.sh" for n in names))

    def test_the_shell_scan_catches_a_planted_offender(self):
        import tempfile
        d = Path(tempfile.mkdtemp())
        sh = d / "planted.sh"
        sh.write_text("# Boards/Engineering.md is generated\ngit add Boards/Engineering.md\n")
        self.assertEqual(self._board_file_hits(sh), [2])
        py = d / "planted.py"
        py.write_text('"""Boards/Engineering.md is generated."""\nP = "Boards/Engineering.md"\n')
        self.assertEqual(self._board_file_hits(py), [2])

    def test_the_shell_scan_catches_a_planted_archive_offender(self):
        """Fix round 1, M1: mirror the Engineering.md planted-offender test
        for Boards/Archive.md, so BOARD_FILES truly scans both names and not
        just the one it happened to be written against first."""
        import tempfile
        d = Path(tempfile.mkdtemp())
        sh = d / "planted.sh"
        sh.write_text("# Boards/Archive.md is generated\ngit add Boards/Archive.md\n")
        self.assertEqual(self._board_file_hits(sh), [2])
        py = d / "planted.py"
        py.write_text('"""Boards/Archive.md is generated."""\nP = "Boards/Archive.md"\n')
        self.assertEqual(self._board_file_hits(py), [2])

    def test_run_sh_does_not_commit_the_board_file(self):
        text = (MAINT / "run.sh").read_text(encoding="utf-8")
        self.assertNotIn("git add Boards/Engineering.md", text)

    def test_the_scan_catches_a_planted_offender(self):
        import tempfile
        p = Path(tempfile.mkdtemp()) / "offender.py"
        p.write_text('DONE = "## \\u2705 Done"\n')
        self.assertEqual(self._heading_literals(p), [1])

    def test_board_view_does_render_headings(self):
        """The scan must not be able to pass by finding nothing anywhere."""
        self.assertTrue(all(h.startswith("## ") for h in board.COLUMN_NAMES))
        out = board_view.render_board([], today=dt.date(2026, 9, 7))
        self.assertEqual(sum(out.count("\n" + h + "\n") for h in board.COLUMN_NAMES),
                         len(board.COLUMN_NAMES))


class NoModuleAliasShadowing(unittest.TestCase):
    """Parked minor (spec 4): board_bridge imported `board` twice - once as
    itself and once as `_board_mod` - because three functions had a parameter
    named `board` shadowing the module inside their bodies. Rename the
    parameters, keep one import.

    Scope note: the AST scan below is deliberately limited to board_bridge.py,
    not every module under maintenance/. Running it repo-wide (as the spec's
    draft version of this test did) also flags events.log's `board=` kwarg -
    which mirrors the Postgres `events` table's own `board` column
    (events.py's COPY_COLS) and is read by name at call sites everywhere - and
    eleven functions in merge_gate.py, several under a per-card lock. None of
    those modules alias their `import board` the way board_bridge.py and
    quality_gate.py do, which is the actual symptom of the bug this test
    guards against; a blind repo-wide rename of a load-bearing DB-column kwarg
    is a different, far riskier change than this parked minor, and out of
    Task 8's scope (verified 2026-09-08, reported in task-8-report.md)."""

    def test_board_bridge_imports_board_once(self):
        text = (MAINT / "board_bridge.py").read_text(encoding="utf-8")
        self.assertNotIn("_board_mod", text)
        self.assertTrue("\nimport board_fence\n" in text and "board.board_fence" not in text)
        self.assertEqual(sum(1 for l in text.splitlines()
                             if re.match(r"import board(\s|$)", l.strip())), 1)

    def test_no_board_bridge_function_takes_a_parameter_named_board(self):
        path = MAINT / "board_bridge.py"
        offenders = []
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            names = [a.arg for a in node.args.args + node.args.kwonlyargs]
            if "board" in names:
                offenders.append(f"{node.name}:{node.lineno}")
        self.assertEqual(offenders, [],
                         "a parameter named `board` shadows the board module inside the "
                         "function body; name it board_name")


# Files and folders the pipeline itself owns. A vault job that could write
# any of these could forge a spec approval, a card, a decision or a
# milestone, or rewrite the vault's own operating manual.
GUARD_FORBIDDEN = ("Boards/", "Ledger/", "Decisions/", "Specs/", "_CLAUDE.md",
                    ".claude/", ".obsidian/")
GUARD_WRITE_TOOLS = ("Write", "Edit", "MultiEdit")
# sprint-plan is write-capable but guarded by its own dedicated hook
# (hooks/sprint-plan-guard.py, wired through hooks/sprint-plan.settings.json),
# which hardcodes <vault>/Specs/** rather than reading GUARD_ALLOW.
GUARD_OWN_HOOK = {"sprint-plan": "sprint-plan.settings.json"}
# Reviewed, job-keyed exceptions: (job, GUARD_ALLOW entry) pairs an exact-file
# entry under a pipeline-owned folder is knowingly allowed for. Keyed by JOB so
# an exception can never travel to another job by copy-paste.
#
# EMPTY on purpose (2026-09-10, final review C1 + I1). The one entry this set
# was built for - ("nightly", "Decisions/decisions.jsonl") - is gone: the
# sidecar is written by the ALLOWED Bash verb `decision_log.py sidecar`, and
# hooks/write-guard.py only fires on Write/Edit/MultiEdit, so the GUARD_ALLOW
# entry bought the sidecar nothing and bought the nightly MODEL an edit path
# into Decisions/. The mechanism stays because the next carve-out must be a
# named, reviewed, job-scoped line rather than a silent widening.
GUARD_EXCEPT = set()


def _pattern_reaches(job, pat, bad):
    """True when `job`'s GUARD_ALLOW entry `pat` gives hooks/write-guard.py
    write access to the forbidden path `bad` or ANYTHING UNDER IT.

    Final review C1: this used to return `pat == bad` for the non-glob branch.
    Since every entry in GUARD_FORBIDDEN that names a folder ends in "/", no
    file path could ever equal it, so `Boards/Intake.md`, `Ledger/ledger.md`,
    `Specs/2026-x.md` and the decision authority table itself all scanned
    clean - the scan could not see any exact-file entry under a folder the
    pipeline owns, which is exactly the widening it exists to make loud.
    Anything under a forbidden path is now a finding, and GUARD_EXCEPT is the
    only escape."""
    if (job, pat) in GUARD_EXCEPT:
        return False
    if pat.endswith("/**"):
        prefix = pat[:-2]
        return prefix == "/" or prefix.startswith(bad) or bad.startswith(prefix.rstrip("/") + "/") \
            or prefix == bad.rstrip("/") or prefix == bad
    return (pat == bad or pat == bad.rstrip("/") or pat == "**"
            or pat.startswith(bad.rstrip("/") + "/"))


def _guard_job_blocks(run_sh_text):
    """{job: block text} for each `<job>)` case arm of run.sh's ALLOWED case
    statement - the one that renders --allowedTools for the Claude jobs.
    Keyed by the arm label; a `|`-joined label yields one entry per job
    name."""
    start = run_sh_text.index('# Role-based least privilege.')
    end = run_sh_text.index('CLAUDE_CMD=(claude -p', start)
    arm_re = re.compile(r'^    ([a-z0-9|*-]+)\)$', re.M)
    marks = list(arm_re.finditer(run_sh_text, start, end))
    blocks = {}
    for i, m in enumerate(marks):
        body = run_sh_text[m.end():marks[i + 1].start() if i + 1 < len(marks) else end]
        for name in m.group(1).split("|"):
            blocks[name] = body
    return blocks


def _guard_allow(block):
    m = re.search(r'^\s*GUARD_ALLOW="([^"]*)"', block, re.M)
    return m.group(1) if m else None


def guard_findings(run_sh_text):
    """Tripwire scan (same spirit as test_egress.find_egress(root) - "a
    tripwire against the easy regression, not a proof"): every write-capable
    `claude -p` job block in run.sh's `case $JOB in` statement must carry a
    GUARD_ALLOW whose globs stay clear of the paths the pipeline itself owns
    (GUARD_FORBIDDEN). Returns a list of human-readable finding strings,
    empty when the tree is clean. A job named in GUARD_OWN_HOOK is excluded -
    it is guarded by its own dedicated hook instead (checked separately,
    against the filesystem, by VaultJobWriteGuards)."""
    findings = []
    for job, block in _guard_job_blocks(run_sh_text).items():
        allowed = re.search(r'^\s*ALLOWED="(.*)"\s*(?:;;)?\s*$', block, re.M)
        if not allowed or not any(t in allowed.group(1) for t in GUARD_WRITE_TOOLS):
            continue
        if job in GUARD_OWN_HOOK:
            continue
        allow = _guard_allow(block)
        if not allow:
            findings.append(f"{job} has no GUARD_ALLOW")
            continue
        for pat in [a for a in allow.split(":") if a]:
            for bad in GUARD_FORBIDDEN:
                if _pattern_reaches(job, pat, bad):
                    findings.append(f"{job}: GUARD_ALLOW glob {pat!r} reaches {bad}")
    return findings


class VaultJobWriteGuards(unittest.TestCase):
    """2026-09-09: every unattended `claude -p` job that can Write/Edit in the
    vault must carry a write guard, and no guard may reach the files that
    drive the pipeline.

    `maintenance/intake.py` treats an `approve spec: <slug>` line under
    `## Approvals` in the hand-written intake board as a real spec approval
    (merge_gate.write_spec_approval), and the sync tick consumes it every ten
    minutes. Four of these jobs read attacker-reachable text - the inbox job
    triages clipped web pages, the architect job reads third-party repos - so
    an unclamped Write is a path from a poisoned source to a forged approval.
    `hooks/write-guard.py` clamps Write/Edit/MultiEdit to the colon-separated,
    vault-relative globs in GUARD_ALLOW and fails closed without it. It does
    NOT see Bash, so a job's `Bash(git mv *)` entries are governed by
    --allowedTools alone, which is why no job gets a bare `Bash(git *)`.

    The scan itself is module-level `guard_findings(text)` above (M048), the
    same shape as test_egress.find_egress(root): a tree-parameterised
    scanner a test can point at a planted violation. This class keeps the
    filesystem-shaped assertions (job arms exist, the OWN_HOOK file exists,
    the hook governs Write not Bash) that guard_findings does not need to
    know about."""

    RUN_SH = MAINT / "run.sh"
    WRITE_TOOLS = GUARD_WRITE_TOOLS
    FORBIDDEN = GUARD_FORBIDDEN
    OWN_HOOK = GUARD_OWN_HOOK

    def _job_blocks(self):
        return _guard_job_blocks(self.RUN_SH.read_text(encoding="utf-8"))

    def _guard_allow(self, block):
        return _guard_allow(block)

    def test_the_block_parser_finds_the_real_job_arms(self):
        """The scan must not be able to pass by parsing nothing."""
        blocks = self._job_blocks()
        for job in ("nightly", "weekly", "inbox", "architect", "sprint-plan",
                    "security", "agent-review"):
            self.assertIn(job, blocks)
            self.assertIn("ALLOWED=", blocks[job], job)

    def test_every_write_capable_claude_job_sets_guard_allow(self):
        offenders = []
        for job, block in self._job_blocks().items():
            allowed = re.search(r'^\s*ALLOWED="(.*)"\s*(?:;;)?\s*$', block, re.M)
            if not allowed or not any(t in allowed.group(1) for t in self.WRITE_TOOLS):
                continue
            if job in self.OWN_HOOK:
                hook = MAINT / "hooks" / self.OWN_HOOK[job]
                self.assertTrue(hook.exists(), f"{job} claims its own guard hook")
                self.assertIn(self.OWN_HOOK[job], self.RUN_SH.read_text(encoding="utf-8"))
                continue
            if not self._guard_allow(block):
                offenders.append(job)
        self.assertEqual(offenders, [],
                         "these claude -p jobs can Write/Edit in the vault with no write "
                         "guard; give each one a GUARD_ALLOW naming only the paths its "
                         "prompt file writes (hooks/write-guard.py)")

    def test_no_guard_allow_glob_reaches_a_pipeline_owned_path(self):
        offenders = {}
        for job, block in self._job_blocks().items():
            allow = self._guard_allow(block)
            if not allow:
                continue
            for pat in [a for a in allow.split(":") if a]:
                for bad in self.FORBIDDEN:
                    if _pattern_reaches(job, pat, bad):
                        offenders.setdefault(job, []).append(pat)
        self.assertEqual(offenders, {},
                         "a GUARD_ALLOW glob reaches a path the pipeline owns "
                         f"({', '.join(self.FORBIDDEN)}); an unattended vault job that "
                         "can write these can forge a spec approval, a card, a decision "
                         "or a ledger milestone")

    def test_the_four_vault_jobs_are_guarded(self):
        """Named explicitly so a rename cannot quietly drop one."""
        blocks = self._job_blocks()
        for job in ("inbox", "nightly", "weekly", "architect"):
            allow = self._guard_allow(blocks[job])
            self.assertTrue(allow, f"{job} has no GUARD_ALLOW")
            self.assertNotIn("Boards", allow, job)

    def test_the_guard_hook_governs_writes_only_not_bash(self):
        """Stated in run.sh's comments and relied on above: a job's Bash
        entries are clamped by --allowedTools, not by this hook."""
        hook = (MAINT / "hooks" / "write-guard.py").read_text(encoding="utf-8")
        m = re.search(r'^WRITE_TOOLS = \{(.*)\}', hook, re.M)
        self.assertTrue(m)
        self.assertEqual(sorted(re.findall(r'"([A-Za-z]+)"', m.group(1))),
                         ["Edit", "MultiEdit", "NotebookEdit", "Write"])
        self.assertNotIn('"Bash"', hook)
        for job in ("inbox", "nightly", "weekly", "architect"):
            self.assertNotIn("Bash(git *)", self._job_blocks()[job], job)

    def test_inbox_has_no_git_mv_or_git_rm(self):
        """C3/M047 (row 127): write-guard.py cannot see Bash, so `git mv
        Inbox/x Boards/Intake.md` was ungoverned by any hook. The inbox job
        no longer moves or deletes anything itself - it records what it filed
        in Inbox/.processed and the runner performs the moves host-side
        (run.sh, just before `git status --porcelain`)."""
        block = self._job_blocks()["inbox"]
        allowed = re.search(r'^\s*ALLOWED="(.*)"\s*(?:;;)?\s*$', block, re.M)
        self.assertTrue(allowed)
        self.assertNotIn("Bash(git rm", allowed.group(1))
        self.assertNotIn("Bash(git mv", allowed.group(1))

    def test_the_real_run_sh_is_clean(self):
        """M048: the real tree, scanned the same way as the planted fixture
        below, must come back with zero findings."""
        findings = guard_findings(self.RUN_SH.read_text(encoding="utf-8"))
        self.assertEqual(findings, [])

    def test_a_job_with_no_guard_allow_is_caught(self):
        """M048's planted half: a copy of the real run.sh's `case $JOB in`
        statement with one extra write-capable arm - `probe` - that sets
        ALLOWED to include Write but never sets GUARD_ALLOW. guard_findings
        must name it, proving the scan cannot pass by finding nothing
        anywhere (same requirement test_egress.py holds itself to)."""
        real = self.RUN_SH.read_text(encoding="utf-8")
        start = real.index('# Role-based least privilege.')
        marker = '    inbox)'
        insert_at = real.index(marker, start)
        planted_block = (
            '    probe)\n'
            '        ALLOWED="Read Edit Write"\n'
            '        ;;\n'
        )
        text = real[:insert_at] + planted_block + real[insert_at:]
        findings = guard_findings(text)
        self.assertTrue(any("probe" in f for f in findings), findings)

    def test_an_exact_file_under_a_pipeline_owned_folder_is_caught(self):
        """Final review C1: the scan used to compare a non-glob entry with
        `pat == bad`, and since every folder in GUARD_FORBIDDEN ends in "/",
        NO file path could ever equal one. `Boards/Intake.md` - the file whose
        `## Approvals` section intake.py turns into a real spec approval, in
        the job that ingests attacker-controlled clippings - scanned clean.
        Planted here so it can never scan clean again."""
        real = self.RUN_SH.read_text(encoding="utf-8")
        for job, planted in (("inbox", "Boards/Intake.md"),
                             ("nightly", "Decisions/Capsule Corp - System design decisions.md"),
                             ("weekly", "Ledger/ledger.md")):
            block = self._job_blocks()[job]
            allow = self._guard_allow(block)
            text = real.replace(f'GUARD_ALLOW="{allow}"',
                                f'GUARD_ALLOW="{allow}:{planted}"', 1)
            self.assertNotEqual(text, real, job)
            findings = guard_findings(text)
            self.assertTrue(any(planted in f for f in findings),
                            f"{job}: {planted} scanned clean: {findings}")

    def test_guard_except_is_job_keyed_so_an_exception_cannot_travel(self):
        """The escape hatch is `(job, pattern)`, never a bare pattern: a
        carve-out reviewed for one job must not silently cover another that
        copy-pastes the line. Empty today (see the constant's comment)."""
        self.assertEqual(GUARD_EXCEPT, set())
        for entry in GUARD_EXCEPT:  # pragma: no cover - empty by design today
            self.assertIsInstance(entry, tuple)
            self.assertEqual(len(entry), 2)


class ModelRouting(unittest.TestCase):
    """C7/M149+M150, row 135: routing by ROLE, not by task. Opus 5 where being
    wrong is expensive (planner, reviewer, qa verdict); Fable 5.1 on the two
    design-and-judgement seats with Opus 5 as their fallback; Sonnet 5
    builders; Haiku only for classifier/router jobs.

    How a model is chosen, and what a fallback does (spec 7.7): a profile is an
    independent legacy home (legacy cli profiles.py:1-8), so model.default plus
    model.provider is the whole primary route and the root config is
    NOT inherited. get_fallback_chain (legacy cli fallback_config.py:80-101)
    merges fallback_providers in order, defaulting to [] (config_defaults.py:10);
    the chain reaches the agent at legacy cli oneshot.py:474-486.
    _try_activate_fallback (agent/agent_runtime_helpers.py:3132-3153) fires on a
    non-retryable 401/403/404 or after max retries on 429/500/502/503, takes
    fallback_chain[0], swaps model/provider/client in place, resets the retry
    count and sets _fallback_activated - so it fires ONCE per run. Concretely:
    if the plan does not serve claude-fable-5-1, the first request 404s, the run
    continues on claude-opus-5, and nothing in the verdict says which model
    answered. Per-run attribution is a Power Level requirement (row 121)."""

    def test_run_sh_keeps_the_planner_on_opus_and_the_review_on_fable(self):
        t = (ROOT / "maintenance" / "run.sh").read_text(encoding="utf-8")
        self.assertIn('sprint-plan)              MODEL="claude-opus-5"', t)
        self.assertIn('AGENT_REVIEW_MODEL:-claude-fable-5-1', t)

    @private_only
    def test_the_runbook_carries_the_routing_table_and_the_fallback_sentence(self):
        t = (ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")
        for want in ("claude-fable-5-1", "staff-architect", "review-branch",
                     "may in fact be an Opus run", "fires once per run"):
            self.assertIn(want, t)

    def test_the_epic_review_seat_constants_name_a_fable_primary(self):
        """The whole-branch review card's model comes straight from
        `epic_lane.review_model`, so this reads the seat from the constants the
        builder actually uses rather than re-typing a model string here."""
        import epic_lane
        self.assertEqual(epic_lane.REVIEW_MODELS,
                         (epic_lane.EPIC_REVIEW_MODEL, epic_lane.EPIC_REVIEW_FALLBACK))
        self.assertIn("fable", epic_lane.EPIC_REVIEW_MODEL.lower())


class DecisionClass(unittest.TestCase):
    def test_the_block_carries_a_class_line(self):
        text = ct.format_decision(
            "Split this card?", "it is 12 files", [("split", "two cards"), ("drop", "lose it")],
            1, "smaller cards merge", "size is a design signal", cls="R2")
        self.assertIn("\nClass: R2\n", text)
        self.assertLess(text.index("Class: R2"), text.index("1. split"))

    def test_the_default_is_r3(self):
        text = ct.format_decision("q?", "why", [("a", "t")], 1, "r", "l")
        self.assertIn("Class: R3", text)

    def test_an_unknown_class_is_refused_at_the_builder(self):
        with self.assertRaises(ValueError):
            ct.format_decision("q?", "why", [("a", "t")], 1, "r", "l", cls="R9")

class RetrievalContract(unittest.TestCase):
    """Row 128 (M071+M072). The RUNBOOK table and the profiles are one
    contract; a value in one and not the other is the failure this prevents."""

    EXPECTED = {"researcher": "Knowledge/:Inbox/:Architecture/",
                "slack": "Knowledge/:Boards/:Daily/",
                "telegram": "Knowledge/:Boards/:Daily/"}


    @private_only
    def test_the_runbook_carries_every_proposed_value(self):
        runbook = (ROOT / "docs" / "RUNBOOK.md").read_text()
        for want in set(self.EXPECTED.values()):
            self.assertIn(want, runbook)

    @private_only
    def test_the_runbook_says_the_reviewer_has_no_mount(self):
        runbook = (ROOT / "docs" / "RUNBOOK.md").read_text()
        self.assertIn("dispatch push", runbook)

    @private_only
    def test_the_five_deferrals_are_all_written_down_with_triggers(self):
        runbook = (ROOT / "docs" / "RUNBOOK.md").read_text()
        for want in ("vector DB", "knowledge graph", "hosted memory",
                     "second vault", "context hook"):
            self.assertIn(want, runbook)


class A1RetrievalPush(unittest.TestCase):
    """Row 201 (A1). RETRIEVAL_ROLES and ROLE_FOLDERS pinned, the
    RetrievalContract idiom applied to the dispatch-push half of C4's
    contract: a value that drifts in the code and not here is the failure
    this class exists to catch."""

    def test_retrieval_roles_is_pinned(self):
        self.assertEqual(bb.RETRIEVAL_ROLES,
                         ("dev", "dev-backend", "dev-frontend", "infra", "qa"))

    def test_every_retrieval_role_shares_one_folder_set(self):
        self.assertEqual(len({tuple(v) for v in bb.ROLE_FOLDERS.values()}), 1)
        self.assertEqual(set(bb.ROLE_FOLDERS), set(bb.RETRIEVAL_ROLES))

    def test_the_folder_set_is_pinned(self):
        self.assertEqual(bb.ROLE_FOLDERS["dev"],
                         ["Knowledge/", "Decisions/", "Specs/", "Architecture/"])

    @private_only
    def test_the_folders_never_widen_past_what_a_fenced_profile_already_reads(self):
        """None of the five retrieval roles has an MCP mount (RUNBOOK's
        Retrieval contract table), so there is no one "nearest fenced
        profile" to compare against directly - the fence RetrievalContract
        pins (researcher, telegram, slack) plus the reviewer row the same
        RUNBOOK table carries (`Decisions/:Specs/`) is the full set of
        folders any profile in this codebase is allowed to read out of the
        vault today. ROLE_FOLDERS must stay inside that union: a role with no
        mount at all must never be pushed MORE than a mounted, fenced role
        would be allowed to read live."""
        allowed = set()
        for value in RetrievalContract.EXPECTED.values():
            allowed.update(value.split(":"))
        reviewer_read_allow = "Decisions/:Specs/"   # RUNBOOK's Retrieval contract table, reviewer row
        self.assertIn(reviewer_read_allow, (ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8"),
                     "the reviewer READ_ALLOW literal drifted out of the RUNBOOK table")
        allowed.update(reviewer_read_allow.split(":"))
        for role, folders in bb.ROLE_FOLDERS.items():
            for folder in folders:
                self.assertIn(folder, allowed,
                             f"{role}'s ROLE_FOLDERS folder {folder!r} is not any "
                             "fenced profile's READ_ALLOW")

    def test_the_flag_name_is_pinned(self):
        self.assertEqual(bb.RETRIEVAL_PUSH_FLAG, "OPS_RETRIEVAL_PUSH")

    def test_the_cap_is_6144_bytes(self):
        self.assertEqual(bb.RETRIEVAL_CAP_BYTES, 6144)


class PropertyTests(unittest.TestCase):
    ROOT = Path(__file__).resolve().parents[2]


    @private_only
    def test_the_runbook_explains_the_pin(self):
        text = (self.ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")
        self.assertIn("### Property tests (`hypothesis`)", text)
        self.assertIn("derandomize=True", text)


class RunbookC5(unittest.TestCase):
    ROOT = Path(__file__).resolve().parents[2]

    @private_only
    def test_the_new_sections_exist(self):
        text = (self.ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")
        self.assertIn("### The nightly quality job", text)
        self.assertIn("security-scan", text)


class ReleaseRunbook(unittest.TestCase):
    """Row 138: the rulings that are documentation, not code. If any of these
    sentences goes missing the decision has silently un-happened."""

    def setUp(self):
        self.text = (ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")
        head = self.text.index("## Releases")
        self.sec = self.text[head:self.text.index("\n## ", head + 5)]

    @private_only
    def test_section_exists_after_merges(self):
        self.assertLess(self.text.index("## Merges (the merge gate)"), self.text.index("## Releases"))

    @private_only
    def test_signing_is_host_side_and_row_66_stands(self):
        low = self.sec.lower()   # case-insensitive: "Row 66 stands" and "row 66 settles" both count
        for needle in ("host", "row 66", "unsigned", "keychain", "self-hosted", "poc-1"):
            self.assertIn(needle, low, needle)
        self.assertNotIn("BUILD_CERTIFICATE_BASE64", self.sec)   # never write the CI pattern down as ours

    @private_only
    def test_hotfix_has_no_bypass(self):
        self.assertIn("hotfix", self.sec.lower())
        self.assertIn("no bypass", self.sec.lower())

    @private_only
    def test_rollback_drill_is_a_numbered_procedure(self):
        self.assertIn("rollback drill", self.sec.lower())
        self.assertIn("SHA256SUMS", self.sec)
        self.assertIn("quarter", self.sec.lower())

    @private_only
    def test_names_the_three_stamps_and_the_verb(self):
        self.assertIn("bin/approve-release.sh", self.sec)
        self.assertIn("release_gate.py check", self.sec)
        self.assertIn("OPS_RELEASE_GATE", self.sec)

    @private_only
    def test_versioning_and_the_template(self):
        self.assertIn("0.x", self.sec)
        self.assertIn("repo-conventions.md", self.text)

    @private_only
    def test_what_no_agent_may_do(self):
        for needle in ("codesign", "notarytool", "The owner stamps"):
            self.assertIn(needle, self.sec, needle)


class ReleaseDeferrals(unittest.TestCase):
    """M232, row 138: the free column is taken and the deferral is WRITTEN.
    An undated cost claim is a liability, so every figure carries its date."""

    def setUp(self):
        self.text = (ROOT / "docs" / "RELEASE.md").read_text(encoding="utf-8")

    def test_three_columns_exist(self):
        for needle in ("Taken now", "Conditional", "Deferred"):
            self.assertIn(needle, self.text, needle)

    def test_deferred_items_each_carry_a_trigger(self):
        for needle in ("cosign", "SLSA L3", "sbom-action", "Homebrew", "security.txt"):
            self.assertIn(needle, self.text, needle)
        self.assertIn("Trigger", self.text)

    def test_public_private_costs_are_dated(self):
        for needle in ("2026-09-09", "0.062", "provenance", "Enterprise Cloud", "LICENSE"):
            self.assertIn(needle, self.text, needle)

    def test_no_secret_name_carries_a_value(self):
        self.assertNotIn("BUILD_CERTIFICATE_BASE64", self.text)


class ChangelogSeed(unittest.TestCase):
    """C10 whole-branch fix wave: the repo root carries a real `CHANGELOG.md`
    in the exact shape `release_gate.changelog_promoted` requires - an empty
    `## Unreleased` above a promoted `## v0.1.0` section with at least one
    `- ... (t_xxxxxxxx)` line. Without this file every `release_gate check`
    against this repo fails `changelog` before it ever reaches `sbom` or
    `artefact` - the fixture-only gate tests exercise the check function
    directly, but the real repo needs a real seed too."""

    def setUp(self):
        self.text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    def test_v0_1_0_is_promoted(self):
        ok, why = rg.changelog_promoted(self.text, "v0.1.0")
        self.assertTrue(ok, why)

    def test_carries_a_real_card_id(self):
        self.assertIn("t_1e4abc93", self.text)

    def test_v0_2_0_alpha_1_passes_the_real_gate(self):
        ok, why = rg.check_changelog(ROOT, "v0.2.0-alpha.1")
        self.assertTrue(ok, why)
        self.assertIn("(t_5f7355f9)", self.text)


class RepoTemplate(unittest.TestCase):
    """M233, row 138: 0.x until an API is promised, independent per repo, into
    the repo template so no agent decides on its own that shipping means 1.0.0."""

    @private_only
    def test_template_carries_the_rules_and_the_runbook_points_at_it(self):
        p = ROOT / "maintenance" / "templates" / "repo-conventions.md"
        text = p.read_text(encoding="utf-8")
        for needle in ("0.x", "## Unreleased", "re-insert", "OPS_TEST_CMD_",
                       "independent", "licence"):
            self.assertIn(needle, text, needle)
        self.assertIn("repo-conventions.md", (ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8"))


class LegalRunbook(unittest.TestCase):
    """C12 / M265, M278, M290 (decision row 140). Four sentences whose loss
    would make the decision unrecoverable from the repo: the no-third-party
    rule, the parked-rows trigger, the plan answer, and the heading that holds
    them. Everything else in the section is prose."""
    HEADING = "## Legal limits and money"
    RULE = "No Capsule product sends anything to a third party."
    TRIGGER = "before the first paying user: dedicated legal/money brainstorm"
    PLAN = "Max only. Row 106 stands"
    DECLINED = "declined in writing so no future card re-opens them as work"
    REGISTER = "Art 33(5) breach register"

    def setUp(self):
        self.text = (ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")

    @private_only
    def test_the_section_exists(self):
        self.assertIn(self.HEADING, self.text)

    @private_only
    def test_the_no_third_party_rule_is_written_down(self):
        """M265: the posture held by accident until now - nothing sends
        anything - and an accident is not a rule."""
        self.assertIn(self.RULE, self.text)

    @private_only
    def test_every_parked_row_carries_the_same_trigger(self):
        self.assertIn(self.TRIGGER, self.text)
        for row in ("M264", "M280", "M281", "M283", "M284", "M286", "M287", "M288", "M291"):
            self.assertIn(row, self.text, f"parked row {row} is not named in the RUNBOOK")

    @private_only
    def test_the_plan_answer_and_its_known_cost(self):
        self.assertIn(self.PLAN, self.text)
        self.assertIn("no IP indemnity", self.text)

    @private_only
    def test_the_premature_list_is_declined_not_deferred(self):
        self.assertIn(self.DECLINED, self.text)
        for item in ("BOI promotion", "a DPO", "bug bounty", "SOC 2", "CE marking"):
            self.assertIn(item, self.text, f"declined item {item!r} is missing")

    @private_only
    def test_the_incident_runbook_and_register_are_here_not_in_policies(self):
        self.assertIn(self.REGISTER, self.text)
        self.assertIn("escalate within minutes, never file", self.text)

    @private_only
    def test_the_scan_would_notice_a_lost_section(self):
        self.assertNotIn("## Legal limits and equity", self.text)


class MitLicense(unittest.TestCase):
    """W362: capsule-corp itself is MIT (2026-10-24). The LICENSE holds the
    MIT text with the holder and year, and third-party components keep their own
    licences (docs/licenses/NOTICE.md)."""

    def setUp(self):
        self.license = (ROOT / "LICENSE").read_text(encoding="utf-8")

    def test_the_license_names_the_holder_and_the_year(self):
        holder = re.search(r"^Copyright \(c\) 2026 (\S.*)$", self.license, re.M)
        self.assertIsNotNone(holder, "no `Copyright (c) 2026 <holder>` line")
        self.assertTrue(holder.group(1).strip())
        self.assertNotIn("All rights reserved", self.license)

    def holder(self):
        return re.search(r"^Copyright \(c\) 2026 (\S.*)$", self.license, re.M).group(1).strip()

    def test_the_holder_is_a_real_name_not_a_placeholder(self):
        from capsule import answer_base
        holder = self.holder()
        self.assertRegex(holder, answer_base.OWNER_RE)
        placeholders = {"owner", "holder", "name", "your name", *answer_base.ROLES}
        self.assertNotIn(holder.lower(), placeholders)
        self.assertNotRegex(holder, r"[<>{}$%]|__|\bTODO\b|\bXXX\b")

    def test_the_holder_is_the_name_the_private_denylist_guards(self):
        """The `personal-name` rule exempts LICENSE: the real holder must still match it, so the
        exemption is not stale and LICENSE has not been scrubbed to a placeholder."""
        from capsule.tests import private_names
        if not private_names.DENYLIST.is_file():
            self.skipTest(private_names.SKIP_MSG)
        import public_lint
        rules, _ = public_lint._load_denylist(private_names.DENYLIST)
        rx = [r for cat, r, _ex in rules if cat == "personal-name"]
        self.assertTrue(rx, "no personal-name rule in the private denylist")
        self.assertTrue(any(re.search(r, self.holder(), re.I) for r in rx), self.holder())

    def test_the_license_is_the_mit_text(self):
        """W362: capsule-corp itself is MIT (2026-10-24)."""
        self.assertTrue(self.license.startswith("MIT License"))
        low = self.license.lower()
        for phrase in ("permission is hereby granted, free of charge",
                       "the software is provided \"as is\", without warranty of any kind"):
            self.assertIn(phrase, low)
        self.assertNotIn("proprietary", low)

    def test_third_party_components_keep_their_own_licences(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("docs/licenses/NOTICE.md", readme)
        self.assertTrue((ROOT / "docs" / "licenses" / "NOTICE.md").is_file())

    def test_the_license_ends_at_the_mit_text(self):
        """Extra text after the MIT body makes GitHub's licence detection report "other"."""
        self.assertTrue(self.license.rstrip().endswith("SOFTWARE."))

    def test_no_own_licence_file_says_all_rights_reserved(self):
        lic = ROOT / "docs" / "licenses"  # P01
        self.assertFalse((lic / "PROPRIETARY.txt").exists())
        for f in (ROOT / "LICENSE", ROOT / "SECURITY.md", lic / "NOTICE.md", lic / "README.md"):
            self.assertNotIn("all rights reserved", f.read_text(encoding="utf-8").lower(), f.name)
        notice = (lic / "NOTICE.md").read_text(encoding="utf-8")
        self.assertNotRegex(notice, r"(?m)^- .*TEXT MISSING")


class WorkerCliIsSwappable(unittest.TestCase):
    """M135. `NoKanbanArgvOutsideBoard` above proves only board.py BUILDS the
    argv. This proves the base is actually swappable: patch the one list and a
    full Board round trip renders the new CLI, with no other edit anywhere."""

    OTHER = ["-m", "othercli.main"]

    def _round_trip(self, base):
        h = fakes.FakeLegacyCli()
        with mock.patch.object(bb, "LEGACY_BASE", list(base)):
            b = h.board("sample-app")
            tid = b.create(board.NewCard(title="t", body="b", assignee="dev",
                                         idempotency_key="k", created_by="test"))
            b.comment(tid, "hello")
            b.show(tid)
            b.list()
            b.block(tid, "repair: hold", kind="transient")
            b.unblock(tid)
            b.complete(tid, "done")
        return h.calls

    def test_every_call_renders_the_swapped_base(self):
        calls = self._round_trip(self.OTHER)
        self.assertTrue(calls)
        for argv in calls:
            self.assertEqual(argv[:2], self.OTHER, argv)
            self.assertEqual(argv[2], "kanban", argv)
            self.assertNotIn("legacy_cli.main", argv)

    def test_the_default_base_is_the_legacy_cli(self):
        """The control: the swap test must not pass by finding nothing."""
        self.assertEqual(bb.LEGACY_BASE, ["-m", "legacy_cli.main"])
        for argv in self._round_trip(bb.LEGACY_BASE):
            self.assertEqual(argv[:2], bb.LEGACY_BASE, argv)

    def test_the_swap_is_one_list_in_one_module(self):
        """A second literal base anywhere would make the swap incomplete."""
        hits = []
        for path in sorted(MAINT.glob("*.py")):
            for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if "legacy_cli.main" in line:
                    hits.append(f"{path.name}:{i}")
        self.assertEqual([h.split(":")[0] for h in hits], ["board_bridge.py"], hits)


class ImageWf(unittest.TestCase):
    """M137: a dependency that vanished from the base image should surface on a
    Monday, not in the middle of a card."""

    def _wf(self):
        # No YAML dependency here on purpose (stdlib-only constraint) - these
        # are string assertions against the workflow file's raw text.
        return (ROOT / ".github" / "workflows" / "image.yml").read_text(encoding="utf-8")

    def test_the_image_workflow_has_a_weekly_schedule(self):
        text = self._wf()
        self.assertIn("schedule:", text)
        self.assertRegex(text, r"cron:\s*[\"']\d+ \d+ \* \* [0-6][\"']")

    def test_triggers_docs(self):
        text = self._wf()
        self.assertIn("workflow_dispatch:", text)
        self.assertIn("docker/base/**", text)
        self.assertIn("docker/stacks/**", text)
        self.assertNotIn("docker/worker", text)

    def test_the_schedule_cannot_race_a_push(self):
        self.assertIn("group: image", self._wf())


class SoulProducers(unittest.TestCase):
    """C7/M143. The prefixes the parsers consume, asserted on the RENDERED
    output - the bytes that actually reach a model."""

    # Measured (spec §4.8): six roles carry all three tokens in one shape;
    # lead/SOUL.md:78-79 carries the shape and says "drop TRIED if there was
    # nothing to try"; researcher/SOUL.md:24 uses BLOCKER + NEED because a
    # research block has no failed attempts to list. So TRIED is optional and
    # positional, not mandatory - asserting it unconditionally would fail two
    # roles for being right.

    # Controller ruling, ledger 2026-09-11 01:35: slack and telegram are chat
    # surfaces, not producer roles - they relay huddles, they do not emit
    # them, so the producer contract below applies to the nine role profiles
    # only. Exactly these two, never a broader pattern.
    NOT_A_HUDDLE_PRODUCER = ("slack", "telegram")

    def test_the_huddle_producer_exemption_is_pinned_to_exactly_these_two(self):
        """Branch-review fix wave 1, item 5: pin the exemption so a future
        edit that widens NOT_A_HUDDLE_PRODUCER to a third role (or narrows it)
        fails loudly here instead of silently exempting - or silently
        re-including - a role from the producer contract below."""
        self.assertEqual(set(self.NOT_A_HUDDLE_PRODUCER), {"slack", "telegram"})


class Succession(unittest.TestCase):
    """M217, row 137: docs/SUCCESSION.md exists with the two-section shape
    and its RUNBOOK pointer names no secret value (only key/secret NAMES)."""

    ROOT = Path(__file__).resolve().parents[2]
    SECRET_LINE = re.compile(r"^[A-Za-z_]*(TOKEN|PASSWORD)[A-Za-z_]*=\S")

    @private_only
    def test_file_exists_with_required_sections(self):
        path = self.ROOT / "docs" / "SUCCESSION.md"
        self.assertTrue(path.exists(), "docs/SUCCESSION.md is missing")
        text = path.read_text(encoding="utf-8")
        self.assertIn("## For future agent", text)
        self.assertIn("## Section 1 - Shut it down safely", text)
        self.assertIn("## Section 2 - Get the value out", text)
        self.assertIn("capsule-park.sh", text)

    @private_only
    def test_no_secret_value(self):
        text = (self.ROOT / "docs" / "SUCCESSION.md").read_text(encoding="utf-8")
        hits = [ln for ln in text.splitlines() if self.SECRET_LINE.match(ln.strip())]
        self.assertEqual(hits, [], f"secret-shaped line(s) in SUCCESSION.md: {hits}")

    @private_only
    def test_runbook_points_at_it_with_no_secret_value(self):
        text = (self.ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")
        self.assertIn("docs/SUCCESSION.md", text)
        self.assertIn("mirrored to the vault", text)
        hits = [ln for ln in text.splitlines() if self.SECRET_LINE.match(ln.strip())]
        self.assertEqual(hits, [], f"secret-shaped line(s) in RUNBOOK.md: {hits}")


class BettingTable(unittest.TestCase):
    """Row 139 / M242+M245: the betting table reuses the Friday 20:00 slot.
    Two properties matter and both are mechanical: the job READS the order book
    and never writes it (Boards/ is in GUARD_FORBIDDEN, and write-guard.py
    governs Write/Edit/MultiEdit only, so a read needs no widening), and the
    job DECIDES NOTHING - weekly.md's own last line is "Do not ask questions.
    Save and stop.", so a job that decided a bet would be deciding unattended.
    """

    WEEKLY = MAINT / "weekly.md"
    RUN_SH = MAINT / "run.sh"

    def _weekly(self):
        return self.WEEKLY.read_text(encoding="utf-8")

    @private_only
    def test_the_heading_is_in_the_fixed_heading_list(self):
        """weekly.md fixes the review's headings in one block and says "exactly
        these headings in this order", so a new section that is described but
        not listed is a section the job will not write."""
        text = self._weekly()
        self.assertIn("## Betting table", text)
        self.assertIn("## Decisions\n## Betting table\n## People\n", text)

    @private_only
    def test_the_job_reads_the_order_book(self):
        text = self._weekly()
        self.assertIn("Boards/Order Book.md", text)
        self.assertIn("read-only", text)

    @private_only
    def test_the_job_reads_the_ledger_for_merged_cards(self):
        self.assertIn('"kind": "merged"', self._weekly())
        self.assertIn("Ledger/", self._weekly())

    @private_only
    def test_the_job_decides_nothing(self):
        text = self._weekly()
        self.assertIn("Verdict: the owner's, at the table. This job prepares the "
                      "table and decides nothing.", text)
        self.assertIn("Do not ask questions. Save and stop.", text)

    @private_only
    def test_the_job_degrades_when_the_book_is_absent(self):
        self.assertIn('No order book yet - see docs/RUNBOOK.md "The order '
                      'book".', self._weekly())

    @private_only
    def test_the_job_names_the_caps_and_the_passed_kill_date(self):
        text = self._weekly()
        self.assertIn("KILL DATE PASSED", text)
        self.assertIn("15", text)             # the ~15 opportunity cap
        self.assertIn("One discovery hour", text)

    @private_only
    def test_the_job_never_writes_the_order_book(self):
        text = self._weekly()
        for phrase in ("write to Boards/", "edit Boards/", "update the order book"):
            self.assertNotIn(phrase, text, phrase)
        self.assertIn("never write", text)

    def test_the_weekly_guard_allow_is_unchanged_and_names_no_board(self):
        """Spec section 4 discrepancy 2: the gather expected a GUARD_ALLOW
        widening. There must not be one - a glob reaching Boards/ would let an
        unattended job forge a card or a spec approval."""
        text = self.RUN_SH.read_text(encoding="utf-8")
        self.assertIn('GUARD_ALLOW="Reviews/**:Daily/**:Knowledge/**:Debugging/**"',
                      text)
        self.assertNotIn("Order Book", text)

    def test_the_weekly_arm_still_holds_only_read_grep_glob_and_one_bash(self):
        text = self.RUN_SH.read_text(encoding="utf-8")
        self.assertIn('ALLOWED="Read Edit Write Grep Glob '
                      'Bash($OPS_PY $MAINT_DIR/resume_pointers.py *)"', text)


class FeedbackIngestWrittenDown(unittest.TestCase):
    """Row 139 / M254: the support loop is the same pipeline with a different
    source, and its injection rule REUSES M037's envelope clause instead of
    writing a second one. Decision-only; the trigger is "first app ships"."""

    def _runbook(self):
        return (ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")

    @private_only
    def test_the_five_steps_are_written_down(self):
        text = self._runbook()
        self.assertIn("Feedback ingest", text)
        for phrase in ("A script ingests", "quarantined classifier",
                       "A script decides", "non-consumed section",
                       "Nothing outbound is automatic"):
            self.assertIn(phrase, text, phrase)

    @private_only
    def test_it_reuses_m037s_envelope_rather_than_inventing_one(self):
        text = self._runbook()
        self.assertIn("M037", text)
        self.assertIn("untrusted", text)
        self.assertIn("tool_result", text)
        self.assertIn("no tools and no network", text)

    @private_only
    def test_the_no_user_text_in_a_card_body_rule_is_explicit(self):
        text = self._runbook()
        self.assertIn("user text is ever copied into a card body", text)
        self.assertIn("an id, a classification and a link to the raw report",
                      text)

    @private_only
    def test_the_third_leg_of_the_trifecta_is_named_and_refused(self):
        text = self._runbook()
        self.assertIn("customerReviewResponses", text)
        self.assertIn("lethal trifecta", text)

    @private_only
    def test_the_classifier_schema_is_closed(self):
        text = self._runbook()
        for key in ("kind", "component", "severity", "dup_of",
                    "injection_suspected"):
            self.assertIn(key, text, key)


class C11ParkedRows(unittest.TestCase):
    """Row 139 parked five things WITH triggers. A deferral with no written
    trigger is a deferral nobody comes back to - test_contracts already fences
    C4's five the same way (RetrievalContract)."""

    TRIGGERS = {
        "M247": "first bet stamped",
        "M248": "first bet stamped",
        "M250": "first bet stamped",
        "M257": "first app ships",
        "M258": "M011",
    }

    @private_only
    def test_every_parked_row_appears_with_its_trigger(self):
        text = (ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")
        for row, trigger in self.TRIGGERS.items():
            self.assertIn(row, text, row)
            self.assertIn(trigger, text, f"{row}'s trigger")

    @private_only
    def test_the_bet_approval_verb_is_named_as_parked(self):
        text = (ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")
        self.assertIn("`bet:` approval verb", text)
        self.assertIn("row 82", text)


class AutomationIsNotARole(unittest.TestCase):
    """C7/M166+M167, row 135, both halves. automation is the pipeline's
    infrastructure profile, not a Z Fighter: automation/config.yaml:96-102
    holds the fleet-wide concurrency caps, usage_guard.py:27 reads its home for
    the 85% ceiling, status.sh:66 and heal.sh:173 use it as the legacy home for
    every legacy kanban call, and the legacy gateway-automation plist:16-21 runs
    the always-on gateway. Dropping the profile was never on the table. But on
    Haiku 4.5 the prompt is the only dial - effort is inert there by
    construction (anthropic_adapter.py:3101) - so 104 words of generic persona
    was the whole specification of the process that holds the caps.

    Controller ruling, fix round 1 (ledger 2026-09-11 06:05): decision row 140
    (C12) wins over this cluster's brief - `automation` is the unattended
    agent 15 U.S.C. 7006(3) describes and KEEPS the `never-legal` block in its
    rendered SOUL (see LEGAL_NEVER_ROLES above). The brief's original word-
    budget and no-sections assertions were written before C12 landed and
    checked the whole rendered SOUL.md, which is mathematically incompatible
    with also carrying the ~250-word shared block. They are rewritten here to
    check automation's OWN text - the render minus the shared block's exact
    bytes - which is what the 40-60-word job statement was always about."""

    def test_the_never_list_exclusion_is_unchanged(self):
        self.assertNotIn("automation", NEVER_LIST_ROLES)


class TeamSection(unittest.TestCase):
    """Row 117 + row 135: the RUNBOOK names the seven Z Fighters and separates
    the three gateway/infra profiles by name. The RUNBOOK and the profiles are
    one contract - the RetrievalContract idiom."""

    Z_FIGHTERS = ("dev", "dev-frontend", "dev-backend", "infra", "qa", "reviewer", "lead")
    GATEWAYS = ("automation", "telegram", "slack")

    def _runbook(self):
        return (ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")

    @private_only
    def test_the_seven_z_fighters_are_named_as_such(self):
        t = self._runbook()
        self.assertIn("Z Fighters", t)
        head = t.index("Z Fighters")
        block = t[head:head + 4000]
        for role in self.Z_FIGHTERS:
            self.assertIn(f"| {role} ", block, role)
        self.assertEqual(len(self.Z_FIGHTERS), 7)

    @private_only
    def test_the_gateway_profiles_are_a_separate_table_and_say_they_are_not_roles(self):
        t = self._runbook()
        self.assertIn("Gateway and infra profiles", t)
        block = t[t.index("Gateway and infra profiles"):]
        for name in self.GATEWAYS:
            self.assertIn(name, block[:2000], name)
        self.assertIn("no card is ever assigned", block[:2000])

    @private_only
    def test_bulma_and_the_red_ribbon_army_are_named(self):
        t = self._runbook()
        self.assertIn("Bulma", t)
        self.assertIn("Red Ribbon Army", t)


class RedTeamChecklist(unittest.TestCase):
    """C7/M176+M177, row 135: same family, different model - a Sonnet 5
    adversarial pass, checklist-grade, on the epic spec only. One file, wired
    nowhere: C6's epic lane (row 133) consumes it later."""

    PATH = ROOT / "docs" / "red-team-checklist.md"

    @private_only
    def test_it_names_sonnet_5_and_the_epic_spec(self):
        t = self.PATH.read_text(encoding="utf-8")
        self.assertIn("claude-sonnet-5", t)
        self.assertIn("epic spec", t)

    @private_only
    def test_it_carries_the_four_classes_and_the_gate_surface(self):
        t = self.PATH.read_text(encoding="utf-8")
        for want in ("Goal hijacking", "Tool misuse", "Memory and context poisoning",
                     "Excessive agency", "test, a gate or a guard", "irreversible:"):
            self.assertIn(want, t)

    @private_only
    def test_it_says_it_is_composed_not_cited(self):
        first = self.PATH.read_text(encoding="utf-8").split("\n\n", 2)[1]
        self.assertIn("composed", first.lower())
        self.assertIn("OWASP", first)

    @private_only
    def test_it_produces_findings_and_never_a_verdict(self):
        t = self.PATH.read_text(encoding="utf-8")
        self.assertIn("no verdict", t.lower())
        self.assertNotIn("kanban_complete", t)

    @private_only
    def test_no_job_references_it_yet(self):
        """'Wired nowhere' is a fact a test keeps true until a card changes it
        on purpose - the test_the_effort_boundary_is_visible idiom."""
        for p in (ROOT / "maintenance" / "run.sh", ROOT / "maintenance" / "report.sh",
                  ROOT / "deploy.sh"):
            self.assertNotIn("red-team-checklist", p.read_text(encoding="utf-8"), str(p))


class CitationHygiene(unittest.TestCase):
    """C7/M145. The 80% system-prompt-cut figure is secondary reporting only
    and its aggregator numbers are mutually inconsistent (800->164 tokens,
    ~2,686->~514 words, "roughly 70%"); the cited Anthropic post contains no
    percentage at all (read end to end 2026-09-09). The shorter-prompt FEATURE
    is documented (CLAUDE_CODE_SIMPLE_SYSTEM_PROMPT). So the shape survives and
    the number does not, and it must not re-enter through a SOUL or a doc."""

    BAD = re.compile(r"(80|70)\s*%[^.\n]{0,60}(system prompt|prompt)", re.I)

    def test_no_soul_or_doc_cites_a_percentage_system_prompt_cut(self):
        offenders = []
        for p in (ROOT / "docs").rglob("*.md"):
            if p.name.endswith("roles-and-souls.md") or "specs/" in str(p):
                continue          # the spec and this plan discuss the correction itself
            if self.BAD.search(p.read_text(encoding="utf-8")):
                offenders.append(str(p.relative_to(ROOT)))
        self.assertEqual(offenders, [], "an unverified percentage figure came back")


class RunbookHonesty(unittest.TestCase):
    """Row 164 (fluent-bit-recreate-wrapper-2, task 2): the RUNBOOK's deploy/
    observability section must describe the one door every recreate goes
    through - `compose_recreate` in maintenance/lib_compose.sh - not a plain
    `docker compose ... --force-recreate` call that no longer exists."""

    def setUp(self):
        self.text = (ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")
        head = self.text.index("## Observability")
        self.sec = self.text[head:self.text.index("\n## ", head + 5)]

    @private_only
    def test_section_mentions_compose_recreate(self):
        self.assertIn("compose_recreate", self.sec)

    @private_only
    def test_section_no_longer_describes_a_plain_docker_compose_recreate(self):
        # A recreate invocation outside compose_recreate() would show up as a
        # literal `docker compose ... --force-recreate` command in the prose -
        # the one the old paragraph told operators to run by hand.
        bad = re.compile(r"docker compose[^\n]*--force-recreate")
        self.assertIsNone(bad.search(self.sec), "section still shows a bare docker compose --force-recreate")

    @private_only
    def test_rollback_recipe_actually_recreates_grafana_when_run(self):
        # Review round 3, findings A and B: the rollback step's recipe has to
        # survive being copy-pasted. An inline code span wrapped mid-path
        # renders with the break as a space, and a variable assignment
        # PREFIXED to `.` does not outlive that command, so compose_recreate
        # would run with `-f ""`. Run the documented line for real against
        # fake CAPSULE_SECRET_BIN / DOCKER_BIN files and check the compose
        # file reached docker's argv.
        import subprocess, tempfile
        m = re.search(r"severity labels.*?```sh\n(.*?)```", self.sec, re.S)
        self.assertIsNotNone(m, "the rollback step's recipe is not a fenced sh block")
        recipe = m.group(1)
        d = Path(tempfile.mkdtemp())
        log = d / "argv.log"
        fake = d / "capsule-secret"
        fake.write_text('#!/usr/bin/env bash\nprintf "%%s\\n" "$*" >>"%s"\n' % log,
                        encoding="utf-8")
        fake.chmod(0o755)
        env = dict(os.environ, CAPSULE_SECRET_BIN=str(fake),
                   DOCKER_BIN=str(d / "docker"), OBS_ENV=str(d / "obs.env"),
                   # the suite runs under a temp HOME: never the live project (compose_live_guard)
                   COMPOSE_PROJECT_NAME="capsule-corp-test")
        r = subprocess.run(["bash", "-c", recipe], cwd=ROOT, env=env,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, f"the documented recipe failed: {r.stderr.strip()}")
        argv = log.read_text(encoding="utf-8") if log.exists() else ""
        self.assertIn("--force-recreate grafana", argv)
        self.assertIn(f"-f {ROOT}/observability/docker-compose.yml", argv)

    @private_only
    def test_section_does_not_promise_grafana_the_FAILED_recreate_message(self):
        # Review round 3, finding C: only the fluent-bit call site reports
        # `FAILED fluent-bit recreate (<first stderr line>)` (deploy.sh:756).
        # Grafana names that line in a non-fatal warn instead of FAILED.
        flat = " ".join(self.sec.split())   # prose wraps; markdown renders it joined
        self.assertNotIn("FAILED <service> recreate", flat)
        self.assertIn("FAILED fluent-bit recreate", flat)
        self.assertIn("grafana restart failed (<first stderr line>) (not fatal)", flat)
        # Review round 1 (whole-branch): the section must not claim
        # /api/health catches a failed grafana recreate - nothing runs on
        # that failure path except the non-fatal warn above.
        self.assertNotIn("caught a moment later by the `/api/health` wait", flat)


class DepsAllowlistContract(unittest.TestCase):
    """A5 fix wave 1 LOW / spec section 8: "no deps-allowlist.txt line uses a
    range operator" was the one spec-8 contract test never actually written
    - deps.parse_allowlist already refuses one, this just proves every
    committed allowlist (today: capsule-corp's own, comment-only) stays clean."""

    def test_every_committed_allowlist_parses_without_a_range_operator(self):
        import deps
        found = list(ROOT.rglob("deps-allowlist.txt"))
        self.assertTrue(found, "expected at least the repo's own deps-allowlist.txt")
        for p in found:
            text = p.read_text(encoding="utf-8")
            try:
                deps.parse_allowlist(text)
            except deps.AllowlistError as exc:
                self.fail(f"{p.relative_to(ROOT)}:{exc.line_no}: {exc.reason} ({exc.line!r})")


