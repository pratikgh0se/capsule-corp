#!/usr/bin/env python3
"""Test-honesty merge-gate step (decision row 459, card t_a995978e, memory rule
`test-honesty-gate`): a new or changed test that can never fail proves nothing.
Two independent checks, plus static red flags:

  (b) revert-mutation: build a scratch worktree at `--head`, then check out the
      `--base` version of every non-test `.py` file the range touched, plus any
      changed `contracts/intent/<role>.jsonl` (Zenkai row 454's intent examples
      - data, never imported, so it needs its own revert and its own
      reference check, `is_intent_data_file`/`references_intent_data`; I1,
      review round on the guide-bundle card) (the new tests stay as HEAD
      wrote them). Run only the new/changed tests. When the range touched at
      least one production file (or intent data file), every changed test
      FILE must show at least one failure here - a file whose tests all still pass with the
      production change reverted is not discriminating and is reported. A range with both code
      and intent data that reads "did not fail" is retried with only the code reverted.

  (c) assertion-weakening mutation: for each changed test file, in a second,
      unmodified (HEAD) scratch worktree, flip the first `assertEqual` to
      `assertNotEqual` (or `assertTrue` to `assertFalse`, or a bare
      `assert a == b` to `assert a != b`) and confirm that alone makes the
      file's changed tests fail.

  (d) static flags on the new/changed test functions: no assert at all; a
      `@skip`/`@skipIf`/`@skipUnless`/`@expectedFailure` decorator; a bare
      `except: pass` (or `except Exception: pass`) that could swallow the
      subject's own error; `assertTrue(True)` (or `assert True`), a tautology.

Zenkai Z1/Z2 (docs/ZENKAI.md): this script's own intent examples and second
oracle are `tests/test_mutation_check.py`, run against a tiny fixture repo
built in tempfile - a discriminating test passes this gate, a tautological one
fails it.

A test file whose subject genuinely predates the range it is committed in can
opt out of (b) revert-mutation with a `# mutation-check: targets-predate-range
<sha> -- <reason> :: Class.test_a, Class.test_b` top-of-file marker (round 5).
Round 13: this marker is per-TEST, not per-file - it must name the exact `Class.test_name` (or
bare `test_name`) ids it covers after a literal `::`; only those ids read `n/a (targets predate
range)`, every OTHER new/changed test in the same file is still revert-checked below it. A marker
with no `::` ids at all (the old file-level shape), or one naming an id that is not in this range's
new/changed set, or a marker not added in this range (an earlier card's), is `marker_stale`; a sha
that is neither `--base` nor its ancestor is `marker_mismatch`; both fail the gate, never n/a.

Usage: tests/mutation_check.py --base <sha> --head <sha> [--repo .]
Exit 0 and a clean markdown table when every changed test file discriminates and no static flag
fired; exit 1 and the table naming what didn't; exit 75 on an environment error. Stdlib only.

Every internal `unittest discover` subprocess (`run_discover`) uses a deterministic
per-call timeout, `max(180, 10 * len(names))` seconds (round 11, `unittest_timeout_budget`),
with no per-file markers or overrides, so N test ids scale their own budget instead of one
fixed 180s ceiling (mirrored from the sibling project's integrator-reproduced timeout).
"""
import argparse
import ast
import os
import re
import subprocess
import sys
import tempfile
from itertools import accumulate
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import discover_tmp  # noqa: E402 - t_dd689940: a swept $TMPDIR per discover run
import size_check  # noqa: E402 - Zenkai Z7 (row 509): the size oracle, run alongside
import simplicity_check  # noqa: E402 - Zenkai Z8 (row 559): and the simplicity rules
from size_baseline import TEST_DIRS, added_line_numbers, is_test_file  # noqa: E402
import mutation_newsymbol as newsymbol  # noqa: E402 - row 459
import mutation_contractdata as contractdata  # noqa: E402 - card t_3ff01dd1
import mutation_info as info  # noqa: E402 - row 568: info-level heuristics
import mutation_alias as alias  # noqa: E402 - row 526: a test reaching a changed file through an alias
import mutation_literals as literals  # noqa: E402 - t_bd08469e: string masks, new-module stubs
import mutation_env  # noqa: E402 - t_589d9eff

TAUTOLOGY_RE = re.compile(r"assertTrue\(\s*True\s*\)|(?<!\w)assert\s+True\s*(?:,|$)")
BARE_EXCEPT_RE = re.compile(r"except(?:\s+Exception)?\s*(?:as\s+\w+\s*)?:\s*\n(\s+)pass\b")
ASSERT_EQ_RE = re.compile(r"\bassertEqual\(")
ASSERT_TRUE_RE = re.compile(r"\bassertTrue\(")
BARE_ASSERT_EQ_RE = re.compile(r"(?<!\w)assert\s+(.+?)\s==\s(.+?)\s*(#.*)?$", re.MULTILINE)


def sh(argv, cwd=None, timeout=300, input_text=None):
    # Round 12 (mirrored from the sibling project's mutation_check.py): the revert- and
    # weaken-mutation phases rewrite the SAME on-disk path several times in
    # quick succession (revert -> run -> restore; then each weaken candidate
    # in turn), and two different mutations can produce a file of the
    # IDENTICAL byte size (e.g. `assertEqual`->`assertNotEqual` on line N vs
    # the same flip on line M). Python's bytecode cache keys a cached
    # `.pyc` on (mtime, size); with an identical size and a write fast
    # enough to land on the same mtime tick, the second run can silently
    # reuse the FIRST mutation's already-compiled (and already-exercised)
    # bytecode instead of the file that is actually on disk right now - a
    # real, observed flake in the sibling project's sibling script, reproduced there
    # with `python3 -m unittest` alone (10/10 manual reruns failed before
    # this fix, 8/8 clean after). Every subprocess this gate spawns must
    # therefore never write - and never trust a pre-existing - `.pyc`:
    # force PYTHONDONTWRITEBYTECODE=1 and drop the agent-session markers `capsule human` refuses
    # on (CLAUDECODE, AI_AGENT) regardless of the caller's own environment.
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "AI_AGENT")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        p = subprocess.run(argv, cwd=str(cwd) if cwd else None, capture_output=True,
                            text=True, timeout=timeout, input=input_text, env=env)
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    return p.returncode, (p.stdout or "") + (p.stderr or "")


_SUPPORT_BASENAMES = ("conftest.py", "__init__.py")
_SUPPORT_NAME_HINTS = ("fake", "mock", "stub", "oracle")


def is_test_support_file(rel_path):
    """A `.py` file under a test directory that is test-DOUBLE support, not
    production - `conftest.py`, `tests/**/__init__.py`, anything under a
    `fixtures/` directory, or a fakes/mocks/stubs/oracle module (e.g.
    `maintenance/tests/fakes.py`, `capsule/tests/<name>_oracle.py`). Fix
    round 3 (sibling-project audit, fix/htc-applied-verify): these must never be
    reverted (that would test against a stale double, not the actual bug) or
    required to "discriminate" (they are not tests).

    Deliberately narrower than "any non-test_*.py file under a test
    directory": a module with real application logic that merely lives under
    a test directory for organizational reasons (the sibling-project audit's example
    was `tests/gauntlet/observe.py`/`execute.py`) must stay ordinary
    production - excluding it from mutation testing would silently blind the
    gate on exactly the file its own test exists to cover."""
    rel_path = str(rel_path).replace("\\", "/")
    if not rel_path.endswith(".py"):
        return False
    if not rel_path.startswith(TEST_DIRS) or is_test_file(rel_path):
        return False
    name = Path(rel_path).name
    if name in _SUPPORT_BASENAMES:
        return True
    if "fixtures" in rel_path.split("/"):
        return True
    stem = name[:-3].lower()
    return any(hint in stem for hint in _SUPPORT_NAME_HINTS)


def changed_files(repo, base, head):
    rc, out = sh(["git", "diff", "--name-only", base, head], cwd=repo)
    if rc != 0:
        raise RuntimeError(f"git diff --name-only {base} {head} failed: {out}")
    return [l.strip() for l in out.splitlines() if l.strip()]


def file_status(repo, base, head, path):
    rc, out = sh(["git", "diff", "--name-status", base, head, "--", path], cwd=repo)
    if rc != 0 or not out.strip():
        return "M"
    return out.strip().splitlines()[0].split("\t", 1)[0][0]


def show_file(repo, ref, path):
    rc, out = sh(["git", "show", f"{ref}:{path}"], cwd=repo)
    return out if rc == 0 else None


def test_defs(source):
    """{funcname: (lineno, end_lineno, class_or_None)} for every test* def."""
    out = {}
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return out

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.cls = None

        def visit_ClassDef(self, node):
            prev, self.cls = self.cls, node.name
            self.generic_visit(node)
            self.cls = prev

        def _fn(self, node):
            if node.name.startswith("test"):
                out[(self.cls, node.name)] = (node.lineno, getattr(node, "end_lineno", node.lineno))
            self.generic_visit(node)

        visit_FunctionDef = _fn
        visit_AsyncFunctionDef = _fn

    Visitor().visit(tree)
    return out


