"""S11 (R14): `flake.failures` over printed rerun output: what each result shape is called.
Split from test_flake.py to keep both under the new-file length limit."""
import json
import tempfile
import unittest
from pathlib import Path

from capsule import flake
from capsule.tests import tmpclean
from capsule.tests.test_flake import ON, write


class Findings(unittest.TestCase):
    def setUp(self):
        self.spool = Path(tempfile.mkdtemp())
        self.addCleanup(tmpclean.sweep, self.spool)

    def failures(self, outputs, cfg=ON, run="r1"):
        if outputs is not None:
            write(self.spool, "r1/job.json", json.dumps({"inputs": {"outputs": outputs}}))
        return flake.failures(cfg, None, self.spool, {"run_id": run} if run else None)

    @staticmethod
    def entry(output, cid="a.b.C.test_x"):
        return {"cmd": f"{flake.PREFIX}{cid} ; bad=0", "exit": 0, "output": output}

    @staticmethod
    def runs(*pairs):
        return "".join(f"run {i} rc={rc} {summary}\n" for i, (rc, summary) in enumerate(pairs, 1))

    def test_five_clear_passes_are_no_finding_including_a_skip_and_colour(self):
        for summary in ("OK", "OK (skipped=1)", "\x1b[32mOK\x1b[0m"):
            self.assertEqual(self.failures([self.entry(self.runs(*[(0, summary)] * 5))]), [])

    def test_a_mix_is_flaky_and_all_failing_is_its_own_line(self):
        mixed = self.runs((0, "OK"), (1, "FAILED (failures=1)"), (0, "OK"), (0, "OK"), (0, "OK"))
        self.assertEqual(self.failures([self.entry(mixed)]),
                         ["flaky test `a.b.C.test_x`: 4 of 5 reruns passed, exit codes"
                          " [0, 1, 0, 0, 0] (flake rerun, S11)"])
        bad = self.runs(*[(1, "FAILED (errors=1)")] * 5)
        self.assertEqual(self.failures([self.entry(bad)]),
                         ["test `a.b.C.test_x` failed all 5 reruns, exit codes"
                          " [1, 1, 1, 1, 1] (flake rerun, S11)"])

    def test_a_result_that_cannot_be_read_is_never_a_pass(self):
        ok, failed = (0, "OK"), (1, "FAILED (failures=1)")
        for output in (self.runs(*[(0, "FAILED (failures=1)")] * 5),     # code and summary disagree
                       self.runs(*[(1, "OK")] * 5),
                       self.runs(*[(5, "NO TESTS RAN")] * 5),
                       self.runs(*[(124, "")] * 5),                      # every run timed out
                       self.runs(ok, ok, ok, ok),                        # a run is missing
                       self.runs(ok, ok, ok, ok, ok, ok),                # one too many
                       self.runs(ok, ok, ok, ok, (0, "FAILED (failures=1)")),
                       self.runs(failed, failed, failed, failed, (1, "OK")),
                       "timed out after 600s", "", None):
            got = self.failures([self.entry(output)])
            self.assertEqual(len(got), 1, output)
            self.assertIn("flake rerun of `a.b.C.test_x` unreadable", got[0])
            self.assertIn("(flake rerun, S11)", got[0])

    def test_unreadable_names_how_many_runs_were_clear(self):
        got = self.failures([self.entry(self.runs((0, "OK"), (0, "OK")))])
        self.assertEqual(got, ["flake rerun of `a.b.C.test_x` unreadable: 2 of 5 runs printed a"
                               " result whose exit code and runner summary agree"
                               " (flake rerun, S11)"])

    def test_unreadable_counts_only_runs_whose_code_and_summary_agree(self):
        ok = (0, "OK")
        mixed = self.runs(ok, ok, ok, (0, "FAILED (failures=1)"), (1, "OK"))
        got = self.failures([self.entry(mixed)])
        self.assertIn("unreadable: 3 of 5 runs", got[0])

    def test_one_line_per_added_test_and_other_commands_are_ignored(self):
        ok = self.runs(*[(0, "OK")] * 5)
        flaky = self.runs((0, "OK"), (1, "FAILED (failures=1)"), (1, "FAILED (failures=1)"),
                          (0, "OK"), (0, "OK"))
        got = self.failures([{"cmd": "make test", "exit": 1, "output": "run 1 rc=1 FAILED"},
                             self.entry(ok, "m.A.test_ok"), self.entry(flaky, "m.A.test_mixed"),
                             {"cmd": ": flake-rerun-cap 3 of 28 added tests were not rerun",
                              "exit": 0, "output": ""},
                             self.entry(flaky, "m.B.test_mixed")])
        self.assertEqual([line.split("`")[1] for line in got], ["m.A.test_mixed", "m.B.test_mixed"])

    def test_nothing_with_the_switch_off_no_validator_run_or_no_evidence(self):
        flaky = self.runs((0, "OK"), (1, "FAILED (failures=1)"), (0, "OK"), (0, "OK"), (0, "OK"))
        out = [self.entry(flaky)]
        self.assertEqual(len(self.failures(out)), 1)
        for cfg in ({}, {"redesign": {"flake_reruns": False}}):
            self.assertEqual(self.failures(out, cfg), [])
        self.assertEqual(self.failures(out, run=None), [])
        self.assertEqual(self.failures([]), [])
        self.assertEqual(self.failures(None, run="gone"), [])       # no job.json at all
        write(self.spool, "r2/job.json", "{not json")
        self.assertEqual(self.failures(None, run="r2"), [])


if __name__ == "__main__":
    unittest.main()
