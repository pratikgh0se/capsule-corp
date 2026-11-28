"""M173 `capsule threat-model check` and the doctor warning (specs/threat-model.md 1-6).
Two oracles: the verb's exit code and printed lines, and a direct read of the same file."""
import contextlib
import io
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from capsule.tests.public_tree import private_only
from capsule import doctor, threat_model
from capsule.__main__ import main

REPO = Path(__file__).resolve().parents[2]
GOOD = ("## Assets\n\n- the board DBs\n\n## Entry points\n\n- card text\n\n"
        "## Accepted risks\n\n- host agents are trusted (row 12)\n- a second one (D7)\n")


class _Repo(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.repo, True)

    def write(self, text):
        (self.repo / "docs").mkdir(exist_ok=True)
        (self.repo / "docs" / "threat-model.md").write_text(text, encoding="utf-8")

    def run_verb(self, repo=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = main(["threat-model", "check", "--repo", str(repo or self.repo)])
        return rc, out.getvalue().splitlines()


class Check(_Repo):
    def test_a_valid_file_is_ok(self):
        self.write(GOOD)
        self.assertEqual(self.run_verb(), (0, ["threat model ok"]))
        self.assertEqual(threat_model.check(self.repo), [])

    def test_none_recorded_is_valid(self):
        self.write(GOOD.split("## Accepted risks")[0] + "## Accepted risks\n\nnone recorded\n")
        self.assertEqual(self.run_verb(), (0, ["threat model ok"]))

    def test_row_and_d_decisions_both_count(self):
        self.write(GOOD)
        self.assertEqual(threat_model.check(self.repo), [])
        self.write(GOOD.replace("row 12", "no number").replace("D7", "D"))
        self.assertEqual(len(threat_model.check(self.repo)), 2)

    def test_a_wrapped_bullet_is_one_bullet(self):
        self.write(GOOD.replace("trusted (row 12)", "trusted\n  (row 12)"))
        self.assertEqual(threat_model.check(self.repo), [])

    def head(self, tail):
        return GOOD.split("## Accepted risks")[0] + "## Accepted risks\n\n" + tail

    def test_prose_star_and_numbered_risk_lines_are_problems(self):
        for line in ("Prose about row 12.", "* star item row 12", "1. numbered row 12"):
            self.write(self.head(f"- ok (row 1)\n{line}\n"))
            rc, lines = self.run_verb()
            self.assertEqual(rc, 1, line)
            self.assertEqual(len(lines), 1, line)
            self.assertIn("not a `- ` bullet", lines[0])
            self.assertTrue(lines[0].endswith(line))

    def test_none_recorded_must_be_alone(self):
        self.write(self.head("none recorded\n- x (row 1)\n"))
        self.assertEqual(len(threat_model.check(self.repo)), 1)

    def test_no_decision_line_says_lowercase(self):
        self.write(self.head("- Row 12 only\n"))
        self.assertIn("lowercase `row N` or `DN`", threat_model.check(self.repo)[0])

    def test_a_bom_is_fine(self):
        (self.repo / "docs").mkdir()
        (self.repo / "docs" / "threat-model.md").write_bytes(b"\xef\xbb\xbf" + GOOD.encode())
        self.assertEqual(threat_model.check(self.repo), [])

    def test_a_duplicate_heading_is_a_problem(self):
        self.write(GOOD + "\n## Assets\n\n- again\n")
        lines = threat_model.check(self.repo)
        self.assertEqual(len(lines), 1)
        self.assertIn("duplicate heading: ## Assets", lines[0])

    def test_fenced_lines_are_not_headings_or_bullets(self):
        self.write(GOOD + "\n```\n## Assets\n- no decision here\n```\n")
        self.assertEqual(threat_model.check(self.repo), [])
        self.write("```\n## Assets\n- a\n```\n" + GOOD.replace("## Assets", "## Other"))
        self.assertIn("heading missing: ## Assets", threat_model.check(self.repo)[0])

    def test_open_risks_section_is_ignored(self):
        self.write(GOOD + "\n## Open risks\n\n- no decision, fine\nprose\n")
        self.assertEqual(threat_model.check(self.repo), [])

    def test_tilde_and_empty_repo(self):
        self.write(GOOD)
        with mock.patch.dict("os.environ", {"HOME": str(self.repo.parent)}):
            rel = "~/" + self.repo.name
            self.assertEqual(threat_model.check(rel), [])
        self.assertEqual(threat_model.check(""), threat_model.check("."))

    def test_missing_file(self):
        rc, lines = self.run_verb()
        self.assertEqual(rc, 1)
        self.assertEqual(len(lines), 1)
        self.assertIn("docs/threat-model.md: file missing", lines[0])

    def test_missing_heading_one_line_each(self):
        self.write("## Assets\n\n- a\n")
        rc, lines = self.run_verb()
        self.assertEqual(rc, 1)
        self.assertEqual(len(lines), 2)
        self.assertTrue(any("heading missing: ## Entry points" in ln for ln in lines))
        self.assertTrue(any("heading missing: ## Accepted risks" in ln for ln in lines))

    def test_empty_section_and_section_without_bullet(self):
        self.write("## Assets\n\n## Entry points\n\nprose only\n\n## Accepted risks\n\n- x row 1\n")
        rc, lines = self.run_verb()
        self.assertEqual(rc, 1)
        self.assertTrue(any("section empty: ## Assets" in ln for ln in lines))
        self.assertTrue(any("no bullet: ## Entry points" in ln for ln in lines))

    def test_risk_without_decision_quotes_the_bullet_cut_to_80(self):
        bullet = "- " + "x" * 120
        self.write(GOOD.split("## Accepted risks")[0] + f"## Accepted risks\n\n{bullet}\n")
        rc, lines = self.run_verb()
        self.assertEqual(rc, 1)
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].endswith(bullet[:80]))
        self.assertNotIn(bullet[:81], lines[0])

    def test_it_writes_nothing(self):
        self.write(GOOD)
        before = sorted(p.name for p in self.repo.rglob("*"))
        self.run_verb()
        self.assertEqual(sorted(p.name for p in self.repo.rglob("*")), before)

    @private_only
    def test_this_repo_passes(self):
        self.assertEqual(self.run_verb(REPO), (0, ["threat model ok"]))


