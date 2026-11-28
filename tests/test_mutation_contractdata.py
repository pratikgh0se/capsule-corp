"""Card t_3ff01dd1 fixtures for `mutation_contractdata`, and the end-to-end gate
regressions it fixes in `mutation_check.check_range`: `contracts/*.md` and
`contracts/schemas/*.json` were never in the revert set (so no contract or schema pin
could ever be judged). Review round 1, C1: the first cut also made
`contracts/intent/*.jsonl`'s own reference check strictly narrower than main's, which is
never allowed - `IntentReferenceCheckStaysAtLeastAsStrictAsMain` below reproduces the
reviewer's two probes end to end."""
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutation_check as mc  # noqa: E402
import mutation_contractdata as cd  # noqa: E402
from test_mutation_check import PLACEHOLDER_TEST, commit_all, init_repo, write_test  # noqa: E402

PROD_UNCHANGED = "def add(a, b):\n    return a + b\n"

CONTRACTS_MODULE = (
    "from pathlib import Path\n\n\n"
    "def render(role):\n"
    "    return (Path(__file__).parent / 'contracts' / f'{role}.md').read_text()\n"
)

SCHEMAS_MODULE = (
    "import json\nfrom pathlib import Path\n\n\n"
    "def load():\n"
    "    out = {}\n"
    "    for p in (Path(__file__).parent / 'contracts' / 'schemas').glob('*.json'):\n"
    "        out[p.stem] = json.loads(p.read_text())\n"
    "    return out\n"
)

INTENT_LOAD_MODULE = (
    "import json\nfrom pathlib import Path\n\n\n"
    "def load(role):\n"
    "    p = Path(__file__).parent / 'contracts' / 'intent' / f'{role}.jsonl'\n"
    "    return [json.loads(l)['id'] for l in p.read_text().splitlines() if l.strip()]\n"
)

# P1 (review round 1, C1): a path-built read, inline in the test file itself - the real
# shape at `maintenance/tests/test_usage_window_state.py:312`.
HOLLOW_PATH_BUILT_TEST = (
    "import json\nimport unittest\nfrom pathlib import Path\n\n\n"
    "INTENT_FILE = Path(__file__).resolve().parents[1] / 'contracts' / 'intent' / 'widget.jsonl'\n\n\n"
    "def _ids():\n"
    "    return [json.loads(l)['id'] for l in INTENT_FILE.read_text().splitlines() if l.strip()]\n\n\n"
    "class ProbeHollow(unittest.TestCase):\n"
    "    def test_probe_new_example_is_listed(self):\n"
    "        self.assertGreaterEqual(len(_ids()), 1)\n"
)

# P2 (review round 1, C1): a `ROLES`-tuple loop calling `intent.load(role)` with a
# variable, never a literal - the real shape at `capsule/tests/test_intent.py`
# (`for role in ROLES: intent.load(role)`).
HOLLOW_LOOP_TEST = (
    "import unittest\nimport intent\n\n\n"
    "ROLES = ('widget', 'korin')\n\n\n"
    "class ProbeLoop(unittest.TestCase):\n"
    "    def test_probe_every_role_loads(self):\n"
    "        for role in ROLES:\n"
    "            self.assertGreaterEqual(len(intent.load(role)), 1)\n"
)


