"""Zenkai Z1/Z2 (docs/ZENKAI.md, Z7 row 509) for tests/size_check.py: its own intent
examples (contracts/intent/size_check.jsonl), run for real through `check()` below
(`IntentExamples`), and its second oracle (ruff C901 vs the ast function-length
measure). Everything runs against a tiny fixture repo built fresh, per test, in
tempfile - never against capsule-corp itself, exactly like tests/test_mutation_check.py."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import size_check as sc  # noqa: E402
import size_baseline as bl  # noqa: E402


def git(cwd, *args):
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert p.returncode == 0, f"git {args} failed: {p.stdout}{p.stderr}"
    return p.stdout


def init_repo(root):
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")


def commit_all(root, message):
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", message)
    return git(root, "rev-parse", "HEAD").strip()


def make_repo():
    root = Path(tempfile.mkdtemp())
    init_repo(root)
    return root


def write_baseline(root, data):
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests" / "size_baseline.json").write_text(json.dumps(data))


def frozen_entries(root, sha, path):
    """{key: value} for every real violation (ast AND ruff) `path` has at `sha` - so a
    baseline built from this actually covers what `check()` will re-measure, complexity
    included, not just the function_length half of it."""
    ok, violations, err = sc.measure(str(root), sha, [path])
    assert ok, err
    return {sc.key(v): v[4] for v in violations}


OVERSIZED_FUNCTION = "def busy(x):\n" + "\n".join(f"    if x == {i}:\n        x += {i}" for i in range(40)) + "\n    return x\n"
TINY_FUNCTION = "def add(a, b):\n    return a + b\n"


class AstOracle(unittest.TestCase):
    def test_function_over_60_lines_flagged(self):
        kinds = {v[2] for v in sc.ast_violations("prod.py", OVERSIZED_FUNCTION)}
        self.assertIn("function_length", kinds)

    def test_short_function_not_flagged(self):
        self.assertEqual(sc.ast_violations("prod.py", TINY_FUNCTION), [])

    def test_file_over_800_lines_flagged(self):
        text = "\n".join(f"x{i} = {i}" for i in range(900))
        self.assertTrue(any(v[2] == "file_length" for v in sc.ast_violations("prod.py", text)))

    def test_method_gets_a_class_qualified_name(self):
        text = "class C:\n    def m(self):\n" + "\n".join("        pass" for _ in range(65)) + "\n"
        v = sc.ast_violations("prod.py", text)[0]
        self.assertEqual(v[3], "C.m")


class TwoOracles(unittest.TestCase):
    """Z2: ruff C901 and the ast function-length measure agree on the same fixture
    function - both are real, required oracles (never an advisory skip)."""

    def test_ruff_and_ast_agree_on_an_oversized_complex_function(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_repo(root)
            (root / "prod.py").write_text(OVERSIZED_FUNCTION)
            sha = commit_all(root, "add busy()")
            self.assertTrue(any(v[2] == "function_length" for v in sc.ast_violations("prod.py", OVERSIZED_FUNCTION)))
            ok, ruff_hits, err = sc.ruff_violations(str(root), sha, ["prod.py"])
            self.assertTrue(shutil.which("ruff") or shutil.which("uvx"), "neither ruff nor uvx on PATH")
            self.assertTrue(ok, err)
            self.assertTrue(any(v[2] == "complexity" for v in ruff_hits))


class RuffHardFailure(unittest.TestCase):
    """Z2: ruff/uvx trouble is a hard failure, never a silent pass."""

    def test_check_fails_hard_when_neither_is_on_path(self):
        root = make_repo()
        write_baseline(root, {})
        base = commit_all(root, "base: an established, empty baseline")
        (root / "prod.py").write_text(TINY_FUNCTION)
        head = commit_all(root, "add prod.py")
        # a scratch dir holding only a `git` link: Homebrew puts git, ruff and uvx side by side
        bindir = Path(tempfile.mkdtemp())
        (bindir / "git").symlink_to(shutil.which("git"))
        git_only = str(bindir)
        with unittest.mock.patch.dict(os.environ, {"PATH": git_only}):
            self.assertIsNone(shutil.which("ruff"))
            self.assertIsNone(shutil.which("uvx"))
            ok, report = sc.check(str(root), base, head)
        self.assertFalse(ok)
        self.assertIn("ruff", report.lower())
        self.assertIn("uvx", report.lower())

    def test_empty_uv_cache_fails_hard_not_a_silent_pass(self):
        """Review B1: `uvx --offline` with nothing cached exits 1 saying so - that rc,
        forced by `--exit-zero` to mean only "the tool broke", must hard-fail, never
        read as "ruff ran clean". PATH is pinned to a scratch dir holding ONLY `git`
        and `uvx` symlinks, so this exercises the uvx branch regardless of whether the
        host machine also happens to have a real `ruff` on its normal PATH."""
        uvx = shutil.which("uvx")
        self.assertIsNotNone(uvx, "uvx must be installed for this test to mean anything")
        bindir = Path(tempfile.mkdtemp())
        (bindir / "git").symlink_to(shutil.which("git"))
        (bindir / "uvx").symlink_to(uvx)
        root = make_repo()
        (root / "prod.py").write_text(TINY_FUNCTION)
        sha = commit_all(root, "add prod.py")
        with tempfile.TemporaryDirectory() as empty_cache:
            with unittest.mock.patch.dict(os.environ, {"PATH": str(bindir), "UV_CACHE_DIR": empty_cache}):
                ok, violations, err = sc.ruff_violations(str(root), sha, ["prod.py"])
        self.assertFalse(ok)
        self.assertEqual(violations, [])
        self.assertTrue(err)


class BaselineGate(unittest.TestCase):
    def test_new_violation_not_in_baseline_fails(self):
        root = make_repo()
        (root / "prod.py").write_text(TINY_FUNCTION)
        base = commit_all(root, "base")
        write_baseline(root, {})
        commit_all(root, "freeze empty baseline")
        (root / "prod.py").write_text(OVERSIZED_FUNCTION)
        head = commit_all(root, "add an oversized function")
        ok, report = sc.check(str(root), base, head)
        self.assertFalse(ok)
        self.assertIn("busy", report)

    def test_frozen_violation_in_a_changed_file_passes(self):
        # Review I3: the earlier version of this test changed only README.md, so it
        # measured zero .py files and passed regardless of the baseline logic. This one
        # touches prod.py itself (adding an unrelated tiny function beside the frozen
        # one), so `busy`'s frozen complexity AND function_length entries are both
        # genuinely re-measured and must be found in the baseline to pass.
        root = make_repo()
        (root / "prod.py").write_text(OVERSIZED_FUNCTION)
        sha = commit_all(root, "oversized function, not frozen yet")
        write_baseline(root, frozen_entries(root, sha, "prod.py"))
        base = commit_all(root, "base: oversized function, already frozen")
        (root / "prod.py").write_text(OVERSIZED_FUNCTION + "\n\n" + TINY_FUNCTION)
        head = commit_all(root, "touch prod.py: add an unrelated tiny function")
        ok, report = sc.check(str(root), base, head)
        self.assertTrue(ok, report)

    def test_frozen_violation_growing_past_its_stored_value_fails(self):
        root = make_repo()
        (root / "prod.py").write_text(OVERSIZED_FUNCTION)
        sha = commit_all(root, "oversized function, not frozen yet")
        entries = frozen_entries(root, sha, "prod.py")
        entries["prod.py:busy:function_length"] -= 1  # frozen one line short of the real measurement
        write_baseline(root, entries)
        base = commit_all(root, "base: function_length frozen below what the code measures")
        (root / "prod.py").write_text(OVERSIZED_FUNCTION + "\n\n" + TINY_FUNCTION)
        head = commit_all(root, "touch prod.py")
        ok, report = sc.check(str(root), base, head)
        self.assertFalse(ok)
        self.assertIn("new or worse", report)
        self.assertIn("busy:function_length", report)
        self.assertNotIn("busy:complexity", report)  # only the lowered entry should trip

    def _frozen_busy_repo(self):
        root = make_repo()
        (root / "prod.py").write_text(OVERSIZED_FUNCTION)
        sha = commit_all(root, "oversized function, not frozen yet")
        write_baseline(root, frozen_entries(root, sha, "prod.py"))
        return root, commit_all(root, "base: oversized function, frozen")

    def test_gone_violation_in_a_changed_file_is_an_advisory_not_a_failure(self):
        # row 636: deleting a grandfathered function without touching the baseline passes
        root, base = self._frozen_busy_repo()
        (root / "prod.py").write_text(TINY_FUNCTION)  # the violation is gone
        head = commit_all(root, "remove busy()")
        ok, report = sc.check(str(root), base, head)
        self.assertTrue(ok, report)
        self.assertIn("size_check: stale baseline entry, CoS prunes: prod.py:busy:function_length",
                      report)

    def test_a_stale_entry_cannot_be_reused_by_a_later_card(self):
        # row 636 probe: card A shrinks busy() (entry stays, advisory); card B, based on A,
        # grows it back to the old size. Card B is a new violation, not the frozen one.
        root, base = self._frozen_busy_repo()
        (root / "prod.py").write_text(TINY_FUNCTION)
        card_a = commit_all(root, "card A: shrink busy()")
        self.assertTrue(sc.check(str(root), base, card_a)[0])
        (root / "prod.py").write_text(OVERSIZED_FUNCTION)
        card_b = commit_all(root, "card B: busy() grows back")
        ok, report = sc.check(str(root), card_a, card_b)
        self.assertFalse(ok)
        self.assertIn("new or worse", report)
        self.assertIn("prod.py:busy:function_length", report)

    def test_deleting_then_re_adding_a_same_name_function_in_a_later_card_fails(self):
        root = make_repo()
        (root / "prod.py").write_text(OVERSIZED_FUNCTION + "\n\n" + TINY_FUNCTION)
        sha = commit_all(root, "busy() and add(), not frozen yet")
        write_baseline(root, frozen_entries(root, sha, "prod.py"))
        base = commit_all(root, "base: busy() frozen")
        (root / "prod.py").write_text(TINY_FUNCTION)
        card_a = commit_all(root, "card A: delete busy(), add() stays")
        (root / "prod.py").write_text(TINY_FUNCTION + "\n\n" + OVERSIZED_FUNCTION)
        card_b = commit_all(root, "card B: re-add busy()")
        self.assertTrue(sc.check(str(root), base, card_a)[0])
        ok, report = sc.check(str(root), card_a, card_b)
        self.assertFalse(ok)
        self.assertIn("prod.py:busy:function_length", report)

    def test_a_deleted_file_s_entries_appear_in_the_advisory(self):
        root, base = self._frozen_busy_repo()
        (root / "prod.py").unlink()
        head = commit_all(root, "delete prod.py")
        ok, report = sc.check(str(root), base, head)
        self.assertTrue(ok, report)
        self.assertIn("CoS prunes: prod.py:busy:function_length", report)
        self.assertIn("CoS prunes: prod.py:busy:complexity", report)

    def test_a_gone_violation_does_not_excuse_a_baseline_that_grows(self):
        root, base = self._frozen_busy_repo()
        (root / "prod.py").write_text(TINY_FUNCTION)
        entries = json.loads((root / "tests" / "size_baseline.json").read_text())
        write_baseline(root, {**entries, "prod.py:other:function_length": 61})
        head = commit_all(root, "remove busy() and sneak an entry into the baseline")
        ok, report = sc.check(str(root), base, head)
        self.assertFalse(ok)
        self.assertIn("grew", report)

    def test_a_gone_violation_does_not_excuse_a_new_oversized_function(self):
        root, base = self._frozen_busy_repo()
        (root / "prod.py").write_text(OVERSIZED_FUNCTION.replace("busy", "busier"))
        head = commit_all(root, "rename busy() to busier(): a new oversized function")
        ok, report = sc.check(str(root), base, head)
        self.assertFalse(ok)
        self.assertIn("busier", report)

    def test_baseline_that_adds_an_entry_fails(self):
        root = make_repo()
        write_baseline(root, {})
        base = commit_all(root, "empty baseline")
        write_baseline(root, {"prod.py::function_length": 61})
        head = commit_all(root, "sneak an entry into the baseline")
        ok, report = sc.check(str(root), base, head)
        self.assertFalse(ok)
        self.assertIn("grew", report)

    def test_baseline_that_raises_a_stored_value_fails(self):
        root = make_repo()
        write_baseline(root, {"prod.py::function_length": 61})
        base = commit_all(root, "baseline at 61")
        write_baseline(root, {"prod.py::function_length": 500})
        head = commit_all(root, "raise the recorded value instead of fixing the code")
        ok, report = sc.check(str(root), base, head)
        self.assertFalse(ok)
        self.assertIn("grew", report)

    def test_baseline_that_shrinks_passes(self):
        root = make_repo()
        write_baseline(root, {"prod.py::function_length": 61, "prod.py::gone:function_length": 70})
        base = commit_all(root, "two entries")
        write_baseline(root, {"prod.py::function_length": 61})
        head = commit_all(root, "one entry removed")
        ok, report = sc.check(str(root), base, head)
        self.assertTrue(ok, report)

    # test_missing_baseline_at_head_falls_back_to_base (row 509 round 1) retired: R2
    # review found the fallback never fires under the real gate's `--base` (a
    # merge-base predating the file has none either), so round 2's R1MainFallback
    # class below replaces it with the real fallback target - the local `main` branch.

    def test_first_introduction_of_the_baseline_is_never_a_grow(self):
        root = make_repo()
        (root / "prod.py").write_text(OVERSIZED_FUNCTION)
        base = commit_all(root, "no baseline file yet, has debt")
        entries = {sc.key(v): v[4] for v in sc.ast_violations("prod.py", OVERSIZED_FUNCTION)}
        write_baseline(root, entries)
        head = commit_all(root, "introduce the baseline for the first time")
        ok, report = sc.check(str(root), base, head)
        self.assertTrue(ok, report)

    def test_corrupt_baseline_fails_closed(self):
        root = make_repo()
        (root / "tests").mkdir()
        (root / "tests" / "size_baseline.json").write_text("{not valid json")
        base = commit_all(root, "base")
        (root / "README.md").write_text("x\n")
        head = commit_all(root, "unrelated")
        ok, report = sc.check(str(root), base, head)
        self.assertFalse(ok)
        self.assertIn("corrupt", report)

    def test_renamed_frozen_function_is_reported_as_new(self):
        root = make_repo()
        (root / "prod.py").write_text(OVERSIZED_FUNCTION)
        sha = commit_all(root, "oversized function, not frozen yet")
        write_baseline(root, frozen_entries(root, sha, "prod.py"))
        base = commit_all(root, "base: busy(), frozen")
        (root / "prod.py").write_text(OVERSIZED_FUNCTION.replace("busy", "renamed"))
        head = commit_all(root, "rename busy -> renamed, same body")
        ok, report = sc.check(str(root), base, head)
        self.assertFalse(ok)
        self.assertIn("renamed", report)

    def test_line_shift_elsewhere_in_the_file_does_not_unfreeze_it(self):
        # Row 509 fix round 1 (78f9679): a baseline keyed by line number silently drops
        # an entry whenever an unrelated edit earlier in the file shifts line numbers.
        root = make_repo()
        (root / "prod.py").write_text(OVERSIZED_FUNCTION)
        sha = commit_all(root, "oversized function, not frozen yet")
        write_baseline(root, frozen_entries(root, sha, "prod.py"))
        base = commit_all(root, "base: busy(), frozen")
        shifted = "# a comment\n" * 5 + OVERSIZED_FUNCTION
        (root / "prod.py").write_text(shifted)
        head = commit_all(root, "shift busy() down 5 lines with unrelated comments")
        ok, report = sc.check(str(root), base, head)
        self.assertTrue(ok, report)


class IntentExamples(unittest.TestCase):
    """Zenkai Z1: contracts/intent/size_check.jsonl run through the real `check()`,
    not merely linted for shape - matching capsule/tests/test_unblocked.py's
    `IntentExamples` pattern."""

    def test_every_example_matches_its_oracle(self):
        path = Path(__file__).resolve().parent.parent / "contracts" / "intent" / "size_check.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        self.assertTrue(rows)
        for row in rows:
            doc = row["given"]["document"]
            root = make_repo()
            write_baseline(root, {})
            if doc["baseline_grew"]:
                base = commit_all(root, "empty baseline")
                write_baseline(root, {"prod.py::function_length": 61})
                head = commit_all(root, "sneak an entry into the baseline")
            else:
                name = "shared" if doc["name_exists_at_base"] else "fresh"
                (root / "prod.py").write_text(_fn("shared", 45))
                base = commit_all(root, "base: an established, empty baseline")
                (root / "prod.py").write_text(_fn(name, doc["function_lines"]))
                head = commit_all(root, "head: a function of the stated length")
            ok, report = sc.check(str(root), base, head)
            self.assertEqual(ok, row["expect"]["ok"], (row["id"], report))


def _complex_method(name, threshold=20):
    body = "\n".join(f"        if x == {i}:\n            x += {i}" for i in range(threshold))
    return f"    def {name}(self, x):\n{body}\n        return x\n"


class R1MainFallback(unittest.TestCase):
    """Row 509 fix round 2, R1: when neither base nor head has a baseline (e.g. a
    branch forked before the file existed at all), fall back to the LOCAL `main`
    branch's baseline, not an empty one - kills a mutant that swaps the real main
    baseline for `{}` in size_baseline.pair()."""

    def test_falls_back_to_mains_baseline_when_neither_side_has_one(self):
        root = make_repo()
        (root / "prod.py").write_text(TINY_FUNCTION)
        fork = commit_all(root, "no baseline yet, forked here")
        git(root, "checkout", "-q", "-b", "feature", fork)
        (root / "prod.py").write_text(OVERSIZED_FUNCTION)
        (root / "other.py").write_text(TINY_FUNCTION)
        head = commit_all(root, "feature: introduce busy(), forked before main had one")
        git(root, "checkout", "-q", "main")
        write_baseline(root, frozen_entries(root, head, "prod.py"))
        commit_all(root, "main gets a baseline covering busy(), after the fork point")
        ok, report = sc.check(str(root), fork, head)
        self.assertTrue(ok, report)

    def test_fails_when_main_also_has_no_baseline(self):
        root = make_repo()
        (root / "prod.py").write_text(TINY_FUNCTION)
        base = commit_all(root, "no baseline anywhere")
        (root / "other.py").write_text(TINY_FUNCTION)
        head = commit_all(root, "still none")
        ok, report = sc.check(str(root), base, head)
        self.assertFalse(ok)
        self.assertIn("rebase or regenerate", report)


class R2QualifiedComplexityKeys(unittest.TestCase):
    """Row 509 fix round 2, R2: a complexity entry's baseline key is the ast-qualified
    name (mapped from ruff's own reported line), not ruff's bare `` `name` `` - two
    classes can each have their own same-named method."""

    def test_a_same_named_method_in_a_different_class_is_not_frozen_by_the_first(self):
        root = make_repo()
        (root / "prod.py").write_text("class A:\n" + _complex_method("validate"))
        sha = commit_all(root, "A.validate, not frozen yet")
        write_baseline(root, frozen_entries(root, sha, "prod.py"))
        base = commit_all(root, "base: A.validate frozen")
        (root / "prod.py").write_text(
            "class A:\n" + _complex_method("validate") + "\n\nclass K:\n" + _complex_method("validate"))
        head = commit_all(root, "add K.validate: same bare name, different class")
        ok, report = sc.check(str(root), base, head)
        self.assertFalse(ok)
        self.assertIn("K.validate", report)


class DuplicateKeyFailsClosed(unittest.TestCase):
    def test_two_functions_with_the_same_qualified_name_fail_closed(self):
        root = make_repo()
        (root / "prod.py").write_text(OVERSIZED_FUNCTION + "\n\n" + OVERSIZED_FUNCTION)  # busy() defined twice
        sha = commit_all(root, "busy() defined twice")
        ok, violations, err = sc.measure(str(root), sha, ["prod.py"])
        self.assertFalse(ok)
        self.assertIn("duplicate", err)


class R3BaselineDeleted(unittest.TestCase):
    def test_deleting_the_baseline_fails(self):
        root = make_repo()
        write_baseline(root, {"prod.py:foo:function_length": 61})
        base = commit_all(root, "base has a baseline")
        (root / "tests" / "size_baseline.json").unlink()
        head = commit_all(root, "head deletes it")
        ok, report = sc.check(str(root), base, head)
        self.assertFalse(ok)
        self.assertIn("deleted", report)


class RuffFlagsMatter(unittest.TestCase):
    """M15/M16: the two ruff flags that keep a stray project/noqa from silencing C901
    actually do something - each test fails if the corresponding flag is dropped."""

    def test_ignore_noqa_still_flags_a_noqad_function(self):
        root = make_repo()
        body = "\n".join(f"    if x == {i}:\n        x += {i}" for i in range(20))
        (root / "prod.py").write_text(f"def busy(x):  # noqa: C901\n{body}\n    return x\n")
        sha = commit_all(root, "busy(), noqa'd")
        ok, violations, err = sc.ruff_violations(str(root), sha, ["prod.py"])
        self.assertTrue(ok, err)
        self.assertTrue(any(v[2] == "complexity" for v in violations))

    def test_isolated_still_flags_despite_a_per_file_ignore_in_pyproject_toml(self):
        root = make_repo()
        body = "\n".join(f"    if x == {i}:\n        x += {i}" for i in range(20))
        (root / "prod.py").write_text(f"def busy(x):\n{body}\n    return x\n")
        (root / "pyproject.toml").write_text('[tool.ruff.lint.per-file-ignores]\n"prod.py" = ["C901"]\n')
        sha = commit_all(root, "busy(), C901 ignored for this file in pyproject.toml")
        ok, violations, err = sc.ruff_violations(str(root), sha, ["prod.py"])
        self.assertTrue(ok, err)
        self.assertTrue(any(v[2] == "complexity" for v in violations))


def _fn(name, n_lines):
    return f"def {name}(x):\n" + "    x += 1\n" * (n_lines - 2) + "    return x\n"


def _complex_fn(name, branches):
    body = "\n".join(f"    if x == {i}:\n        x += {i}" for i in range(branches))
    return f"def {name}(x):\n{body}\n    return x\n"


class NewCodeLimits(unittest.TestCase):
    """Z8 rule 4: new code gets 40 / 400 / 10, code that existed at base keeps 60 / 800 / 15."""

    def _range(self, base_src, head_src, path="prod.py"):
        root = make_repo()
        write_baseline(root, {})
        if base_src is not None:
            (root / path).write_text(base_src)
        base = commit_all(root, "base")
        (root / path).write_text(head_src)
        head = commit_all(root, "head")
        return sc.check(str(root), base, head)

    def test_new_41_line_function_fails(self):
        ok, report = self._range("x = 0\n", "x = 0\n" + _fn("fresh", 41))
        self.assertFalse(ok, report)
        self.assertIn("prod.py:fresh:function_length=41", report)

    def test_new_40_line_function_passes(self):
        ok, report = self._range("x = 0\n", "x = 0\n" + _fn("fresh", 40))
        self.assertTrue(ok, report)

    def test_existing_function_growing_to_50_keeps_the_60_ceiling(self):
        ok, report = self._range(_fn("old", 45), _fn("old", 50))
        self.assertTrue(ok, report)

    def test_existing_function_over_60_still_fails(self):
        ok, report = self._range(_fn("old", 45), _fn("old", 61))
        self.assertFalse(ok, report)
        self.assertIn("prod.py:old:function_length=61", report)

    def test_renamed_function_gets_new_code_limit(self):
        ok, report = self._range(_fn("old", 50), _fn("renamed", 50))
        self.assertFalse(ok, report)
        self.assertIn("prod.py:renamed:function_length=50", report)

    def test_new_method_in_an_existing_class_gets_new_code_limit(self):
        def cls(extra):
            return "class C:\n    def keep(self):\n        return 1\n" + extra
        method = "    def fresh(self):\n" + "        x = 1\n" * 44 + "        return x\n"
        ok, report = self._range(cls(""), cls(method))
        self.assertFalse(ok, report)
        self.assertIn("prod.py:C.fresh:function_length=46", report)

    def test_new_file_over_400_lines_fails(self):
        ok, report = self._range(None, "x = 1\n" * 401, path="big.py")
        self.assertFalse(ok, report)
        self.assertIn("big.py::file_length=401", report)

    def test_function_in_a_new_file_gets_new_code_limits(self):
        ok, report = self._range(None, _fn("fresh", 45), path="mod.py")
        self.assertFalse(ok, report)
        self.assertIn("mod.py:fresh:function_length=45", report)
        ok, report = self._range(None, _complex_fn("f", 10), path="mod.py")
        self.assertFalse(ok, report)
        self.assertIn("mod.py:f:complexity=11", report)

    def test_new_file_of_400_lines_passes(self):
        ok, report = self._range(None, "x = 1\n" * 400, path="big.py")
        self.assertTrue(ok, report)

    def test_existing_file_growing_to_500_lines_keeps_the_800_ceiling(self):
        ok, report = self._range("x = 1\n" * 450, "x = 1\n" * 500, path="big.py")
        self.assertTrue(ok, report)

    def test_new_function_over_complexity_10_fails(self):
        ok, report = self._range("x = 0\n", "x = 0\n" + _complex_fn("f", 10))
        self.assertFalse(ok, report)
        self.assertIn("prod.py:f:complexity=11", report)

    def test_new_function_at_complexity_10_passes(self):
        ok, report = self._range("x = 0\n", "x = 0\n" + _complex_fn("f", 9))
        self.assertTrue(ok, report)

    def test_existing_function_keeps_the_complexity_15_ceiling(self):
        ok, report = self._range(_complex_fn("f", 11), _complex_fn("f", 12))
        self.assertTrue(ok, report)
        ok, report = self._range(_complex_fn("f", 11), _complex_fn("f", 15))
        self.assertFalse(ok, report)

    def test_existing_function_at_exactly_complexity_15_passes(self):
        ok, report = self._range(_complex_fn("f", 11), _complex_fn("f", 14))
        self.assertTrue(ok, report)

    def test_file_unparseable_at_base_judges_every_name_as_new(self):
        broken = "def old(x:\n" + "    x += 1\n" * 45
        ok, report = self._range(broken, _fn("old", 45))
        self.assertFalse(ok, report)
        self.assertIn("prod.py:old:function_length=45", report)

    def test_baseline_generation_never_uses_the_new_limits(self):
        root = make_repo()
        (root / "prod.py").write_text(_fn("mid", 50) + _complex_fn("c", 11))
        (root / "big.py").write_text("x = 1\n" * 500)
        sha = commit_all(root, "x")
        ok, violations, err = sc.measure(str(root), sha, ["prod.py", "big.py"])
        self.assertTrue(ok, err)
        self.assertEqual(violations, [])


class SharedPlumbing(unittest.TestCase):
    def test_added_line_numbers_are_head_side(self):
        root = make_repo()
        (root / "m.py").write_text("a = 1\nb = 2\n")
        base = commit_all(root, "base")
        (root / "m.py").write_text("a = 1\nx = 9\nb = 2\ny = 8\n")
        head = commit_all(root, "head")
        self.assertEqual(bl.added_line_numbers(str(root), base, head, "m.py"), {2, 4})

    def test_is_test_file_is_scoped_to_the_suites(self):
        self.assertTrue(bl.is_test_file("tests/test_x.py"))
        self.assertFalse(bl.is_test_file("tools/test_x.py"))

    def _path_with(self, *tools):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        for t in tools:
            (d / t).write_text("#!/bin/sh\n")
            (d / t).chmod(0o755)
        return str(d)

    def test_ruff_cmd_prefers_ruff_then_pinned_uvx_then_none(self):
        pinned = ["uvx", "--offline", f"ruff@{sc.RUFF_VERSION}"]
        for tools, want in ((("ruff", "uvx"), ["ruff"]), (("uvx",), pinned), ((), None)):
            with unittest.mock.patch.dict(os.environ, {"PATH": self._path_with(*tools)}):
                self.assertEqual(sc.ruff_cmd(), want)


if __name__ == "__main__":
    unittest.main()