def changed_test_functions(repo, base, head, path, status, added):
    """[(class_or_None, name, lineno, end_lineno)] new/changed test functions."""
    head_src = show_file(repo, head, path)
    if head_src is None:
        return []
    defs = test_defs(head_src)
    if status == "A":
        return [(cls, name, s, e) for (cls, name), (s, e) in defs.items()]
    out = []
    for (cls, name), (s, e) in defs.items():
        if any(s <= ln <= e for ln in added):
            out.append((cls, name, s, e))
    return out


def make_worktree(repo, ref, base_dir):
    wt = base_dir / "wt"
    rc, out = sh(["git", "worktree", "add", "--detach", "-q", str(wt), ref], cwd=repo)
    if rc != 0:
        raise RuntimeError(f"git worktree add {ref} failed: {out}")
    return wt


def remove_worktree(repo, wt):
    sh(["git", "worktree", "remove", "--force", str(wt)], cwd=repo)


def revert_to_base(repo, base, scratch, path, status):
    target = scratch / path
    if status == "A":
        if target.exists():
            target.unlink()
        return
    content = show_file(repo, base, path)
    if content is None:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def unittest_timeout_budget(names):
    """Round 11 (mirrored from the sibling project's own integrator-reproduced timeout):
    `max(180, 10 * len(names))` seconds - deterministic, scales with the
    selection, no per-file markers or overrides."""
    return max(180, 10 * len(names))


def run_discover(root, test_dir, basename, names, timeout=None):
    """Run only `names` (test method/function ids) inside `basename`, discovered
    from `test_dir`, cwd=`root`. Deliberately no explicit `-t` (fix round 1):
    on this Python, `unittest discover -s <abs> -t <abs>` raises "Start
    directory is not importable" whenever the path from `-t` down to `-s`
    isn't a full package chain (`__init__.py` at every level) - true for
    `capsule/tests` here but NOT for `maintenance/tests`, which has none, and
    is exactly how this repo's own suite already runs it
    (`python3 -m unittest discover maintenance/tests`, deploy.sh's
    `OPS_TEST_CMD_capsule_corp`). Passing `-s` as a path RELATIVE to `root`
    with no `-t` matches that working invocation - top-level then defaults to
    the start dir itself, and `from pkg import module` still resolves because
    `python -m unittest` puts `cwd` (root) on `sys.path[0]` regardless of
    discover's own top-level bookkeeping.

    Uses `sys.executable`, never the bare string "python3" (fix round 9,
    mirrored from the sibling project's own integrator-reproduced failure): a bare
    "python3" subprocess resolves via whatever PATH the CALLING process
    happens to have, which need not be the interpreter actually running this
    script (e.g. invoked via an absolute interpreter path with no matching `bin/`
    on PATH); `sys.executable` is self-consistent. Own swept $TMPDIR (discover_tmp)."""
    if not names:
        return 0, "no tests selected"
    if timeout is None:
        timeout = unittest_timeout_budget(names)
    rel_start = os.path.relpath(str(test_dir), str(root))
    argv = [sys.executable, "-m", "unittest", "discover", "-s", rel_start, "-p", basename]
    for n in names:
        argv += ["-k", n]
    return discover_tmp.run(sh, argv, cwd=root, timeout=timeout)


# unittest's own signature for "this test module could not even be imported"
# (unittest.loader._FailedTest) - distinct from a genuine assertion failure,
# which never prints either string. Matched on OUTPUT, not rc: an import
# error and a real failure both exit 1 from `unittest discover` (fix round 1).
COLLECTION_ERROR_RE = re.compile(r"_FailedTest|Failed to import test module")


def is_collection_error(out):
    return bool(COLLECTION_ERROR_RE.search(out or ""))


# Fix round 2 (row 459 audit, capsule/intent-examples f3031c3..0e184f7): when a
# production file is NEW in the range, reverting it to base means deleting it
# - every test that imports it then ERRORs no matter how well it discriminates,
# which is not a test-quality signal. Distinguish that specific shape from a
# genuine, unrelated import error.
NO_MODULE_RE = re.compile(r"No module named '([\w.]+)'")
CANNOT_IMPORT_RE = re.compile(r"cannot import name '(\w+)' from '([\w.]+)'")

# Fix round 5: an explicit, auditable opt-out for a test file whose subject
# predates the range entirely (new tests for old code - the round-4 heuristic
# already catches the common "some unrelated file changed too" shape via
# imports, but a human author can assert this directly and cite WHY, which
# the table then shows a reviewer). Added in the range, with a sha that is --base or an ancestor
# of it (t_0080b449), so it cannot silently outlive the range it was written for.
#
# Round 13: a marker naming no `::` ids used to switch off revert-mutation for EVERY test in the
# file, even brand-new tests whose subject changed in this range - it must now name the
# `Class.test_name` (or bare `test_name`) ids it covers after a literal `::`. `ids_group` is
# optional in the regex only so a marker with no `::` still PARSES (ids=None);
# `_check_range_impl` turns that into the `marker_stale` failure, this only reads what is on disk.
MARKER_RE = re.compile(
    r"^#\s*mutation-check:\s*targets-predate-range\s+([0-9a-fA-F]{7,40})\s+--\s*(.+?)"
    r"(?:\s*::\s*([\w.]+(?:\s*,\s*[\w.]+)*))?\s*$",
    re.MULTILINE)


def extract_marker(source, top_lines=20):
    """(marker_sha, reason, ids) from a top-of-file `# mutation-check:
    targets-predate-range <sha> -- <reason> :: Class.test_a, Class.test_b`
    comment, or None. Only the first `top_lines` are scanned. The marker may wrap over
    `#` lines indented 2+ spaces after the `#` (t_c04395ea). `ids` is a tuple of the ids exactly as
    written (whitespace-stripped), or None when the marker carries no `::`
    ids section at all (round 13: that shape is a `marker_stale` gate failure
    downstream, not silently accepted as file-level)."""
    if not source:
        return None
    head_block = "\n".join(source.splitlines()[:top_lines])
    head_block = re.sub(r"\n#[ \t]{2,}(?![ \t]|mutation-check)", " ", head_block)
    m = MARKER_RE.search(head_block)
    if not m:
        return None
    sha, reason, ids_str = m.group(1), m.group(2), m.group(3)
    ids = tuple(i.strip() for i in ids_str.split(",")) if ids_str else None
    return (sha, reason, ids)


def resolve_sha(repo, ref):
    rc, out = sh(["git", "rev-parse", f"{ref}^{{commit}}"], cwd=repo)
    return out.strip() if rc == 0 else None


def marker_sha_ok(repo, marker, base):
    """A resolved marker sha is valid when it is the resolved base or an ancestor of it."""
    return bool(marker and base) and (marker == base or
        sh(["git", "merge-base", "--is-ancestor", marker, base], cwd=repo)[0] == 0)


def _prod_module_candidates(path):
    """(dotted, attr, package) for a production .py file path - `dotted` is
    the fully-qualified module name; `attr`/`package` is the "from package
    import attr" shape a `from pkg import newmodule` statement produces when
    newmodule.py is missing (Python reports the parent package name, not the
    missing module, in that phrasing)."""
    p = str(path)
    p = p[: -len("/__init__.py")] if p.endswith("/__init__.py") else p[:-3]
    dotted = p.replace("/", ".")
    parts = dotted.split(".")
    attr = parts[-1]
    package = ".".join(parts[:-1])
    return dotted, attr, package


def new_module_import_error(out, new_prod_files):
    """True when the collection error in `out` is explained by one of
    `new_prod_files` (production files added in this range, so absent after a
    revert-to-base) simply not existing - as opposed to a genuinely broken or
    unrelated import."""
    out = out or ""
    m1 = NO_MODULE_RE.search(out)
    m2 = CANNOT_IMPORT_RE.search(out)
    for f in new_prod_files:
        dotted, attr, package = _prod_module_candidates(f)
        if m1 and (m1.group(1) == dotted or dotted.startswith(m1.group(1) + ".")
                   or m1.group(1).startswith(dotted + ".")
                   # Fix round 4 (window-5 gate run, gate-w5.txt): a test file
                   # that hacks sys.path to import a submodule by its bare stem
                   # (exactly what tests/test_mutation_check.py itself does:
                   # `sys.path.insert(...); import mutation_check`) gets
                   # "No module named 'mutation_check'", not the dotted
                   # "tests.mutation_check" - match the bare stem too.
                   or m1.group(1) == attr):
            return True
        if m2 and m2.group(1) == attr and m2.group(2) in (package, dotted.rsplit(".", 1)[0] if "." in dotted else ""):
            return True
    return False


# B1 (round 10, mirrored from the sibling project's HTC gate-rows audit): `cannot import
# name 'X' from 'M'` (or the AttributeError-shaped equivalent) after
# reverting M is not the same failure as M not existing at all
# (new_module_import_error, above) - M exists at base, it just does not
# define X yet, because the range ADDED X to it. Not a test-quality signal.
ATTRIBUTE_ERROR_RE = re.compile(r"AttributeError: module '([\w.]+)' has no attribute '(\w+)'")


