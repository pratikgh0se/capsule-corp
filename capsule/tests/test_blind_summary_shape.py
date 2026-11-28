"""The status line of the blind run (R35, S20): `_parse` takes unittest's own final line whole
(`OK`, `OK (k=N, ...)`, `FAILED (k=N, ...)`, `NO TESTS RAN`; digits only, no trailing text, a few
hundred bytes at most), never a line that only starts with `OK` or `FAILED`."""
import unittest

from capsule.blind import run as blind_run
from capsule.tests.test_blind_run_parts import AGREE, result_rows

NO_SUMMARY = "no usable summary"


class SummaryShape(unittest.TestCase):
    def parse(self, status, rows, code, ran=1):
        return blind_run._parse(rows, AGREE.format(n=ran, status=status), code)[1]

    def test_unittests_own_lines_are_accepted(self):
        one, two = result_rows(("a", "pass")), result_rows(("a", "pass"), ("b", "fail"))
        self.assertIsNone(self.parse("OK", one, 0))
        self.assertIsNone(self.parse("OK (expected failures=1)", one, 0))
        self.assertIsNone(self.parse("OK (skipped=1, expected failures=2)",
                                     result_rows(("a", "skipped")), 0))
        self.assertIsNone(self.parse("FAILED (failures=1)", two, 1, ran=2))
        self.assertIsNone(self.parse("FAILED (failures=1, errors=1, skipped=2)",
                                     result_rows(("a", "fail"), ("b", "error")), 1, ran=2))

    def test_a_line_that_only_starts_with_ok_or_failed_is_no_summary(self):
        rows = result_rows(("a", "pass"))
        for line in ("OK ((( failures=x, skipped=", "OK and then some trailing text",
                     "OK (skipped=one)", "OK (skipped=1) trailing", "OK (skipped=1,)",
                     "OK ()", "OK (bogus=1)", "OK (skipped=1", "OKAY", " OK"):
            self.assertIn(NO_SUMMARY, self.parse(line, rows, 0), line)
        self.assertIn(NO_SUMMARY, self.parse("FAILED (failures=x)", result_rows(("a", "fail")), 1))
        self.assertIn(NO_SUMMARY, self.parse("FAILED whatever", result_rows(("a", "fail")), 1))

    def test_a_huge_line_is_no_summary_and_is_not_scanned_for_counts(self):
        rows = result_rows(("a", "pass"))
        self.assertIn(NO_SUMMARY, self.parse("OK" + " " * 300_000, rows, 0))
        self.assertIn(NO_SUMMARY, self.parse("OK" + " " * 400, rows, 0))   # padded past the cap
        self.assertIsNone(self.parse("OK  ", rows, 0))                     # short padding is fine
        self.assertIn(NO_SUMMARY, self.parse("OK (skipped=1" + ", skipped=1" * 100 + ")", rows, 0))

    def test_no_tests_ran_stays_no_summary_for_a_card_with_tests(self):
        self.assertIn(NO_SUMMARY, self.parse("NO TESTS RAN", result_rows(("a", "pass")), 0))


if __name__ == "__main__":
    unittest.main()