class ContractPinNowJudgeable(unittest.TestCase):
    """Before this card, `contracts/*.md` was never reverted, so a test pinning a
    role's contract CONTENT (`contracts.render('widget')`, the real shape every one of
    this repo's own contract tests uses) could pass no matter what the file said -
    reverting it to `--base` never happened, so the pin never had a chance to fail."""

    WIDGET_PIN_TEST = (
        "import unittest\nimport contracts\n\n\nclass Widget(unittest.TestCase):\n"
        "    def test_widget_contract_carries_the_pin_sentence(self):\n"
        "        self.assertIn('pinned-sentence', contracts.render('widget'))\n")

    def test_a_pinned_contract_sentence_with_a_real_read_scores_good(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_UNCHANGED)
            (root / "contracts.py").write_text(CONTRACTS_MODULE)
            (root / "contracts").mkdir()
            (root / "contracts" / "widget.md").write_text("# widget\ngets: a\n")
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base: widget.md has no pinned sentence yet")

            (root / "contracts" / "widget.md").write_text("# widget\ngets: a\npinned-sentence\n")
            write_test(root, self.WIDGET_PIN_TEST)
            head = commit_all(root, "add the pinned sentence to widget.md, with a test for it")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertTrue(ok, rows)
            f, names, rev_cell, weak_cell, flags_cell = rows[0]
            self.assertEqual(rev_cell, "FAIL (good)")

    def test_without_the_fix_the_same_range_is_silently_waved_through(self):
        """Guard: with contract data files never entering the revert set (simulating the
        gate before this card - `is_contract_data_file` patched to see nothing), the
        exact same range reads a bare 'n/a', never counted against the gate - the
        silent gap this card closes."""
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_UNCHANGED)
            (root / "contracts.py").write_text(CONTRACTS_MODULE)
            (root / "contracts").mkdir()
            (root / "contracts" / "widget.md").write_text("# widget\ngets: a\n")
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base: widget.md has no pinned sentence yet")

            (root / "contracts" / "widget.md").write_text("# widget\ngets: a\npinned-sentence\n")
            write_test(root, self.WIDGET_PIN_TEST)
            head = commit_all(root, "add the pinned sentence to widget.md, with a test for it")

            with unittest.mock.patch.object(cd, "is_contract_data_file", lambda rel_path: False):
                ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertTrue(ok, rows)   # the silent gap: nothing here fails the gate
            f, names, rev_cell, weak_cell, flags_cell = rows[0]
            self.assertEqual(rev_cell, "n/a")


class SchemaPinNowJudgeable(unittest.TestCase):
    """Same gap, `contracts/schemas/*.json`: `schemas['name']`/`.get('name')` is the real
    data-read shape (mirrors `capsule/tests/test_schema_contracts.py`'s own dict-of-loaded-
    schemas pattern)."""

    def test_a_pinned_schema_field_with_a_real_read_scores_good(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_UNCHANGED)
            (root / "schemas_mod.py").write_text(SCHEMAS_MODULE)
            (root / "contracts" / "schemas").mkdir(parents=True)
            (root / "contracts" / "schemas" / "foo.json").write_text('{"type": "object"}')
            (root / "contracts" / "schemas" / "SHA256SUMS").write_text("deadbeef  foo.json\n")
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base: foo.json has no required fields yet")

            (root / "contracts" / "schemas" / "foo.json").write_text('{"type": "object", "required": ["widget"]}')
            write_test(
                root,
                "import unittest\nfrom schemas_mod import load\n\n\nclass Foo(unittest.TestCase):\n"
                "    def test_foo_schema_requires_a_widget_field(self):\n"
                "        schemas = load()\n"
                "        self.assertIn('widget', schemas['foo']['required'])\n")
            head = commit_all(root, "pin foo.json's required field, with a test for it")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertTrue(ok, rows)
            f, names, rev_cell, weak_cell, flags_cell = rows[0]
            self.assertEqual(rev_cell, "FAIL (good)")


