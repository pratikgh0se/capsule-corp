"""Row 526 (card t_1758e0bd item 6): the revert phase of tests/mutation_check.py must follow an alias.

Factory-autonomy Task 10 moved logic out of capsule/unblocked.py into capsule/dispatch_gate.py and
left `dispatcher_state = dispatch_gate.state` aliases behind. A test that says
`unblocked.dispatcher_blocker(...)` never imports dispatch_gate, so the gate judged it "no production
file changed in this range is imported by this test file" (n/a) and never reverted dispatch_gate.py:
a test that passes with the production change reverted went unseen.

Oracle 1 (real CLI on a fixture repo): a test that reaches a changed file only through an alias
is reverted like any other, so a tautology fails the gate and a discriminating test passes it.
Oracle 2 (this repo's own files, no fixture): the alias finder sees dispatch_gate.py behind
unblocked.py for the real test_unblocked.py. Z1: contracts/intent/mutation_alias.jsonl is replayed
through the same CLI."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutation_alias as alias  # noqa: E402
from test_mutation_check import commit_all, init_repo, write_empty_baseline, write_test  # noqa: E402
from test_mutation_info import run_gate  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
INTENT = REPO / "contracts" / "intent" / "mutation_alias.jsonl"

GATE_BUGGY = "def check(a, b):\n    return a - b\n"
GATE_FIXED = "def check(a, b):\n    return a + b\n"
FACADE_ASSIGN = "import gate\n\ncheck = gate.check\nOTHER = 1\n"
FACADE_FROM = "from gate import check\n"
PLACEHOLDER = "import unittest\nimport facade\n\n\nclass Facade(unittest.TestCase):\n    def test_placeholder(self):\n        self.assertTrue(facade.OTHER)\n"
DISCRIMINATING = (
    "import unittest\nimport facade\n\n\nclass Facade(unittest.TestCase):\n"
    "    def test_check_adds(self):\n        self.assertEqual(facade.check(2, 3), 5)\n")
TAUTOLOGY = (
    "import unittest\nimport facade\n\n\nclass Facade(unittest.TestCase):\n"
    "    def test_check_is_callable(self):\n        self.assertTrue(callable(facade.check))\n")
NOT_THE_ALIAS = (
    "import unittest\nimport facade\n\n\nclass Facade(unittest.TestCase):\n"
    "    def test_other_is_set(self):\n        self.assertEqual(facade.OTHER, 1)\n")


def make_range(root, facade, test_text):
    init_repo(root)
    write_empty_baseline(root)
    (root / "gate.py").write_text(GATE_BUGGY)
    (root / "facade.py").write_text(facade)
    write_test(root, PLACEHOLDER)
    base = commit_all(root, "base")
    (root / "gate.py").write_text(GATE_FIXED)
    write_test(root, test_text)
    return base, commit_all(root, "head")


class RevertFollowsAnAlias(unittest.TestCase):
    def run_case(self, facade, test_text):
        with tempfile.TemporaryDirectory(prefix="mc-alias-") as tmp:
            root = Path(tmp)
            base, head = make_range(root, facade, test_text)
            return run_gate(root, base, head)

    def test_a_tautology_reaching_the_change_through_an_assigned_alias_fails_the_gate(self):
        rc, out = self.run_case(FACADE_ASSIGN, TAUTOLOGY)
        self.assertEqual(rc, 1, out)
        self.assertIn("did not fail (BAD)", out)

    def test_a_tautology_reaching_the_change_through_a_from_import_fails_the_gate(self):
        rc, out = self.run_case(FACADE_FROM, TAUTOLOGY)
        self.assertEqual(rc, 1, out)
        self.assertIn("did not fail (BAD)", out)

    def test_a_discriminating_test_through_an_alias_is_reverted_and_passes(self):
        rc, out = self.run_case(FACADE_ASSIGN, DISCRIMINATING)
        self.assertEqual(rc, 0, out)
        self.assertNotIn("n/a (no production change)", out)

    def test_a_test_that_never_uses_the_alias_stays_not_applicable(self):
        rc, out = self.run_case(FACADE_ASSIGN, NOT_THE_ALIAS)
        self.assertEqual(rc, 0, out)
        self.assertIn("n/a (no production change)", out)


class RealFiles(unittest.TestCase):
    """Oracle 2: the repo's own alias, no fixture."""

    def reaches(self, test_path, prod_file):
        import mutation_check as mc
        src = (REPO / test_path).read_text()
        names = mc._import_names(src)
        return alias.reaches(names, src, prod_file, lambda p: (REPO / p).read_text() if (REPO / p).is_file() else None,
                             mc._import_names, mc.references_prod_file)

    def test_test_unblocked_reaches_dispatch_gate_through_unblocked(self):
        self.assertTrue(self.reaches("capsule/tests/test_unblocked.py", "capsule/dispatch_gate.py"))

    def test_test_unblocked_does_not_reach_an_unrelated_module_through_unblocked(self):
        self.assertFalse(self.reaches("capsule/tests/test_unblocked.py", "capsule/amend.py"))


class ReadsAreMemoised(unittest.TestCase):
    """Fable finding 1 (t_f842fa87): one `git show` per (rev, path), however many prod files and tests ask."""

    def test_the_facade_is_read_once_across_prod_files_and_tests(self):
        import mutation_check as mc
        shows = []
        real_sh = mc.sh

        def counting_sh(argv, *a, **k):
            if argv[:2] == ["git", "show"]:
                shows.append(argv[2])
            return real_sh(argv, *a, **k)

        with tempfile.TemporaryDirectory(prefix="mc-alias-") as tmp:
            root = Path(tmp)
            _, head = make_range(root, FACADE_ASSIGN, TAUTOLOGY)
            mc.sh = counting_sh
            try:
                for _ in range(2):
                    src = TAUTOLOGY
                    imports = mc._import_names(src)
                    for pf in ("gate.py", "facade.py", "other.py"):
                        alias.reaches(imports, src, pf, alias.reader(root, head, mc.show_file),
                                      mc._import_names, mc.references_prod_file)
            finally:
                mc.sh = real_sh
        self.assertEqual(sorted(shows), sorted(set(shows)), shows)
        self.assertEqual(shows.count(f"{head}:facade.py"), 1, shows)


class IntentExamples(unittest.TestCase):
    """Zenkai Z1: contracts/intent/mutation_alias.jsonl replayed through the real CLI."""
    FIXTURES = {
        "assigned-alias": (FACADE_ASSIGN, TAUTOLOGY),
        "from-import-alias": (FACADE_FROM, DISCRIMINATING),
        "alias-never-used": (FACADE_ASSIGN, NOT_THE_ALIAS),
    }

    def test_every_example_matches_its_oracle(self):
        rows = [json.loads(line) for line in INTENT.read_text().splitlines() if line.strip()]
        self.assertTrue(1 <= len(rows) <= 3)
        for row in rows:
            doc = row["given"]["document"]
            with tempfile.TemporaryDirectory(prefix="mc-alias-") as tmp:
                root = Path(tmp)
                base, head = make_range(root, *self.FIXTURES[doc["range"]])
                rc, out = run_gate(root, base, head)
                self.assertEqual(rc == 0, row["expect"]["ok"], (row["id"], out))
                self.assertEqual("n/a (no production change)" in out, row["expect"]["na"], (row["id"], out))


if __name__ == "__main__":
    unittest.main()