def _bare_stem_candidates(dotted, candidate_files):
    """Every file in `candidate_files` whose bare stem (the last dotted
    component - `_prod_module_candidates`' `attr`) equals `dotted`, in the
    order given. Shared by `_module_path_for_dotted` (which only trusts a
    UNIQUE match) and, since review round 2's E1c, `_mentions_missing_prod_symbol`
    (which - unlike resolving WHICH module to keep - can safely check every
    ambiguous candidate at once, since it only needs to know whether ANY of
    them might still define the missing symbol)."""
    return [f for f in candidate_files if _prod_module_candidates(f)[1] == dotted]


def _module_path_for_dotted(dotted, candidate_files):
    for f in candidate_files:
        d, _, _ = _prod_module_candidates(f)
        if d == dotted:
            return f
    # E1 (review round 1): a test that hacks `sys.path.insert(...)` and
    # imports a submodule by its bare stem (exactly what this file's own
    # test file does for `mutation_check` itself) gets `module 'prod' has no
    # attribute 'X'` / `cannot import name 'X' from 'prod'`, naming the bare
    # stem, not the dotted path - the exact same shape `new_module_import_error`
    # already handles at its `m1.group(1) == attr` check. Only a UNIQUE
    # bare-stem match among the candidates counts; an ambiguous one (two
    # reverted files sharing a stem, e.g. `a/prod.py` and `b/prod.py`) must
    # never guess which one the error actually names - that ambiguity is
    # exactly what `_mentions_missing_prod_symbol`'s own, separate bare-stem
    # handling (E1c) exists to still catch, fail-closed, without guessing.
    bare_matches = _bare_stem_candidates(dotted, candidate_files)
    if len(bare_matches) == 1:
        return bare_matches[0]
    return None


def _top_level_names(source, include_imports=True):
    """Set of names bound at module top level: function/class defs, simple
    `NAME = ...` / `NAME: T = ...` assignments, tuple/list-unpacking targets
    (`A, B = ...`, E2 review round 1), and any of the above nested ONE level
    inside a top-level `if`/`try` block (`try: NEW = compute() except
    Exception: NEW = None`, or a version-gated `if ...: NEW = ...`) - never
    inside a nested function or class body, which are not module-level
    names. When `include_imports` is True (the default), also names
    re-exported via a top-level `from x import NEW` (E2b - the module's own
    top-level `ImportFrom` aliases; `import x` similarly binds `x`).

    E9 (review round 2): `include_imports=False` is used for the
    `additions_only` safety check ONLY - an unrelated import dropped or
    added alongside a genuine new definition (e.g. an unused `import os`
    removed in the same range) must never turn a pure addition into a napt.
    `include_imports=True` (the default) is still used for the "is X new at
    HEAD" test itself, so a name that only ever arrives via a re-export
    (E2b's exact shape) is still recognized as newly added."""
    names = set()
    try:
        tree = ast.parse(source or "")
    except SyntaxError:
        return names

    def add_target(t):
        if isinstance(t, ast.Name):
            names.add(t.id)
        elif isinstance(t, (ast.Tuple, ast.List)):
            for elt in t.elts:
                add_target(elt)

    def visit(stmts):
        for node in stmts:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    add_target(t)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names.add(node.target.id)
            elif isinstance(node, ast.ImportFrom):
                if include_imports:
                    for alias in node.names:
                        names.add(alias.asname or alias.name)
            elif isinstance(node, ast.Import):
                if include_imports:
                    for alias in node.names:
                        names.add((alias.asname or alias.name).split(".")[0])
            elif isinstance(node, ast.If):
                visit(node.body)
                visit(node.orelse)
            elif isinstance(node, ast.Try):
                visit(node.body)
                for handler in node.handlers:
                    visit(handler.body)
                visit(node.orelse)
                visit(node.finalbody)

    visit(tree.body)
    return names


def _symbol_error_candidates(out):
    """Every `cannot import name 'X' from 'M'` / `AttributeError: module 'M'
    has no attribute 'X'` occurrence in `out`, as `(start_offset, module_dotted,
    symbol)`, in the order they appear in the text - E3 (review round 1):
    the OLD code took only the single first regex `.search()` hit in the
    whole output; when a test file selects several tests and the FIRST one
    to fail hits an unresolvable name (e.g. one `_top_level_names` still
    can't see) while a LATER one hits a genuinely resolvable one, that first,
    unresolvable match used to mask the resolvable one entirely - the whole
    file fell through to a plain "resolved" (a false good). Returning every
    candidate, in order, lets the caller try each in turn."""
    out = out or ""
    candidates = [(m.start(), m.group(2), m.group(1)) for m in CANNOT_IMPORT_RE.finditer(out)]
    candidates += [(m.start(), m.group(1), m.group(2)) for m in ATTRIBUTE_ERROR_RE.finditer(out)]
    candidates.sort(key=lambda c: c[0])
    return candidates


def analyze_symbol_import_error(out, repo, base, head, reverted_prod_files):
    """None (not this shape - stays a plain "error"), or
    `(module_path, symbol, additions_only)` for the B1 shape: the collection
    error names a symbol `X` missing from module `M`, `M` is one of
    `reverted_prod_files` (so IT exists at base, just not with `X` yet), and
    `X` is a name HEAD's `M` defines but base's `M` does not. `additions_only`
    is True when every one of base's top-level names is still present in
    HEAD's `M` (a pure addition, safe to restore in full) - the caller
    reruns with `M` kept at HEAD in that case; otherwise n/a.

    E3 (review round 1): tries every `cannot import name`/`AttributeError`
    occurrence in `out`, in the order they appear, and returns the FIRST one
    that actually resolves (a candidate whose module isn't a reverted
    production file, or whose symbol doesn't qualify, is skipped rather than
    giving up on the whole output).

    E9 (review round 2): `additions_only` is computed from `_top_level_names(
    ..., include_imports=False)` - definitions and assignments only. The
    plain "is `symbol` new at HEAD" test above still uses the default
    (imports included), so a re-exported name (E2b) still qualifies as new;
    only the SAFETY check for "is it fine to restore this module whole"
    ignores import-statement churn, so an unrelated dropped/added import
    alongside a genuine new definition can never turn a pure addition into a
    napt."""
    for _, module_dotted, symbol in _symbol_error_candidates(out):
        module_path = _module_path_for_dotted(module_dotted, reverted_prod_files)
        if module_path is None:
            continue
        base_source = show_file(repo, base, module_path) or ""
        head_source = show_file(repo, head, module_path) or ""
        base_names = _top_level_names(base_source)
        head_names = _top_level_names(head_source)
        if symbol not in head_names or symbol in base_names:
            continue
        base_defs = _top_level_names(base_source, include_imports=False)
        head_defs = _top_level_names(head_source, include_imports=False)
        return module_path, symbol, base_defs <= head_defs
    return None


def _import_names(source):
    """Set of dotted MODULE paths a test file's `import`/`from ... import`
    statements genuinely execute - fix round 7: no bare last-component
    addition and no "the whole base package is a candidate for any of its
    submodules" - `import a.b.c` really does execute `a`, `a.b` and `a.b.c`
    (Python imports every ancestor package), so all three are real
    candidates; but `from pkg import x, y` must resolve to `pkg.x` and
    `pkg.y` specifically (the two submodules x/y might be), NEVER to bare
    `pkg` standing in for some OTHER, unrelated submodule under the same
    package. `pkg` itself IS a legitimate candidate here (pkg/__init__.py
    genuinely executes to make the from-import work), so it is one of the
    dotted PREFIXES added for the base - it is just never added as a stand-in
    for `pkg.x`/`pkg.y` themselves. A bare relative import (`from . import
    x`, no module name) is skipped - too ambiguous to resolve without
    walking the package tree, and skipping it only means this file falls
    through to running the revert check as before (never a false n/a).

    Returns `(dotted, bare)`: `dotted` is matched by exact dotted-path
    equality (round 7's precision fix); `bare` is a SEPARATE set of
    genuinely unqualified, dot-free `import x` names (the `sys.path` hack
    case: a test that inserts a tools dir onto `sys.path` and does a plain
    `import toolkit` carries no package-qualifying information at all, so
    that one case is still matched against a production file's bare last
    component - it is not the "shared package" ambiguity round 7 fixes,
    since there is no package name here to be ambiguous about)."""
    dotted = set()
    bare = set()
    try:
        tree = ast.parse(source or "")
    except SyntaxError:
        return dotted, bare

    def add_prefixes(name):
        parts = name.split(".")
        for i in range(1, len(parts) + 1):
            dotted.add(".".join(parts[:i]))

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                add_prefixes(alias.name)
                if "." not in alias.name:
                    bare.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level and not node.module:
                continue
            base = node.module or ""
            if base:
                add_prefixes(base)
            for alias in node.names:
                if base:
                    dotted.add(f"{base}.{alias.name}")
                else:
                    bare.add(alias.name)
    return dotted, bare


