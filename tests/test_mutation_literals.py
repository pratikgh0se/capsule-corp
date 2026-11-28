"""Card t_bd08469e: Z1 intent examples and Z2 second oracle for tests/mutation_literals.py.

Oracle 1: unit checks of the string mask and `apply_weakening*` on source text.
Oracle 2: the real `check_range` on a fixture repo built fresh per test: (a) a test that embeds
test source as a string is flipped at its REAL assertion, and a real tautology carrying such a
string still fails the gate; (b) a test whose subject module is new in the range is revert-checked
by stubbing the module: a discriminating test passes, a hollow one fails, and a module that cannot
be stubbed safely stays n/a. Z1 examples are the named fixtures below (the contracts/intent jsonl
copy is a separate, protected-path step)."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutation_check as mc  # noqa: E402
import mutation_literals as lit  # noqa: E402
from test_mutation_check import (  # noqa: E402
    PLACEHOLDER_TEST, PROD_BUGGY, PROD_FIXED, TAUTOLOGICAL_TEST, commit_all, init_repo, write_test)

EMBEDDED = (
    "import unittest\nfrom production import add\n\n\n"
    "class Add(unittest.TestCase):\n"
    "    def test_add(self):\n"
    '        src = "def test_x(self):\\n    self.assertEqual(1, 1)\\n"\n'
    '        doc = """\n        self.assertTrue(x)\n        assert a == b\n        """\n'
    "        self.assertEqual(add(2, 3), 5)\n")


class MaskStrings(unittest.TestCase):
    def test_mask_blanks_string_contents_keeps_length_and_newlines(self):
        text = 'x = "assertEqual("\ny = f"{a} assertTrue("\nz = 1  # assertEqual(\n'
        masked = lit.mask_strings(text)
        self.assertEqual(len(masked), len(text))
        self.assertEqual(masked.count("\n"), text.count("\n"))
        self.assertNotIn("assertTrue", masked)
        self.assertIn("# assertEqual(", masked)

    def test_untokenizable_text_is_returned_unchanged(self):
        self.assertEqual(lit.mask_strings('x = """unterminated\n'), 'x = """unterminated\n')

    def test_weakening_skips_the_embedded_source_and_hits_the_real_assertion(self):
        n = len(EMBEDDED.splitlines())
        new, what = mc.apply_weakening(EMBEDDED, [(6, n)])
        self.assertEqual(what, "assertEqual -> assertNotEqual")
        self.assertIn("self.assertNotEqual(add(2, 3), 5)", new)
        self.assertIn('self.assertEqual(1, 1)\\n"', new)
        self.assertIn("self.assertTrue(x)", new)

    def test_text_whose_only_assertions_are_in_strings_has_nothing_to_flip(self):
        text = 'def test_a(self):\n    s = "self.assertEqual(1, 2)"\n    t = """assert a == b"""\n'
        self.assertEqual(mc.apply_weakening(text, [(1, 3)]), (None, None))
        self.assertEqual(mc.apply_weakening_at(text, 1, 3, 0), (None, None))

    def test_a_real_assertion_is_still_flipped_in_every_form(self):
        cases = (("self.assertEqual(a, b)", "assertNotEqual("),
                 ("self.assertTrue(a)", "assertFalse("),
                 ("assert 'a == b' == x", "assert 'a == b' != x"))
        for line, want in cases:
            text = f"def test_a(self):\n    s = 'assertEqual('\n    {line}\n"
            new, _ = mc.apply_weakening(text, [(1, 3)])
            self.assertIn(want, new, line)
            self.assertEqual(new, mc.apply_weakening_at(text, 1, 3, 0)[0], line)

    def test_second_occurrence_skips_strings_too(self):
        text = ('def test_a(self):\n    self.assertEqual(a, 1)\n    s = "self.assertEqual("\n'
                '    self.assertEqual(b, 2)\n')
        new, _ = mc.apply_weakening_at(text, 1, 4, 1)
        self.assertIn("self.assertNotEqual(b, 2)", new)
        self.assertEqual(mc.apply_weakening_at(text, 1, 4, 2), (None, None))


def gate(prod_files, test_text, test_name="test_newmod.py", base_test=PLACEHOLDER_TEST,
         base_prod=PROD_FIXED):
    """check_range on a fixture repo: base has production.py and a placeholder test,
    head adds `prod_files`
    (name -> source) and `test_name`. Returns (ok, the row for the new test file)."""
    with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
        root = Path(tmp)
        init_repo(root)
        (root / "production.py").write_text(base_prod)
        write_test(root, base_test)
        base = commit_all(root, "base")
        for name, src in prod_files.items():
            (root / name).write_text(src)
        (root / "tests" / test_name).write_text(test_text)
        head = commit_all(root, "head")
        ok, rows, note = mc.check_range(root, base, head)
        assert note is None
        return ok, next(r for r in rows if r[0] == f"tests/{test_name}")


NEWMOD_TEST = ("import unittest\nfrom newmod import double\n\n\nclass T(unittest.TestCase):\n"
               "    def test_double(self):\n        self.assertEqual(double(3), 6)\n")
NEWMOD = "def double(x):\n    return x * 2\n"
NEWMOD_CLASS = ("class Doubler:\n    def __init__(self):\n        self.k = 2\n\n"
                "    def run(self, x):\n        def inner():\n            return self.k\n"
                "        return x * inner()\n")


class EmbeddedSourceThroughTheGate(unittest.TestCase):
    """Z1 (a): a test file that embeds test source as a string."""

    def test_discriminating_test_with_embedded_source_passes_with_a_real_flip(self):
        text = EMBEDDED.replace("test_add", "test_add_returns_the_sum")
        ok, (_, _, rev, weak, _) = gate({"production.py": PROD_FIXED}, text, "test_production.py",
                                        base_prod=PROD_BUGGY)
        self.assertTrue(ok, (rev, weak))
        self.assertEqual(weak, "FAIL (good)")

    def test_a_real_tautology_carrying_an_embedded_string_still_fails_the_gate(self):
        embedded = '        s = "self.assertEqual(add(2, 3), 5)"\n'
        text = TAUTOLOGICAL_TEST.replace("        add(2, 3)\n", embedded)
        text = text.replace("True)", "len(s) > 0)")
        ok, (_, _, rev, weak, _) = gate({"production.py": PROD_FIXED}, text, "test_production.py",
                                        base_prod=PROD_BUGGY)
        self.assertFalse(ok, (rev, weak))


class AssertOnlyInAStringThroughTheGate(unittest.TestCase):
    def test_a_test_whose_only_assert_is_inside_a_string_still_fails_the_gate(self):
        text = ("import unittest\nfrom production import add\n\n\nclass Add(unittest.TestCase):\n"
                "    def test_add(self):\n"
                '        doc = "self.assertEqual(add(2, 3), 5)"\n        add(2, 3)\n')
        ok, (_, _, rev, weak, flags) = gate(
            {"production.py": PROD_FIXED}, text, "test_production.py", base_prod=PROD_BUGGY)
        self.assertFalse(ok, (rev, weak))
        self.assertIn("no assert", flags)


class HelperCallOnlyInAString(unittest.TestCase):
    SRC = ("import unittest\n\n\nclass T(unittest.TestCase):\n"
           "    def check(self, x):\n        self.assertEqual(x, 1)\n\n"
           "    def test_real(self):\n        self.check(1)\n\n"
           '    def test_text(self):\n        doc = "self.check(1)"\n        len(doc)\n')

    def test_a_helper_call_inside_a_string_does_not_count_as_asserting(self):
        funcs = mc._all_top_level_functions(self.SRC)
        real = mc.static_flags(self.SRC, 8, 9, own_class="T", all_funcs=funcs)
        text = mc.static_flags(self.SRC, 11, 13, own_class="T", all_funcs=funcs)
        self.assertEqual(real, ["info: asserts via helper check"])
        self.assertEqual(text, ["no assert in the function body"])


class NewModuleRevertCheck(unittest.TestCase):
    """Z1 (b): the test's subject module is new in the range."""

    def test_discriminating_test_on_a_new_module_gets_a_real_revert_verdict(self):
        ok, (_, _, rev, weak, _) = gate({"newmod.py": NEWMOD}, NEWMOD_TEST)
        self.assertTrue(ok, (rev, weak))
        self.assertEqual(rev, "FAIL (good) [revert: new module stubbed: newmod.py]")

    def test_methods_are_stubbed_but_dunders_keep_the_class_constructible(self):
        test = ("import unittest\nfrom newmod import Doubler\n\n\nclass T(unittest.TestCase):\n"
                "    def test_run(self):\n        self.assertEqual(Doubler().run(3), 6)\n")
        ok, (_, _, rev, weak, _) = gate({"newmod.py": NEWMOD_CLASS}, test)
        self.assertTrue(ok, (rev, weak))
        self.assertEqual(rev, "FAIL (good) [revert: new module stubbed: newmod.py]")

    def test_a_hollow_test_on_a_new_module_fails_the_gate(self):
        hollow = ("import unittest\nimport newmod\n\n\nclass T(unittest.TestCase):\n"
                  "    def test_loads(self):\n"
                  "        self.assertTrue(newmod.__name__ == 'newmod')\n")
        ok, (_, _, rev, weak, _) = gate({"newmod.py": NEWMOD}, hollow)
        self.assertFalse(ok, (rev, weak))
        self.assertTrue(rev.startswith("did not fail (BAD)"), rev)

    def test_a_module_with_nothing_to_stub_stays_n_a(self):
        test = NEWMOD_TEST.replace("double", "K").replace("K(3), 6", "K, 6")
        ok, (_, _, rev, _, _) = gate({"newmod.py": "K = 6\n"}, test)
        self.assertTrue(ok)
        self.assertEqual(rev, "n/a (new module)")

    def test_a_module_whose_import_time_call_breaks_when_stubbed_stays_n_a(self):
        src = "def double(x):\n    return x * 2\n\n\nTABLE = {1: double(1) + 1}\n"
        ok, (_, _, rev, _, _) = gate({"newmod.py": src}, NEWMOD_TEST)
        self.assertTrue(ok)
        self.assertEqual(rev, "n/a (new module)")


if __name__ == "__main__":
    unittest.main()
