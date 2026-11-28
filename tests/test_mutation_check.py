# mutation-check: targets-predate-range b957739c9 -- fixture rename only, no behaviour pinned ::
#   TestSupportFilesAreNeverProduction.test_is_test_support_file_classification
"""Zenkai Z1/Z2 (docs/ZENKAI.md) for tests/mutation_check.py: its own intent
examples and second oracle. A discriminating test file must PASS this gate; a
tautological one must FAIL it. Everything runs against a tiny fixture repo
built fresh, per test, in tempfile - never against capsule-corp itself.

Fixtures live under `tests/` in the fixture repo (fix round 1: `is_test_file`
is now scoped to this repo's real test directories - `capsule/tests/`,
`maintenance/tests/`, `tests/` - not basename alone), while `production.py`
stays at the fixture repo's root, exactly like this repo's own layout."""
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutation_check as mc  # noqa: E402


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


def write_test(root, text):
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests" / "test_production.py").write_text(text)


def write_empty_baseline(root):
    """Row 509 round 2, R1: size_check.check() now hard-fails a repo with no baseline
    anywhere reachable (base, head or main) - give these CLI-subprocess fixtures a
    trivial one so they still test mutation_check's OWN behaviour, not size_check's."""
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests" / "size_baseline.json").write_text(json.dumps({}))


PROD_BUGGY = "def add(a, b):\n    return a - b\n"
PROD_FIXED = "def add(a, b):\n    return a + b\n"

PLACEHOLDER_TEST = (
    "import unittest\nfrom production import add\n\n\n"
    "class Add(unittest.TestCase):\n"
    "    def test_placeholder(self):\n"
    "        self.assertTrue(callable(add))\n"
)

DISCRIMINATING_TEST = (
    "import unittest\n"
    "from production import add\n\n\n"
    "class Add(unittest.TestCase):\n"
    "    def test_add_returns_the_sum(self):\n"
    "        self.assertEqual(add(2, 3), 5)\n"
)

TAUTOLOGICAL_TEST = (
    "import unittest\n"
    "from production import add\n\n\n"
    "class Add(unittest.TestCase):\n"
    "    def test_add_exists(self):\n"
    "        add(2, 3)\n"
    "        self.assertTrue(True)\n"
)

IMPORT_ERROR_TEST = (
    "import unittest\n"
    "import nonexistent_module_xyz\n"
    "from production import add\n\n\n"
    "class Add(unittest.TestCase):\n"
    "    def test_add_returns_the_sum(self):\n"
    "        self.assertEqual(add(2, 3), 5)\n"
)


class DiscriminatingTestPassesTheGate(unittest.TestCase):
    def test_a_real_bug_fix_with_a_real_test_passes(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_BUGGY)
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base: buggy add()")

            (root / "production.py").write_text(PROD_FIXED)
            write_test(root, DISCRIMINATING_TEST)
            head = commit_all(root, "fix add(): a - b -> a + b, cover it")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertTrue(ok, rows)
            self.assertEqual(len(rows), 1)
            f, names, rev_cell, weak_cell, flags_cell = rows[0]
            self.assertEqual(f, "tests/test_production.py")
            self.assertIn("FAIL (good)", rev_cell)
            self.assertIn("FAIL (good)", weak_cell)
            self.assertEqual(flags_cell, "none")

    def test_cli_exit_code_zero_and_table_on_stdout(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            write_empty_baseline(root)
            (root / "production.py").write_text(PROD_BUGGY)
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base")
            (root / "production.py").write_text(PROD_FIXED)
            write_test(root, DISCRIMINATING_TEST)
            head = commit_all(root, "fix + real test")

            p = subprocess.run([sys.executable, str(Path(__file__).resolve().parent / "mutation_check.py"),
                                 "--base", base, "--head", head, "--repo", str(root)],
                                capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertIn("| test file |", p.stdout)


class TautologicalTestFailsTheGate(unittest.TestCase):
    def test_a_no_op_assertTrue_true_test_is_caught(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_BUGGY)
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base: buggy add()")

            (root / "production.py").write_text(PROD_FIXED)
            write_test(root, TAUTOLOGICAL_TEST)
            head = commit_all(root, "fix add(), but the new test asserts nothing real")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertFalse(ok, rows)
            f, names, rev_cell, weak_cell, flags_cell = rows[0]
            self.assertIn("did not fail (BAD)", rev_cell)
            self.assertIn("tautology", flags_cell)

    def test_cli_exit_code_nonzero(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_BUGGY)
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base")
            (root / "production.py").write_text(PROD_FIXED)
            write_test(root, TAUTOLOGICAL_TEST)
            head = commit_all(root, "fix + tautological test")

            p = subprocess.run([sys.executable, str(Path(__file__).resolve().parent / "mutation_check.py"),
                                 "--base", base, "--head", head, "--repo", str(root)],
                                capture_output=True, text=True)
            self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
            self.assertIn("did not fail (BAD)", p.stdout)


class ImportErrorIsNotDiscriminating(unittest.TestCase):
    """Fix round 1: a test file that cannot even be imported must not read as
    "FAIL (good)" just because `unittest discover` exits non-zero - that exit
    code is identical for a genuine assertion failure and a broken import, and
    only the latter is not discrimination at all."""

    def test_an_import_error_reads_as_error_not_fail_good(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_BUGGY)
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base: buggy add()")

            (root / "production.py").write_text(PROD_FIXED)
            write_test(root, IMPORT_ERROR_TEST)
            head = commit_all(root, "fix add(), but the new test module cannot import")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertFalse(ok, rows)
            f, names, rev_cell, weak_cell, flags_cell = rows[0]
            self.assertIn("ERROR", rev_cell)
            self.assertNotIn("FAIL (good)", rev_cell)
            self.assertIn("ERROR", weak_cell)
            self.assertNotIn("FAIL (good)", weak_cell)

    def test_cli_exit_code_nonzero_and_table_says_error(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_BUGGY)
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base")
            (root / "production.py").write_text(PROD_FIXED)
            write_test(root, IMPORT_ERROR_TEST)
            head = commit_all(root, "fix + a test module that cannot import")

            p = subprocess.run([sys.executable, str(Path(__file__).resolve().parent / "mutation_check.py"),
                                 "--base", base, "--head", head, "--repo", str(root)],
                                capture_output=True, text=True)
            self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
            self.assertIn("ERROR (not discriminating)", p.stdout)


class NewModuleImportErrorIsNotAFailure(unittest.TestCase):
    """Fix round 2 (row 459 audit, capsule/intent-examples f3031c3..0e184f7):
    when the range ADDS a production module, reverting it to base means
    deleting it - the test that imports it ERRORs no matter how well it
    discriminates. That specific shape reads `n/a (new module)`, not
    `ERROR (not discriminating)`, and the requirement moves to the
    assertion-weakening column instead."""

    def _write_newmod_test(self, root, text):
        (root / "tests").mkdir(exist_ok=True)
        (root / "tests" / "test_newmod.py").write_text(text)

    def test_a_new_module_plus_its_test_gets_a_stub_revert_verdict_not_error(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_FIXED)
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base: production.py only, no newmod yet")

            (root / "newmod.py").write_text("def double(x):\n    return x * 2\n")
            self._write_newmod_test(
                root,
                "import unittest\nfrom newmod import double\n\n\n"
                "class Newmod(unittest.TestCase):\n"
                "    def test_double(self):\n"
                "        self.assertEqual(double(3), 6)\n")
            head = commit_all(root, "add newmod.py and cover it")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertTrue(ok, rows)
            row = next(r for r in rows if r[0] == "tests/test_newmod.py")
            f, names, rev_cell, weak_cell, flags_cell = row
            self.assertEqual(rev_cell, "FAIL (good) [revert: new module stubbed: newmod.py]")
            self.assertIn("FAIL (good)", weak_cell)

    def test_a_new_module_with_no_assert_still_fails_the_gate(self):
        """The n/a on revert must not become a free pass - a test covering a
        brand-new module with no assert at all (nothing for the weaken column
        to even flip) still fails the gate, via the static flag."""
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_FIXED)
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base: production.py only, no newmod yet")

            (root / "newmod.py").write_text("def double(x):\n    return x * 2\n")
            self._write_newmod_test(
                root,
                "import unittest\nfrom newmod import double\n\n\n"
                "class Newmod(unittest.TestCase):\n"
                "    def test_double(self):\n"
                "        double(3)\n")
            head = commit_all(root, "add newmod.py, but the test asserts nothing")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertFalse(ok, rows)
            row = next(r for r in rows if r[0] == "tests/test_newmod.py")
            f, names, rev_cell, weak_cell, flags_cell = row
            self.assertEqual(rev_cell, "did not fail (BAD)")  # t_bd08469e: stub survived
            self.assertIn("no assert", flags_cell)


class NoTestFilesChanged(unittest.TestCase):
    def test_range_with_no_test_changes_is_a_noop_pass(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_BUGGY)
            base = commit_all(root, "base")
            (root / "readme.md").write_text("docs only\n")
            head = commit_all(root, "docs only, no test touched")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertTrue(ok)
            self.assertEqual(rows, [])
            self.assertIn("no new/changed test files", note)


class NonSuiteTestNamedFileIsIgnored(unittest.TestCase):
    """Fix round 1: `is_test_file` is scoped to `capsule/tests/`,
    `maintenance/tests/`, `tests/` - a `test_*.py` OUTSIDE those directories
    (e.g. a scratch file at the repo root) is not part of the collected
    suite and must not be required to discriminate."""

    def test_a_test_named_file_outside_a_test_dir_is_not_treated_as_a_test(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_BUGGY)
            base = commit_all(root, "base")
            (root / "production.py").write_text(PROD_FIXED)
            # a stray test_*.py at repo root, not under any real tests/ dir
            (root / "test_scratch.py").write_text("x = 1\n")
            head = commit_all(root, "fix add(), plus an unrelated scratch file")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertTrue(ok)
            self.assertEqual(rows, [])
            self.assertIn("no new/changed test files", note)


class TestSupportFilesAreNeverProduction(unittest.TestCase):
    """Fix round 3 (sibling-project audit, fix/htc-applied-verify): a `.py` file under
    a test directory that is not itself `test_*.py` - `conftest.py`, a
    fakes/fixtures helper, `tests/**/__init__.py` - is test-support, never
    production: never reverted, never mutated, never counted toward "at least
    one production file changed"."""

    def test_is_test_support_file_classification(self):
        support = ["capsule/tests/legacy_oracle.py", "maintenance/tests/fakes.py",
                   "tests/conftest.py", "capsule/tests/__init__.py", "tests/fixtures/quality/a.py"]
        production = ["capsule/amend.py", "maintenance/quality_gate.py", "production.py", "capsule/__init__.py"]
        for p in support:
            self.assertTrue(mc.is_test_support_file(p), p)
            self.assertFalse(mc.is_test_file(p), p)
        for p in production:
            self.assertFalse(mc.is_test_support_file(p), p)

    def test_a_helper_changed_alongside_a_real_fix_is_never_reverted(self):
        """A support helper (`tests/fakes.py`) co-changes with the real
        production fix. Spy on `revert_to_base` and assert it is called for
        the production file but never for the helper - proof the helper's
        HEAD content survives the revert-mutation run untouched."""
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_BUGGY)
            (root / "tests").mkdir()
            (root / "tests" / "fakes.py").write_text("OLD = True\n")
            write_test(root,
                       "import unittest\nfrom production import add\n\n\n"
                       "class Add(unittest.TestCase):\n"
                       "    def test_placeholder(self):\n"
                       "        self.assertTrue(callable(add))\n")
            base = commit_all(root, "base: buggy add(), old helper")

            (root / "production.py").write_text(PROD_FIXED)
            (root / "tests" / "fakes.py").write_text("OLD = True\nNEW = True\n")
            write_test(root, DISCRIMINATING_TEST)
            head = commit_all(root, "fix add(), plus a co-changed test helper")

            calls = []
            real_revert = mc.revert_to_base
            def spy(repo_, base_, scratch, path, status):
                calls.append(path)
                return real_revert(repo_, base_, scratch, path, status)
            with unittest.mock.patch.object(mc, "revert_to_base", side_effect=spy):
                ok, rows, note = mc.check_range(root, base, head)

            self.assertIsNone(note)
            self.assertTrue(ok, rows)
            self.assertIn("production.py", calls)
            self.assertNotIn("tests/fakes.py", calls)


