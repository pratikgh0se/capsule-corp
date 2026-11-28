# mutation-check: targets-predate-range b957739c9 -- owner-word rename, no behaviour pinned ::
#   ContradictionRulingSeesAnswers.test_the_note_says_to_ask_owner_rather_than_reverse_an_answer,
#   test_a_guide_report_with_the_old_ask_verb_is_accepted_only_where_ask_owner_is_offered
"""inc_eea0243d: the guide's ruling run sees the card's human answers, so a ruling cannot reverse a
CoS answer it never read."""
import json
import unittest

from capsule import bundles, human
from capsule.tests.test_runaway import decision
from capsule.tests.test_tick import GUIDE_OK, IMPL_DONE, REV_FAIL, TickCase

ANSWER = "RULING-ANSWER-7731: put the new tests in test_blocks_epic_wait.py, not test_blocks.py."
ENTRY = {"kind": "answer", "text": ANSWER, "at": 1790000000, "author": "cos"}
TASK = {"id": "t_1", "title": "t", "body": ""}
CONTRADICTION = {"kind": "contradiction", "role": "reviewer", "precedence": "spec", "document": {},
                 "examples": [{"id": "e1", "source": "intent", "example": {}, "differs": ["x"]}]}


class RulingSeesHumanAnswers(TickCase):
    def setUp(self):
        super().setUp()
        self.cfg["runaway"] = {"max_rounds": 1}
        self.cfg["korin"] = dict(self.cfg["korin"], enabled=False)
        self.write_cfg()
        self.script.update(implementer=[[IMPL_DONE], [IMPL_DONE]],
                           reviewer=[[REV_FAIL], [REV_FAIL]],
                           guide=[[GUIDE_OK], [decision("ask-owner", "only the owner can say")]])

    def _ruling_prompt(self):
        for _ in range(8):
            self.tick.once()
        (run,) = [r for r in self.runs("guide")
                  if json.loads(r["metadata"] or "{}").get("ruling_of")]
        bundle = self.spool_dir(run["run_id"]) / "bundle.json"
        return json.loads(bundle.read_text())["prompt"]

    def test_a_runaway_ruling_bundle_carries_the_cards_human_answers(self):
        human.write(self.home, "test", self.tid, "answer", ANSWER, author="cos", now=1)
        prompt = self._ruling_prompt()
        self.assertIn("## Human answers", prompt)
        self.assertIn(ANSWER, prompt)
        self.assertIn(bundles.RULING_ANSWER_NOTE, prompt)
        self.assertIn("answer by cos: " + ANSWER, prompt)

    def test_a_card_with_no_human_file_gets_no_human_section_in_its_ruling(self):
        prompt = self._ruling_prompt()
        self.assertNotIn("## Human answers", prompt)
        self.assertNotIn(bundles.RULING_ANSWER_NOTE, prompt)


class ContradictionRulingSeesAnswers(unittest.TestCase):
    def test_a_contradiction_ruling_gets_the_same_section_and_note(self):
        prompt = bundles.guide_ruling(TASK, CONTRADICTION, interventions=[ENTRY])["prompt"]
        self.assertIn("## Contradiction to rule", prompt)
        self.assertIn("## Human answers", prompt)
        self.assertIn(ANSWER, prompt)
        self.assertIn(bundles.RULING_ANSWER_NOTE, prompt)

    def test_the_note_says_to_ask_owner_rather_than_reverse_an_answer(self):
        self.assertIn("Do not reverse a human answer", bundles.RULING_ANSWER_NOTE)
        self.assertIn("`ask-owner`", bundles.RULING_ANSWER_NOTE)
        self.assertIn("`context`", bundles.RULING_ANSWER_NOTE)

    def test_no_answers_means_no_section_and_the_default_call_is_unchanged(self):
        for kwargs in ({}, {"interventions": []}, {"interventions": None}):
            prompt = bundles.guide_ruling(TASK, CONTRADICTION, **kwargs)["prompt"]
            self.assertNotIn("## Human answers", prompt)
            self.assertNotIn(bundles.RULING_ANSWER_NOTE, prompt)


class OldAskVerb(unittest.TestCase):
    def test_a_guide_report_with_the_old_ask_verb_is_accepted_only_where_ask_owner_is_offered(self):
        offered = tuple(i for i, _ in bundles.RULING_OPTIONS)
        self.assertIn(bundles.LEGACY_ASK, bundles.accepted(offered))
        red = tuple(i for i, _ in bundles.RED_EXEMPT_OPTIONS)
        self.assertEqual(bundles.accepted(red), red)


if __name__ == "__main__":
    unittest.main()