def expand_via_init_reexports(repo, head, dotted_names):
    """Fix round 7, the `__init__` case: `from pkg import name` where
    `pkg/__init__.py` itself does `from pkg.sub import name` (or `from .sub
    import name`) re-exports `name` from `sub`, not from a same-named
    `pkg/name.py` that may not even exist - add `pkg.sub` (and `pkg.sub.name`)
    as additional candidates for every "pkg.name"-shaped entry in
    `dotted_names` whose `pkg/__init__.py` does this. Only ever ADDS
    candidates - a package with no such re-export, or no `__init__.py` at
    all, is untouched. Takes and returns just the `dotted` half of
    `_import_names`'s result - the `bare` half needs no expansion, since it
    already carries no package to resolve through an `__init__.py`."""
    expanded = set(dotted_names)
    for cand in list(dotted_names):
        pkg, sep, attr = cand.rpartition(".")
        if not sep:
            continue
        init_path = pkg.replace(".", "/") + "/__init__.py"
        src = show_file(repo, head, init_path)
        if not src:
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            for alias in node.names:
                exported = alias.asname or alias.name
                if exported != attr:
                    continue
                if node.level:
                    sub_base = node.module or ""
                    resolved = pkg if not sub_base else f"{pkg}.{sub_base}"
                else:
                    resolved = node.module or ""
                if resolved:
                    expanded.add(resolved)
                    expanded.add(f"{resolved}.{alias.name}")
    return expanded


# I1 (review, guide-bundle): `contracts/intent/<role>.jsonl` (Zenkai row 454's intent examples) is
# read as DATA, never `import`ed - a test whose only real dependency is one of these files (e.g.
# calling `intent.check("guide", ...)` against a new example) had no way to be required to
# discriminate: it is not `.py`, so it never entered `prod_files`, and `references_prod_file` below
# is import-name matching, which cannot see a data-file dependency at all. Reverting these files to
# `--base` alongside `prod_files` closes that gap. Card t_3ff01dd1 widens this to `contracts/*.md`
# and `contracts/schemas/*.json` (also data, never imported) - `mutation_contractdata` carries all
# three predicates plus the reference check (`references_data_file`, a same-spirit, data-file
# analogue of `references_prod_file` below, tightened to a real call shape rather than a bare
# word-and-quoted-string coincidence).
INTENT_DATA_DIR = "contracts/intent/"


def is_intent_data_file(rel_path):
    rel_path = str(rel_path).replace("\\", "/")
    if not rel_path.startswith(INTENT_DATA_DIR) or not rel_path.endswith(".jsonl"):
        return False
    return rel_path == INTENT_DATA_DIR + Path(rel_path).name


def references_prod_file(test_import_names, prod_file):
    """True when `test_import_names` (a `(dotted, bare)` pair - `dotted` from
    `_import_names`, optionally widened by `expand_via_init_reexports`;
    `bare` from `_import_names` unchanged) references `prod_file` - fix
    round 7: exact dotted-path membership for anything package-qualified
    (never a bare shared top-level package: a test doing `from pkg import a`
    must not look like it also references an unrelated `pkg/b.py` merely
    because both live under `pkg` - only `pkg` itself, i.e. `pkg/__init__.py`,
    and `pkg.a` are real candidates for that import), plus a bare
    last-component match ONLY for genuinely unqualified `import x` names
    (the `sys.path`-hack case, which carries no package to be ambiguous
    about in the first place)."""
    dotted_names, bare_names = test_import_names
    dotted, attr, package = _prod_module_candidates(prod_file)
    return dotted in dotted_names or attr in bare_names


def any_assert_in_ranges(text, func_ranges):
    """True when at least one of `func_ranges` (the new/changed test
    functions) contains the word `assert` anywhere in its body - i.e. the
    static-flags "no assert" case, computed the same way, but as a boolean
    gate for round 6's file-level fallback rather than a report line. An `assert` inside a
    string literal does not count (t_bd08469e)."""
    lines = literals.mask_strings(text).splitlines(keepends=True)
    for start, end in func_ranges:
        body = "".join(lines[start - 1:min(end, len(lines))])
        if "assert" in body:
            return True
    return False


def apply_weakening(text, func_ranges):
    """Flip the FIRST discriminating assertion INSIDE one of `func_ranges`
    (the new/changed test functions themselves - flipping an assertion in some
    untouched test elsewhere in the file would prove nothing, since only the
    changed tests are re-run). `func_ranges`: [(lineno, end_lineno), ...].
    Returns (new_text, what) or (None, None) if nothing recognized was found
    inside those ranges."""
    lines = text.splitlines(keepends=True)
    masked_text = literals.mask_strings(text)
    offsets = [0, *accumulate(map(len, lines))]
    for start, end in sorted(func_ranges):
        body_start = offsets[start - 1]
        body_end = offsets[min(end, len(lines))]
        body = text[body_start:body_end]
        masked = masked_text[body_start:body_end]
        for pattern, repl, what in (
            (ASSERT_EQ_RE, "assertNotEqual(", "assertEqual -> assertNotEqual"),
            (ASSERT_TRUE_RE, "assertFalse(", "assertTrue -> assertFalse"),
        ):
            m = pattern.search(masked)
            if m:
                new_body = body[:m.start()] + repl + body[m.end():]
                return text[:body_start] + new_body + text[body_end:], what
        m = BARE_ASSERT_EQ_RE.search(masked)
        if m:
            flipped = literals.flip_eq(body[m.start():m.end()], m.group(0))
            new_body = body[:m.start()] + flipped + body[m.end():]
            return text[:body_start] + new_body + text[body_end:], "assert a == b -> assert a != b"
    return None, None


def _assertion_occurrences(body, masked=None):
    """[(start, end, replacement_text, what), ...], in source order, for
    every recognized assertion pattern occurrence in `body` - B5 (mirrored
    from the sibling project): lets a caller try the 1st, 2nd, ... occurrence in turn,
    not just the first. Matching runs on `masked` (string literals blanked,
    same length; t_bd08469e), the edits apply to `body`."""
    occ = []
    masked = body if masked is None else masked
    for m in ASSERT_EQ_RE.finditer(masked):
        occ.append((m.start(), m.end(), "assertNotEqual(", "assertEqual -> assertNotEqual"))
    for m in ASSERT_TRUE_RE.finditer(masked):
        occ.append((m.start(), m.end(), "assertFalse(", "assertTrue -> assertFalse"))
    for m in BARE_ASSERT_EQ_RE.finditer(masked):
        occ.append((m.start(), m.end(), literals.flip_eq(body[m.start():m.end()], m.group(0)),
                    "assert a == b -> assert a != b"))
    occ.sort(key=lambda o: o[0])
    return occ


def apply_weakening_at(text, region_start_line, region_end_line, index):
    """B5: flip the `index`-th (0-based, source order) recognized assertion
    occurrence within the line range [region_start_line, region_end_line] of
    `text` (1-based, inclusive - same convention as `func_ranges`
    elsewhere). Returns (new_text, what) or (None, None) if there is no such
    occurrence - the caller tries index 0, 1, 2, ... until this returns
    None, meaning every assertion in that range has been tried."""
    lines = text.splitlines(keepends=True)
    offsets = [0, *accumulate(map(len, lines))]
    body_start = offsets[region_start_line - 1]
    body_end = offsets[min(region_end_line, len(lines))]
    body = text[body_start:body_end]
    occ = _assertion_occurrences(body, literals.mask_strings(text)[body_start:body_end])
    if index >= len(occ):
        return None, None
    start, end, new_span, what = occ[index]
    new_body = body[:start] + new_span + body[end:]
    return text[:body_start] + new_body + text[body_end:], what


HELPER_CALL_RE = re.compile(r"\bself\.(\w+)\(|(?<![.\w])([a-zA-Z_]\w*)\(")


def _all_top_level_functions(source):
    """{(class_or_None, name): (lineno, end_lineno)} for EVERY function or
    method defined at module level or directly in a class body - any name,
    not just test* (B4: used to resolve a same-file helper a test body
    calls, one level deep)."""
    out = {}
    try:
        tree = ast.parse(source or "")
    except SyntaxError:
        return out

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.cls = None
            self.func_depth = 0

        def visit_ClassDef(self, node):
            prev, self.cls = self.cls, node.name
            self.generic_visit(node)
            self.cls = prev

        def _fn(self, node):
            if self.func_depth == 0:
                out[(self.cls, node.name)] = (node.lineno, getattr(node, "end_lineno", node.lineno))
            self.func_depth += 1
            self.generic_visit(node)
            self.func_depth -= 1

        visit_FunctionDef = _fn
        visit_AsyncFunctionDef = _fn

    Visitor().visit(tree)
    return out


def _find_helper_with_assert(source, body, own_class, all_funcs):
    """B4/B5 (mirrored from the sibling project): a test body with no `assert` of its
    own still counts as asserting when it calls a helper - `self.<name>(...)`
    (a method on the SAME class, or its base class within this file, when
    the base's own name ends in `Fixture`/`Base`) or a bare `<name>(...)`
    (a module-level function in this file) - whose OWN body contains
    `assert`. Returns `(name, lineno, end_lineno)` for the first such helper
    found, or None (a genuinely assert-free chain still flags)."""
    for m in HELPER_CALL_RE.finditer(body):
        name = m.group(1) or m.group(2)
        if not name or name.startswith("assert"):
            continue
        candidates = [(own_class, name), (None, name)]
        for cls, fname in all_funcs:
            if fname == name and cls and cls != own_class and (cls.endswith("Fixture") or cls.endswith("Base")):
                candidates.append((cls, name))
        for key in candidates:
            if key in all_funcs:
                s2, e2 = all_funcs[key]
                lines = literals.mask_strings(source).splitlines()
                helper_body = "\n".join(lines[s2 - 1:e2])
                if "assert" in helper_body:
                    return name, s2, e2
    return None


