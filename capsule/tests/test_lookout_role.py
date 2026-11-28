"""TEAM-14: the Lookout role contract (rows 21, 64). The role proposes, never applies; it is
not wired into the tick here. Isolated: contract, schema, fixture and intent examples only."""
import copy
import hashlib
import json
import unittest

from capsule import contracts, intent, paths
from capsule.jsonschema import load_all, validate

ZENKAI = "never bypass a Zenkai Rule (docs/ZENKAI.md) without an override that names it"
FIXTURE = paths.REPO / "capsule/tests/fixtures/schemas/lookout_proposal.v1.valid.json"
GAP = {"signal": "x", "status": "missing", "evidence": "none"}
SLO = {"kind": "slo", "path": "docs/observability.md",
       "change": "Set a 99 percent poll success target.", "reason": "No target exists today."}


def _doc(**over):
    doc = json.loads(FIXTURE.read_text())
    doc.update(over)
    return doc


def _sums(directory):
    lines = (directory / "SHA256SUMS").read_text().splitlines()
    return {name: digest for digest, name in (x.split(maxsplit=1) for x in lines)}


def _ids(doc):
    return [f["id"] for f in intent.check("lookout", doc)]


class LookoutRole(unittest.TestCase):
    def test_contract_lints_and_carries_both_may_not_lines(self):
        path = contracts.contract_path("lookout")
        self.assertEqual(contracts.lint(path, load_all(paths.SCHEMAS)), [])
        self.assertEqual(contracts.schema_for("lookout"), "lookout_proposal.v1")
        may_not = contracts.parse(path)["may-not"]
        self.assertTrue(may_not.endswith(ZENKAI + "."))
        for part in ("apply a proposal", "silence an alert", "edit a dashboard",
                     "fix application code"):
            self.assertIn(part, may_not)
        self.assertIn(ZENKAI, contracts.render("lookout"))

    def test_role_reads_only(self):
        sees = contracts.parse(contracts.contract_path("lookout"))["sees"]
        self.assertIn("read-only", sees)
        self.assertIn("no Grafana credentials", sees)

    def test_fixture_validates_and_bad_documents_fail(self):
        schema = load_all(paths.SCHEMAS)["lookout_proposal.v1"]
        self.assertEqual(validate(_doc(), schema), [])
        self.assertTrue(validate(_doc(trigger="every_card"), schema))
        # no "applied" field: the role proposes only
        self.assertTrue(validate(_doc(applied=True), schema))
        bad = copy.deepcopy(_doc())
        bad["proposals"][0]["reason"] = ""
        self.assertTrue(validate(bad, schema))

    def test_pinned_hashes(self):
        pinned = ((paths.REPO / "contracts" / "schemas", "lookout_proposal.v1.json"),
                  (paths.INTENT, "lookout.jsonl"))
        for directory, name in pinned:
            digest = hashlib.sha256((directory / name).read_bytes()).hexdigest()
            self.assertEqual(digest, _sums(directory)[name])

    def test_intent_examples_lint_and_are_one_to_three(self):
        self.assertEqual(intent.lint_all(), [])
        self.assertTrue(1 <= len(intent.load("lookout")) <= 3)

    def test_a_gap_without_a_proposal_contradicts_intent(self):
        self.assertEqual(_ids(_doc(coverage=[GAP], proposals=[])),
                         ["gap-or-partial-needs-a-proposal"])
        partial = dict(GAP, status="partial")
        self.assertEqual(_ids(_doc(coverage=[partial], proposals=[])),
                         ["gap-or-partial-needs-a-proposal"])
        self.assertEqual(_ids(_doc()), [])

    def test_full_coverage_with_proposals_contradicts_intent(self):
        doc = _doc(coverage=[{"signal": "x", "status": "covered", "evidence": "ok"}])
        self.assertEqual(_ids(doc), ["full-coverage-needs-no-proposal"])

    def test_slo_proposal_without_question_contradicts_intent(self):
        doc = _doc(proposals=[SLO], questions=[])
        self.assertIn("slo-target-needs-a-question", _ids(doc))


if __name__ == "__main__":
    unittest.main()