class IntentReferenceCheckStaysAtLeastAsStrictAsMain(unittest.TestCase):
    """Review round 1, C1 (Critical): the first cut of `references_data_file` REPLACED
    the old, broad intent-jsonl predicate (word 'intent' anywhere, role quoted anywhere)
    with a strictly narrower one (a real call-shaped read only) - main's gate reads two
    real-world hollow tests as `did not fail (BAD)`, this branch's first cut read them as
    `n/a (no production change)`, silently losing their judgment entirely. The old
    predicate is now kept as a floor, OR'd with the new checks, so the gate is only ever
    WIDENED here, never narrowed. Both probes below are the reviewer's own, reproduced
    end to end; both must still read BAD."""

    def test_p1_a_path_built_read_with_a_hollow_test_still_reads_bad(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_UNCHANGED)
            (root / "contracts" / "intent").mkdir(parents=True)
            (root / "contracts" / "intent" / "widget.jsonl").write_text('{"id": "old-example"}\n')
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base: one widget intent example")

            (root / "contracts" / "intent" / "widget.jsonl").write_text(
                '{"id": "old-example"}\n{"id": "new-example"}\n')
            write_test(root, HOLLOW_PATH_BUILT_TEST)
            head = commit_all(root, "add a new widget intent example, with a hollow path-built test")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertFalse(ok, rows)
            f, names, rev_cell, weak_cell, flags_cell = rows[0]
            self.assertIn("did not fail (BAD)", rev_cell)

    def test_p2_a_roles_loop_read_with_a_hollow_test_still_reads_bad(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "production.py").write_text(PROD_UNCHANGED)
            (root / "intent.py").write_text(INTENT_LOAD_MODULE)
            (root / "contracts" / "intent").mkdir(parents=True)
            (root / "contracts" / "intent" / "widget.jsonl").write_text('{"id": "old-example"}\n')
            (root / "contracts" / "intent" / "korin.jsonl").write_text('{"id": "k-old"}\n')
            write_test(root, PLACEHOLDER_TEST)
            base = commit_all(root, "base: one widget intent example")

            (root / "contracts" / "intent" / "widget.jsonl").write_text(
                '{"id": "old-example"}\n{"id": "new-example"}\n')
            write_test(root, HOLLOW_LOOP_TEST)
            head = commit_all(root, "add a new widget intent example, with a hollow ROLES-loop test")

            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertFalse(ok, rows)
            f, names, rev_cell, weak_cell, flags_cell = rows[0]
            self.assertIn("did not fail (BAD)", rev_cell)


class ReferencesDataFileUnitFixtures(unittest.TestCase):
    """Direct fixtures for `is_contract_data_file`/`sha256sums_for`/`references_data_file` -
    the second oracle beside the end-to-end `check_range` regressions above."""

    def test_is_contract_data_file_scopes_to_direct_children_only(self):
        self.assertTrue(cd.is_contract_data_file("contracts/reviewer.md"))
        self.assertTrue(cd.is_contract_data_file("contracts/schemas/report.v1.json"))
        self.assertFalse(cd.is_contract_data_file("contracts/intent/guide.jsonl"))
        self.assertFalse(cd.is_contract_data_file("contracts/schemas/nested/x.json"))
        self.assertFalse(cd.is_contract_data_file("capsule/contracts.py"))

    def test_sha256sums_for_reverts_the_matching_dirs_own_sums_file(self):
        self.assertEqual(cd.sha256sums_for(["contracts/schemas/report.v1.json"]), ["contracts/schemas/SHA256SUMS"])
        self.assertEqual(cd.sha256sums_for(["contracts/reviewer.md"]), [])
        self.assertEqual(cd.sha256sums_for(["contracts/intent/guide.jsonl"]), ["contracts/intent/SHA256SUMS"])
        self.assertEqual(
            sorted(cd.sha256sums_for(["contracts/schemas/report.v1.json", "contracts/intent/guide.jsonl"])),
            ["contracts/intent/SHA256SUMS", "contracts/schemas/SHA256SUMS"])

    def test_references_data_file_needs_the_role_inside_a_real_call(self):
        self.assertTrue(cd.references_data_file("contracts.render('reviewer')", "contracts/reviewer.md"))
        self.assertFalse(cd.references_data_file("ROLES = ('reviewer',)", "contracts/reviewer.md"))
        self.assertTrue(cd.references_data_file("schemas['report.v1']", "contracts/schemas/report.v1.json"))
        self.assertTrue(cd.references_data_file("from intent import ids\nids('guide')",
                                                  "contracts/intent/guide.jsonl"))

    def test_references_data_file_floor_still_catches_the_old_loose_intent_shape(self):
        """The pre-existing, unscoped floor for intent-jsonl only (never md/schema, which have
        no floor since main never reverted either) - restored by review round 1, C1."""
        self.assertTrue(cd.references_data_file("ROLES = ('guide',)  # intent examples live elsewhere",
                                                  "contracts/intent/guide.jsonl"))
        self.assertFalse(cd.references_data_file("ROLES = ('guide',)  # nothing to do with any module",
                                                   "contracts/intent/guide.jsonl"))

    def test_references_data_file_recognizes_a_quoted_full_filename_literal(self):
        """Review round 1, I2: `capsule/tests/test_schema_contracts.py:39`'s real shape,
        `paths.SCHEMAS / "report.v1.json"` - the path-join literal, not a call argument."""
        self.assertTrue(cd.references_data_file('paths.SCHEMAS / "report.v1.json"',
                                                  "contracts/schemas/report.v1.json"))
        self.assertTrue(cd.references_data_file('Path(...) / "contracts" / "reviewer.md"',
                                                  "contracts/reviewer.md"))
        self.assertTrue(cd.references_data_file('Path(...) / "contracts" / "intent" / "guide.jsonl"',
                                                  "contracts/intent/guide.jsonl"))

    def test_references_data_file_recognizes_a_bare_subscript_on_any_container(self):
        """Review round 1, I2: `capsule/tests/test_contracts.py:326`'s real shape,
        `load_all(paths.SCHEMAS)["decision.v1"]` - a subscript on a dict that is not itself
        named `schemas`."""
        self.assertTrue(cd.references_data_file('load_all(paths.SCHEMAS)["decision.v1"]',
                                                  "contracts/schemas/decision.v1.json"))