class SharedProductionModulePlusNewModule(unittest.TestCase):
    """Fix round 3, second half: `errors.py`/`types.py`-style shared
    production modules that already existed at base are treated as ordinary
    production (reverted normally) - but when a test also imports a module
    that's NEW in the range, the new-module rule still applies to THAT half
    of the collection error."""

    def test_existing_shared_module_plus_a_new_module_keeps_the_new_module_at_head(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "errors.py").write_text("class BadInput(Exception):\n    pass\n")
            (root / "production.py").write_text(PROD_BUGGY)
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base: errors.py + buggy production.py, no newmod yet")

            # errors.py is genuinely modified (still exists at base - ordinary
            # production), production.py is fixed, and a brand-new module is
            # added; the test imports all three.
            (root / "errors.py").write_text("class BadInput(Exception):\n    pass\n\n\nclass Extra(Exception):\n    pass\n")
            (root / "production.py").write_text(PROD_FIXED)
            (root / "newmod.py").write_text("def double(x):\n    return x * 2\n")
            (root / "tests" / "test_production.py").write_text(
                "import unittest\n"
                "from errors import BadInput\n"
                "from production import add\n"
                "from newmod import double\n\n\n"
                "class Add(unittest.TestCase):\n"
                "    def test_add_and_double(self):\n"
                "        self.assertEqual(add(2, 3), 5)\n"
                "        self.assertEqual(double(3), 6)\n"
                "        self.assertTrue(issubclass(BadInput, Exception))\n")
            head = commit_all(root, "extend errors.py, fix add(), add newmod.py, cover all three")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertTrue(ok, rows)
            f, names, rev_cell, weak_cell, flags_cell = rows[0]
            # errors.py and production.py revert cleanly to base (both existed at base);
            # newmod.py has no base, so it is kept at head and the rerun fails on the
            # reverted add(): FAIL (good), labelled, and the weaken column agrees.
            self.assertEqual(rev_cell, "FAIL (good) [revert: new module kept at head: newmod.py]")
            self.assertIn("FAIL (good)", weak_cell)


