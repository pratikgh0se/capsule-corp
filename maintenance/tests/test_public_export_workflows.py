"""maintenance/public_export.py (P10), CI self-consistency: exported workflows only name paths the
export has, run on ubuntu, use no secret but GITHUB_TOKEN, and drop `# private-only` lines.
Fixture repos and a temp DEST only."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from public_export_fixture import Fixture  # noqa: E402

WF = ".github/workflows/ci.yml"
HEAD = "on: push\njobs:\n  t:\n    runs-on: ubuntu-latest\n    steps:\n"


class Workflows(Fixture):
    def wf(self, steps, head=HEAD):
        self.put("release/public-paths.txt",
                 "capsule/\nREADME.md\n.github/workflows/\nscripts/ok.sh\ntests/\n")
        self.put("scripts/ok.sh", "echo ok")
        self.put("scripts/secret.sh", "echo private")
        self.put("tests/t.sh", "echo t")
        self.put(WF, head + steps)
        return self.commit("wf")

    def refused(self, sha, *words):
        rc, _, err = self.export(sha)
        self.assertEqual(rc, 2)
        for w in words:
            self.assertIn(w, err)
        self.assertFalse((self.dest / ".git").exists())  # nothing committed

    def test_missing_path_names_workflow_and_path(self):
        self.refused(self.wf("      - run: bash scripts/secret.sh\n"), WF, "scripts/secret.sh")

    def test_missing_dir_in_block_run_fails(self):
        self.refused(self.wf("      - run: |\n          bash scripts/ok.sh\n          ls docs/\n"),
                     "docs")

    def test_present_paths_and_comments_pass(self):
        sha = self.wf("      - run: bash scripts/ok.sh tests/t.sh  # scripts/secret.sh\n"
                      "      # run scripts/secret.sh\n")
        self.assertEqual(self.export(sha)[0], 0)

    def test_private_only_line_is_trimmed_from_the_copy(self):
        sha = self.wf("      - run: |\n          bash scripts/ok.sh\n"
                      "          bash scripts/secret.sh   # private-only\n")
        self.assertEqual(self.export(sha)[0], 0)
        self.assertNotIn("secret", (self.dest / WF).read_text())
        self.assertIn("scripts/ok.sh", (self.dest / WF).read_text())

    def test_other_secret_fails_github_token_passes(self):
        self.refused(self.wf("      - run: echo ${{ secrets.NPM_TOKEN }}\n"), WF, "NPM_TOKEN")
        ok = self.wf("      - run: echo ${{ secrets.GITHUB_TOKEN }}\n")
        self.assertEqual(self.export(ok)[0], 0)

    def test_github_hosted_runners_pass(self):
        for label in ["ubuntu-latest", "macos-26", "macos-latest", "macos-14-xlarge"]:
            with self.subTest(label=label):
                sha = self.wf("      - run: echo hi\n", HEAD.replace("ubuntu-latest", label))
                self.assertEqual(self.export(sha)[0], 0)

    def test_other_runners_fail(self):
        for label in ["self-hosted", "[self-hosted, macOS]", "my-mac", "${{ matrix.os }}"]:
            with self.subTest(label=label):
                self.refused(self.wf("      - run: echo hi\n",
                                     HEAD.replace("ubuntu-latest", label)),
                             "only GitHub-hosted ubuntu or macos runners are allowed")

    def test_quoted_and_spaced_runs_on_keys_are_checked(self):
        for line in ['"runs-on": self-hosted', "'runs-on': [self-hosted]",
                     "runs-on : self-hosted", '"runs-on" : my-mac']:
            with self.subTest(line=line):
                head = HEAD.replace("runs-on: ubuntu-latest", line)
                self.refused(self.wf("      - run: echo hi\n", head),
                             "only GitHub-hosted ubuntu or macos runners are allowed")
        for line in ['"runs-on": ubuntu-latest', "runs-on : macos-26"]:
            with self.subTest(line=line):
                head = HEAD.replace("runs-on: ubuntu-latest", line)
                self.assertEqual(self.export(self.wf("      - run: echo hi\n", head))[0], 0)

    def test_every_path_form_is_checked(self):
        for step, word in [
            ("      - run: pip install -r requirements-ci.txt\n", "requirements-ci.txt"),
            ("      - run: python3 -m unittest discover nodir/tests\n", "nodir/tests"),
            ("      - run: python3 -m unittest discover -s nodir -t .\n", "nodir"),
            ("      - run: bash ./scripts/secret.sh\n", "scripts/secret.sh"),
            ("      - uses: ./.github/actions/x\n", ".github/actions/x"),
            ("      - run: tool --file=scripts/secret.sh\n", "scripts/secret.sh"),
            ("      - run: bash ${{ github.workspace }}/scripts/secret.sh\n", "scripts/secret.sh"),
        ]:
            with self.subTest(step=step):
                self.refused(self.wf(step), WF, word)

    def test_runs_on_without_value_and_any_secrets_fail(self):
        for step, head, word in [
            ("      - run: echo hi\n", HEAD.replace("ubuntu-latest", ""), "block form"),
            ("      - run: echo hi\n", HEAD + "      # x\n    secrets: inherit\n", "secrets"),
            ("      - run: echo ${{ secrets['X'] }}\n", HEAD, "secrets"),
            ("      - run: echo ${{ toJSON(secrets) }}\n", HEAD, "secrets"),
        ]:
            with self.subTest(step=step, head=head):
                self.refused(self.wf(step, head), WF, word)

    def test_private_only_marker_only_on_plain_run_block_lines(self):
        for step in [
            "      - run: bash scripts/ok.sh   # private-only\n",
            "      - run: |   # private-only\n          bash scripts/ok.sh\n",
            "      - name: x   # private-only\n        run: bash scripts/ok.sh\n",
            "      - run: |\n          foo:   # private-only\n          bash scripts/ok.sh\n",
            "      - run: |\n          - echo hi   # private-only\n",
        ]:
            with self.subTest(step=step):
                self.refused(self.wf(step), "private-only")

    def test_hash_inside_quotes_is_not_a_comment(self):
        self.refused(self.wf('      - run: echo "a # b" scripts/secret.sh\n'), "scripts/secret.sh")

    def test_second_export_makes_no_commit(self):
        sha = self.wf("      - run: bash scripts/ok.sh\n")
        self.assertEqual(self.export(sha)[0], 0)
        _, out, _ = self.export(sha)
        self.assertIn("no commit", out)


if __name__ == "__main__":
    unittest.main()
