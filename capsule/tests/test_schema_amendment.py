"""Factory autonomy Task 2 (spec 2.1, 3.3): additive optional fields. Oracle 1: the pinned JSON
schema. Oracle 2: autoamend.parse_hunks on the same value (shares no code with the validator)."""
import copy
import shutil
import tempfile
import unittest

from capsule import autoamend, bundles, intent, paths, spool
from capsule.jsonschema import load_all, validate

S = load_all(paths.SCHEMAS)
VERDICT = {"schema": "alignment_verdict.v1", "run_id": "0" * 32, "task_id": "t_0000aaaa", "claim_lock": "h:x",
           "verdict": "drifted", "rows": [452], "reason": "count is stale"}
DECISION = {"schema": "decision.v1", "run_id": "0" * 32, "task_id": "t_0000aaaa", "claim_lock": "h:x",
            "question": "q", "options": [{"id": "revert", "label": "revert the plan hunk"}, {"id": "keep", "label": "keep"}],
            "recommended": "revert"}
GOOD = [{"target": "card", "find": "38 tests", "replace": "39 tests"}]
BAD = [[], [{"target": "card", "find": "", "replace": "x"}], [{"target": "plan", "find": "a", "replace": "b"}],
       [{"target": "card", "find": "a", "replace": "b", "why": "x"}], GOOD * 4]


def parses(value):
    try:
        autoamend.parse_hunks(value)
        return True
    except ValueError:
        return False


class Amendment(unittest.TestCase):
    def test_verdict_accepts_exactly_what_parse_hunks_accepts(self):
        for value, want in [(GOOD, True)] + [(b, False) for b in BAD]:
            with self.subTest(value=value):
                doc = dict(VERDICT, amendment=value)
                self.assertEqual(validate(doc, S["alignment_verdict.v1"]) == [], want)   # oracle 1
                self.assertEqual(parses(value), want)                                    # oracle 2

    def test_identity_hunk(self):
        identity = [{"target": "card", "find": "unused", "replace": "unused"}]
        doc = dict(VERDICT, amendment=identity)
        # oracle 1: the schema has no find != replace rule
        self.assertEqual(validate(doc, S["alignment_verdict.v1"]), [])
        # oracle 2: parse_hunks refuses
        self.assertFalse(parses(identity))

    def test_verdict_without_amendment_still_valid(self):
        self.assertEqual(validate(VERDICT, S["alignment_verdict.v1"]), [])

    def test_decision_option_amendment_and_scope(self):
        doc = copy.deepcopy(DECISION)
        doc["options"][0]["amendment"] = GOOD
        doc["scope"] = "text"
        self.assertEqual(validate(doc, S["decision.v1"]), [])
        self.assertEqual(autoamend.recommended_option(doc)["amendment"], GOOD)
        for bad_scope in ("other", 1):
            self.assertNotEqual(validate(dict(doc, scope=bad_scope), S["decision.v1"]), [])
        doc["options"][0]["amendment"] = BAD[2]
        self.assertNotEqual(validate(doc, S["decision.v1"]), [])
        for bad_amendment in (GOOD * 4, []):
            with self.subTest(amendment=bad_amendment):
                self.assertNotEqual(validate(dict(doc, options=[dict(doc["options"][0], amendment=bad_amendment),
                                                                  doc["options"][1]]), S["decision.v1"]), [])


class LoaderAndPrompt(unittest.TestCase):
    """The real subjects: the loader every Tick uses (spool.read_report over a report file, the
    schemas load_all read at Tick init) and the guide prompt (bundles.VERDICT_NOTE)."""

    def test_a_guide_report_with_an_amendment_is_read_back_valid(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        d = spool.make(root, "0" * 32)
        spool.write_doc(d, "report", dict(VERDICT, amendment=GOOD))
        doc, errs = spool.read_report(d, load_all(paths.SCHEMAS))
        self.assertEqual((errs, doc["amendment"]), ([], GOOD))

    def test_verdict_note_asks_for_amendment(self):
        self.assertIn("amendment", bundles.VERDICT_NOTE)
        self.assertIn("Files list", bundles.VERDICT_NOTE)
        self.assertIn("never edit an existing one", bundles.VERDICT_NOTE)
        self.assertIn("omit the key", bundles.VERDICT_NOTE)

    def test_aligned_with_an_amendment_contradicts_the_guide_example(self):
        found = intent.check("guide", dict(VERDICT, verdict="aligned", amendment=GOOD))
        self.assertIn("aligned-verdict-carries-no-amendment", [f["id"] for f in found])


if __name__ == "__main__":
    unittest.main()
