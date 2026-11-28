"""Unit test for capsule/validator_report.py (t_99136481, row 447: two oracles - this direct
unit test on `not_run_violation`, plus the tick-path test in test_tick.py's ValidatorOutputs)."""
import unittest

from capsule import validator_report


class NotRunViolation(unittest.TestCase):
    def test_no_outputs_means_not_run_is_legitimate(self):
        doc = {"not_run": ["nothing ran"]}
        self.assertIsNone(validator_report.not_run_violation(doc, []))
        self.assertIsNone(validator_report.not_run_violation(doc, None))

    def test_a_top_level_not_run_entry_with_outputs_is_a_violation(self):
        doc = {"not_run": ["cannot read source file to confirm changes"], "assertions": []}
        err = validator_report.not_run_violation(doc, [{"cmd": "make test", "exit": 0}])
        self.assertIn("cannot read source file to confirm changes", err)

    def test_an_assertion_result_not_run_with_outputs_is_also_a_violation(self):
        doc = {"not_run": [], "assertions": [{"cmd": "make test", "exit": 0, "result": "not_run"}]}
        err = validator_report.not_run_violation(doc, [{"cmd": "make test", "exit": 0}])
        self.assertIn("make test", err)

    def test_a_clean_document_with_outputs_is_no_violation(self):
        doc = {"not_run": [], "assertions": [{"cmd": "make test", "exit": 0, "result": "pass"}]}
        self.assertIsNone(validator_report.not_run_violation(doc, [{"cmd": "make test", "exit": 0}]))


class BlindRowsWithoutExit(unittest.TestCase):
    """A blind row with no `exit` recorded is not shown to have passed; and no message the
    implementer may read names a blind test."""
    ROW = {"cmd": "blind:t1.test_x", "blind": "gate", "result": "fail"}

    def test_a_pass_over_a_gate_row_with_no_exit_is_invalid(self):
        doc = {"verdict": "pass", "assertions": [
            {"cmd": "blind:t1.test_x", "exit": 0, "result": "pass"}]}
        err = validator_report.not_run_violation(doc, [dict(self.ROW)])
        self.assertIn("did not fail failing blind test", err)

    def test_a_fail_citing_a_row_that_did_not_fail_names_a_count_never_the_test(self):
        doc = {"verdict": "fail", "assertions": [
            {"cmd": "blind:t1.test_x", "exit": 1, "result": "fail", "excerpt": "given: SECRET"}]}
        for outputs in ([{"cmd": "blind:t1.test_x", "blind": "report", "exit": 0}],
                        [{"cmd": "blind:t1.test_y", "blind": "report", "exit": 1}]):
            err = validator_report.not_run_violation(doc, outputs)
            self.assertIn("1 blind row(s) that did not fail", err)
            self.assertNotIn("t1", err)
            self.assertNotIn("test_x", err)

    def test_a_blind_looking_cmd_in_any_spelling_is_a_citation_of_a_row_that_did_not_fail(self):
        for cmd in ("`blind:x`", "Blind:x", " blind:x"):
            doc = {"verdict": "fail", "assertions": [{"cmd": cmd, "exit": 1, "result": "fail"}]}
            self.assertIn("did not fail", validator_report.not_run_violation(doc, []), cmd)

    def test_a_citation_matches_its_row_ignoring_case_and_backticks_but_not_a_different_id(self):
        rows = [{"cmd": "blind:Foo", "blind": "gate", "exit": 1}]
        for cmd in ("`blind:Foo`", "blind:foo", " `BLIND:FOO` "):
            doc = {"verdict": "fail", "assertions": [{"cmd": cmd, "exit": 1, "result": "fail"}]}
            self.assertIsNone(validator_report.not_run_violation(doc, rows), cmd)
        other = {"verdict": "fail", "assertions": [
            {"cmd": "blind:foo", "exit": 1, "result": "fail"},
            {"cmd": "blind:bar", "exit": 1, "result": "fail"}]}
        self.assertIn("1 blind row(s) that did not fail", validator_report.not_run_violation(
            other, rows))


if __name__ == "__main__":
    unittest.main()


class BlindCitationNormalised(unittest.TestCase):
    """A blind citation that differs from the row's cmd only in case, backticks, quotes or
    whitespace is the same citation; a different one still mismatches."""
    ROW = [{"cmd": "blind:test_foo bar", "blind": "gate", "exit": 1}]

    def _violation(self, cited):
        doc = {"assertions": [{"cmd": cited, "exit": 1, "result": "fail"}]}
        return validator_report._blind_citation_violation(doc, self.ROW)

    def test_case_backticks_quotes_and_whitespace_still_match(self):
        for cited in ("BLIND:Test_Foo Bar", "`blind:test_foo bar`", '"blind:test_foo bar"',
                      "'blind:test_foo bar'", "blind:test_foo   bar", "  `Blind:test_foo\tbar`  "):
            self.assertIsNone(self._violation(cited), cited)

    def test_a_genuinely_different_citation_mismatches(self):
        self.assertIn("did not fail", self._violation("blind:test_other bar"))
