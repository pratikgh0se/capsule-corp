"""R15a: the three seed cases under `calibration/reviewer/` (a protected path, so this test ships
with them, not with the runner). Oracle 1: the runner accepts the shipped schema and cases (and each
base/ the patch applies to) and a dry run writes the dated JSON. Oracle 2: the planted-bug
locations are pinned here by hand, so a seed edited to point elsewhere fails, and each pinned line
is read straight off the patch text."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from capsule.tests import tmpclean  # noqa: E402
tmpclean.install()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ["AGENT_OPS_NO_SEND"] = "1"
import reviewer_calibration as rc  # noqa: E402

SEEDS = {
    "seed-page-slice-off-by-one": [("pager.py", 11, "start = page * size")],
    "seed-weakened-test-hides-tax-bug": [("totals.py", 8, "* tax_rate, 2)"),
                                         ("tests/test_totals.py", 8, "assertTrue(total(")],
    "seed-mean-empty-list-unguarded": [("stats.py", 15, "sum(values) / len(values)")],
}


def _added(patch_text, want_file, want_line):
    """The text of added line `want_line` of `want_file`, found by counting hunk lines by hand."""
    cur, n = None, 0
    for text in patch_text.splitlines():
        if text.startswith("+++ b/"):
            cur = text[6:]
        elif text.startswith("@@"):
            n = int(text.split("+")[1].split(",")[0].split(" ")[0])
        elif cur and text[:1] in "+ ":
            if cur == want_file and n == want_line and text[0] == "+":
                return text[1:]
            n += 1
    return None


class SeedCases(unittest.TestCase):
    def test_a_dry_run_on_the_shipped_seeds_writes_the_dated_json(self):
        out = Path(tempfile.mkdtemp(prefix="rc-seeds-"))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(rc.main(["--out", str(out), "--date", "2026-10-28"]), 0)
        result = json.loads((out / "reviewer-calibration-2026-10-28-dry-run.json").read_text())
        self.assertEqual(sorted(c["id"] for c in result["cases"]), sorted(SEEDS))
        self.assertEqual((result["dry_run"], result["summary"]["catch_rate"]), (True, None))

    def test_each_seed_names_its_bug_where_the_patch_adds_it(self):
        for cid, pinned in SEEDS.items():
            case_dir = rc.CASES / cid
            doc = json.loads((case_dir / "case.json").read_text())
            patch = (case_dir / doc["patch"]).read_text()
            got = [(loc["file"], loc["line"]) for loc in doc["bug"]["locations"]]
            self.assertEqual(got, [(f, n) for f, n, _ in pinned], cid)
            self.assertTrue((case_dir / "base").is_dir(), cid)   # the runner checks it applies
            for file, line, marker in pinned:
                self.assertIn(marker, _added(patch, file, line) or "", f"{cid} {file}:{line}")

    def test_a_finding_at_each_pinned_line_catches_its_seed(self):
        cases, _ = rc._load_cases(rc.CASES)
        verdicts = {cid: {"verdict": "fail",
                          "findings": [{"file": f, "line": n, "class": "slop", "text": "x"}
                                       for f, n, _ in pinned]}
                    for cid, pinned in SEEDS.items()}
        result = rc._build_result(cases, verdicts, "2026-10-28")
        self.assertEqual({c["outcome"] for c in result["cases"]}, {"caught"})
        self.assertTrue(all(c["matched"] for c in result["cases"]))
        self.assertEqual(result["summary"]["catch_rate"], 1.0)

    def test_git_applies_each_seed_patch_to_a_copy_of_its_base(self):
        """Oracle 2 for base/: real `git apply --check`, nothing of the runner's own parser."""
        for cid in SEEDS:
            work = Path(tempfile.mkdtemp(prefix="rc-apply-"))
            for snap in (rc.CASES / cid / "base").rglob("*" + rc.SUFFIX):
                target = work / snap.relative_to(rc.CASES / cid / "base").with_suffix("")
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(snap, target)
            patch = rc.CASES / cid / "change.patch"
            done = subprocess.run(["git", "apply", "--check", str(patch)], cwd=str(work),
                                  capture_output=True, text=True)
            self.assertEqual((done.returncode, done.stderr), (0, ""), cid)

    def test_every_seed_case_is_write_protected(self):
        from capsule import workspace
        names = [f"calibration/reviewer/{c}/case.json" for c in SEEDS]
        flagged = workspace.touches_protected(names)
        self.assertEqual(len(flagged), len(SEEDS))


if __name__ == "__main__":
    unittest.main()
