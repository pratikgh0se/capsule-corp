"""t_cfed2fd9 (inc_e6170a47): after a hand merge a gate pass finds nothing new to commit, so
`finish_gate_pass` named no commit and landing never saw the card. The card's own commit on the
lane is named instead (never the merge). Real origin, real clone; `landing._candidates` is the
second oracle."""
import json
import time
import unittest
from types import SimpleNamespace

from capsule import epic_sync, lander, landing, workspace
from capsule.tests.test_epic_sync import BRANCH, EpicSyncCase, git

GATES = {"validator": "pass", "reviewer": "pass"}
TICK = SimpleNamespace(_first_screen=lambda *a: None, _start_sha=lambda *a: "0" * 40,
                       _gate_word=lambda doc: "pass")


class HandMergedCardLands(EpicSyncCase):
    def setUp(self):
        super().setUp()
        self.cfg.update(repo_url=str(self.origin), lander={"mode": "dry-run"},
                        contract_commands=[])

    def on_main(self, text, name="README.md"):
        (self.origin / name).write_text(text)
        git(self.origin, "add", ".")
        git(self.origin, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "main moved")

    def card_commit(self, task, text="card line\n", name="README.md"):
        (self.ws / name).write_text(text)
        return workspace.commit(self.ws, f"the card ({task['id']}, capsule run r1)")

    def hand_merge(self, text="both kept\n"):
        workspace._git("fetch", "origin", "main", ws=self.ws)
        with self.assertRaises(RuntimeError):
            workspace._git(*workspace.IDENTITY, "merge", "--no-edit", "origin/main", ws=self.ws)
        (self.ws / "README.md").write_text(text)
        workspace._git("add", "-A", ws=self.ws)
        workspace._git(*workspace.IDENTITY, "commit", "-qm", "merge main by hand", ws=self.ws)

    def finish(self, task):
        impl = {"id": None, "run_id": "r1", "handoff": None}
        epic_sync.finish_gate_pass(TICK, self.conn, self.cfg, task, self.ws, [], impl, {}, "r1",
                                   int(time.time()), False, [], ("validator", "reviewer"))

    def completed(self, task):
        row = self.conn.execute("SELECT payload FROM task_events WHERE task_id=? AND "
                                "kind='completed'", (task["id"],)).fetchone()
        return json.loads(row[0])

    def candidates(self):
        return list(landing._candidates(self.conn, "dry-run", int(time.time()), self.cfg,
                                        self.dir, None))

    def conflicted_card(self):
        task = self.new_task()
        self.on_main("main line\n")
        tip = self.card_commit(task)
        return task, tip

    def \
test_a_gate_pass_with_nothing_new_after_a_hand_merge_still_offers_the_card_commit_to_landing(
            self):
        task, tip = self.conflicted_card()
        self.hand_merge()
        merge = workspace.head(self.ws)
        self.finish(task)
        self.assertEqual(self.completed(task)["commit"], tip)
        self.assertNotEqual(self.completed(task)["commit"], merge)
        self.assertEqual([(t, b, s) for t, b, s in self.candidates()], [(task["id"], BRANCH, tip)])

    def test_a_card_with_no_commit_of_its_own_completes_with_no_commit(self):
        task = self.new_task()
        self.finish(task)
        self.assertNotIn("commit", self.completed(task))
        self.assertEqual(self.candidates(), [])

    def test_a_branch_that_is_not_a_declared_epic_keeps_a_commit_less_completed(self):
        task = self.new_task(branch="not-an-epic")
        self.card_commit(task)
        workspace._git(*workspace.IDENTITY, "commit", "-q", "--allow-empty", "-m",
                       f"empty ({task['id']}, capsule run r1)", ws=self.ws)
        self.finish(task)
        self.assertNotIn("commit", self.completed(task))

    def test_a_replay_conflict_after_a_hand_merge_holds_with_the_files_named(self):
        task, tip = self.conflicted_card()
        self.hand_merge()
        self.finish(task)
        workspace._git("push", "-q", "origin", f"HEAD:refs/heads/{BRANCH}", ws=self.ws)
        res = lander.run(self.ws, BRANCH, cfg=self.cfg, commit=tip, task_id=task["id"])
        self.assertEqual(res["verdict"], "wait")
        self.assertEqual(res["hold"], "lander_wait_rebase")
        sync = next(r for r in res["checks"] if r["name"] == "sync")
        self.assertIn("README.md", sync["detail"])

    def test_a_new_commit_in_the_round_is_named_as_before(self):
        task = self.new_task()
        (self.ws / "new.txt").write_text("new work\n")
        self.finish(task)
        tip = workspace.head(self.ws)
        self.assertEqual(self.completed(task)["commit"], tip)
        self.assertIn(task["id"], workspace._git("log", "-1", "--format=%s", ws=self.ws).stdout)


if __name__ == "__main__":
    unittest.main()