def _asserts_via_helper(source, body, own_class, all_funcs):
    """B4 wrapper: just the helper name, or None."""
    hit = _find_helper_with_assert(source, body, own_class, all_funcs)
    return hit[0] if hit else None


def static_flags(source, lineno, end_lineno, own_class=None, all_funcs=None):
    lines = source.splitlines()
    body = "\n".join(lines[lineno - 1:end_lineno])
    flags = []
    masked = "\n".join(literals.mask_strings(source).splitlines()[lineno - 1:end_lineno])
    if "assert" not in masked:
        helper = _asserts_via_helper(source, masked, own_class, all_funcs or {})
        if helper:
            flags.append(f"info: asserts via helper {helper}")
        else:
            flags.append("no assert in the function body")
    if TAUTOLOGY_RE.search(body):
        flags.append("assertTrue(True) / assert True - a tautology")
    if BARE_EXCEPT_RE.search(body):
        flags.append("bare `except: pass` could swallow the subject's own error")
    # decorators sit on the lines immediately above the def
    deco_window = "\n".join(lines[max(0, lineno - 6):lineno - 1])
    flags.extend(info.skip_flags(deco_window))
    return flags


def _cell(state, why=None):
    """`state` is None (n/a), True (failed - good), False (did not fail - bad),
    "error" (import/collection error - not discriminating, fix round 1),
    "new_module" (the reverted file doesn't exist yet - fix round 2),
    "no_prod_change" (no production file changed in this range is imported by this test file -
    fix round 4), "targets_predate_range" (an explicit, sha-bound, per-test marker says so - fix
    round 5/13, `why` is the marker's reason), "marker_mismatch" (marker sha does not match this
    gate's --base - fix round 5, never silently ignored), "marker_stale" (fix round 13: no `::`
    ids - the old file-level shape - or an id not in this range's new/changed set for that file;
    a gate failure, like marker_mismatch),
    "fallback_ok"/"fallback_bad" (weaken column, round 6: the changed function(s) had no
    assertion, so the mutation ran against the file's first assertion anywhere - always labelled),
    "no_functions" (round 8: no new/changed test FUNCTION - a decorator-only or fixture-signature
    edit - so nothing to select), or "prod_only_revert_ok" (attempt 2: code reverted, data kept).
    Every other state but fallback_bad, marker_mismatch and marker_stale is n/a, not a fail and
    not counted against the gate - the weaken column carries the requirement."""
    if state is None:
        return "n/a"
    if state == "new_module":
        return "n/a (new module)"
    if state == "no_prod_change":
        return "n/a (no production change)"
    if state == "no_functions":
        return "n/a (no test functions changed)"
    if state == "targets_predate_range":
        return f"n/a (targets predate range: {why})"
    if state == "marker_mismatch":
        return f"marker mismatch (BAD): {why}" if why else "marker mismatch (BAD)"
    if state == "marker_stale":
        return f"marker stale (BAD): {why}" if why else "marker stale (BAD)"
    if state in ("fallback_ok", "helper_ok"):
        return f"FAIL (good) [{why}]" if why else "FAIL (good) [weaken: file-level fallback]"
    if state in ("fallback_bad", "helper_bad"):
        return f"did not fail (BAD) [{why}]" if why else "did not fail (BAD) [weaken: file-level fallback]"
    if state == "error":
        # Review round 1, finding 6: the caller now folds the kept-chain
        # (module paths + symbols) and the unresolvable name into `why` -
        # show it, rather than the bare label alone, so a reviewer can see
        # exactly what didn't resolve without digging into the raw output.
        return f"ERROR (not discriminating): {why}" if why else "ERROR (not discriminating)"
    if state == "reverted_symbol_napt":
        return f"n/a (reverted symbol {why} imported at collection)"
    if state == "symbol_kept_ok":
        return f"FAIL (good) [revert: {why}]"
    if state in ("symbol_kept_bad", "symbol_kept_info"):
        return info.kept_cell(state, why)
    if state == "prod_only_revert_ok":
        return f"FAIL (good) [{why}]"
    if state == "symbol_addition_ok":
        return f"FAIL (good; new symbol {why})"
    if state == "unresolved_chain":
        return f"error_runner: unresolved chain {why}"
    return "FAIL (good)" if state else "did not fail (BAD)"


# Round 13: the sentinel key `revert_by_file[f]` uses for a state that
# applies to the whole test FILE rather than to any specific test id - the
# only case left where that happens is "no_functions" (the file has zero
# new/changed test functions at all, so there is no id to key a per-test
# state under). Every other state below is always assigned per test id, even
# when every id in the file happens to share the same one (`render_revert_cell`
# collapses that identical-everywhere case back to the old, un-prefixed cell
# text, so a file with no marker and one uniform outcome renders exactly as
# every pre-round-13 fixture already expects).
WHOLE_FILE_KEY = "*file*"


def _ids_for(funcs):
    """Fix round 13: the same `{cls}.{name}`-or-bare-`name` id shape used
    everywhere else in this file, as a set, for whatever `funcs` list
    (possibly already filtered down to the ids a marker did NOT cover) is in
    scope."""
    return {f"{cls}.{name}" if cls else name for cls, name, _, _ in funcs}


def _assign_revert_state(revert_by_file, f, funcs, state, why):
    """Round 13: record `(state, why)` for every id in `funcs` under
    `revert_by_file[f]` - one dict entry per test id, so a file can carry
    DIFFERENT revert-mutation outcomes for different tests (some n/a via a
    per-test marker, others genuinely revert-checked). Falls back to
    `WHOLE_FILE_KEY` only when `funcs` has no ids at all (the round-8
    no-functions-changed shape)."""
    ids_here = sorted(_ids_for(funcs))
    bucket = revert_by_file.setdefault(f, {})
    if not ids_here:
        bucket[WHOLE_FILE_KEY] = (state, why)
        return
    for tid in ids_here:
        bucket[tid] = (state, why)


def render_revert_cell(entries, ids):
    """Round 13: render the (b) column for one test file from its per-test
    `entries` dict (`{test_id: (state, why)}`, plus possibly the
    `WHOLE_FILE_KEY` sentinel) and `ids` (every new/changed test id in the
    file, sorted). When every id shares the identical `(state, why)` - true
    of every file this gate ever produced before round 13, since a whole file
    always got one uniform outcome - this renders EXACTLY the old, bare
    `_cell(...)` text (no id prefix), so every pre-round-13 fixture keeps
    passing unchanged. Only a file with a per-test marker that covers some
    but not all of its new/changed ids (the new round-13 shape) ever produces
    a mixed cell, one `id: cell` segment per id, in id order."""
    if not entries:
        return _cell(None, "-")
    if WHOLE_FILE_KEY in entries:
        state, why = entries[WHOLE_FILE_KEY]
        return _cell(state, why)
    values = {entries[tid] for tid in ids if tid in entries}
    if len(values) == 1 and all(tid in entries for tid in ids):
        state, why = next(iter(values))
        return _cell(state, why)
    parts = []
    for tid in ids:
        if tid in entries:
            state, why = entries[tid]
            parts.append(f"{tid}: {_cell(state, why)}")
    return "; ".join(parts) if parts else _cell(None, "-")


# Card t_285a8851: round 10's B1 (analyze_symbol_import_error, above) resolves
# ONE missing new name by keeping its module at HEAD and rerunning once. When
# the kept module itself imports a SECOND new name from a still-reverted
# module, that single rerun fails collection again - seen for real on
# The sibling project's fix/n1-parity-harness, `tests/gauntlet/policy.py` importing
# `KNOWN_FLAGS` from the reverted `gauntlet/evaluate.py`. `_resolve_import_chain`
# repeats keep-and-rerun until nothing more resolves, bounded by this limit so
# a genuinely broken or unrelated failure can never spin forever.
CHAIN_ITERATION_LIMIT = 5

_SYMBOL_WORD_RE_CACHE = {}


def _collapse_for_cell(out):
    """Review round 2, finding 4: an "error" state's `why` now renders
    straight into a markdown table row (`render_table`'s `| ... |`), but the
    raw output used to be `out[-800:]` - a multi-line traceback that breaks
    the table (and a gate that keeps only the last 15 lines
    of output means a long `why` can push the row's own file name out of
    what a reviewer even sees). Keep just the last NON-EMPTY line of `out`,
    with any embedded newline collapsed to a space (defensive - a single
    line should never contain one, but never trust that) and `|` escaped so
    it can never be mistaken for a new table cell."""
    out = out or ""
    lines = [ln for ln in out.splitlines() if ln.strip()]
    last = lines[-1] if lines else ""
    last = last.replace("\n", " ").replace("\r", " ")
    return last.replace("|", "\\|")