ALERT_RULES = "observability/grafana/provisioning/alerting/rules.yaml"


class AlertRulesYamlNowJudgeable(unittest.TestCase):
    """Intake rq_9b17453e: Grafana's `alerting/rules.yaml` is data no test imports, so a
    rule-shape test (`capsule/tests/test_unowned_alert_rule.py`, which reads
    `ALERT_DIR / "rules.yaml"`) read BAD ('did not fail when production was reverted')."""

    RULE_TEST = (
        "import unittest\nfrom pathlib import Path\n\n"
        "RULES = Path(__file__).resolve().parents[1] / 'observability' / 'grafana' / 'provisioning'"
        " / 'alerting' / 'rules.yaml'\n\n\n"
        "class Rule(unittest.TestCase):\n"
        "    def test_rule_is_unowned_idle(self):\n"
        "        self.assertIn('unowned_idle', RULES.read_text())\n")

    def _range(self, root):
        init_repo(root)
        (root / "production.py").write_text(PROD_UNCHANGED)
        (root / "observability" / "grafana" / "provisioning" / "alerting").mkdir(parents=True)
        (root / ALERT_RULES).write_text("groups: []\n")
        write_test(root, PLACEHOLDER_TEST)
        base = commit_all(root, "base: rules.yaml has no unowned_idle rule")
        (root / ALERT_RULES).write_text("groups:\n  - uid: unowned_idle\n")
        write_test(root, self.RULE_TEST)
        return base, commit_all(root, "add the unowned_idle rule, with a test for it")

    def test_a_rule_shape_test_reading_rules_yaml_scores_good(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            base, head = self._range(root)
            ok, rows, note = mc.check_range(root, base, head)
            self.assertIsNone(note)
            self.assertTrue(ok, rows)
            self.assertEqual(rows[0][2], "FAIL (good)")

    def test_without_the_fix_the_same_range_is_silently_waved_through(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            base, head = self._range(root)
            with unittest.mock.patch.object(cd, "is_contract_data_file", lambda rel_path: False):
                ok, rows, note = mc.check_range(root, base, head)
            self.assertEqual(rows[0][2], "n/a")

    def test_unit_fixtures_for_the_alert_rules_file(self):
        self.assertTrue(cd.is_contract_data_file(ALERT_RULES))
        alert_dir = ALERT_RULES.rsplit("/", 1)[0]
        self.assertFalse(cd.is_contract_data_file(alert_dir + "/nested/x.yaml"))
        self.assertFalse(cd.is_contract_data_file(alert_dir + "/notes.md"))
        self.assertEqual(cd.sha256sums_for([ALERT_RULES]), [])
        self.assertTrue(cd.references_data_file('ALERT_DIR / "rules.yaml"', ALERT_RULES))
        self.assertFalse(cd.references_data_file('ALERT_DIR / "other.yaml"', ALERT_RULES))
