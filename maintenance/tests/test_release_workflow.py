"""M232 / D222: release.yml is checksum-only. Raw-text assertions (stdlib only)."""
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WF = ROOT / ".github" / "workflows" / "release.yml"


class ReleaseWf(unittest.TestCase):
    def setUp(self):
        self.text = WF.read_text(encoding="utf-8")

    def test_triggers_are_tags_and_dispatch_only(self):
        self.assertRegex(self.text, r"tags:\s*\[\"v\*\"\]")
        self.assertIn("workflow_dispatch:", self.text)
        self.assertNotIn("branches:", self.text)

    def test_every_action_is_pinned_by_full_sha(self):
        uses = re.findall(r"uses:\s*(\S+)", self.text)
        self.assertGreaterEqual(len(uses), 4)
        for u in uses:
            self.assertRegex(u, r"^[\w.-]+/[\w.-]+@[0-9a-f]{40}$", u)

    def test_default_permissions_are_read_only_and_write_scopes_only_on_attest(self):
        top = self.text.split("\njobs:")[0]
        self.assertIn("permissions:\n  contents: read", top)
        self.assertNotIn("id-token", top)
        build, attest = self.text.split("\n  attest:")
        self.assertNotIn("id-token", build)
        self.assertNotIn("attestations: write", build)
        self.assertIn("id-token: write", attest)
        self.assertIn("attestations: write", attest)

    def test_attest_job_is_skipped_on_private_repos(self):
        attest = self.text.split("\n  attest:")[1]
        head = attest.split("steps:")[0]
        self.assertIn("if: ${{ !github.event.repository.private }}", head)
        self.assertIn("attest-build-provenance@", attest)
        self.assertIn("subject-path: dist/*.tar.gz", attest)

    @unittest.skipUnless((ROOT / "release" / "public-paths.txt").exists(),
                         "public tree: the export trims the private-only line this test pins")
    def test_export_scan_precedes_tar_and_public_branch_uses_git_archive(self):
        run = next(b for b in self.text.split("run: |")[1:] if "tar -C" in b)
        export = run.index("public_export.py")
        self.assertIn("--no-commit", run)
        self.assertIn("# private-only", run.splitlines()[run[:export].count("\n")])
        self.assertLess(export, run.index("tar -C"))
        self.assertLess(export, run.index("git archive"))
        self.assertNotIn("public_lint", self.text)

    def test_public_export_accepts_the_workflow_and_trims_the_private_line(self):
        sys.path.insert(0, str(ROOT / "maintenance"))
        import public_export
        lines = self.text.splitlines(keepends=True)
        kept = public_export._trim_private("release.yml", lines)
        self.assertFalse(any("public_export" in x and not x.lstrip().startswith("#") for x in kept))
        with tempfile.TemporaryDirectory() as tree:
            (Path(tree) / "maintenance").mkdir()
            (Path(tree) / "maintenance" / "public_export.py").write_text("x")
            top = {"release", "maintenance", "docs"}
            for line in kept:
                code = public_export._code(line)
                self.assertEqual(public_export._line_problems(code, top, tree), [])

    def test_writes_sha256sums_and_creates_no_release(self):
        self.assertIn("SHA256SUMS", self.text)
        self.assertIn("actions/upload-artifact@", self.text)
        for banned in ("gh release", "action-gh-release", "create-release", "softprops",
                       "contents: write"):
            self.assertNotIn(banned, self.text)


if __name__ == "__main__":
    unittest.main()
