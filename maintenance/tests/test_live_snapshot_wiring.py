"""W347: the live-snapshot job is scheduled and listed like its siblings; its migration lints."""
import plistlib
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PG = REPO / "observability" / "postgres"
sys.path.insert(0, str(REPO / "maintenance"))
import migration_lint  # noqa: E402

LABEL = "__PREFIX__.capsule-corp-live-snapshot"


class Wiring(unittest.TestCase):
    def test_plist_runs_the_collector_directly_every_minute_into_one_log(self):
        path = REPO / "launchd" / "templates" / f"{LABEL}.plist"
        d = plistlib.loads(path.read_bytes())
        self.assertEqual((d["Label"], d["StartInterval"]), (LABEL, 60))
        self.assertEqual(d["ProgramArguments"][-1], "__REPO__/maintenance/live_snapshot.py")
        # not via run.sh: that would write a log file and two job events per run
        self.assertNotIn("run.sh", " ".join(d["ProgramArguments"]))
        self.assertEqual(d["StandardOutPath"], d["StandardErrorPath"])
        self.assertTrue(d["StandardOutPath"].endswith("/logs/live-snapshot.log"))
        self.assertNotIn("RunAtLoad", d)

    def test_job_is_custodial(self):
        jobs = (REPO / "bin" / "lib_jobs.sh").read_text()
        self.assertIn(LABEL.replace("__PREFIX__", "${_P}"), jobs)

    def test_migration_has_a_down_block_and_matching_init(self):
        self.assertEqual(migration_lint.lint(PG / "migrations"), [])
        mig = (PG / "migrations" / "007-live-snapshot.sql").read_text()
        body = mig[mig.index("create table"):]
        self.assertIn(body, (PG / "init.sql").read_text())


if __name__ == "__main__":
    unittest.main()
