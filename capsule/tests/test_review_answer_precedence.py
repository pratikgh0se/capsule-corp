"""inc_40e0f8a8: a human answer that replaces a card criterion reaches the reviewer and the guide,
and the prompt says it outranks that criterion's text. No test here names the note constant or
calls a bundle function (inc_a9d793f8): each reads composed prompts only."""
import json
import unittest

from capsule import human
from capsule.tests.test_tick import GUIDE_OK, IMPL_DONE, REV_PASS, VAL_PASS, TickCase

ANSWER = "PRECEDENCE-ANSWER-5521: criterion 4 now asks for a prompt read, not a constant read."
SENTENCE = "A later human answer that replaces a card criterion outranks that criterion's text."


class ReviewersFollowTheLatestAnswer(TickCase):
    def _prompts(self):
        self.script.update(implementer=[[IMPL_DONE]], reviewer=[[REV_PASS]], guide=[[GUIDE_OK]],
                           validator=[[VAL_PASS]])
        for _ in range(4):
            self.tick.once()
        runs = {role: self.runs(role)[0]["run_id"] for role in ("reviewer", "guide")}
        return {role: json.loads((self.spool_dir(run) / "bundle.json").read_text())["prompt"]
                for role, run in runs.items()}

    def test_an_answer_that_replaces_a_criterion_reaches_the_reviewer_and_guide_with_its_precedence(
            self):
        human.write(self.home, "test", self.tid, "answer", ANSWER, author="cos", now=1)
        for role, prompt in self._prompts().items():
            self.assertIn("## Human answers", prompt, role)
            self.assertIn(ANSWER, prompt, role)
            self.assertIn(SENTENCE, prompt, role)

    def test_the_precedence_line_keeps_other_criteria_and_findings_valid(self):
        human.write(self.home, "test", self.tid, "answer", ANSWER, author="cos", now=1)
        for role, prompt in self._prompts().items():
            line = prompt[prompt.index(SENTENCE):].split("\n", 1)[0]
            self.assertIn("other criteria", line, role)
            self.assertIn("other findings stay valid", line, role)

    def test_with_no_human_file_neither_prompt_holds_the_answers_or_the_precedence_line(self):
        for role, prompt in self._prompts().items():
            self.assertNotIn("## Human answers", prompt, role)
            self.assertNotIn(SENTENCE, prompt, role)



if __name__ == "__main__":
    unittest.main()
