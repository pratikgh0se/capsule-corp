"""R28: `blind_result.v1` `red` also allows `not-run` (the base-commit red check was not performed,
as in a head-commit report-mode run); a made-up value is still refused."""
import unittest

from capsule import paths
from capsule.jsonschema import load_all, validate

DOC = {"schema": "blind_result.v1", "run_id": "ab" * 16, "task_id": "t_0a1b2c3d",
       "claim_lock": "host:" + "ab" * 16, "blind_commit": "abcdef1", "base_sha": "abcdef1",
       "head_sha": "abcdef2", "red": "not-run",
       "counts": {"pass": 1, "fail": 0, "error": 0, "skipped": 0}, "tests": []}


class RedValues(unittest.TestCase):
    def errors(self, red):
        schema = load_all(paths.SCHEMAS)["blind_result.v1"]
        return validate({**DOC, "red": red}, schema)

    def test_not_run_is_accepted(self):
        self.assertEqual(self.errors("not-run"), [])

    def test_the_existing_values_are_still_accepted(self):
        for red in ("pass", "fail", "inconclusive", "exempt-3"):
            self.assertEqual(self.errors(red), [], red)

    def test_a_made_up_value_is_refused(self):
        for red in ("not_run", "notrun", "skipped", "not-run-yet", "", "exempt-"):
            self.assertTrue(self.errors(red), red)


if __name__ == "__main__":
    unittest.main()