def _mentions_missing_prod_symbol(out, repo, base, prod_files):
    """Fail-closed backstop (review round 1, finding 3): `_top_level_names`
    is a best-effort AST scan and can still miss a binding shape (a `for`/
    `with` target, a name computed by a decorator, a star-import re-export,
    ...). Rather than let `analyze_symbol_import_error` finding "no
    candidate resolves" silently fall through to `_resolve_import_chain`'s
    plain "resolved" (the same false-good shape E1/E2/E2b exploited before
    their own fixes), treat ANY cannot-import/AttributeError occurrence that
    names a `prod_files` module and a symbol that is textually absent from
    that module's `--base` source AT ALL (not merely absent from the AST
    scan - an even weaker, more conservative check) as "still unresolved":
    the caller returns `"error"`, never `"resolved"`.

    E1c (review round 2): `_module_path_for_dotted` correctly REFUSES to
    guess which module an ambiguous bare-stem name (e.g. `a/prod.py` and
    `b/prod.py` both changed, error names the bare `prod`) actually refers
    to - but that same refusal used to make this backstop blind to the
    ambiguous case too (`module_path is None` skipped it entirely). This
    backstop doesn't need to pick ONE module, only to know whether the
    symbol might be missing from any of them: when the unique/exact lookup
    fails, it falls back to every bare-stem candidate and fails closed if
    the symbol is absent from ALL of their `--base` sources."""
    for _, module_dotted, symbol in _symbol_error_candidates(out):
        module_path = _module_path_for_dotted(module_dotted, prod_files)
        candidates = [module_path] if module_path is not None else _bare_stem_candidates(module_dotted, prod_files)
        if not candidates:
            continue
        pattern = _SYMBOL_WORD_RE_CACHE.get(symbol)
        if pattern is None:
            pattern = re.compile(r"\b" + re.escape(symbol) + r"\b")
            _SYMBOL_WORD_RE_CACHE[symbol] = pattern
        if not any(pattern.search(show_file(repo, base, c) or "") for c in candidates):
            return True
    return False


def _resolve_import_chain(repo, base, head, wt, test_path, prod_files, names, weaken_is_good=False):
    """Keep-and-rerun `test_path`'s `names` selection inside worktree `wt`
    until collection stops failing on a resolvable missing-new-name shape, a
    genuinely different failure surfaces, `CHAIN_ITERATION_LIMIT` modules
    have been kept without resolving, or the exact same `(module, symbol)`
    resolution repeats (a cycle, review round 1 finding 6 - no point burning
    the remaining iterations' timeouts, each at least 180s, on a repeat).

    Handles a collection-time `cannot import name 'X' from 'M'` AND (card
    t_285a8851) a run-time `AttributeError: module 'M' has no attribute 'X'`
    inside a test body, which `is_collection_error` never sees - so it is
    tried before the collection-error branch, not instead of it.

    Returns `(state, extra, out, kept)`:
      - state="resolved": `extra` is the final `rc`, `out` the final output,
        `kept` the (possibly empty) list of `(module_path, symbol)` restored.
        A genuine pass/fail (or an unrelated failure that reached test
        execution) is scored normally by the caller from `extra`/`kept`.
      - state="napt": the resolvable module's diff is not additions-only AND
        no module has been kept yet (`kept` is empty); `extra` is the symbol
        name (n/a, weaken carries the requirement) - unchanged from round 10.
      - state="addition_explained" (row 459, only when `weaken_is_good`): the
        pure additions explain every failure; `extra` names them, no keep.
      - state="new_module": the collection error is explained by a production
        file that is new in this range (round 2's rule); `extra` is None.
      - state="error": a genuinely unrelated/broken collection error
        (`extra` is None); the review round 1 fail-closed backstop
        (`extra` is None); OR a napt hit found PARTWAY down an already
        non-empty chain (review round 1 finding 5 - reaching n/a directly
        here would be a quieter version of round 10's plain ERROR for the
        exact same shape; `extra` is the `(module_path, symbol)` that wasn't
        a pure addition, `kept` still carries every module resolved before
        it, so the caller's label shows the whole chain).
      - state="unresolved": `CHAIN_ITERATION_LIMIT` modules were kept, or the
        same `(module_path, symbol)` resolution repeated, and it still
        doesn't resolve; `extra` is the `(module_path, symbol)` that could
        not be kept (the Nth name, review round 1 finding 6), `kept` names
        every module/symbol actually resolved before it.

    Every module written into `wt` to "keep" it is restored back to its
    `--base` content before this function returns, on every exit path - the
    caller sees `wt` exactly as it found it, just like `make_worktree`/
    `remove_worktree` bracket the whole revert phase."""
    kept = []
    seen = set()
    new_prod_files = [pf for pf in prod_files if file_status(repo, base, head, pf) == "A"]
    context = newsymbol.collect(*newsymbol.reachable_support(show_file(repo, head, test_path),
        changed_files(repo, base, head), alias.reader(repo, head, show_file)))
    try:
        while True:
            rc, out = run_discover(wt, (wt / test_path).parent, Path(test_path).name, names)
            if rc == 0:
                return "resolved", rc, out, list(kept)
            symbol_hit = analyze_symbol_import_error(out, repo, base, head, prod_files)
            if symbol_hit is not None:
                module_path, symbol, additions_only = symbol_hit
                if not additions_only:
                    return (("error", (module_path, symbol)) if kept else ("napt", symbol)) + (out, list(kept))
                known = newsymbol.known_names(kept, symbol, *(show_file(repo, r, module_path) for r in (base, head)))
                if weaken_is_good and not is_collection_error(out) and newsymbol.addition_explains_failures(
                        out, known, context, (CANNOT_IMPORT_RE, ATTRIBUTE_ERROR_RE)):
                    return "addition_explained", ", ".join(sorted(known)), out, list(kept)
                if (module_path, symbol) in seen or len(kept) >= CHAIN_ITERATION_LIMIT:
                    return "unresolved", (module_path, symbol), out, list(kept)
                (wt / module_path).write_text(show_file(repo, head, module_path), encoding="utf-8")
                kept.append((module_path, symbol))
                seen.add((module_path, symbol))
                continue
            if is_collection_error(out):
                if new_module_import_error(out, new_prod_files):
                    return "new_module", None, out, list(kept)
                return "error", None, out, list(kept)
            if _mentions_missing_prod_symbol(out, repo, base, prod_files):
                return "error", None, out, list(kept)
            # rc != 0, not a resolvable missing-new-name shape, not a
            # collection error at all: a genuine failure (or a genuinely
            # unrelated run-time error) that reached test execution.
            return "resolved", rc, out, list(kept)
    finally:
        for module_path, _ in kept:
            revert_to_base(repo, base, wt, module_path, file_status(repo, base, head, module_path))


def check_range(repo, base, head):
    """Round 12 (worktree hygiene, mirrored from the sibling project): every scratch
    worktree this gate creates - (b) revert, (c) weaken - is already
    removed in its own `finally`, but a hard process kill (SIGKILL) or a
    bare git failure inside `remove_worktree` (which swallows its own exit
    code - see `sh`) can still leave a stale `.git/worktrees/*` entry
    behind (the sibling project's sibling script had leaked 501 such entries into its
    own .git). `git worktree prune` is therefore run UNCONDITIONALLY when
    this function returns, raises, or is interrupted (Ctrl-C/
    KeyboardInterrupt) - it is a pure cleanup pass over metadata for
    worktrees whose directory is already gone, never a mutation of a live
    one, so running it even when nothing leaked is always safe."""
    try:
        return _check_range_impl(repo, base, head)
    finally:
        sh(["git", "worktree", "prune"], cwd=Path(repo).resolve())


def _prod_only_retry(repo, base, head, wt, f, prod, data_files, names, weak, failed, out):
    """(b) attempt 2: code AND intent data, "did not fail": rerun with code alone reverted."""
    if failed or not (prod and data_files):
        return failed, out[-800:]
    data = data_files + contractdata.sha256sums_for(data_files)
    for df in data:
        revert_to_base(repo, head, wt, df, "M")
    try:
        st, rc, _o, kept = _resolve_import_chain(repo, base, head, wt, f, prod, names, weak)
    finally:
        for df in data:
            revert_to_base(repo, base, wt, df, file_status(repo, base, head, df))
    if st == "resolved" and rc != 0 and not kept:
        return "prod_only_revert_ok", "revert: production only; contracts data kept at HEAD"
    return failed, out[-800:]