class DoctorWarning(_Repo):
    def doctor(self, repo):
        out = io.StringIO()
        rows = [("a thing", None)]
        args = SimpleNamespace(offline=True, capsule_home=str(self.repo), repo=repo)
        with mock.patch.object(doctor, "run", return_value=rows), \
                mock.patch.object(doctor.host, "check_row", return_value=("ok", 0, "x")), \
                contextlib.redirect_stdout(out):
            rc = doctor.handle(args, None)
        return rc, out.getvalue()

    def test_warns_when_missing_and_exit_status_is_unchanged(self):
        rc, out = self.doctor(str(self.repo))
        self.assertEqual(rc, 0)
        self.assertIn("warning", out)
        self.assertIn("docs/threat-model.md", out)
        self.assertIn("doctor: all prerequisites met", out)

    def test_silent_when_valid_or_no_repo_given(self):
        self.write(GOOD)
        for repo in (str(self.repo), None):
            rc, out = self.doctor(repo)
            self.assertEqual(rc, 0)
            self.assertNotIn("docs/threat-model.md", out)

    def test_empty_repo_means_the_current_directory_in_doctor_too(self):
        self.write(GOOD)
        empty = Path(tempfile.mkdtemp())     # our own empty directory, not the shared system temp
        self.addCleanup(shutil.rmtree, empty, True)
        with mock.patch.dict("os.environ", {}), contextlib.chdir(self.repo):
            rc, out = self.doctor("")
        self.assertEqual((rc, "docs/threat-model.md" in out), (0, False))
        with contextlib.chdir(empty):
            rc, out = self.doctor("")       # a directory with no docs/threat-model.md
        self.assertEqual(rc, 0)
        self.assertEqual(out.count("docs/threat-model.md"), 1)
        self.assertIn("warning docs/threat-model.md", out)

    def test_a_missing_prerequisite_still_exits_1(self):
        out = io.StringIO()
        args = SimpleNamespace(offline=True, capsule_home=str(self.repo), repo=str(self.repo))
        with mock.patch.object(doctor, "run", return_value=[("x", "fix it")]), \
                mock.patch.object(doctor.host, "check_row", return_value=("ok", 0, "x")), \
                contextlib.redirect_stdout(out):
            self.assertEqual(doctor.handle(args, None), 1)


if __name__ == "__main__":
    unittest.main()
