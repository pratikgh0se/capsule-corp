"""A label in launchd/unloaded is install-only by design: dark_test must not FAIL it as unloaded."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from capsule.tests import tmpclean  # noqa: E402
tmpclean.install()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import dark_test  # noqa: E402
from dark_fixture import HEAL, DarkBase  # noqa: E402

REPORT = "dev.capsule.obsidian-report"
WATCH = "dev.capsule.capsule-corp-keeper-watch"


class DarkUnloaded(DarkBase):
    def listed(self, text):
        f = self.d / "unloaded"
        f.write_text(text)
        p = mock.patch.dict("os.environ", {"CAPSULE_LAUNCHD_UNLOADED": str(f)})
        p.start()
        self.addCleanup(p.stop)

    def plists(self):
        return {p["label"]: p for p in dark_test._plists()}

    def test_a_listed_label_is_excluded_as_install_only(self):
        self.listed(f"# why\n\n  {REPORT}  \r\n")
        row = self.plists()[REPORT]
        self.assertFalse(row["included"])
        self.assertEqual(row["reason"], "install-only (launchd/unloaded)")

    def test_prefix_placeholder_expands_and_unlisted_labels_stay_included(self):
        self.listed("__PREFIX__.capsule-corp-keeper-watch\n")
        rows = self.plists()
        self.assertFalse(rows[WATCH]["included"])
        self.assertTrue(rows[HEAL]["included"])
        self.assertTrue(rows[REPORT]["included"])

    def test_check_fails_an_unlisted_unloaded_job_but_not_a_listed_one(self):
        self.listed(f"{REPORT}\n")
        self.started()
        self.check_heal_ok()
        (self.d / "stubs" / f"launchctl.{REPORT}").unlink()      # neither is loaded
        (self.d / "stubs" / f"launchctl.{WATCH}").unlink()
        self.run_cmd("check")
        self.assertEqual(self.row(WATCH)["reason"], "not loaded in launchd")
        self.assertNotIn(REPORT, [r["item"] for r in self.results()])

    def test_a_missing_unloaded_file_excludes_nothing(self):
        self.listed("")
        (self.d / "unloaded").unlink()
        self.assertTrue(self.plists()[REPORT]["included"])


if __name__ == "__main__":
    unittest.main()
