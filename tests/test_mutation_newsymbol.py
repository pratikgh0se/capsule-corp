"""Row 459 fixtures for `mutation_newsymbol`: a pure-addition symbol explains a
reverted run's failures only when an independent failure exists as well."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutation_check as mc  # noqa: E402
from test_mutation_check import PLACEHOLDER_TEST, commit_all, init_repo, write_test  # noqa: E402


class PureAdditionWithAnIndependentFailureScoresGood(unittest.TestCase):
    """Row 459 (card t_a995978e): the keep-and-rerun chain's own false-BAD,
    reproduced first on real ranges (capsule/fa-task1 43614b1's
    `config.autonomy`, capsule/human-assign 97e37a9's `parse_builds`/
    `resolve_assignee` - both a brand-new, no-callers-at-head symbol AND a
    genuine, unrelated production bug reached by a DIFFERENT changed test in
    the SAME file). The old unconditional keep-the-module-and-rerun step
    erased that second test's own honest failure along with the missing
    symbol - `production.py kept at head for import of NEW_THING` used to
    read `did not fail (BAD)` here even though the reverted run itself had
    already failed for two independent reasons. `addition_explains_failures`
    plus the (c) weaken oracle now let the CURRENT reverted run stand as its
    own proof instead."""

    PRODUCTION_BASE = "DATA = [1]\n\n\ndef risky():\n    return DATA[1]\n"
    PRODUCTION_HEAD = "DATA = [1, 2]\n\n\ndef risky():\n    return DATA[1]\n\n\nNEW_THING = True\n"
    HONEST_TEST = ("import unittest\nimport production\n\n\n"
                   "class Feature(unittest.TestCase):\n"
                   "    def test_new_thing_exists(self):\n"
                   "        self.assertTrue(production.NEW_THING)\n\n"
                   "    def test_risky_still_works(self):\n"
                   "        self.assertEqual(production.risky(), 2)\n")

    def test_pure_addition_accessor_with_an_honest_test_scores_good(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(self.PRODUCTION_BASE)
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base: DATA has one element (risky() out of range), no NEW_THING")

            (root / "production.py").write_text(self.PRODUCTION_HEAD)
            write_test(root, self.HONEST_TEST)
            head = commit_all(root, "fix DATA (real bug fix); add NEW_THING (pure addition); cover both, honestly")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertTrue(ok, rows)
            f, names, rev_cell, weak_cell, flags_cell = rows[0]
            self.assertIn("FAIL (good; new symbol NEW_THING)", rev_cell)
            self.assertIn("FAIL (good)", weak_cell)

    def test_pure_addition_with_no_weaken_oracle_still_fails_the_gate(self):
        """Same production module and the same two independent, reverted-run
        failures as above - but neither test's assertion is one
        `apply_weakening` recognizes (`assertIsNotNone`, never `assertEqual`/
        `assertTrue`/bare `==`), so (c) has nothing to flip and reports
        `n/a`, never `FAIL (good)`. Row 459's ruling requires a GOOD (c) as
        well as (a)/(b) - without it, this must still fall through to the
        old keep-and-rerun path and read exactly as before."""
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(self.PRODUCTION_BASE)
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base: DATA has one element (risky() out of range), no NEW_THING")

            (root / "production.py").write_text(self.PRODUCTION_HEAD)
            write_test(
                root,
                "import unittest\nimport production\n\n\n"
                "class Feature(unittest.TestCase):\n"
                "    def test_new_thing_exists(self):\n"
                "        self.assertIsNotNone(production.NEW_THING)\n\n"
                "    def test_risky_still_works(self):\n"
                "        self.assertIsNotNone(production.risky())\n")
            head = commit_all(root, "fix DATA (real bug fix); add NEW_THING (pure addition); cover both, weakly")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertFalse(ok, rows)
            f, names, rev_cell, weak_cell, flags_cell = rows[0]
            self.assertNotIn("new symbol", rev_cell)
            self.assertIn("did not fail (BAD)", rev_cell)
            self.assertIn("production.py kept at head for import of NEW_THING", rev_cell)


class TwoNewSymbolsInOnePureAdditionModuleBothSeedKnown(unittest.TestCase):
    """Card t_a7491776: the old `known = {s for _, s in kept} | {symbol}` seeded `known`
    with only the ONE symbol the regex happened to match - a SECOND brand-new top-level
    name in the same pure-addition module (the real repro: `paths.resolve_config` plus a
    second addition) made its own result block read as an "unknown symbol",
    `addition_explains_failures` aborted outright on it even though a genuinely
    independent failure (`test_risky_still_works`, unrelated to either addition) was also
    in the same run, and the gate fell through to keep-and-rerun - which then SUCCEEDS
    (the whole module, both additions included, is back), reading a plain pass: "did not
    fail (BAD)" for a file that was, in fact, discriminating."""

    PRODUCTION_BASE = "DATA = [1]\n\n\ndef risky():\n    return DATA[1]\n"
    PRODUCTION_HEAD = ("DATA = [1, 2]\n\n\ndef risky():\n    return DATA[1]\n\n\n"
                        "NEW_THING = True\nSECOND_THING = True\n")
    HONEST_TEST = ("import unittest\nimport production\n\n\n"
                   "class Feature(unittest.TestCase):\n"
                   "    def test_new_thing_exists(self):\n"
                   "        self.assertTrue(production.NEW_THING)\n\n"
                   "    def test_second_thing_exists(self):\n"
                   "        self.assertTrue(production.SECOND_THING)\n\n"
                   "    def test_risky_still_works(self):\n"
                   "        self.assertEqual(production.risky(), 2)\n")

    def test_two_brand_new_symbols_plus_an_honest_independent_test_scores_good(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(self.PRODUCTION_BASE)
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base: DATA has one element (risky() out of range), no additions")

            (root / "production.py").write_text(self.PRODUCTION_HEAD)
            write_test(root, self.HONEST_TEST)
            head = commit_all(root, "fix DATA (real bug fix); add NEW_THING and SECOND_THING; cover all three")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertTrue(ok, rows)
            f, names, rev_cell, weak_cell, flags_cell = rows[0]
            self.assertIn("FAIL (good; new symbol", rev_cell)
            self.assertIn("NEW_THING", rev_cell)
            self.assertIn("SECOND_THING", rev_cell)


class ExistenceOnlyAttacksAreInfoNeverGood(unittest.TestCase):
    """Row 568: with a discriminating (c) flip these are an `info:` cell, exit 0 - never
    `FAIL (good)`; the reviewer carries them. Row 459 round 2 (Opus review, gate-newsymbol-review.md finding 1):
    every test in these fixtures only proves `NEW_THING` EXISTS - no failure
    is independent of the addition, however differently shaped its
    exception. Before `addition_explains_failures` disqualified a block
    whose OWN test body still mentions the symbol, each of these scored a
    false `FAIL (good; new symbol NEW_THING)`. `scratchpad/gns-attack.py`
    and `gns-attack2.py` (the review's own repro) name these A1/A2/A6/A7/A8."""

    def _check(self, prod_base, prod_head, test_src, head_extra=None):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(prod_base)
            write_test(root, PLACEHOLDER_TEST.replace("from production import add", "import production")
                       .replace("callable(add)", "True"))
            base = commit_all(root, "base")
            (root / "production.py").write_text(prod_head)
            for rel, src in (head_extra or {}).items():
                (root / rel).write_text(src)
            write_test(root, test_src)
            head = commit_all(root, "head")
            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertTrue(ok, rows)
            rev_cell = rows[0][2]
            self.assertNotIn("new symbol", rev_cell)
            self.assertIn("info: pure addition", rev_cell)
            return rev_cell

    B = "def f():\n    return 1\n"
    H = "def f():\n    return 1\n\n\nNEW_THING = object()\n"

    def test_a1_hasattr_fail_plus_attribute_error(self):
        self._check(self.B, self.H,
                     "import unittest\nimport production\n\n\nclass T(unittest.TestCase):\n"
                     "    def test_a_exists(self):\n        self.assertTrue(hasattr(production, 'NEW_THING'))\n\n"
                     "    def test_b_touch(self):\n        self.assertTrue(production.NEW_THING is not None)\n")

    def test_a2_getattr_default_then_call(self):
        h = "def f():\n    return 1\n\n\ndef NEW_THING():\n    return 5\n"
        self._check(self.B, h,
                     "import unittest\nimport production\n\n\nclass T(unittest.TestCase):\n"
                     "    def test_a(self):\n        self.assertTrue(getattr(production, 'NEW_THING', None)() is not None)\n\n"
                     "    def test_b(self):\n        self.assertEqual(production.NEW_THING(), production.NEW_THING())\n")

    def test_a6_mock_patch_target_string(self):
        h = "def f():\n    return 1\n\n\ndef NEW_THING():\n    return 1\n"
        self._check(self.B, h,
                     "import unittest\nfrom unittest import mock\nimport production\n\n\nclass T(unittest.TestCase):\n"
                     "    def test_a_patched(self):\n        with mock.patch('production.NEW_THING', return_value=0):\n"
                     "            self.assertEqual(production.NEW_THING(), 0)\n\n"
                     "    def test_b_direct(self):\n        self.assertTrue(production.NEW_THING())\n")

    def test_a7_new_method_on_an_existing_class(self):
        b = "class C:\n    def f(self):\n        return 1\n"
        h = "class C:\n    def f(self):\n        return 1\n\n    def g(self):\n        return 2\n\n\nNEW_THING = C\n"
        self._check(b, h,
                     "import unittest\nimport production\n\n\nclass T(unittest.TestCase):\n"
                     "    def test_a_method(self):\n        self.assertTrue(production.C().g())\n\n"
                     "    def test_b_alias(self):\n        self.assertTrue(production.NEW_THING)\n")

    def test_a8_two_subtests_in_one_test(self):
        h = "def f():\n    return 1\n\n\ndef NEW_THING():\n    return 1\n"
        self._check(self.B, h,
                     "import unittest\nimport production\n\n\nclass T(unittest.TestCase):\n"
                     "    def test_a(self):\n"
                     "        with self.subTest('attr'):\n            self.assertTrue(production.NEW_THING())\n"
                     "        with self.subTest('has'):\n            self.assertTrue(hasattr(production, 'NEW_THING'))\n")

    def test_r1_helper_swallows_absence_test_body_never_names_it(self):
        """Round 3 (Opus re-review, gate-newsymbol-review.md, finding 1, R1):
        a module-level helper `_mk()` reaches the symbol through a
        `getattr(...)` default and swallows the absence into `None` - the
        FAILING test's own body never names `NEW_THING` at all. Before the
        `outside` widening, only the test's own AST was checked, so this
        scored a false good."""
        self._check(self.B, self.H,
                     "import unittest\nimport production\n\n\n"
                     "def _mk():\n    return getattr(production, 'NEW_THING', None)\n\n\n"
                     "class T(unittest.TestCase):\n"
                     "    def test_a(self):\n        self.assertIsNotNone(_mk())\n\n"
                     "    def test_z_touch(self):\n        self.assertTrue(production.NEW_THING is not None)\n")

    def test_r5_setup_touches_it_test_body_does_not(self):
        """Round 3, R5: `setUp` reaches the symbol through a `getattr(...)`
        default; the FAILING test's own body only asserts on `self.thing`,
        never naming `NEW_THING`."""
        self._check(self.B, self.H,
                     "import unittest\nimport production\n\n\nclass T(unittest.TestCase):\n"
                     "    def setUp(self):\n        self.thing = getattr(production, 'NEW_THING', None)\n\n"
                     "    def test_a(self):\n        self.assertIsNotNone(self.thing)\n\n"
                     "    def test_z_touch(self):\n        self.assertTrue(production.NEW_THING is not None)\n")

    def test_d1_mock_patch_decorator_dotted_target(self):
        """D1: the only mention is the dotted `'production.NEW_THING'` patch
        target; mock.patch's `does not have the attribute` error is not
        attribute-shaped, so the block must not count as independent."""
        h = "def f():\n    return 1\n\n\ndef NEW_THING():\n    return 1\n"
        self._check(self.B, h,
                     "import unittest\nfrom unittest import mock\nimport production\n\n\nclass T(unittest.TestCase):\n"
                     "    @mock.patch('production.NEW_THING', return_value=0)\n"
                     "    def test_a(self, m):\n        self.assertEqual(production.f(), 1)\n\n"
                     "    def test_z_touch(self):\n        self.assertTrue(production.NEW_THING())\n")

    def test_s6_shared_patch_helper_in_never_reverted_fakes(self):
        """S6: a shared `mock.patch` helper lives in `tests/fakes.py`, which
        stays at head during revert; the test file never names the symbol."""
        h = "def f():\n    return 1\n\n\ndef NEW_THING():\n    return 1\n"
        self._check(self.B, h,
                     "import unittest\nimport production\nfrom fakes import patched\n\n\nclass T(unittest.TestCase):\n"
                     "    def test_a(self):\n        with patched():\n            self.assertEqual(production.f(), 1)\n\n"
                     "    def test_z_touch(self):\n        self.assertTrue(production.NEW_THING())\n",
                     head_extra={"tests/fakes.py": "from unittest import mock\n\n\ndef patched():\n"
                                 "    return mock.patch('production.NEW_THING', return_value=0)\n"})


class UnknownSymbolErrorAbortsRatherThanSkips(unittest.TestCase):
    """Row 459 round 2, finding 3: an attribute/import-shaped error naming a
    symbol NOT in `known_symbols` must abort `addition_explains_failures`
    outright (`return False`), not merely be skipped (`continue`) - a skip
    would let an unrelated, genuinely independent failure elsewhere in the
    same run still satisfy `saw_other` and wrongly score good. `NEW_LOOP` is
    for-bound (round 1's E3r shape - `_top_level_names` never tracks it, so
    it can never become `known_symbols` itself); `risky()` is a real,
    unrelated production bug (`DATA` too short at base)."""

    def test_an_unresolvable_second_symbol_next_to_a_real_bug_is_info_never_good(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text("DATA = [1]\n\n\ndef risky():\n    return DATA[1]\n")
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base: DATA too short, no NEW_THING/NEW_LOOP")

            (root / "production.py").write_text(
                "DATA = [1, 2]\n\n\ndef risky():\n    return DATA[1]\n\n\n"
                "for NEW_LOOP in [True]:\n    pass\nNEW_THING = True\n")
            write_test(
                root,
                "import unittest\nimport production\n\n\nclass T(unittest.TestCase):\n"
                "    def test_new_thing(self):\n        self.assertTrue(production.NEW_THING)\n\n"
                "    def test_new_loop(self):\n        self.assertTrue(production.NEW_LOOP)\n\n"
                "    def test_risky(self):\n        self.assertEqual(production.risky(), 2)\n")
            head = commit_all(root, "fix DATA (real bug fix); add NEW_THING; add untrackable NEW_LOOP")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertTrue(ok, rows)
            rev_cell = rows[0][2]
            self.assertNotIn("new symbol", rev_cell)
            self.assertIn("info: pure addition", rev_cell)


class UnparseableSupportFileMentionsNothing(unittest.TestCase):
    """`gns-attack5.py` Z1: a changed, deliberately invalid parser fixture under
    `tests/fixtures/` must not crash the gate - it cannot be imported, so it
    mentions nothing, and the honest file still scores the new-symbol good."""

    def test_invalid_fixture_file_next_to_an_honest_addition_scores_good(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PureAdditionWithAnIndependentFailureScoresGood.PRODUCTION_BASE)
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base")
            (root / "production.py").write_text(PureAdditionWithAnIndependentFailureScoresGood.PRODUCTION_HEAD)
            (root / "tests" / "fixtures").mkdir(parents=True, exist_ok=True)
            (root / "tests" / "fixtures" / "broken.py").write_text("def oops(:\n    pass\n")
            write_test(root, PureAdditionWithAnIndependentFailureScoresGood.HONEST_TEST)
            head = commit_all(root, "head: honest addition plus an invalid parser fixture")
            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertTrue(ok, rows)
            self.assertIn("FAIL (good; new symbol NEW_THING)", rows[0][2])


class OutsideMentionsComeOnlyFromReachableSupportFiles(unittest.TestCase):
    """A changed support file the test never imports (directly or through another support
    file) must not make `outside` mention a known new symbol and so veto the
    addition-explained verdict; one the test DOES import still counts."""

    BASE, HEAD = (PureAdditionWithAnIndependentFailureScoresGood.PRODUCTION_BASE,
                  PureAdditionWithAnIndependentFailureScoresGood.PRODUCTION_HEAD)
    HONEST = PureAdditionWithAnIndependentFailureScoresGood.HONEST_TEST

    def _rev_cell(self, test_text):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(self.BASE)
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base")
            (root / "production.py").write_text(self.HEAD)
            (root / "tests" / "fakes.py").write_text("NEW_THING = 1\n")
            write_test(root, test_text)
            head = commit_all(root, "head: addition plus a support file naming it")
            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            return rows[0][2]

    def test_unimported_support_file_naming_the_symbol_does_not_veto(self):
        self.assertIn("FAIL (good; new symbol NEW_THING)", self._rev_cell(self.HONEST))

    def test_imported_support_file_naming_the_symbol_still_counts(self):
        self.assertNotIn("new symbol", self._rev_cell("import fakes\n" + self.HONEST))
