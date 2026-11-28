"""Row 520 (t_5dfcbd9b): an end-to-end check, through the real tick, that a rerun's bundle
carries copies of the earlier runs' real spool files - not a stub of `prior_runs.paths_for`.
Split out of capsule/tests/test_tick.py (docs/ZENKAI.md Z7: that file sits at its size baseline).
The cap always keeps the newest implementer run, and the bundle names read-only copies under
the rerun's own `spool/prior/`, never the original spool dirs.
"""
import json
from pathlib import Path

from capsule.tests.test_tick import GUIDE_OK, IMPL_DONE, REV_FAIL, REV_PASS, VAL_PASS, TickCase


class PriorRunsReachTheRerun(TickCase):
    def test_the_rerun_bundle_carries_copies_of_the_failed_runs_real_files(self):
        self.script["implementer"] = [[IMPL_DONE], [IMPL_DONE]]
        self.script["reviewer"] = [[REV_FAIL], [REV_PASS]]
        self.script["validator"] = [[VAL_PASS], [VAL_PASS]]
        self.script["guide"] = [[GUIDE_OK], [GUIDE_OK]]
        for _ in range(4):
            self.tick.once()
            if len(self.runs("implementer")) == 2:
                break
        first, second = self.runs("implementer")
        reviewer_run, validator_run, guide_run = self.runs("reviewer")[0], self.runs("validator")[0], self.runs("guide")[0]
        second_sp = self.spool_dir(second["run_id"])
        job = json.loads((second_sp / "job.json").read_text())
        entries = job["inputs"]["prior_runs"]
        # the newest implementer run is always kept: it, plus the two newest review runs, drop
        # the oldest review run (validator) rather than the report that names what was
        # actually implemented.
        self.assertEqual([(e["run_id"], e["role"]) for e in entries],
                         [(guide_run["run_id"], "guide"), (reviewer_run["run_id"], "reviewer"),
                          (first["run_id"], "implementer")])
        self.assertNotIn(validator_run["run_id"], [e["run_id"] for e in entries])
        # a second oracle on the job.json data: every named path is a real file, under this
        # rerun's own spool/prior/ (never the original spool dirs), and
        # its content matches the real reviewer run's real report on disk byte for byte.
        rev_entry = next(e for e in entries if e["run_id"] == reviewer_run["run_id"])
        rev_sp = self.spool_dir(reviewer_run["run_id"])
        rev_copy_root = second_sp / "prior" / reviewer_run["run_id"]
        self.assertEqual(Path(rev_entry["paths"][0]), rev_copy_root)
        for p in rev_entry["paths"][1:]:
            self.assertTrue(Path(p).exists(), p)
            self.assertIn(rev_copy_root, Path(p).parents)
        copy_report = next(p for p in rev_entry["paths"] if p.endswith("report/1.json"))
        self.assertEqual(Path(copy_report).read_text(), (rev_sp / "report" / "1.json").read_text())
        rev_doc = json.loads(Path(copy_report).read_text())
        self.assertEqual(rev_doc["verdict"], "fail")
        # the copy cannot be mutated, and the real spool dir
        # was left alone - nothing in it was ever chmod'd or copied from in place.
        with self.assertRaises(PermissionError):
            Path(copy_report).write_text("mutated")
        self.assertTrue((rev_sp / "report" / "1.json").stat().st_mode & 0o200)   # still writable
        prompt = json.loads((second_sp / "bundle.json").read_text())["prompt"]
        section = prompt.split("## Prior runs on this card", 1)[1]
        self.assertIn(reviewer_run["run_id"], section)
        self.assertIn(copy_report, section)
        # the row 520 mount decision: one add-dir, the rerun's own spool/prior/ - never the
        # original spool dirs (which stay closed to the implementer's Read tool).
        self.assertEqual(job["limits"]["add_dirs"], [str(second_sp / "prior")])
        first_prompt = json.loads((self.spool_dir(first["run_id"]) / "bundle.json").read_text())["prompt"]
        self.assertNotIn("## Prior runs on this card", first_prompt)   # the first run had none
