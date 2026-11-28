"""inc_a4dd62f9: the reviewer and the guide see the card's human answers, so an answer that names a
path outside the file set cannot be failed as scope creep by a review that never read it."""
import json
import unittest

from capsule import bundles, human
from capsule.tests.private_names import personal_names
from capsule.tests.test_tick import GUIDE_OK, IMPL_DONE, REV_PASS, VAL_PASS, TickCase

ANSWER = "Add tests/test_mutation_check_remedy.py for the remedy text."


class ReviewersSeeHumanAnswers(TickCase):
    def _review_bundles(self):
        self.script.update(implementer=[[IMPL_DONE]], reviewer=[[REV_PASS]], guide=[[GUIDE_OK]],
                           validator=[[VAL_PASS]])
        for _ in range(4):
            self.tick.once()
        runs = {role: self.runs(role)[0]["run_id"] for role in ("reviewer", "guide")}
        return {role: json.loads((self.spool_dir(run) / "bundle.json").read_text())["prompt"]
                for role, run in runs.items()}

    def test_a_human_answer_naming_a_file_outside_the_set_reaches_the_reviewer_and_guide(self):
        human.write(self.home, "test", self.tid, "unblock", ANSWER, now=1)
        for role, prompt in self._review_bundles().items():
            for part in ("## Human answers", ANSWER, bundles.ANSWER_SCOPE_NOTE):
                self.assertIn(part, prompt, role)

    def test_the_author_is_shown_and_the_answers_come_before_the_workers_report(self):
        human.write(self.home, "test", self.tid, "unblock", ANSWER, now=1, author="cos")
        prompts = self._review_bundles()
        self.assertIn("unblock by cos: " + ANSWER, prompts["guide"])
        for name in personal_names():
            self.assertNotIn(f"{name}'s interventions", prompts["guide"].lower())
        guide = prompts["guide"]
        self.assertLess(guide.index("## Human answers"), guide.index("## Implementer's report"))

    def test_with_no_human_file_neither_prompt_has_a_human_section(self):
        for role, prompt in self._review_bundles().items():
            self.assertNotIn("## Human answers", prompt, role)
            self.assertNotIn(bundles.ANSWER_SCOPE_NOTE, prompt, role)

    def test_the_validator_bundle_stays_code_blind_to_the_answers(self):
        task = {"id": "t", "title": "t", "body": ""}
        self.assertNotIn("## Human answers", bundles.validator(task, [])["prompt"])


if __name__ == "__main__":
    unittest.main()
