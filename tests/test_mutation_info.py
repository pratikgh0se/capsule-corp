"""Row 568 (card t_f51d9056): two mutation-gate heuristics are info-level, not hard fails.

  1. a new `@skipIf` (a conditional guard such as a root-uid check) is an `info:` static flag;
  2. when a pure addition explains every revert failure (the keep-and-rerun passes), the (b)
     cell is an `info:` finding instead of `did not fail (BAD)`.

Both are still printed for reviewers. Every other hard check stays hard (Z2 second oracle: a bare
`@skip`, `@expectedFailure` and a test that passes with production reverted still exit 1).
Everything runs the real CLI against a tiny fixture repo built fresh per test (Z1: the examples
in contracts/intent/mutation_check.jsonl are replayed below through the same CLI)."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_mutation_check import (  # noqa: E402
    PLACEHOLDER_TEST, PROD_BUGGY, PROD_FIXED, commit_all, init_repo, write_empty_baseline, write_test)

from test_mutation_check_retry import (  # noqa: E402
    LOOP_TEST, VACUOUS_LOOP_TEST, make_code_and_example_range)
from test_mutation_literals import EMBEDDED as EMBEDDED_SOURCE_TEST  # noqa: E402

GATE = Path(__file__).resolve().parent / "mutation_check.py"
INTENT = Path(__file__).resolve().parent.parent / "contracts" / "intent" / "mutation_check.jsonl"

SKIPIF_GUARD_TEST = (
    "import os\nimport unittest\nfrom production import add\n\n\n"
    "class Add(unittest.TestCase):\n"
    "    @unittest.skipIf(os.geteuid() == 0, 'root bypasses the permission check')\n"
    "    def test_add_returns_the_sum(self):\n"
    "        self.assertEqual(add(2, 3), 5)\n")
BARE_SKIP_TEST = SKIPIF_GUARD_TEST.replace(
    "@unittest.skipIf(os.geteuid() == 0, 'root bypasses the permission check')", "@unittest.skip('later')")
NEVER_FAILS_TEST = (
    "import unittest\nfrom production import add\n\n\n"
    "class Add(unittest.TestCase):\n"
    "    def test_add_is_callable(self):\n"
    "        self.assertTrue(callable(add))\n")

ADD_BASE = "DATA = [1]\n\n\ndef risky():\n    return DATA[1]\n"
ADD_HEAD = "DATA = [1, 2]\n\n\ndef risky():\n    return DATA[1]\n\n\nNEW_THING = True\n"
ADD_ONLY_TEST = (
    "import unittest\nimport production\n\n\n"
    "class Feature(unittest.TestCase):\n"
    "    def test_new_thing_exists(self):\n"
    "        self.assertTrue(production.NEW_THING)\n")
ADD_WEAK_TEST = (
    "import unittest\nimport production\n\n\n"
    "class Feature(unittest.TestCase):\n"
    "    def test_new_thing_exists(self):\n"
    "        self.assertIsNotNone(production.NEW_THING)\n\n"
    "    def test_risky_still_works(self):\n"
    "        self.assertIsNotNone(production.risky())\n")


def run_gate(root, base, head):
    p = subprocess.run([sys.executable, str(GATE), "--base", base, "--head", head, "--repo", str(root)],
                       capture_output=True, text=True, timeout=600)
    return p.returncode, p.stdout + p.stderr


def make_range(root, prod_base, prod_head, test_text, base_test=PLACEHOLDER_TEST):
    init_repo(root)
    write_empty_baseline(root)
    (root / "production.py").write_text(prod_base)
    write_test(root, base_test)
    base = commit_all(root, "base")
    (root / "production.py").write_text(prod_head)
    write_test(root, test_text)
    return base, commit_all(root, "head")


class SkipIfIsInfo(unittest.TestCase):
    def test_skipif_guard_on_a_discriminating_test_exits_0_with_an_info_line(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            base, head = make_range(root, PROD_BUGGY, PROD_FIXED, SKIPIF_GUARD_TEST)
            rc, out = run_gate(root, base, head)
            self.assertEqual(rc, 0, out)
            self.assertIn("info: " + "@" + "skipIf on a new/changed test", out)
            self.assertIn("conditional guard", out)  # the reason is printed, not just the label

    def test_bare_skip_on_a_new_test_still_exits_1(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            base, head = make_range(root, PROD_BUGGY, PROD_FIXED, BARE_SKIP_TEST)
            rc, out = run_gate(root, base, head)
            self.assertEqual(rc, 1, out)
            self.assertIn("@" + "skip on a new/changed test", out)


class PureAdditionIsInfo(unittest.TestCase):
    def test_pure_addition_explaining_every_revert_failure_exits_0_with_an_info_line(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            base, head = make_range(root, ADD_BASE, ADD_HEAD, ADD_ONLY_TEST)
            rc, out = run_gate(root, base, head)
            self.assertEqual(rc, 0, out)
            self.assertIn("info: pure addition explains every revert failure", out)
            self.assertIn("production.py kept at head for import of NEW_THING", out)

    def test_pure_addition_with_no_weaken_oracle_still_exits_1(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            base, head = make_range(root, ADD_BASE, ADD_HEAD, ADD_WEAK_TEST)
            rc, out = run_gate(root, base, head)
            self.assertEqual(rc, 1, out)
            self.assertIn("did not fail (BAD)", out)

    def test_a_test_that_passes_with_production_reverted_still_exits_1(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            base, head = make_range(root, PROD_BUGGY, PROD_FIXED, NEVER_FAILS_TEST)
            rc, out = run_gate(root, base, head)
            self.assertEqual(rc, 1, out)
            self.assertIn("did not fail (BAD)", out)


ALERT_RULES = "observability/grafana/provisioning/alerting/rules.yaml"
ALERT_RULE_TEST = (
    "import unittest\nfrom pathlib import Path\n\n"
    "ROOT = Path(__file__).resolve().parents[1]\n"
    "RULES = ROOT / 'observability' / 'grafana' / 'provisioning' / 'alerting'\n\n\n"
    "class Rule(unittest.TestCase):\n"
    "    def test_rule_is_unowned_idle(self):\n"
    "        self.assertIn('unowned_idle', (RULES / 'rules.yaml').read_text())\n")


def make_alert_rules_range(root):
    """A rule-shape test judged by reverting rules.yaml (the test's only data read)."""
    init_repo(root)
    write_empty_baseline(root)
    (root / "production.py").write_text(PROD_FIXED)
    (root / "observability" / "grafana" / "provisioning" / "alerting").mkdir(parents=True)
    (root / ALERT_RULES).write_text("groups: []\n")
    write_test(root, PLACEHOLDER_TEST)
    base = commit_all(root, "base: rules.yaml has no unowned_idle rule")
    (root / ALERT_RULES).write_text("groups:\n  - uid: unowned_idle\n")
    write_test(root, ALERT_RULE_TEST)
    return base, commit_all(root, "add the unowned_idle rule, with a test for it")

PRICES = "maintenance/prices.json"
PRICES_TEST = (
    "import json\nimport unittest\nfrom pathlib import Path\n\n"
    "ROOT = Path(__file__).resolve().parents[1]\n\n\n"
    "class Prices(unittest.TestCase):\n"
    "    def test_the_price_is_two(self):\n"
    "        doc = json.loads((ROOT / 'maintenance' / 'prices.json').read_text())\n"
    "        self.assertEqual(doc['price'], 2)\n")
PRICES_WITH_PROD_TEST = PRICES_TEST.replace(
    "import json\n", "import json\nfrom production import add\n")


def make_prices_range(root, data_changes):
    """W365: a data-only change plus a test pinned to the new value, or the same test with
    the data unchanged while a production file changes (so the test is about it)."""
    init_repo(root)
    write_empty_baseline(root)
    (root / "maintenance").mkdir()
    (root / PRICES).write_text('{"price": 1}\n' if data_changes else '{"price": 2}\n')
    (root / "production.py").write_text(PROD_BUGGY)
    write_test(root, PLACEHOLDER_TEST)
    base = commit_all(root, "base")
    if data_changes:
        (root / PRICES).write_text('{"price": 2}\n')
        write_test(root, PRICES_TEST)
    else:
        (root / "production.py").write_text(PROD_FIXED)
        write_test(root, PRICES_WITH_PROD_TEST)
    return base, commit_all(root, "head")


STRING_ONLY_ASSERT_TEST = (
    "import unittest\nfrom production import add\n\n\nclass Add(unittest.TestCase):\n"
    "    def test_add(self):\n"
    '        doc = "self.assertEqual(add(2, 3), 5)"\n'
    "        add(2, 3)\n")
NEWMOD_HOLLOW_TEST = (
    "import unittest\nimport newmod\n\n\nclass T(unittest.TestCase):\n"
    "    def test_f(self):\n        self.assertTrue(callable(newmod.f))\n")


def make_new_module_hollow_range(root):
    """t_bd08469e: a test on a new module that still passes with its functions stubbed."""
    init_repo(root)
    write_empty_baseline(root)
    (root / "production.py").write_text(PROD_FIXED)
    write_test(root, PLACEHOLDER_TEST)
    base = commit_all(root, "base: no newmod yet")
    (root / "newmod.py").write_text("def f():\n    return 1\n")
    (root / "tests" / "test_newmod.py").write_text(NEWMOD_HOLLOW_TEST)
    return base, commit_all(root, "add newmod.py with a test that survives stubbing")


class IntentExamples(unittest.TestCase):
    """Zenkai Z1: contracts/intent/mutation_check.jsonl replayed through the real CLI."""

    FIXTURES = {
        "skipif-guard": (PROD_BUGGY, PROD_FIXED, SKIPIF_GUARD_TEST),
        "pure-addition": (ADD_BASE, ADD_HEAD, ADD_ONLY_TEST),
        "bare-skip": (PROD_BUGGY, PROD_FIXED, BARE_SKIP_TEST),
        "alert-rules-yaml": make_alert_rules_range,
        "embedded-source-string": (PROD_BUGGY, PROD_FIXED, EMBEDDED_SOURCE_TEST),
        "new-module-hollow": make_new_module_hollow_range,
        "string-only-assert": (PROD_BUGGY, PROD_FIXED, STRING_ONLY_ASSERT_TEST),
        "data-only-change": lambda root: make_prices_range(root, True),
        "data-unchanged": lambda root: make_prices_range(root, False),
        "code-and-example": lambda root: make_code_and_example_range(root, LOOP_TEST),
        "code-and-example-vacuous-loop":
            lambda root: make_code_and_example_range(root, VACUOUS_LOOP_TEST),
    }

    def test_every_example_matches_its_oracle(self):
        rows = [json.loads(line) for line in INTENT.read_text().splitlines() if line.strip()]
        self.assertGreaterEqual(len(rows), 1)
        for row in rows:
            self.assertIn(row["given"]["document"]["range"], self.FIXTURES,
                          "new intent example: add a fixture for its range to FIXTURES")
        for row in rows:
            doc = row["given"]["document"]
            with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
                root = Path(tmp)
                fixture = self.FIXTURES[doc["range"]]
                base, head = fixture(root) if callable(fixture) else make_range(root, *fixture)
                rc, out = run_gate(root, base, head)
                self.assertEqual(rc == 0, row["expect"]["ok"], (row["id"], out))
                self.assertEqual("info:" in out, row["expect"]["info"], (row["id"], out))


if __name__ == "__main__":
    unittest.main()