def _check_range_impl(repo, base, head):
    repo = Path(repo).resolve()
    changed = changed_files(repo, base, head)
    test_files = [f for f in changed if is_test_file(f)]
    prod_files = [f for f in changed if f.endswith(".py") and not is_test_file(f) and not is_test_support_file(f)]
    shell_files = [f for f in changed if literals.is_shell_product_file(f)]
    intent_data_files = [f for f in changed if is_intent_data_file(f) or contractdata.is_contract_data_file(f)]

    rows = []
    ok = True

    if not test_files:
        return True, [], "no new/changed test files in this range"

    per_file_funcs = {}
    for f in test_files:
        status = file_status(repo, base, head, f)
        added = added_line_numbers(repo, base, head, f)
        funcs = changed_test_functions(repo, base, head, f, status, added)
        per_file_funcs[f] = (status, funcs)

    # ---- (d) static flags, read straight from HEAD ----
    static_by_file = {}
    for f, (status, funcs) in per_file_funcs.items():
        src = show_file(repo, head, f)
        all_funcs = _all_top_level_functions(src)
        flags = []
        hard_flag = False
        for cls, name, s, e in funcs:
            for flag in static_flags(src, s, e, own_class=cls, all_funcs=all_funcs):
                flags.append(f"{cls + '.' if cls else ''}{name}: {flag}")
                if not flag.startswith("info:"):
                    hard_flag = True
        static_by_file[f] = flags
        if hard_flag:
            ok = False

    # ---- (c) assertion-weakening, per file, same worktree (row 459: ahead of (b), which needs it) ----
    weaken_by_func = {}
    with tempfile.TemporaryDirectory(prefix="mutation-check-weaken-") as tmp:
        wt = make_worktree(repo, head, Path(tmp))
        try:
            for f, (status, funcs) in per_file_funcs.items():
                target = wt / f
                if not target.exists() or not funcs:
                    continue
                original = target.read_text(encoding="utf-8")
                func_ranges = [(s, e) for _, _, s, e in funcs]
                mutated, what = apply_weakening(original, func_ranges)

                if mutated is None:
                    # B5 (mirrored from the sibling project, round 12, HTC integration
                    # audit): the changed function(s) may assert only via a
                    # same-file helper (the exact B4 shape) - try EACH of the
                    # helper's own assertions in turn before ever falling
                    # back to file-level (which would mutate something the
                    # range never even exercises). Only a function with NO
                    # helper at all falls through to the file-level fallback
                    # below.
                    all_funcs = _all_top_level_functions(original)
                    lines = literals.mask_strings(original).splitlines()
                    helper_hit = None
                    for cls, name, s, e in funcs:
                        body = "\n".join(lines[s - 1:e])
                        if "assert" in body:
                            continue
                        hit = _find_helper_with_assert(original, body, cls, all_funcs)
                        if hit:
                            helper_hit = hit
                            break
                    if helper_hit is not None:
                        h_name, h_start, h_end = helper_hit
                        names_for_run = sorted({f"{cls}.{name}" if cls else name
                                                 for cls, name, _, _ in funcs})
                        idx = 0
                        resolved = None
                        while True:
                            candidate, cand_what = apply_weakening_at(original, h_start, h_end, idx)
                            if candidate is None:
                                break
                            target.write_text(candidate, encoding="utf-8")
                            try:
                                rc, out = run_discover(wt, target.parent, target.name, names_for_run)
                            finally:
                                target.write_text(original, encoding="utf-8")
                            if is_collection_error(out):
                                resolved = ("error", _collapse_for_cell(out))
                                break
                            if rc != 0:
                                resolved = ("helper_ok", f"weaken: helper {h_name} assertion {idx + 1} ({cand_what})")
                                break
                            idx += 1
                        if resolved is None:
                            resolved = ("helper_bad",
                                        f"weaken: helper {h_name} - none of its {idx} assertion(s) discriminate")
                        weaken_by_func[f] = resolved
                        if resolved[0] in ("helper_bad", "error"):
                            ok = False
                        continue

                fallback = False
                if mutated is None and not any_assert_in_ranges(original, func_ranges):
                    # Round 6: the changed function(s) contain no assertion at
                    # all (the static flag (d) already reports this
                    # separately) - the ONLY thing left to weaken is
                    # somewhere else in the file. Never silent: `what`
                    # carries "weaken: file-level fallback" into the table
                    # either way, so a reviewer sees this ran against code
                    # the range didn't even touch.
                    all_lines = len(original.splitlines())
                    mutated, what = apply_weakening(original, [(1, all_lines)])
                    if mutated is not None:
                        what = f"weaken: file-level fallback ({what})"
                        fallback = True
                names = sorted({f"{cls}.{name}" if cls else name for cls, name, _, _ in funcs})
                if mutated is None:
                    weaken_by_func[f] = (None, "no assertEqual/assertTrue/`==` assert found to flip")
                    continue
                if fallback:
                    # The file-level fallback may have mutated an assertion
                    # OUTSIDE the changed function(s) - run every test in the
                    # file, not just `names`, or a fallback mutation to an
                    # old, unselected test could never be observed failing.
                    run_names = sorted({f"{cls}.{name}" if cls else name
                                        for cls, name in test_defs(original)})
                else:
                    run_names = names
                target.write_text(mutated, encoding="utf-8")
                try:
                    rc, out = run_discover(wt, target.parent, target.name, run_names)
                finally:
                    target.write_text(original, encoding="utf-8")
                if is_collection_error(out):
                    weaken_by_func[f] = ("error", _collapse_for_cell(out))
                    ok = False
                    continue
                failed = rc != 0
                if fallback:
                    weaken_by_func[f] = ("fallback_ok" if failed else "fallback_bad", what)
                else:
                    weaken_by_func[f] = (failed, what if failed else f"{what}, but the file still passed: {out[-400:]}")
                if not failed:
                    ok = False
        finally:
            remove_worktree(repo, wt)

    # ---- (b) revert-mutation, one worktree, production reverted to base ----
    revert_by_file = {}

    # Fix round 5 (per-test as of round 13): a marker overrides revert-mutation, checked first, so
    # a bad one is not ignored. It must be added in this range, with a sha that is --base or an
    # ancestor (t_0080b449), and name the `:: <ids>` it covers (round 13); other new/changed ids
    # still get the revert check. An earlier card's marker, no ids, or a foreign id: marker_stale.
    resolved_base = resolve_sha(repo, base)
    for f, (status, funcs) in per_file_funcs.items():
        marker = extract_marker(show_file(repo, head, f))
        if marker is None:
            continue
        marker_sha, reason, marker_ids = marker
        if extract_marker(show_file(repo, base, f)) == marker:   # an earlier card's
            why = (f"marker was already in the file at --base ({reason}); remove it, or rewrite it "
                   "for this card's ids with sha `git merge-base <start_sha> origin/main`")
            _assign_revert_state(revert_by_file, f, funcs, "marker_stale", why)
            ok = False
            continue
        file_ids = _ids_for(funcs) | {n for _, n, _, _ in funcs}   # a bare id is a method name
        resolved_marker = resolve_sha(repo, marker_sha)
        if not marker_sha_ok(repo, resolved_marker, resolved_base):
            why = f"marker names {marker_sha}, gate --base is {base} ({reason})"
            _assign_revert_state(revert_by_file, f, funcs, "marker_mismatch", why)
            ok = False
            continue
        if not marker_ids:
            why = (f"marker has no `:: <ids>` list ({reason}) - a file-level marker with "
                   "no ids is stale as of round 13")
            _assign_revert_state(revert_by_file, f, funcs, "marker_stale", why)
            ok = False
            continue
        unknown = [i for i in marker_ids if i not in file_ids]
        if unknown:
            why = (f"marker names test id(s) not in this range's new/changed set for "
                   f"{f}: {', '.join(unknown)} ({reason})")
            _assign_revert_state(revert_by_file, f, funcs, "marker_stale", why)
            ok = False
            continue
        covered = [(cls, name, s, e) for cls, name, s, e in funcs
                   if {f"{cls}.{name}" if cls else name, name} & set(marker_ids)]
        _assign_revert_state(revert_by_file, f, covered, "targets_predate_range", reason)

    # Every id NOT already given a state above (no marker, or a marker covering only some of a
    # file's ids) still needs the ordinary revert-mutation check; `remaining` carries just those;
    # a file the marker loop fully handled is dropped entirely.
    remaining = {}
    for f, (status, funcs) in per_file_funcs.items():
        handled = set(revert_by_file.get(f, {}).keys())
        uncovered_ids = _ids_for(funcs) - handled
        if f in revert_by_file and not uncovered_ids:
            continue
        if handled:
            funcs = [(cls, name, s, e) for cls, name, s, e in funcs
                     if (f"{cls}.{name}" if cls else name) in uncovered_ids]
        remaining[f] = (status, funcs)

    data_targets = intent_data_files + contractdata.sha256sums_for(intent_data_files)
    revert_targets = prod_files + shell_files + data_targets
    if revert_targets and remaining:
        with tempfile.TemporaryDirectory(prefix="mutation-check-revert-") as tmp:
            wt = make_worktree(repo, head, Path(tmp))
            try:
                for f in revert_targets:
                    status = file_status(repo, base, head, f)
                    revert_to_base(repo, base, wt, f, status)
                for f, (status, funcs) in remaining.items():
                    # Fix round 8: a changed test FILE with no new/changed test function (a
                    # decorator-only or fixture-signature edit) has no ids to select; an empty
                    # `run_discover` would read as "BAD".
                    if not funcs:
                        _assign_revert_state(revert_by_file, f, funcs, "no_functions",
                                             "no new/changed test function in this file "
                                             "(decorator-only or fixture-signature edit)")
                        continue
                    # Fix round 4: a test file not ABOUT any changed production file (new tests for
                    # pre-existing code) can never be discriminated by reverting it, and reading that
                    # as "did not fail (BAD)" would be a false alarm. Row 526: ABOUT includes an alias.
                    head_source = show_file(repo, head, f)
                    if not literals.reaches_change(sys.modules[__name__], repo, head, head_source,
                                                   prod_files, intent_data_files, shell_files):
                        _assign_revert_state(revert_by_file, f, funcs, "no_prod_change",
                                             "no production file changed in this range is imported by this test file")
                        continue
                    names = sorted({f"{cls}.{name}" if cls else name for cls, name, _, _ in funcs})
                    weaken_good = weaken_by_func.get(f, (None,))[0] in (True, "fallback_ok", "helper_ok")
                    state, extra, out, chain_kept = _resolve_import_chain(
                        repo, base, head, wt, f, prod_files, names, weaken_is_good=weaken_good)
                    if state in ("napt", "addition_explained"):
                        # B1 fallback: M's diff is not a pure addition - n/a, weaken carries
                        # the requirement. Row 459: pure additions explain every failure.
                        _assign_revert_state(revert_by_file, f, funcs, "reverted_symbol_napt" if state == "napt"
                                             else "symbol_addition_ok", extra)
                        continue
                    if state == "new_module":
                        st, why = literals.check_new_modules(
                            sys.modules[__name__], repo, base, head, wt, f, prod_files, names,
                            weaken_good, out)
                        _assign_revert_state(revert_by_file, f, funcs, st, why or out[-800:])
                        ok = ok and st not in (False, "symbol_kept_bad")
                        continue
                    if state == "error":
                        # Review round 1, finding 5: `extra` is a (module_path, symbol) pair for a
                        # napt hit partway down a non-empty chain (show chain + offending hop, never
                        # the quieter n/a); otherwise (a genuinely unrelated collection error, or
                        # the fail-closed backstop) None, and the message is just the output tail.
                        parts = []
                        if chain_kept:
                            parts.append("; ".join(f"{mp} kept at head for import of {sym}"
                                                    for mp, sym in chain_kept))
                        if isinstance(extra, tuple):
                            mp, sym = extra
                            parts.append(f"then {mp}'s change for {sym} is not a pure addition")
                        parts.append(_collapse_for_cell(out))
                        _assign_revert_state(revert_by_file, f, funcs, "error", " | ".join(p for p in parts if p))
                        ok = False
                        continue
                    if state == "unresolved":
                        # Card t_285a8851: chain limit or repeat (round 1 finding 6): never a pass
                        # or ERROR; names each module kept and the one that could not be.
                        chain_desc = ", ".join(f"{mp}:{sym}" for mp, sym in chain_kept)
                        if isinstance(extra, tuple):
                            next_mp, next_sym = extra
                            chain_desc += f"; next unresolved: {next_mp}:{next_sym}"
                        _assign_revert_state(revert_by_file, f, funcs, "unresolved_chain", chain_desc)
                        ok = False
                        continue
                    # state == "resolved": extra is the final rc.
                    rc = extra
                    failed = rc != 0
                    if chain_kept:
                        # Modules (or a run-time `module.NEW_NAME` access) kept at HEAD (card
                        # t_285a8851): never an unlabelled pass/fail.
                        label = "; ".join(f"{mp} kept at head for import of {sym}" for mp, sym in chain_kept)
                        _assign_revert_state(
                            revert_by_file, f, funcs, "symbol_kept_ok" if failed else (
                                "symbol_kept_info" if weaken_good else "symbol_kept_bad"), label)
                        ok = ok and (failed or weaken_good)
                    else:
                        failed, why = _prod_only_retry(repo, base, head, wt, f, prod_files,
                                                       intent_data_files, names, weaken_good,
                                                       failed, out)
                        _assign_revert_state(revert_by_file, f, funcs, failed, why)
                        if not failed:
                            ok = False
            finally:
                remove_worktree(repo, wt)
    else:
        for f, (status, funcs) in remaining.items():
            _assign_revert_state(revert_by_file, f, funcs, None,
                                 "no production .py, bin/maintenance shell or"
                                 " contracts/intent/*.jsonl file changed in this range"
                                 " - (b) skipped")

    for f in test_files:
        status, funcs = per_file_funcs[f]
        ids_sorted = sorted(_ids_for(funcs))
        names = ", ".join(ids_sorted) or "-"
        weak_ok, weak_why = weaken_by_func.get(f, (None, "-"))
        flags = static_by_file.get(f, [])
        rev_cell = render_revert_cell(revert_by_file.get(f, {}), ids_sorted)
        weak_cell = _cell(weak_ok, weak_why)
        flags_cell = "; ".join(flags) if flags else "none"
        rows.append((f, names, rev_cell, weak_cell, flags_cell))

    return ok, rows, None