class NoProductionChangeForThisTestFile(unittest.TestCase):
    """Fix round 4 (window-5 gate run, gate-w5.txt): a changed test file whose
    SUBJECT module(s) have no production change in this range - e.g. new
    tests added for pre-existing, untouched code, while some UNRELATED
    production file also changed - must not read "did not fail (BAD)" just
    because *some* production file changed in the range. Detected via the
    test file's own imports vs. the set of changed production files."""

    def test_new_tests_for_unchanged_code_read_n_a_no_production_change(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_FIXED)  # never changes
            (root / "unrelated.py").write_text("def sub(a, b):\n    return a - a\n")  # buggy, unrelated
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base: production.py (already correct) + buggy unrelated.py")

            # unrelated.py is fixed (a real production change IS in this
            # range), but the new test only covers the untouched production.py.
            (root / "unrelated.py").write_text("def sub(a, b):\n    return a - b\n")
            write_test(root, DISCRIMINATING_TEST)
            head = commit_all(root, "fix unrelated.py; add new tests for production.py, which did not change")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertTrue(ok, rows)
            f, names, rev_cell, weak_cell, flags_cell = rows[0]
            self.assertEqual(rev_cell, "n/a (no production change)")
            self.assertIn("FAIL (good)", weak_cell)

    def test_cli_exit_code_zero(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            write_empty_baseline(root)
            (root / "production.py").write_text(PROD_FIXED)
            (root / "unrelated.py").write_text("def sub(a, b):\n    return a - a\n")
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base")
            (root / "unrelated.py").write_text("def sub(a, b):\n    return a - b\n")
            write_test(root, DISCRIMINATING_TEST)
            head = commit_all(root, "fix unrelated.py; new tests for untouched production.py")

            p = subprocess.run([sys.executable, str(Path(__file__).resolve().parent / "mutation_check.py"),
                                 "--base", base, "--head", head, "--repo", str(root)],
                                capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertIn("n/a (no production change)", p.stdout)


INTENT_MODULE = (
    "import json\nfrom pathlib import Path\n\n\n"
    "def ids(role):\n"
    "    p = Path(__file__).parent / 'contracts' / 'intent' / f'{role}.jsonl'\n"
    "    return [json.loads(l)['id'] for l in p.read_text().splitlines() if l.strip()]\n"
)

INTENT_DATA_TEST = (
    "import unittest\n"
    "from intent import ids\n\n\n"
    "class Guide(unittest.TestCase):\n"
    "    def test_guide_has_the_new_example(self):\n"
    "        self.assertIn('new-example', ids('guide'))\n"
)


def write_intent_fixture(root, guide_jsonl_lines):
    (root / "intent.py").write_text(INTENT_MODULE)
    (root / "contracts" / "intent").mkdir(parents=True, exist_ok=True)
    (root / "contracts" / "intent" / "guide.jsonl").write_text(
        "".join(l + "\n" for l in guide_jsonl_lines))


class IntentDataFileRevertedToo(unittest.TestCase):
    """I1 (review round, guide-bundle card): `contracts/intent/<role>.jsonl` (Zenkai row 454's
    intent examples) is read as data, never `import`ed - a test whose only real dependency is a
    NEW line in one of these files had no way to be required to discriminate before this fix: the
    file is not `.py`, so it never entered `prod_files`, and the "no production change" guard
    (import-name matching) cannot see a data-file dependency either."""

    def _build(self, root):
        init_repo(root)
        write_intent_fixture(root, ["{\"id\": \"old-example\"}"])
        (root / "production.py").write_text(PROD_FIXED)
        write_test(root, PLACEHOLDER_TEST)
        base = commit_all(root, "base: one intent example")

        write_intent_fixture(root, ["{\"id\": \"old-example\"}", "{\"id\": \"new-example\"}"])
        (root / "tests" / "test_production.py").write_text(INTENT_DATA_TEST)
        head = commit_all(root, "add a new guide intent example, with a test for it")
        return base, head

    def test_a_new_intent_example_with_a_discriminating_test_passes_the_gate(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            base, head = self._build(root)

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertTrue(ok, rows)
            f, names, rev_cell, weak_cell, flags_cell = rows[0]
            self.assertEqual(rev_cell, "FAIL (good)")

    def test_without_the_fix_the_same_range_is_silently_waved_through(self):
        """Guard: with the data-file revert disabled (`is_intent_data_file` patched to see
        nothing - simulating the gate before this fix), the exact same range - a new intent
        example covered only by a test that reads the jsonl, never imports a `.py` module - is
        the gap I1 caught: no `.py` production file changed either, so the gate never even enters
        the revert branch and reads a bare "n/a", `ok` stays True (never counted against the
        range) - the test is silently non-discriminating, not visibly flagged."""
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            base, head = self._build(root)

            with unittest.mock.patch.object(mc, "is_intent_data_file", lambda rel_path: False):
                ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertTrue(ok, rows)   # the silent gap: nothing here fails the gate
            f, names, rev_cell, weak_cell, flags_cell = rows[0]
            self.assertEqual(rev_cell, "n/a")


class NewModuleUnderATestDirectoryNotItselfATest(unittest.TestCase):
    """Fix round 4, second half (window-5 gate run): a NEW production file
    that lives under a test directory but is not a test module itself (e.g.
    `tests/mutation_check.py`, a tool under `tests/`) is still a new module
    for the new-module rule - including when a test imports it via a
    `sys.path` hack and a bare-stem name, exactly as
    `tests/test_mutation_check.py` imports `tests/mutation_check.py` as plain
    `mutation_check`."""

    def test_a_new_tool_under_tests_imported_by_bare_stem_gets_a_stub_revert_verdict(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_FIXED)
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base: no toolkit.py yet")

            # A new NON-test .py file under tests/ (a tool, not test_*.py -
            # exactly tests/mutation_check.py's own shape in this repo).
            (root / "tests" / "toolkit.py").write_text("def triple(x):\n    return x * 3\n")
            write_test(root,
                       "import sys\nimport unittest\nfrom pathlib import Path\n\n"
                       "sys.path.insert(0, str(Path(__file__).resolve().parent))\n"
                       "import toolkit\n\n\n"
                       "class Toolkit(unittest.TestCase):\n"
                       "    def test_triple(self):\n"
                       "        self.assertEqual(toolkit.triple(3), 9)\n")
            head = commit_all(root, "add tests/toolkit.py (a tool, not a test) and cover it by bare-stem import")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertTrue(ok, rows)
            f, names, rev_cell, weak_cell, flags_cell = rows[0]
            self.assertEqual(rev_cell, "FAIL (good) [revert: new module stubbed: tests/toolkit.py]")
            self.assertIn("FAIL (good)", weak_cell)


class TargetsPredateRangeMarker(unittest.TestCase):
    """Fix round 5 (per-test as of round 13): an explicit, auditable `#
    mutation-check: targets-predate-range <base-sha> -- <reason> ::
    Class.test_a, Class.test_b` top-of-file marker. A matching sha and a
    valid `:: <ids>` list reads n/a (with the reason shown) for exactly the
    ids it names; any other use (wrong sha, no ids at all, or an id that
    isn't actually new/changed in this range) is reported, never silently
    ignored."""

    def test_a_marker_matching_base_reads_n_a_with_the_reason_and_requires_weaken(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_FIXED)  # pre-existing, correct, never changes
            (root / "unrelated.py").write_text("def sub(a, b):\n    return a - a\n")
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base: production.py already correct, buggy unrelated.py")

            (root / "unrelated.py").write_text("def sub(a, b):\n    return a - b\n")
            marked_test = (
                f"# mutation-check: targets-predate-range {base} -- "
                "new tests for pre-existing production.py, killed 3 hand mutants "
                ":: Add.test_add_returns_the_sum\n"
                + DISCRIMINATING_TEST)
            write_test(root, marked_test)
            head = commit_all(root, "fix unrelated.py; add marked tests for untouched production.py")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertTrue(ok, rows)
            f, names, rev_cell, weak_cell, flags_cell = rows[0]
            self.assertEqual(rev_cell, "n/a (targets predate range: new tests for pre-existing "
                                        "production.py, killed 3 hand mutants)")
            self.assertIn("FAIL (good)", weak_cell)

    def test_a_marker_with_the_wrong_sha_is_reported_not_silently_ignored(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_FIXED)
            (root / "unrelated.py").write_text("def sub(a, b):\n    return a - a\n")
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base")

            (root / "unrelated.py").write_text("def sub(a, b):\n    return a - b\n")
            # A marker naming some OTHER sha (not this range's base) -
            # 40 hex chars so it parses as a sha-shaped string, but wrong.
            wrong_sha = "0" * 40
            marked_test = (
                f"# mutation-check: targets-predate-range {wrong_sha} -- stale marker\n"
                + DISCRIMINATING_TEST)
            write_test(root, marked_test)
            head = commit_all(root, "fix unrelated.py; test file carries a marker for the wrong base")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertFalse(ok, rows)
            f, names, rev_cell, weak_cell, flags_cell = rows[0]
            self.assertIn("marker mismatch (BAD)", rev_cell)

    def test_cli_exit_code_zero_with_a_matching_marker(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            write_empty_baseline(root)
            (root / "production.py").write_text(PROD_FIXED)
            (root / "unrelated.py").write_text("def sub(a, b):\n    return a - a\n")
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base")
            (root / "unrelated.py").write_text("def sub(a, b):\n    return a - b\n")
            marked_test = (f"# mutation-check: targets-predate-range {base[:7]} -- old coverage "
                            ":: Add.test_add_returns_the_sum\n"
                            + DISCRIMINATING_TEST)
            write_test(root, marked_test)
            head = commit_all(root, "fix unrelated.py; add marked tests")
            p = subprocess.run([sys.executable, str(Path(__file__).resolve().parent / "mutation_check.py"),
                                 "--base", base, "--head", head, "--repo", str(root)],
                                capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertIn("targets predate range: old coverage", p.stdout)
            self.assertIn("simplicity_check: ok", p.stdout)  # Z8: the gate runs it too

    def test_cli_exit_code_nonzero_with_a_mismatched_marker(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_FIXED)
            (root / "unrelated.py").write_text("def sub(a, b):\n    return a - a\n")
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base")
            (root / "unrelated.py").write_text("def sub(a, b):\n    return a - b\n")
            wrong_sha = "1" * 40
            marked_test = (f"# mutation-check: targets-predate-range {wrong_sha} -- stale marker\n"
                            + DISCRIMINATING_TEST)
            write_test(root, marked_test)
            head = commit_all(root, "fix unrelated.py; stale marker")

            p = subprocess.run([sys.executable, str(Path(__file__).resolve().parent / "mutation_check.py"),
                                 "--base", base, "--head", head, "--repo", str(root)],
                                capture_output=True, text=True)
            self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
            self.assertIn("marker mismatch (BAD)", p.stdout)

    def test_extract_marker_only_scans_the_top_of_the_file(self):
        buried = "\n".join([f"# line {i}" for i in range(30)]) + \
            "\n# mutation-check: targets-predate-range abc1234 -- too late, buried\n"
        self.assertIsNone(mc.extract_marker(buried))
        top = "# mutation-check: targets-predate-range abc1234 -- right at the top\nimport os\n"
        self.assertEqual(mc.extract_marker(top), ("abc1234", "right at the top", None))

    def test_extract_marker_parses_the_per_test_ids_list(self):
        top = ("# mutation-check: targets-predate-range abc1234 -- pre-existing coverage "
               ":: Add.test_a, Add.test_b\nimport os\n")
        self.assertEqual(
            mc.extract_marker(top),
            ("abc1234", "pre-existing coverage", ("Add.test_a", "Add.test_b")))


class MarkerIsPerTestAsOfRound13(unittest.TestCase):
    """Round 13: a marker used to switch off revert-mutation for EVERY new/
    changed test in the file - even a brand-new test whose subject genuinely
    changed in the range. It must now name the exact ids it covers; every
    OTHER new/changed test in the same file still gets revert-checked, and a
    marker with no ids at all (the old file-level shape) or one naming an id
    that isn't actually new/changed is `marker_stale` - a gate failure."""

    def test_a_marker_covering_only_some_new_tests_still_revert_checks_the_rest(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_BUGGY)
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base: buggy add()")

            (root / "production.py").write_text(PROD_FIXED)
            mixed_test = (
                f"# mutation-check: targets-predate-range {base} -- pre-existing "
                "coverage for Marked :: Marked.test_marked\n"
                "import unittest\n"
                "from production import add\n\n\n"
                "class Marked(unittest.TestCase):\n"
                "    def test_marked(self):\n"
                "        self.assertTrue(callable(add))\n\n\n"
                "class New(unittest.TestCase):\n"
                "    def test_add_returns_the_sum(self):\n"
                "        self.assertEqual(add(2, 3), 5)\n"
            )
            write_test(root, mixed_test)
            head = commit_all(root, "fix add(); marker covers Marked.test_marked only, "
                                     "New.test_add_returns_the_sum is genuinely new")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertTrue(ok, rows)
            self.assertEqual(len(rows), 1)
            f, names, rev_cell, weak_cell, flags_cell = rows[0]
            self.assertIn("Marked.test_marked", names)
            self.assertIn("New.test_add_returns_the_sum", names)
            # The covered id reads n/a via the marker; the uncovered new test
            # in the SAME file is still genuinely revert-checked.
            self.assertIn(
                "Marked.test_marked: n/a (targets predate range: "
                "pre-existing coverage for Marked)", rev_cell)
            self.assertIn("New.test_add_returns_the_sum: FAIL (good)", rev_cell)

    def test_a_marker_with_no_ids_at_all_is_stale(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_FIXED)
            (root / "unrelated.py").write_text("def sub(a, b):\n    return a - a\n")
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base")

            (root / "unrelated.py").write_text("def sub(a, b):\n    return a - b\n")
            marked_test = (f"# mutation-check: targets-predate-range {base} -- no ids at all\n"
                            + DISCRIMINATING_TEST)
            write_test(root, marked_test)
            head = commit_all(root, "fix unrelated.py; file-level marker with no ids is stale")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertFalse(ok, rows)
            f, names, rev_cell, weak_cell, flags_cell = rows[0]
            self.assertIn("marker stale (BAD)", rev_cell)

    def test_a_marker_naming_an_id_not_in_the_new_changed_set_is_stale(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_FIXED)
            (root / "unrelated.py").write_text("def sub(a, b):\n    return a - a\n")
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base")

            (root / "unrelated.py").write_text("def sub(a, b):\n    return a - b\n")
            marked_test = (
                f"# mutation-check: targets-predate-range {base} -- wrong id "
                ":: Add.test_does_not_exist\n" + DISCRIMINATING_TEST)
            write_test(root, marked_test)
            head = commit_all(root, "fix unrelated.py; marker names a test id that isn't new/changed")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertFalse(ok, rows)
            f, names, rev_cell, weak_cell, flags_cell = rows[0]
            self.assertIn("marker stale (BAD)", rev_cell)
            self.assertIn("Add.test_does_not_exist", rev_cell)

    def test_a_mismatched_sha_still_fails_even_with_a_valid_ids_list(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_FIXED)
            (root / "unrelated.py").write_text("def sub(a, b):\n    return a - a\n")
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base")

            (root / "unrelated.py").write_text("def sub(a, b):\n    return a - b\n")
            wrong_sha = "2" * 40
            marked_test = (
                f"# mutation-check: targets-predate-range {wrong_sha} -- stale marker "
                ":: Add.test_add_returns_the_sum\n" + DISCRIMINATING_TEST)
            write_test(root, marked_test)
            head = commit_all(root, "fix unrelated.py; wrong-sha marker with an otherwise-valid ids list")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertFalse(ok, rows)
            f, names, rev_cell, weak_cell, flags_cell = rows[0]
            self.assertIn("marker mismatch (BAD)", rev_cell)


class WeakeningTargetsOnlyTheChangedFunction(unittest.TestCase):
    """Fix round 6: the assertion-weakening mutation must land INSIDE a test
    function that is new or changed in the range - an old, untouched test
    earlier in the same file must never be the one mutated, even when its
    own assertion would otherwise be the first one `apply_weakening` would
    find by scanning the whole file top to bottom."""

    def test_an_old_test_first_then_a_new_test_below_it_only_mutates_the_new_one(self):
        text = (
            "import unittest\n\n\n"
            "class Old(unittest.TestCase):\n"
            "    def test_old(self):\n"
            "        self.assertEqual(1, 1)\n\n\n"
            "class New(unittest.TestCase):\n"
            "    def test_new(self):\n"
            "        self.assertEqual(2, 2)\n"
        )
        # Old.test_old's def is line 5, body line 6; New.test_new's def is
        # line 10, body line 11. Only the new function's range is passed -
        # exactly what check_range does (func_ranges comes from
        # changed_test_functions, new/changed only).
        new_func_range = [(10, 11)]
        mutated, what = mc.apply_weakening(text, new_func_range)
        self.assertIsNotNone(mutated)
        self.assertEqual(what, "assertEqual -> assertNotEqual")
        # The old test's assertion is byte-identical; only the new one flipped.
        self.assertIn("        self.assertEqual(1, 1)\n", mutated)
        self.assertIn("        self.assertNotEqual(2, 2)\n", mutated)
        self.assertNotIn("assertNotEqual(1, 1)", mutated)

    def test_end_to_end_old_test_first_new_test_below_still_discriminates_correctly(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_BUGGY)
            write_test(root,
                       "import unittest\nfrom production import add\n\n\n"
                       "class Old(unittest.TestCase):\n"
                       "    def test_placeholder(self):\n"
                       "        self.assertTrue(callable(add))\n")
            base = commit_all(root, "base: buggy add(), one old test")

            (root / "production.py").write_text(PROD_FIXED)
            write_test(root,
                       "import unittest\nfrom production import add\n\n\n"
                "class Old(unittest.TestCase):\n"
                "    def test_placeholder(self):\n"
                "        self.assertTrue(callable(add))\n\n\n"
                "class New(unittest.TestCase):\n"
                "    def test_add_returns_the_sum(self):\n"
                "        self.assertEqual(add(2, 3), 5)\n")
            head = commit_all(root, "fix add(); old test untouched, new test added below it")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertTrue(ok, rows)
            f, names, rev_cell, weak_cell, flags_cell = rows[0]
            self.assertEqual(names, "New.test_add_returns_the_sum")
            self.assertIn("FAIL (good)", weak_cell)
            self.assertNotIn("fallback", weak_cell)


class WeakeningFallsBackToFileLevelWhenTheChangedFunctionHasNoAssert(unittest.TestCase):
    """Fix round 6, second half: when the new/changed test function(s)
    contain literally no assertion at all (the static flag already reports
    this separately), the weaken column falls back to the file's first
    assertion anywhere and always says so in the table - pass or fail."""

    def test_no_assert_in_the_new_test_falls_back_and_is_labelled(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_FIXED)
            (root / "tests").mkdir()
            (root / "tests" / "test_production.py").write_text(
                "import unittest\nfrom production import add\n\n\n"
                "class Old(unittest.TestCase):\n"
                "    def test_old_with_a_real_assertion(self):\n"
                "        self.assertEqual(add(2, 3), 5)\n")
            base = commit_all(root, "base: one old test with a real assertion")

            (root / "tests" / "test_production.py").write_text(
                "import unittest\nfrom production import add\n\n\n"
                "class Old(unittest.TestCase):\n"
                "    def test_old_with_a_real_assertion(self):\n"
                "        self.assertEqual(add(2, 3), 5)\n\n\n"
                "class New(unittest.TestCase):\n"
                "    def test_new_makes_a_call_but_checks_nothing(self):\n"
                "        add(2, 3)\n")
            head = commit_all(root, "add a new test with no assert at all")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertFalse(ok, rows)  # the static "no assert" flag alone fails the gate
            f, names, rev_cell, weak_cell, flags_cell = rows[0]
            self.assertIn("no assert", flags_cell)
            self.assertIn("weaken: file-level fallback", weak_cell)
            # It ran against Old's real assertion and that DID fail (good) -
            # still labelled, so a reviewer knows it wasn't the new test.
            self.assertIn("FAIL (good)", weak_cell)


class ReferencesProdFileResolvesToSpecificSubmodules(unittest.TestCase):
    """Fix round 7: `from pkg import a, b` must resolve to the specific
    submodules `pkg/a.py`, `pkg/b.py` (and subpackages) - never to the bare
    `pkg` standing in for some OTHER, unrelated submodule under the same
    package. Mirrors the literal case from the audit: a test doing
    `from capsule import db, incidents` with `db.py` changed and
    `incidents.py` unchanged must count only `db.py`."""

    def _make_pkg(self, root, db_body, incidents_body):
        (root / "pkg").mkdir(exist_ok=True)
        (root / "pkg" / "__init__.py").write_text("")
        (root / "pkg" / "db.py").write_text(db_body)
        (root / "pkg" / "incidents.py").write_text(incidents_body)

    def test_importing_both_names_with_only_one_changed_counts_only_that_one(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            self._make_pkg(root, "def get(x):\n    return x - x\n", "INCIDENTS = []\n")
            write_test(root, "from pkg import db, incidents\n\n\ndef test_placeholder():\n    assert db\n")
            base = commit_all(root, "base: buggy db.get(), incidents.py untouched throughout")

            (root / "pkg" / "db.py").write_text("def get(x):\n    return x\n")
            write_test(root,
                       "from pkg import db, incidents\n\n\n"
                       "def test_get_returns_its_argument():\n"
                       "    assert db.get(3) == 3\n"
                       "    assert incidents.INCIDENTS == []\n")
            head = commit_all(root, "fix db.get(); incidents.py never changes in this range")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertTrue(ok, rows)
            f, names, rev_cell, weak_cell, flags_cell = rows[0]
            self.assertIn("FAIL (good)", rev_cell)  # correctly recognized as referencing db.py
            self.assertIn("FAIL (good)", weak_cell)

    def test_importing_the_unchanged_sibling_only_reads_no_production_change(self):
        """The regression this round actually guards against: incidents.py
        (unrelated) changes; the test imports ONLY db (unchanged) from the
        same package. The OLD bare-package match would have treated "pkg" as
        a stand-in for incidents.py too and wrongly run (and fail) the
        revert; the fix must read n/a (no production change) instead."""
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            self._make_pkg(root, "def get(x):\n    return x\n", "INCIDENTS = []\n")
            write_test(root, "from pkg import db\n\n\ndef test_placeholder():\n    assert db\n")
            base = commit_all(root, "base: db.py already correct, incidents.py about to change")

            (root / "pkg" / "incidents.py").write_text("INCIDENTS = ['fixed']\n")
            write_test(root,
                       "from pkg import db\n\n\n"
                       "def test_get_returns_its_argument():\n"
                       "    assert db.get(3) == 3\n")
            head = commit_all(root, "change incidents.py (unrelated); new test only imports db")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertTrue(ok, rows)
            f, names, rev_cell, weak_cell, flags_cell = rows[0]
            self.assertEqual(rev_cell, "n/a (no production change)")
            self.assertIn("FAIL (good)", weak_cell)


class ReferencesProdFileHandlesInitReexports(unittest.TestCase):
    """Fix round 7, the `__init__` case: `pkg/__init__.py` doing `from
    pkg._core import shared` re-exports `shared` from `_core`, not from a
    same-named `pkg/shared.py` (which does not exist) - `from pkg import
    shared` must still be recognized as referencing `pkg/_core.py`."""

    def test_a_name_reexported_through_init_is_traced_to_its_real_submodule(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "pkg").mkdir()
            (root / "pkg" / "__init__.py").write_text("from pkg._core import shared\n")
            (root / "pkg" / "_core.py").write_text("def shared(x):\n    return x - x\n")
            write_test(root, "from pkg import shared\n\n\ndef test_placeholder():\n    assert shared\n")
            base = commit_all(root, "base: buggy shared(), re-exported through __init__")

            (root / "pkg" / "_core.py").write_text("def shared(x):\n    return x\n")
            write_test(root,
                       "from pkg import shared\n\n\n"
                       "def test_shared_returns_its_argument():\n"
                       "    assert shared(3) == 3\n")
            head = commit_all(root, "fix _core.shared(), only ever imported via the pkg re-export")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertTrue(ok, rows)
            f, names, rev_cell, weak_cell, flags_cell = rows[0]
            self.assertIn("FAIL (good)", rev_cell)  # NOT "n/a (no production change)"
            self.assertIn("FAIL (good)", weak_cell)


class UnittestTimeoutBudgetScalesDeterministically(unittest.TestCase):
    """Fix round 11 (mirrored from the sibling project's own integrator-reproduced
    timeout): max(180, 10 * len(names)) - no per-file markers or overrides.
    A selection large enough that 10*N clearly exceeds a fixed 180s ceiling
    must not share that ceiling with a tiny one."""

    def test_the_formula(self):
        self.assertEqual(mc.unittest_timeout_budget([]), 180)
        self.assertEqual(mc.unittest_timeout_budget(["a"] * 5), 180)
        self.assertEqual(mc.unittest_timeout_budget(["a"] * 18), 180)
        self.assertEqual(mc.unittest_timeout_budget(["a"] * 19), 190)
        self.assertEqual(mc.unittest_timeout_budget(["a"] * 48), 480)

    def test_run_discover_defaults_its_timeout_to_the_budget_formula(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            write_test(root,
                       "import time\nimport unittest\n\n\n"
                       "class Slow(unittest.TestCase):\n"
                       "    def test_slow(self):\n"
                       "        time.sleep(1)\n"
                       "        self.assertEqual(1, 1)\n")
            commit_all(root, "one slow-ish test")
            rc, out = mc.run_discover(root, root / "tests", "test_production.py",
                                      ["Slow.test_slow"], timeout=0.01)
            self.assertEqual(out, "timed out")
            rc2, out2 = mc.run_discover(root, root / "tests", "test_production.py",
                                        ["Slow.test_slow"])  # default: the formula
            self.assertNotEqual(out2, "timed out")


class NoChangedTestFunctionsIsNotAFailure(unittest.TestCase):
    """Fix round 8 (HTC integration report, mirrored from the sibling project): a changed
    test FILE whose only edit in-range is NOT inside any `def test_*`
    (decorator-only, or a fixture/helper function's signature changing) has
    zero new/changed test functions - there is nothing to select and run.
    `names = []` must never reach `run_discover` and be misread as
    `rc == 0` -> "did not fail (BAD)"; it must read
    n/a (no test functions changed), and must NOT count against the gate."""

    def test_a_helper_signature_change_with_no_test_function_touched_reads_n_a(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "unrelated.py").write_text("def sub(a, b):\n    return a - a\n")
            write_test(root,
                       "import unittest\n\n\n"
                       "class Old(unittest.TestCase):\n"
                       "    def test_placeholder(self):\n"
                       "        self.assertEqual(1, 1)\n")
            base = commit_all(root, "base: buggy unrelated.py, one placeholder test")

            (root / "unrelated.py").write_text("def sub(a, b):\n    return a - b\n")
            write_test(root,
                       "import unittest\n\n\n"
                       "class Old(unittest.TestCase):\n"
                       "    def test_placeholder(self):\n"
                       "        self.assertEqual(1, 1)\n\n\n"
                       "def fake_commit(x, expect_module=None):\n"
                       "    return x\n")
            head = commit_all(root, "fix unrelated.py; add a non-test helper, no test function touched")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertTrue(ok, rows)
            f, names, rev_cell, weak_cell, flags_cell = rows[0]
            self.assertEqual(names, "-")  # no new/changed test function at all
            self.assertEqual(rev_cell, "n/a (no test functions changed)")
            self.assertEqual(weak_cell, "n/a")

    def test_cli_exit_code_zero(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            write_empty_baseline(root)
            (root / "unrelated.py").write_text("def sub(a, b):\n    return a - a\n")
            write_test(root,
                       "import unittest\n\n\n"
                       "class Old(unittest.TestCase):\n"
                       "    def test_placeholder(self):\n"
                       "        self.assertEqual(1, 1)\n")
            base = commit_all(root, "base")
            (root / "unrelated.py").write_text("def sub(a, b):\n    return a - b\n")
            write_test(root,
                       "import unittest\n\n\n"
                       "class Old(unittest.TestCase):\n"
                       "    def test_placeholder(self):\n"
                       "        self.assertEqual(1, 1)\n\n\n"
                       "def fake_commit(x, expect_module=None):\n"
                       "    return x\n")
            head = commit_all(root, "fix unrelated.py; helper-only edit")

            p = subprocess.run([sys.executable, str(Path(__file__).resolve().parent / "mutation_check.py"),
                                 "--base", base, "--head", head, "--repo", str(root)],
                                capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertIn("n/a (no test functions changed)", p.stdout)
            self.assertNotIn("did not fail (BAD)", p.stdout)

    def test_the_file_level_fallback_still_runs_for_files_that_DO_have_changed_functions(self):
        """Round 6's fallback rule must stay intact - this guard only fires
        when funcs is genuinely empty, never for a file that has a
        new/changed test function (even one with no assert of its own)."""
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_FIXED)
            write_test(root,
                       "import unittest\nfrom production import add\n\n\n"
                       "class Old(unittest.TestCase):\n"
                       "    def test_old_with_a_real_check(self):\n"
                       "        self.assertEqual(add(2, 3), 5)\n")
            base = commit_all(root, "base: one old test with a real assertion")

            write_test(root,
                       "import unittest\nfrom production import add\n\n\n"
                       "class Old(unittest.TestCase):\n"
                       "    def test_old_with_a_real_check(self):\n"
                       "        self.assertEqual(add(2, 3), 5)\n\n\n"
                       "class New(unittest.TestCase):\n"
                       "    def test_new_makes_a_call_but_checks_nothing(self):\n"
                       "        add(2, 3)\n")
            head = commit_all(root, "add a new test with no assert at all")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertFalse(ok, rows)
            f, names, rev_cell, weak_cell, flags_cell = rows[0]
            self.assertNotEqual(names, "-")
            self.assertIn("weaken: file-level fallback", weak_cell)


class B1RevertedSymbolImportError(unittest.TestCase):
    """Fix round 10, B1 (mirrored from the sibling project's HTC gate-rows audit):
    `cannot import name 'X' from 'M'` after reverting M is not the same as M
    not existing at all (round 2's new_module rule) - M exists at base, it
    just does not define X yet, because the range ADDED X to M. When M's
    diff is additions-only, rerun with M kept at HEAD and score normally."""

    def test_reverting_a_pure_addition_reruns_with_the_symbol_kept_at_head(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "errors.py").write_text("class Existing(Exception):\n    pass\n")
            (root / "production.py").write_text(PROD_BUGGY)
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base: errors.py has Existing only, buggy add()")

            (root / "errors.py").write_text(
                "class Existing(Exception):\n    pass\n\n\nclass BadInput(Exception):\n    pass\n")
            (root / "production.py").write_text(PROD_FIXED)
            write_test(root,
                       "import unittest\n"
                       "from errors import BadInput\n"
                       "from production import add\n\n\n"
                       "class Add(unittest.TestCase):\n"
                       "    def test_add_and_the_new_error_class(self):\n"
                       "        self.assertEqual(add(2, 3), 5)\n"
                       "        self.assertTrue(issubclass(BadInput, Exception))\n")
            head = commit_all(root, "fix add(); add errors.BadInput (pure addition); cover both")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertTrue(ok, rows)
            f, names, rev_cell, weak_cell, flags_cell = rows[0]
            self.assertIn("FAIL (good)", rev_cell)
            self.assertIn("errors.py kept at head for import of BadInput", rev_cell)
            self.assertIn("FAIL (good)", weak_cell)

    def test_a_genuinely_unrelated_import_error_stays_error(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_BUGGY)
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base")
            (root / "production.py").write_text(PROD_FIXED)
            write_test(root, IMPORT_ERROR_TEST)
            head = commit_all(root, "fix add(); new test imports a genuinely missing module")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertFalse(ok, rows)
            f, names, rev_cell, weak_cell, flags_cell = rows[0]
            # Review round 2, finding 7/P3: restored to exact equality now
            # that finding 4's collapse-to-last-line fix makes the text
            # deterministic again (previously loosened to `startswith`
            # because `why` carried an unbounded, multi-line traceback tail).
            self.assertEqual(rev_cell, "ERROR (not discriminating): FAILED (errors=1)")


class TwoModuleImportChainKeepsBothAndReruns(unittest.TestCase):
    """Card t_285a8851: round 10's B1 single-hop keep-and-rerun handles ONE
    missing new name at collection - when the kept module itself imports a
    SECOND new name from a still-reverted module, the single rerun used to
    fail collection again and score a plain ERROR. Real shape hit on
    The sibling project's fix/n1-parity-harness: `tests/gauntlet/policy.py` imported
    `KNOWN_FLAGS` from the reverted `gauntlet/evaluate.py`. The gate now
    repeats keep-and-rerun until nothing more resolves (bounded by
    `CHAIN_ITERATION_LIMIT`). Source: card t_285a8851, citing the observed
    sibling-project run."""

    def test_a_two_hop_chain_resolves_and_the_range_still_discriminates(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "evaluate.py").write_text("def evaluate():\n    return True\n")
            (root / "policy.py").write_text(
                "from evaluate import evaluate\n\n\n"
                "def check():\n    return evaluate()\n")
            (root / "production.py").write_text(PROD_BUGGY)
            (root / "tests").mkdir()
            (root / "tests" / "test_chain.py").write_text(
                "import unittest\nfrom production import add\nfrom policy import check\n\n\n"
                "class Chain(unittest.TestCase):\n"
                "    def test_placeholder(self):\n"
                "        self.assertTrue(callable(add) and callable(check))\n")
            base = commit_all(root, "base: policy.py has no POLICY_TAG, evaluate.py has no KNOWN_FLAGS")

            (root / "evaluate.py").write_text(
                "KNOWN_FLAGS = {'a', 'b'}\n\n\ndef evaluate():\n    return True\n")
            (root / "policy.py").write_text(
                "from evaluate import evaluate, KNOWN_FLAGS\n\n\n"
                "POLICY_TAG = 'n1-parity'\n\n\n"
                "def check():\n    return evaluate() and bool(KNOWN_FLAGS)\n")
            (root / "production.py").write_text(PROD_FIXED)
            (root / "tests" / "test_chain.py").write_text(
                "import unittest\n"
                "from production import add\n"
                "from policy import POLICY_TAG, check\n\n\n"
                "class Chain(unittest.TestCase):\n"
                "    def test_add_and_policy_tag(self):\n"
                "        self.assertEqual(add(2, 3), 5)\n"
                "        self.assertEqual(POLICY_TAG, 'n1-parity')\n"
                "        self.assertTrue(check())\n")
            head = commit_all(root, "fix add(); add policy.POLICY_TAG chained through evaluate.KNOWN_FLAGS")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertTrue(ok, rows)
            row = next(r for r in rows if r[0] == "tests/test_chain.py")
            f, names, rev_cell, weak_cell, flags_cell = row
            self.assertNotIn("ERROR (not discriminating)", rev_cell)
            self.assertNotIn("error_runner", rev_cell)
            self.assertIn("policy.py kept at head for import of POLICY_TAG", rev_cell)
            self.assertIn("evaluate.py kept at head for import of KNOWN_FLAGS", rev_cell)
            self.assertIn("FAIL (good)", rev_cell)


class ImportChainExceedingTheLimitIsUnresolved(unittest.TestCase):
    """Card t_285a8851: a chain longer than `CHAIN_ITERATION_LIMIT` (5) must
    never loop forever or silently pass - it reports a clear
    `error_runner: unresolved chain <names>` and fails the gate. Source:
    card t_285a8851's "limit 5 iterations, then a clear error" requirement.
    Review round 1, finding 6: the message must carry MODULE PATHS (not just
    symbol names) for every hop actually kept, plus the Nth name that could
    not be kept (`mod5.py:NEXT5` here) - never silently dropped."""

    def test_a_six_module_chain_exceeds_the_limit_and_reports_unresolved(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            n = 6
            for k in range(n):
                (root / f"mod{k}.py").write_text(f"OWN_{k} = {k}\n")
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base: mod0..mod5 exist, no chained imports yet")

            for k in range(n - 1):
                (root / f"mod{k}.py").write_text(
                    f"from mod{k + 1} import NEXT{k + 1}\n\n\n"
                    f"OWN_{k} = {k}\nNEXT{k} = {k}\n")
            (root / f"mod{n - 1}.py").write_text(f"OWN_{n - 1} = {n - 1}\nNEXT{n - 1} = {n - 1}\n")
            write_test(root,
                       "import unittest\nfrom mod0 import NEXT0\n\n\n"
                       "class Chain(unittest.TestCase):\n"
                       "    def test_next0(self):\n"
                       "        self.assertEqual(NEXT0, 0)\n")
            head = commit_all(root, "chain mod0 -> mod1 -> ... -> mod5, 5 hops deep")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertFalse(ok, rows)
            f, names, rev_cell, weak_cell, flags_cell = rows[0]
            self.assertIn("error_runner: unresolved chain", rev_cell)
            for k in range(n - 1):
                self.assertIn(f"mod{k}.py:NEXT{k}", rev_cell)
            self.assertIn(f"next unresolved: mod{n - 1}.py:NEXT{n - 1}", rev_cell)


class RuntimeAttributeErrorOnANewNameIsTreatedLikeCollection(unittest.TestCase):
    """Card t_285a8851: an `AttributeError` raised on a newly added name
    INSIDE a test body (the workaround pattern - `module.NEW_NAME` instead
    of `from module import NEW_NAME`) fails at test-execution time, not at
    collection, so `is_collection_error` never sees it. Treated exactly like
    the collection case: keep the module, rerun, never counted as a genuine
    discriminating failure. Source: card t_285a8851's "AttributeError ...
    INSIDE a test body" requirement."""

    def test_an_attribute_style_access_of_a_new_name_is_kept_and_rerun_not_scored_good(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text("def add(a, b):\n    return a + b\n")
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base: production.py has no NEW_FLAG yet")

            (root / "production.py").write_text(
                "def add(a, b):\n    return a + b\n\n\nNEW_FLAG = True\n")
            write_test(root,
                       "import unittest\nimport production\n\n\n"
                       "class Feature(unittest.TestCase):\n"
                       "    def test_new_flag_is_true(self):\n"
                       "        self.assertTrue(production.NEW_FLAG)\n")
            head = commit_all(root, "add production.NEW_FLAG, accessed via attribute not import")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertTrue(ok, rows)  # nothing else in production.py changed - not discriminating
            f, names, rev_cell, weak_cell, flags_cell = rows[0]
            self.assertIn("production.py kept at head for import of NEW_FLAG", rev_cell)
            self.assertIn("info: pure addition", rev_cell)
            self.assertNotEqual(rev_cell, "FAIL (good)")


class BareStemModuleResolvesAnAttributeError(unittest.TestCase):
    """Review round 1, finding 1 (E1): a test that `sys.path.insert`s a
    tools directory and does `import prod` (a bare-stem import - exactly
    what this repo's own `tests/test_mutation_check.py` does for
    `mutation_check` itself, and what 58 test files in this repo do for
    their own subject) reports `module 'prod' has no attribute 'NEW_FLAG'` -
    the bare stem, never the dotted path. Before this fix,
    `_module_path_for_dotted` only matched the exact dotted path, so this
    NEVER resolved and the range scored a false `FAIL (good)`. Source: card
    t_285a8851 fix round 1, Opus review finding E1 (gate-chain-review.md)."""

    def test_bare_stem_sys_path_import_resolves_the_new_attribute(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "tools" / "prod.py").parent.mkdir(parents=True, exist_ok=True)
            (root / "tools" / "prod.py").write_text("def add(a, b):\n    return a + b\n")
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base: tools/prod.py has no NEW_FLAG yet")

            (root / "tools" / "prod.py").write_text(
                "def add(a, b):\n    return a + b\n\nNEW_FLAG = True\n")
            write_test(root,
                       "import sys, unittest\nfrom pathlib import Path\n"
                       "sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))\n"
                       "import prod\n\n\n"
                       "class A(unittest.TestCase):\n"
                       "    def test_new_flag(self):\n"
                       "        self.assertTrue(prod.NEW_FLAG)\n")
            head = commit_all(root, "add tools/prod.NEW_FLAG, imported by bare stem")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertTrue(ok, rows)  # nothing else changed - not discriminating
            f, names, rev_cell, weak_cell, flags_cell = rows[0]
            self.assertIn("tools/prod.py kept at head for import of NEW_FLAG", rev_cell)
            self.assertIn("info: pure addition", rev_cell)


class TopLevelNameScanCoversTuplesReexportsAndIfTry(unittest.TestCase):
    """Review round 1, finding 3 (E2/E2b): `_top_level_names` used to see
    only simple `NAME = ...` assignments and defs - a name bound by tuple
    unpacking (`A, B = ...`) or re-exported via a top-level `from x import
    NEW` was invisible, so `analyze_symbol_import_error` could never resolve
    an attribute access to it and the range scored a false `FAIL (good)`.
    Source: card t_285a8851 fix round 1, Opus review findings E2/E2b."""

    def test_a_tuple_bound_new_name_resolves(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_FIXED)
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base: no NEW_A/NEW_B yet")

            (root / "production.py").write_text(PROD_FIXED + "\nNEW_A, NEW_B = True, True\n")
            write_test(root,
                       "import unittest\nimport production\n\n\n"
                       "class A(unittest.TestCase):\n"
                       "    def test_new_a(self):\n"
                       "        self.assertTrue(production.NEW_A)\n")
            head = commit_all(root, "add NEW_A, NEW_B via tuple assignment")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertTrue(ok, rows)
            f, names, rev_cell, weak_cell, flags_cell = rows[0]
            self.assertIn("production.py kept at head for import of NEW_A", rev_cell)
            self.assertIn("info: pure addition", rev_cell)

    def test_a_reexported_new_name_resolves(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_FIXED)
            (root / "helpers.py").write_text("X = 1\n")
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base: helpers.py has no NEW_FLAG yet")

            (root / "production.py").write_text("from helpers import NEW_FLAG\n" + PROD_FIXED)
            (root / "helpers.py").write_text("X = 1\nNEW_FLAG = True\n")
            write_test(root,
                       "import unittest\nimport production\n\n\n"
                       "class A(unittest.TestCase):\n"
                       "    def test_new_flag(self):\n"
                       "        self.assertTrue(production.NEW_FLAG)\n")
            head = commit_all(root, "re-export helpers.NEW_FLAG through production's top-level import")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertTrue(ok, rows)
            f, names, rev_cell, weak_cell, flags_cell = rows[0]
            self.assertIn("kept at head for import of NEW_FLAG", rev_cell)
            self.assertIn("info: pure addition", rev_cell)

    def test_a_name_bound_under_a_top_level_try_resolves(self):
        """Review round 2, finding P3/M2d: the `if`/`try` fixtures added in
        round 1 only ever exercised the `if` arm (via the tuple/re-export
        fixtures above, both of which happen to be plain top-level
        statements, never wrapped in a `try`) - mutant M2d (delete the
        `ast.Try` visit arm) left the whole suite green. This fixture binds
        `NEW_FLAG` ONLY inside a top-level `try`/`except` body."""
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_FIXED)
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base: no NEW_FLAG yet")

            (root / "production.py").write_text(
                PROD_FIXED + "\ntry:\n    NEW_FLAG = True\nexcept Exception:\n    NEW_FLAG = False\n")
            write_test(root,
                       "import unittest\nimport production\n\n\n"
                       "class A(unittest.TestCase):\n"
                       "    def test_new_flag(self):\n"
                       "        self.assertTrue(production.NEW_FLAG)\n")
            head = commit_all(root, "add NEW_FLAG, bound only inside a top-level try/except")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertTrue(ok, rows)
            f, names, rev_cell, weak_cell, flags_cell = rows[0]
            self.assertIn("kept at head for import of NEW_FLAG", rev_cell)
            self.assertIn("info: pure addition", rev_cell)


class PureAdditionCheckIgnoresImportChurn(unittest.TestCase):
    """Review round 2, finding 1 (E9): round 1's E2b fix made `_top_level_names`
    count `import`/`from` aliases, and `additions_only` reused that SAME set
    (`base_names <= head_names`). So a range that adds a genuine new name
    AND drops an unrelated, unused import (e.g. `import os` no longer
    needed) became napt - `os` vanishing from the import-inclusive
    `head_names` set made `base_names` no longer a subset - and the range
    read a quiet `n/a`, exit 0, instead of the `did not fail (BAD)` it read
    on 3a54b0d (before the E2b fix even existed). `additions_only` is now
    computed from `_top_level_names(..., include_imports=False)` -
    definitions and assignments only - while the "is X new" test above it
    still uses the import-inclusive default. Source: card t_285a8851 fix
    round 2, Opus review finding 1 / probe E9."""

    def test_dropping_an_unused_import_alongside_a_new_name_still_fails_the_gate(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text("import os\n" + PROD_FIXED)
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base: production.py has an unused `import os`, no NEW_FLAG yet")

            (root / "production.py").write_text(PROD_FIXED + "\nNEW_FLAG = True\n")
            write_test(root,
                       "import unittest\nfrom production import add, NEW_FLAG\n\n\n"
                       "class A(unittest.TestCase):\n"
                       "    def test_x(self):\n"
                       "        self.assertTrue(NEW_FLAG)\n")
            head = commit_all(root, "drop the unused `import os`; add NEW_FLAG")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertTrue(ok, rows)
            f, names, rev_cell, weak_cell, flags_cell = rows[0]
            self.assertNotIn("n/a", rev_cell)
            self.assertIn("kept at head for import of NEW_FLAG", rev_cell)
            self.assertIn("info: pure addition", rev_cell)


class BareStemBackstopCoversAmbiguousCandidates(unittest.TestCase):
    """Review round 2, finding 5 (E1c): `_module_path_for_dotted` correctly
    refuses to guess which of two same-stem modules (`a/prod.py` and
    `b/prod.py`, both changed) an ambiguous bare-stem error actually names -
    but the fail-closed backstop used to reuse that same lookup and go
    blind right along with it (`module_path is None` skipped the check
    entirely), so an ambiguous-stem range with a genuinely undetected new
    name still scored a false `FAIL (good)`. The backstop now falls back to
    checking EVERY bare-stem candidate when the unique lookup fails, and
    fails closed if the symbol is absent from all of their `--base`
    sources. Source: card t_285a8851 fix round 2, Opus review finding 5 /
    probe E1c."""

    def test_an_ambiguous_bare_stem_still_fails_closed(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "a" / "prod.py").parent.mkdir(parents=True, exist_ok=True)
            (root / "a" / "prod.py").write_text("X = 1\n")
            (root / "b" / "prod.py").parent.mkdir(parents=True, exist_ok=True)
            (root / "b" / "prod.py").write_text("Y = 1\n")
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base: a/prod.py and b/prod.py both exist, no NEW_FLAG anywhere")

            (root / "a" / "prod.py").write_text("X = 1\nNEW_FLAG = True\n")
            (root / "b" / "prod.py").write_text("Y = 2\n")  # also changes, so BOTH are prod_files
            write_test(root,
                       "import sys, unittest\nfrom pathlib import Path\n"
                       "sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'a'))\n"
                       "import prod\n\n\n"
                       "class A(unittest.TestCase):\n"
                       "    def test_x(self):\n"
                       "        self.assertTrue(prod.NEW_FLAG)\n")
            head = commit_all(root, "a/prod.py gains NEW_FLAG; b/prod.py also changes (ambiguous bare stem)")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertFalse(ok, rows)
            f, names, rev_cell, weak_cell, flags_cell = rows[0]
            self.assertTrue(rev_cell.startswith("ERROR (not discriminating)"), rev_cell)
            self.assertNotIn("FAIL (good)", rev_cell)


class FirstMatchMaskingResolvesLaterOccurrences(unittest.TestCase):
    """Review round 1, finding 1 / round 2, finding 2 (E3, then E3r): the OLD
    `analyze_symbol_import_error` took only `.search()`'s single first regex
    hit in the whole test run's output. When the alphabetically-first
    failing test hits an unresolvable name while a LATER test in the same
    run hits a genuinely resolvable new name, the unresolvable first match
    used to mask the resolvable one entirely and the whole file fell
    through to a plain, unlabelled `FAIL (good)`. `analyze_symbol_import_error`
    now tries every occurrence, in order, and returns the first that
    actually resolves.

    Round 2, finding 2: the round-1 fixture here (`other.py`'s AttributeError
    was resolved via `hasattr`, so it never actually FAILED at all - there
    was no first, unresolvable match in the output to mask anything) was
    vacuous - mutant M3 (`_symbol_error_candidates` truncated to
    `candidates[:1]`, restoring the old first-match-only behaviour) left it
    green. This is probe E3r from the round-2 review: `test_a_loop` reads a
    name bound ONLY by a top-level `for` (a shape `_top_level_names` still
    doesn't track, so it genuinely never resolves and the backstop would
    otherwise catch it as `"error"`), `test_z_flag` reads a plain, resolvable
    `NEW_FLAG` - alphabetically AFTER `test_a_loop`, so its AttributeError
    is genuinely the SECOND occurrence in the combined output. Source: card
    t_285a8851 fix round 2, Opus review finding 2 / probe E3r."""

    def test_an_unresolvable_first_match_does_not_hide_a_resolvable_second_one(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_FIXED)
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base: no NEW_LOOP, no NEW_FLAG yet")

            (root / "production.py").write_text(
                PROD_FIXED + "\nfor NEW_LOOP in [True]:\n    pass\nNEW_FLAG = True\n")
            write_test(root,
                       "import unittest\nimport production\n\n\n"
                       "class A(unittest.TestCase):\n"
                       "    def test_a_loop(self):\n"
                       "        self.assertTrue(production.NEW_LOOP)\n\n"
                       "    def test_z_flag(self):\n"
                       "        self.assertTrue(production.NEW_FLAG)\n")
            head = commit_all(root, "add NEW_LOOP (for-bound, untracked) and NEW_FLAG (plain, resolvable)")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            f, names, rev_cell, weak_cell, flags_cell = rows[0]
            # The resolvable NEW_FLAG match must have been found and acted
            # on despite the genuinely unresolvable NEW_LOOP match coming
            # first in the combined output.
            self.assertIn("kept at head for import of NEW_FLAG", rev_cell)


class FailClosedBackstopCatchesAnUndetectedBinding(unittest.TestCase):
    """Review round 2, finding 3 (E2c): the fail-closed backstop
    (`_mentions_missing_prod_symbol`) had no regression test at all - mutant
    M4 (delete its call site in `_resolve_import_chain`) left the full suite
    green. `NEW_LOOP` here is bound ONLY by a top-level `for` (still
    untracked by `_top_level_names` even after round 1's if/try fix - `for`
    was never on that list), so `analyze_symbol_import_error` can never
    resolve it and, with NOTHING else in `production.py` to fall back to,
    the backstop is the ONLY thing standing between this range and a false
    `FAIL (good)`. Source: card t_285a8851 fix round 2, Opus review finding
    3 / probe E2c."""

    def test_a_for_bound_name_with_nothing_else_to_resolve_fails_closed(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_FIXED)
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base: no NEW_LOOP yet")

            (root / "production.py").write_text(PROD_FIXED + "\nfor NEW_LOOP in [True]:\n    pass\n")
            write_test(root,
                       "import unittest\nimport production\n\n\n"
                       "class A(unittest.TestCase):\n"
                       "    def test_x(self):\n"
                       "        self.assertTrue(production.NEW_LOOP)\n")
            head = commit_all(root, "add NEW_LOOP, bound only by a top-level for loop")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertFalse(ok, rows)
            f, names, rev_cell, weak_cell, flags_cell = rows[0]
            self.assertTrue(rev_cell.startswith("ERROR (not discriminating)"), rev_cell)
            self.assertNotIn("FAIL (good)", rev_cell)


class NaptPartwayDownAChainIsAnErrorNotAQuietNA(unittest.TestCase):
    """Review round 1, finding 5 (E6): a napt (not-a-pure-addition) hit found
    AFTER one or more modules have already been kept used to read as a
    quiet `n/a (reverted symbol ... imported at collection)`, and the gate
    passed - reaching this n/a directly via the chain is a QUIETER version
    of the exact shape round 10's single-shot B1 used to score a plain,
    gate-failing `ERROR`. A napt found partway down a non-empty chain is now
    `"error"`, carrying the whole chain in its message, never a silent n/a.
    Source: card t_285a8851 fix round 1, Opus review finding E6."""

    def test_a_napt_hit_after_one_kept_module_fails_the_gate(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_BUGGY)
            (root / "m1.py").write_text("A = 1\n")
            (root / "m2.py").write_text("OLD = 1\nB = 1\n")
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base: m1 has no X yet, m2 has OLD (removed later) + B")

            (root / "production.py").write_text(PROD_FIXED)
            (root / "m1.py").write_text("from m2 import Y\nA = 1\nX = 1\n")
            (root / "m2.py").write_text("B = 1\nY = 1\n")  # OLD removed: NOT a pure addition
            write_test(root,
                       "import unittest\nfrom production import add\nfrom m1 import X\n\n\n"
                       "class Chain(unittest.TestCase):\n"
                       "    def test_add_and_x(self):\n"
                       "        self.assertEqual(add(2, 3), 5)\n"
                       "        self.assertEqual(X, 1)\n")
            head = commit_all(root, "fix add(); m1 gains X (pure addition) but imports m2.Y; "
                                     "m2 loses OLD while gaining Y (not a pure addition)")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertFalse(ok, rows)
            f, names, rev_cell, weak_cell, flags_cell = rows[0]
            self.assertIn("ERROR (not discriminating)", rev_cell)
            self.assertNotIn("n/a", rev_cell)
            self.assertIn("m1.py kept at head for import of X", rev_cell)
            self.assertIn("m2.py", rev_cell)
            self.assertIn("Y", rev_cell)


class SameModuleSymbolRepeatingStopsTheChainEarly(unittest.TestCase):
    """Review round 1, finding 6: if the EXACT SAME `(module, symbol)`
    resolution recurs (a cycle - here, an `if False:` dead branch that
    `_top_level_names` correctly sees as a bound top-level name but that
    never actually executes, so keeping the module never actually fixes the
    attribute access), the chain must stop immediately rather than burn
    through all `CHAIN_ITERATION_LIMIT` iterations (each run at least a
    180s timeout) making no progress. Source: card t_285a8851 fix round 1,
    Opus review finding 6 ("bail early if the same (module, symbol)
    repeats")."""

    def test_a_dead_branch_name_repeats_and_stops_after_one_extra_iteration(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text("OK = 1\n")
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base: no NEW_DEAD, no dead branch")

            (root / "production.py").write_text("if False:\n    NEW_DEAD = 1\n\nOK = 1\n")
            write_test(root,
                       "import unittest\nimport production\n\n\n"
                       "class Chk(unittest.TestCase):\n"
                       "    def test_dead_branch(self):\n"
                       "        self.assertTrue(production.NEW_DEAD)\n")
            head = commit_all(root, "add NEW_DEAD only inside an `if False:` dead branch")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertFalse(ok, rows)
            f, names, rev_cell, weak_cell, flags_cell = rows[0]
            self.assertIn("error_runner: unresolved chain", rev_cell)
            self.assertIn("production.py:NEW_DEAD", rev_cell)
            self.assertIn("next unresolved: production.py:NEW_DEAD", rev_cell)
            # Exactly ONE hop in the kept chain plus the "next unresolved"
            # mention (2 total) - stopped after the FIRST repeat, not after
            # burning all CHAIN_ITERATION_LIMIT (5) iterations re-adding the
            # same (module, symbol) pair each time.
            self.assertEqual(rev_cell.count("production.py:NEW_DEAD"), 2, rev_cell)


class KeptModuleRestoreDoesNotLeakBetweenTestFiles(unittest.TestCase):
    """Review round 1, finding 4 (E7): the per-module restore-to-base in
    `_resolve_import_chain`'s `finally` was a surviving mutant - deleting it
    left all existing tests green. Two test files in the same range both
    access `production.NEW_FLAG`; if the first file's run leaves
    `production.py` kept at HEAD in the shared worktree, the second file's
    own revert-mutation run would never even see a failure (production.py
    would already be fully at HEAD when its run starts), and its cell would
    read a bare, unlabelled `did not fail (BAD)` with no `[revert: ...]`
    label at all. Both files must carry the label independently. Source:
    card t_285a8851 fix round 1, Opus review finding E7."""

    def test_the_kept_label_appears_independently_in_both_files(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_FIXED)
            (root / "tests").mkdir()
            (root / "tests" / "test_production.py").write_text(PLACEHOLDER_TEST)
            (root / "tests" / "test_second.py").write_text(PLACEHOLDER_TEST)
            base = commit_all(root, "base: no NEW_FLAG yet, two placeholder test files")

            (root / "production.py").write_text(PROD_FIXED + "\nNEW_FLAG = True\n")
            (root / "tests" / "test_production.py").write_text(
                "import unittest\nimport production\n\n\n"
                "class A(unittest.TestCase):\n"
                "    def test_a(self):\n"
                "        self.assertTrue(production.NEW_FLAG)\n")
            (root / "tests" / "test_second.py").write_text(
                "import unittest\nimport production\n\n\n"
                "class B(unittest.TestCase):\n"
                "    def test_b(self):\n"
                "        self.assertTrue(production.NEW_FLAG)\n")
            head = commit_all(root, "add production.NEW_FLAG; two test files both use it via attribute")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertTrue(ok, rows)
            self.assertEqual(len(rows), 2)
            for f, names, rev_cell, weak_cell, flags_cell in rows:
                self.assertIn("production.py kept at head for import of NEW_FLAG", rev_cell,
                              f"{f} lost its kept-module label - the restore leaked across files")


class B4StaticNoAssertFollowsSameFileHelpers(unittest.TestCase):
    """Fix round 10, B4 (mirrored from the sibling project's HTC gate-rows audit): a test
    body with no `assert` of its own, but a call to `self.<helper>(...)`
    whose OWN body contains `assert`, reads as informational, never the
    failing "no assert" flag. A genuinely assert-free chain still flags."""

    def test_a_test_that_only_calls_a_same_class_helper_with_a_real_assert_is_not_flagged(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_FIXED)
            base = commit_all(root, "base")
            write_test(root,
                       "import unittest\nfrom production import add\n\n\n"
                       "class Checks(unittest.TestCase):\n"
                       "    def check_sum(self, a, b, expected):\n"
                       "        self.assertEqual(add(a, b), expected)\n\n"
                       "    def test_add_via_helper(self):\n"
                       "        self.check_sum(2, 3, 5)\n")
            head = commit_all(root, "add a new test that only calls a same-class helper")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            f, names, rev_cell, weak_cell, flags_cell = rows[0]
            self.assertIn("asserts via helper check_sum", flags_cell)
            self.assertNotIn("no assert in the function body", flags_cell)
            self.assertIn("FAIL (good)", weak_cell)
            self.assertIn("weaken: helper check_sum assertion 1", weak_cell)

    def test_a_test_that_calls_nothing_with_a_real_assert_still_flags(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_FIXED)
            base = commit_all(root, "base")
            write_test(root,
                       "import unittest\nfrom production import add\n\n\n"
                       "class Checks(unittest.TestCase):\n"
                       "    def test_calls_nothing_useful(self):\n"
                       "        add(2, 3)\n")
            head = commit_all(root, "add a new test that asserts nothing, calls nothing helpful")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            f, names, rev_cell, weak_cell, flags_cell = rows[0]
            self.assertIn("no assert in the function body", flags_cell)


class B5WeakenTargetsTheHelperAssertions(unittest.TestCase):
    """Fix round 12 (mirrored from the sibling project, HTC integration audit,
    htc/integration 9bdd740): when a changed test asserts only via a
    same-file/base-class helper (the B4 shape), the weaken mutation must
    land in the HELPER's own assertions - trying each one in source order
    until one run fails - never the file-level fallback, which can land on
    an assertion the range never even exercises."""

    def test_helper_targeted_directly_when_it_is_the_only_assertion_source(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_FIXED)
            base = commit_all(root, "base")
            write_test(root,
                       "import unittest\nfrom production import add\n\n\n"
                       "class Checks(unittest.TestCase):\n"
                       "    def check_sum(self, a, b, expected):\n"
                       "        self.assertEqual(add(a, b), expected)\n\n"
                       "    def test_add_via_helper(self):\n"
                       "        self.check_sum(2, 3, 5)\n")
            head = commit_all(root, "add a new test that only calls a same-class helper")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            f, names, rev_cell, weak_cell, flags_cell = rows[0]
            self.assertIn("weaken: helper check_sum assertion 1", weak_cell)
            self.assertIn("FAIL (good)", weak_cell)
            self.assertNotIn("file-level fallback", weak_cell)

    def test_iterates_past_a_helper_assertion_the_selected_ids_never_reach(self):
        """The helper's FIRST recognized assertion is dead code for every
        selected id (guarded by `skip_first=True`, always passed by the one
        new test) - mutating it changes nothing observable, so B5 must move
        on to the helper's SECOND assertion (the one that actually governs
        the test), not stop and report a false "did not fail (BAD)" after
        trying only the first."""
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_FIXED)
            base = commit_all(root, "base")
            write_test(root,
                       "import unittest\nfrom production import add\n\n\n"
                       "class Checks(unittest.TestCase):\n"
                       "    def check_sum(self, a, b, expected, skip_first=False):\n"
                       "        if not skip_first:\n"
                       "            self.assertEqual(a + 0, a)\n"
                       "        self.assertEqual(add(a, b), expected)\n\n"
                       "    def test_add_via_helper(self):\n"
                       "        self.check_sum(2, 3, 5, skip_first=True)\n")
            head = commit_all(root, "add a new test whose helper has a dead-code first assertion")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            f, names, rev_cell, weak_cell, flags_cell = rows[0]
            self.assertIn("FAIL (good)", weak_cell)
            self.assertIn("weaken: helper check_sum assertion 2", weak_cell)


class ScratchWorktreesNeverLeak(unittest.TestCase):
    """Round 12 (mirrored from the sibling project, HTC integration audit): the sibling
    pytest-based script had leaked 501 `mutation-check-{revert,weaken}-*`
    scratch worktree entries into its own `.git` - `remove_worktree`'s own
    git call can fail silently (it swallows its exit code) and a process
    kill can skip a `finally` outright, so `check_range` must ALSO run
    `git worktree prune` unconditionally on every exit path (return,
    exception, or KeyboardInterrupt), never rely on `remove_worktree`
    alone."""

    def test_no_leftover_worktree_entries_after_a_normal_run(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_BUGGY)
            base = commit_all(root, "base")
            (root / "production.py").write_text(PROD_FIXED)
            write_test(root,
                       "import unittest\nfrom production import add\n\n\n"
                       "class Add(unittest.TestCase):\n"
                       "    def test_add_returns_the_sum(self):\n"
                       "        self.assertEqual(add(2, 3), 5)\n")
            head = commit_all(root, "fix add() and add a discriminating test")

            before = git(root, "worktree", "list", "--porcelain")
            ok, rows, note = mc.check_range(root, base, head)
            after = git(root, "worktree", "list", "--porcelain")

            self.assertIsNone(note)
            self.assertEqual(before, after,
                              "check_range must leave `git worktree list` exactly as it found it")

    def test_worktree_prune_runs_even_when_check_range_is_interrupted(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_BUGGY)
            base = commit_all(root, "base")
            (root / "production.py").write_text(PROD_FIXED)
            write_test(root,
                       "import unittest\nfrom production import add\n\n\n"
                       "class Add(unittest.TestCase):\n"
                       "    def test_add_returns_the_sum(self):\n"
                       "        self.assertEqual(add(2, 3), 5)\n")
            head = commit_all(root, "fix add() and add a discriminating test")

            calls = []
            real_sh = mc.sh

            def raising_sh(argv, *a, **kw):
                if argv[:2] == ["git", "worktree"] and "prune" not in argv:
                    calls.append(list(argv))
                if len(calls) == 1 and "add" in argv:
                    # Let the FIRST scratch worktree get created, then blow up
                    # partway through the run as a stand-in for a genuine
                    # KeyboardInterrupt/SIGINT landing mid-check.
                    result = real_sh(argv, *a, **kw)
                    raise KeyboardInterrupt("simulated interrupt mid-run")
                return real_sh(argv, *a, **kw)

            before = git(root, "worktree", "list", "--porcelain")
            with unittest.mock.patch.object(mc, "sh", side_effect=raising_sh):
                with self.assertRaises(KeyboardInterrupt):
                    mc.check_range(root, base, head)
            after = git(root, "worktree", "list", "--porcelain")

            self.assertEqual(before, after,
                              "git worktree prune must run in check_range's own finally "
                              "even when the run is interrupted, leaving no stale entry behind")


class SkippedTestIsFlagged(unittest.TestCase):
    def test_expectedFailure_on_a_new_test_is_a_static_flag(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_BUGGY)
            base = commit_all(root, "base")
            (root / "production.py").write_text(PROD_FIXED)
            write_test(root,
                       "import unittest\nfrom production import add\n\n\n"
                       "class Add(unittest.TestCase):\n"
                       "    @unittest.expectedFailure\n"
                       "    def test_add_returns_the_sum(self):\n"
                       "        self.assertEqual(add(2, 3), 5)\n")
            head = commit_all(root, "fix add(), but mark the new test xfail")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertFalse(ok, rows)
            self.assertIn("expectedFailure", rows[0][-1])


class DirtyTreeRefusal(unittest.TestCase):
    """Mirrored from the sibling project's `tests/mutation_check.py`. The
    incident this closes: the gate's own wiring (`maintenance/merge_gate.py`,
    this file) sat UNCOMMITTED for several merge-gate rounds while a run
    exercised it by file path only - a "clean" gate run said nothing about
    the fact that the thing actually running, or the production code
    (`capsule/`) it exists to protect, was not what HEAD said it was. The
    entry point must refuse outright - exit 75, "environment error", every
    dirty tracked path named - unless the caller passes `--allow-dirty` for
    a local experiment, which must still print a loud warning naming the
    same paths."""

    @staticmethod
    def _fixture_with_dirty_path(root, dirty_relpath):
        """A repo with `production.py`/`tests/` (mutation_check.py's own
        check_range needs a real base/head range) PLUS a tracked file at
        `dirty_relpath` under one of the watched directories, committed
        clean at HEAD and then modified WITHOUT committing."""
        init_repo(root)
        (root / "production.py").write_text(PROD_BUGGY)
        write_test(root, PLACEHOLDER_TEST)
        watched = root / dirty_relpath
        watched.parent.mkdir(parents=True, exist_ok=True)
        watched.write_text("# committed, clean\n")
        base = commit_all(root, "base")
        (root / "production.py").write_text(PROD_FIXED)
        write_test(root, DISCRIMINATING_TEST)
        head = commit_all(root, "fix + a real, discriminating test")
        # The dirty part: a tracked-file change that is never committed.
        watched.write_text("# uncommitted change\n")
        return base, head

    def test_refuses_on_an_uncommitted_capsule_change(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            base, head = self._fixture_with_dirty_path(root, "capsule/foo.py")

            p = subprocess.run(
                [sys.executable, str(Path(__file__).resolve().parent / "mutation_check.py"),
                 "--base", base, "--head", head, "--repo", str(root)],
                capture_output=True, text=True)
            self.assertEqual(p.returncode, 75, p.stdout + p.stderr)
            self.assertIn("environment error", p.stdout)
            self.assertIn("capsule/foo.py", p.stdout)
            self.assertNotIn("| test file |", p.stdout)

    def test_refuses_on_an_uncommitted_maintenance_change(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            base, head = self._fixture_with_dirty_path(root, "maintenance/helper.py")

            p = subprocess.run(
                [sys.executable, str(Path(__file__).resolve().parent / "mutation_check.py"),
                 "--base", base, "--head", head, "--repo", str(root)],
                capture_output=True, text=True)
            self.assertEqual(p.returncode, 75, p.stdout + p.stderr)
            self.assertIn("environment error", p.stdout)
            self.assertIn("maintenance/helper.py", p.stdout)
            self.assertNotIn("| test file |", p.stdout)

    def test_allow_dirty_bypasses_the_refusal_with_a_loud_warning(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            base, head = self._fixture_with_dirty_path(root, "capsule/foo.py")

            p = subprocess.run(
                [sys.executable, str(Path(__file__).resolve().parent / "mutation_check.py"),
                 "--base", base, "--head", head, "--repo", str(root), "--allow-dirty"],
                capture_output=True, text=True)
            self.assertNotEqual(p.returncode, 75, p.stdout + p.stderr)
            self.assertIn("WARN", p.stderr)
            self.assertIn("capsule/foo.py", p.stderr)
            self.assertIn("| test file |", p.stdout)

    def test_a_clean_tree_is_unaffected(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            write_empty_baseline(root)
            (root / "production.py").write_text(PROD_BUGGY)
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base")
            (root / "production.py").write_text(PROD_FIXED)
            write_test(root, DISCRIMINATING_TEST)
            head = commit_all(root, "fix + a real, discriminating test")

            p = subprocess.run(
                [sys.executable, str(Path(__file__).resolve().parent / "mutation_check.py"),
                 "--base", base, "--head", head, "--repo", str(root)],
                capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertIn("| test file |", p.stdout)


class SizeGateWiring(unittest.TestCase):
    """Row 509 fix round 1, I3: `main()` must use `size_check.check()`'s actual return
    value, on the early "no test files changed" note path AND the full table path - a
    dropped `size_ok` (M5) or the call replaced by a `(True, '')` stub (M5b) both leave
    `size_check.check` un-consulted or its verdict discarded; this fails either way."""

    def _note_path_fixture(self, root):
        init_repo(root)
        (root / "production.py").write_text(PROD_BUGGY)
        base = commit_all(root, "base")
        (root / "readme.md").write_text("docs only\n")
        head = commit_all(root, "docs only, no test touched")
        return base, head

    def test_size_failure_fails_main_even_on_the_note_path(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            base, head = self._note_path_fixture(root)
            with unittest.mock.patch.object(mc.size_check, "check",
                                             return_value=(False, "size_check: fake failure xyz")) as m:
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = mc.main(["--base", base, "--head", head, "--repo", str(root)])
            m.assert_called_once_with(str(root), base, head)
        self.assertEqual(rc, 1)
        self.assertIn("fake failure xyz", buf.getvalue())

    def test_size_pass_leaves_the_note_path_green(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            base, head = self._note_path_fixture(root)
            with unittest.mock.patch.object(mc.size_check, "check", return_value=(True, "size_check: ok")):
                rc = mc.main(["--base", base, "--head", head, "--repo", str(root)])
        self.assertEqual(rc, 0)

    def test_size_failure_fails_main_on_the_full_table_path(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_BUGGY)
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base: buggy add()")
            (root / "production.py").write_text(PROD_FIXED)
            write_test(root, DISCRIMINATING_TEST)
            head = commit_all(root, "fix add(), cover it")
            with unittest.mock.patch.object(mc.size_check, "check",
                                             return_value=(False, "size_check: fake failure xyz")):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = mc.main(["--base", base, "--head", head, "--repo", str(root)])
        self.assertEqual(rc, 1)
        self.assertIn("fake failure xyz", buf.getvalue())
        self.assertIn("| test file |", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
