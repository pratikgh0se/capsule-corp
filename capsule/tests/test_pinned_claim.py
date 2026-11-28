"""A legacy implementer run row (metadata NULL) pins nothing: the next claim pins the card."""
import subprocess
import unittest

from capsule import lease, paths, workspace
from capsule.tests.test_tick import BRANCH, IMPL_DONE, TickCase
from capsule.tests.test_tick import git


class NullMetadataRunPins(TickCase):
    def test_a_card_with_only_a_null_metadata_implementer_run_is_pinned_on_its_next_claim(self):
        origin = self.home / "origin"
        workspace.clone(self.cfg["repo_url"], BRANCH, paths.board_dir("test", self.home) / "work")
        (origin / "README.md").write_text("origin moved on\n")
        git(origin, "add", ".")
        git(origin, "commit", "-qm", "origin moved on")
        origin_tip = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(origin),
                                    capture_output=True, text=True, check=True).stdout.strip()
        lease.start_run(self.conn, self.tid, role="implementer", claim_lock=None, expires=0,
                        run_id="legacy", now=1)
        self.conn.execute("UPDATE task_runs SET status='done', ended_at=2 WHERE run_id='legacy'")
        self.conn.commit()
        self.script["implementer"] = [[IMPL_DONE]]
        for _ in range(6):
            self.tick.once()
            if self.task()["status"] == "done":
                break
        self.assertEqual(lease.pinned_start_sha(self.conn, self.tid), origin_tip)


if __name__ == "__main__":
    unittest.main()
