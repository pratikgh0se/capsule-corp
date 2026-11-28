"""EVAL-03 run recording: the start state of a run lands beside run.json."""
import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capsule import golden, run_record, workspace


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


class RunRecordTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "work" / "ws"            # workspace.gitdir(ws) layout
        self.spool = self.tmp / "spool" / "r1"
        self.spool.mkdir(parents=True)
        self.repo.mkdir(parents=True)
        workspace.gitdir(self.repo).parent.mkdir()
        subprocess.run(["git", "init", "-q", f"--separate-git-dir={workspace.gitdir(self.repo)}",
                        str(self.repo)], check=True, capture_output=True)
        (self.repo / "f.txt").write_text("one\n")
        _git(self.repo, "add", ".")
        _git(self.repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "c")
        self.head = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"],
                                   capture_output=True, text=True).stdout.strip()

    def test_planted_git_dir_is_not_trusted(self):
        (self.repo / ".git").unlink()
        _git(self.repo, "init", "-q")
        sentinel = self.tmp / "ran"
        hook = self.tmp / "hook.sh"
        hook.write_text(f"#!/bin/sh\ntouch {sentinel}\n")
        hook.chmod(0o755)
        _git(self.repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x",
             "--allow-empty")
        _git(self.repo, "config", "core.fsmonitor", str(hook))
        (self.repo / "f.txt").write_text("two\n")
        run_record.record(self.spool, self.repo, "implementer", {})
        self.assertFalse(sentinel.exists())
        self.assertEqual(golden._start_state("r1", self.spool.parent)["head_sha"], self.head)

    def test_patch_is_private(self):
        (self.repo / "f.txt").write_text("two\n")
        run_record.record(self.spool, self.repo, "implementer", {})
        self.assertEqual((self.spool / "start_diff.patch").stat().st_mode & 0o777, 0o600)

    def test_dirty_tree_records_sha_and_diff_hash(self):
        (self.repo / "f.txt").write_text("two\n")
        with mock.patch.object(run_record, "_image", return_value="img@sha256:ab"):
            run_record.record(self.spool, self.repo, "implementer", {"model": "m1"})
        got = golden._start_state("r1", self.spool.parent)
        patch = (self.spool / "start_diff.patch").read_bytes()
        self.assertEqual(got["head_sha"], self.head)
        self.assertEqual(got["diff_sha256"], hashlib.sha256(patch).hexdigest())
        self.assertEqual((got["model"], got["role"], got["runner_image"]),
                         ("m1", "implementer", "img@sha256:ab"))

    def test_meter_fields_come_from_route_and_run(self):
        meta = '{"batch_id": "b1", "lane": "epic"}'
        run = {"resumed_from": "r0", "metadata": meta}
        route = {"adapter": "claude", "reasoning_effort": "high"}
        run_record.record(self.spool, self.repo, "implementer", route, run)
        got = golden._start_state("r1", self.spool.parent)
        keys = ("reasoning_effort", "resumed_from", "batch_id", "lane")
        self.assertEqual([got[k] for k in keys], ["high", "r0", "b1", "epic"])

    def test_reasoning_effort_is_null_when_the_adapter_does_not_apply_it(self):
        route = {"adapter": "stub", "reasoning_effort": "high"}
        run_record.record(self.spool, self.repo, "implementer", route, None)
        self.assertIsNone(golden._start_state("r1", self.spool.parent)["reasoning_effort"])

    def test_meter_fields_are_null_when_unset(self):
        for run in (None, {"resumed_from": None, "metadata": None}):
            run_record.record(self.spool, self.repo, "implementer", {}, run)
            got = golden._start_state("r1", self.spool.parent)
            for key in ("reasoning_effort", "resumed_from", "batch_id", "lane"):
                self.assertIn(key, got)
                self.assertIsNone(got[key])

    def test_clean_tree_has_no_diff_file(self):
        run_record.record(self.spool, self.repo, "reviewer", {})
        got = golden._start_state("r1", self.spool.parent)
        self.assertIsNone(got["diff_sha256"])
        self.assertFalse((self.spool / "start_diff.patch").exists())

    def test_diff_capped(self):
        (self.repo / "f.txt").write_text("x" * (run_record.DIFF_CAP + 10))
        run_record.record(self.spool, self.repo, "implementer", {})
        got = golden._start_state("r1", self.spool.parent)
        self.assertIsNone(got["diff_sha256"])
        self.assertFalse((self.spool / "start_diff.patch").exists())

    def test_golden_run_hands_start_state_to_adapter(self):
        run_record.record(self.spool, self.repo, "reviewer", {})
        case = self.tmp / "cases" / "c1"
        case.mkdir(parents=True)
        (case / "case.json").write_text('{"bundle": "b", "expect": {"a": 1}, "run_id": "r1"}')
        seen = []
        adapter = mock.Mock()
        adapter.run.side_effect = lambda *a: seen.append(a[3]) or mock.Mock(report={"a": 1})
        golden.run("reviewer", self.tmp / "cases", adapter, spool_root=self.spool.parent)
        self.assertEqual(seen[0]["start_state"]["head_sha"], self.head)

    def test_error_never_raises(self):
        self.assertIsNone(run_record.record(self.spool, self.tmp / "nope", "guide", {}))
        self.assertIsNone(golden._start_state("r1", self.spool.parent))


if __name__ == "__main__":
    unittest.main()