def render_table(rows):
    header = "| test file | new/changed tests | revert-mutation (b) | assertion-weaken (c) | static flags (d) |"
    sep = "|---|---|---|---|---|"
    lines = [header, sep]
    for f, names, rev_cell, weak_cell, flags_cell in rows:
        lines.append(f"| `{f}` | {names} | {rev_cell} | {weak_cell} | {flags_cell} |")
    return "\n".join(lines)


#: Directories/files this gate's own integrity depends on (mirrored from
#: the sibling project's sibling `tests/mutation_check.py`). The incident this closes:
#: the gate's own wiring (`maintenance/merge_gate.py`, this file) sat
#: UNCOMMITTED for several merge-gate rounds while a run exercised it by
#: file path only - a "clean" gate run said nothing about the fact that the
#: thing actually running was not what HEAD said it was. `capsule/` is
#: watched for the same reason on the other side: it is the production
#: package this gate exists to protect.
DIRTY_TREE_WATCHED_PATHS = ("maintenance/", "tests/mutation_check.py", "tests/size_check.py",
                             "tests/simplicity_check.py", "tests/size_baseline.py",
                             "tests/size_baseline.json", "capsule/")


#: inc_df5777e3: printed after the table when a revert cell reads "did not fail (BAD)".
REMEDY = ("\nAdd a test that fails on --base ({}): assert the behaviour this range adds, "
          "changes or removes (for a removal, assert the removed name, key or path is absent). "
          "Keep the existing tests. Rerun the gate before you report this as left undone.")


def dirty_tracked_paths(repo):
    """Tracked files under `DIRTY_TREE_WATCHED_PATHS` in `repo` with
    uncommitted changes (staged or unstaged) - never untracked files (`??`
    in `git status --porcelain`), which are not "the gate's own wiring" or
    "the code under test" disagreeing with HEAD, just scratch files nobody
    has added yet. Returns a sorted list of relative paths, empty when clean (or when `repo` is
    not a git worktree at all - a fixture repo without these paths reports nothing dirty)."""
    rc, out = sh(["git", "status", "--porcelain", "--no-renames", "--",
                  *DIRTY_TREE_WATCHED_PATHS], cwd=str(repo), timeout=30)
    if rc != 0:
        return []
    dirty = []
    for line in (out or "").splitlines():
        if not line or line.startswith("??"):
            continue
        dirty.append(line[3:].strip())
    return sorted(set(dirty))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", required=True)
    ap.add_argument("--head", required=True)
    ap.add_argument("--repo", default=".")
    ap.add_argument("--allow-dirty", action="store_true",
                     help="Local experiments only: run despite uncommitted gate-critical "
                          "paths in --repo; warns loudly; never for a real merge decision.")
    a = ap.parse_args(argv)
    # Refuse before any check: never a verdict about a tree that is not HEAD or cannot import.
    dirty = dirty_tracked_paths(a.repo)
    if dirty:
        if not a.allow_dirty:
            print("environment error: uncommitted changes to gate-critical paths:")
            for p in dirty:
                print(f"  {p}")
            print("Commit them first, or pass --allow-dirty for a local experiment only.")
            return 75    # EX_TEMPFAIL: the environment, not the code; lander.ENV_RC retries it
        print("WARN: --allow-dirty set; running with uncommitted changes to gate-critical "
              "paths (local experiments only, never a real merge decision):", file=sys.stderr)
        for p in dirty:
            print(f"  {p}", file=sys.stderr)
    if rc := mutation_env.refuse(a.repo, a.base):
        return rc
    ok, rows, note = check_range(a.repo, a.base, a.head)
    size_ok, size_report = size_check.check(a.repo, a.base, a.head)
    simple_ok, simple_report = simplicity_check.check(a.repo, a.base, a.head)
    print(size_report, simple_report, sep="\n")
    if note:
        print(note)
        return 0 if size_ok and simple_ok else 1
    bad = sorted({r[0] for r in rows if "did not fail (BAD)" in r[2]})
    print(render_table(rows) + (REMEDY.format(", ".join(f"`{f}`" for f in bad)) if bad else ""))
    return 0 if ok and size_ok and simple_ok else 1


if __name__ == "__main__":
    sys.exit(main())
