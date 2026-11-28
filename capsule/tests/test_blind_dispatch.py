"""R30 senior-review fixes: the leak lint finds a plain name inside any backtick span, and the leak
check at dispatch sees the work a running card has done so far in the shared epic clone."""
import collections
import json
import os
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from capsule import bundles, db, paths, workspace
from capsule.blind import dispatch as blind_dispatch
from capsule.blind import cli, launch, scan, snapshot
from capsule.tests import test_tick as tt


def _git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@e.invalid", *args], cwd=cwd,
                   check=True, capture_output=True)


class PlainNames(unittest.TestCase):
    """The leak lint, through the brief renderer: which leaked names it refuses."""

    def lint(self, text, names=("take", "run", "dispatcher")):
        card = {"id": "t_1", "title": "T", "acceptance": [text], "card_hash": "h"}
        with tempfile.TemporaryDirectory() as empty:
            try:
                bundles.render_blind_tester_brief(card, empty, names)
            except snapshot.BriefLeak as leak:
                return leak.identifiers
        return []

    def test_dotted_and_multiword_spans_count(self):
        self.assertEqual(self.lint("call `snapshot.take` first"), ["take"])
        self.assertEqual(self.lint("then `take x` and go"), ["take"])
        self.assertEqual(self.lint("`dispatcher --fast`"), ["dispatcher"])

    def test_call_form_counts(self):
        self.assertEqual(self.lint("it calls take(x)"), ["take"])
        self.assertEqual(self.lint("`run()`"), ["run"])

    def test_bare_prose_is_allowed(self):
        self.assertEqual(self.lint("take a moment and run the dispatcher"), [])
        self.assertEqual(self.lint("a `code span` here, then take it"), [])

    def test_a_span_never_crosses_a_line(self):
        self.assertEqual(self.lint("one ` take\nthe other ` run"), [])

    def test_a_compound_name_counts_in_prose(self):
        self.assertEqual(self.lint("use frobnicate_zebra now", ("frobnicate_zebra",)),
                         ["frobnicate_zebra"])


class BlindBoard(tt.TickCase):
    """A board with a blind repo, `report` mode and a tester route; no card yet."""

    def setUp(self):
        super().setUp()
        # a tester's setup runs on a thread; one step waits for it, as the inline setup did
        patch = mock.patch.object(launch, "GRACE", 60)
        patch.start()
        self.addCleanup(patch.stop)
        blind = self.home / "blindrepo"
        blind.mkdir()
        _git(blind, "init", "-q", "-b", "main")
        (blind / "blind.toml").write_text(
            f'version = 1\nboard = "test"\nadapter = "unittest"\nrun = "{cli.RUN_LINE}"\n'
            'list = "y"\n')
        _git(blind, "add", ".")
        _git(blind, "commit", "-qm", "i")
        self.cfg["blind_tests"] = {"repo": str(blind), "adapter": "unittest",
                                   "tester_may_read": [], "interface_commands": []}
        self.cfg["redesign"] = dict(self.cfg.get("redesign") or {}, blind_tests="report")
        self.cfg["routing"]["blind-tester"] = dict(self.cfg["routing"]["implementer"])
        self.cfg["caps"].update(max_review=3, max_heavy=6, max_running=2)
        self.write_cfg()
        self.t = self.make_tick(spawn=lambda *a: None)
        self.t.host_tiers, self.t.strong, self.t.board_db = {"strong"}, self.runner, self.board
        self.conn.execute("UPDATE tasks SET status='archived' WHERE id=?", (self.tid,))
        self.conn.commit()

    def step(self):
        cfg = json.loads((self.home / "config.json").read_text())
        blind_dispatch.step(self.t, self.conn, cfg, int(time.time()), collections.Counter())

    def blind_runs(self):
        return self.conn.execute("SELECT * FROM task_runs WHERE profile='blind-tester'").fetchall()

    def card_with(self, status, title="c", body="Acceptance:\n- it works\n"):
        card = db.new_task(self.conn, title=title, assignee="dev-frontend", branch=tt.BRANCH,
                           body=body)
        self.conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, card))
        self.conn.commit()
        return card

    def covered(self):
        """Ids of the cards that have a tester run."""
        return {r[0] for r in self.conn.execute(
            "SELECT task_id FROM task_runs WHERE profile='blind-tester'")}


class OwnWork(BlindBoard):
    def setUp(self):
        super().setUp()
        self.card = db.new_task(self.conn, title="c", assignee="dev-frontend", branch=tt.BRANCH,
                                body="Acceptance:\n- frobnicate_zebra works\n- other\n")
        origin = self.home / "origin"
        start = subprocess.run(["git", "rev-parse", "HEAD"], cwd=origin, capture_output=True,
                               text=True).stdout.strip()
        self.conn.execute("UPDATE tasks SET status='running' WHERE id=?", (self.card,))
        self.conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, started_at, run_id, metadata)"
            " VALUES (?,?,?,?,?,?)",
            (self.card, "implementer", "running", 1, "impl1", json.dumps({"start_sha": start})))
        self.conn.commit()

    def skips(self):
        return [json.loads(r[0]) for r in self.conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='blind_tester_skipped'",
            (self.card,))]

    def test_names_in_the_running_cards_own_work_refuse_the_brief(self):
        cfg = json.loads((self.home / "config.json").read_text())
        epic = workspace.clone(cfg["repo_url"], tt.BRANCH, self.t._work_root(cfg))
        (epic / "gizmo.py").write_text("def frobnicate_zebra():\n    pass\n")   # uncommitted
        self.step()
        self.assertEqual(self.blind_runs(), [])          # a refusal leaves no tester run
        self.assertEqual(len(self.skips()), 1)
        self.assertTrue(self.skips()[0]["final"])
        self.assertIn("frobnicate_zebra", self.skips()[0]["reason"])
        self.assertEqual(self.conn.execute("SELECT status FROM tasks WHERE id=?",
                                           (self.card,)).fetchone()[0], "running")

    def test_a_refused_start_is_not_retried_and_leaves_nothing_behind(self):
        cfg = json.loads((self.home / "config.json").read_text())
        (workspace.clone(cfg["repo_url"], tt.BRANCH, self.t._work_root(cfg)) / "gizmo.py"
         ).write_text("def frobnicate_zebra():\n    pass\n")
        for _ in range(3):
            self.step()
        self.assertEqual(self.blind_runs(), [])
        self.assertEqual(len(self.skips()), 1)           # the final event stops the retries
        board = paths.board_dir("test", self.home)
        self.assertEqual([p.name for p in (board / "spool").glob("*")], [])
        self.assertEqual([p.name for p in (board / "blind-work").glob("*") if p.name != ".gitdirs"],
                         [])

    def test_reading_the_work_leaves_the_implementers_index_alone(self):
        cfg = json.loads((self.home / "config.json").read_text())
        epic = workspace.clone(cfg["repo_url"], tt.BRANCH, self.t._work_root(cfg))
        (epic / "gizmo.py").write_text("def frobnicate_zebra():\n    pass\n")   # new, untracked
        (epic / "README.md").unlink()                                            # tracked, deleted
        index = workspace.gitdir(epic) / "index"

        def state():
            return (index.read_bytes(), workspace._git("ls-files", ws=epic).stdout,
                    workspace._git("diff", "--cached", "--name-only", ws=epic).stdout)
        before = state()
        self.step()
        self.assertIn("frobnicate_zebra", self.skips()[0]["reason"])     # it did read the work
        self.assertEqual(before, state())
        self.assertEqual(state()[1], "README.md\n")    # `ls-files` still lists the old file only

    def test_no_epic_clone_yet_means_no_names_and_no_clone(self):
        cfg = json.loads((self.home / "config.json").read_text())
        pinned = launch._pinned(self.conn, self.card)
        epic = self.t._work_root(cfg) / workspace._slug(tt.BRANCH)
        self.assertEqual(scan._work_names(epic, pinned), set())
        self.assertFalse(list(self.t._work_root(cfg).glob("*")))

    def epic(self):
        cfg = json.loads((self.home / "config.json").read_text())
        return workspace.clone(cfg["repo_url"], tt.BRANCH, self.t._work_root(cfg))

    def test_a_stat_stale_index_is_not_rewritten(self):
        epic = self.epic()
        os.utime(epic / "README.md", (1_000_000_000, 1_000_000_000))   # same content, new stat
        index = workspace.gitdir(epic) / "index"
        before = index.read_bytes()
        scan._work_names(epic, launch._pinned(self.conn, self.card))
        self.assertEqual(before, index.read_bytes())

    def test_links_unreadable_and_huge_files_are_read_safely(self):
        epic = self.epic()
        outside = self.home / "outside.py"
        outside.write_text("def via_link():\n    pass\n")
        (epic / "link.py").symlink_to(outside)
        (epic / "locked.py").write_text("def hidden_name():\n    pass\n")
        (epic / "locked.py").chmod(0)
        (epic / "big.py").write_text("#" * 250_000 + "\ndef past_the_cap():\n    pass\n")
        (epic / "small.py").write_text("def inside_the_cap():\n    pass\n")
        try:
            names = scan._work_names(epic, launch._pinned(self.conn, self.card))
        finally:
            (epic / "locked.py").chmod(0o644)
        self.assertLessEqual({"link.py", "locked.py", "big.py", "inside_the_cap"}, names)
        self.assertFalse({"via_link", "hidden_name", "past_the_cap"} & names)

    def test_a_ready_card_that_keeps_its_work_still_gets_its_names(self):
        self.conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (self.card,))
        self.conn.commit()
        (self.epic() / "gizmo.py").write_text("def frobnicate_zebra():\n    pass\n")
        self.step()
        self.assertIn("frobnicate_zebra", self.skips()[0]["reason"])

    def test_a_running_card_with_no_work_yet_gets_its_tester(self):
        self.step()
        self.assertEqual([r["status"] for r in self.blind_runs()], ["running"])
        self.assertEqual(self.skips(), [])
        self.assertTrue(paths.board_dir("test", self.home).is_dir())


class Seats(BlindBoard):
    def test_the_limit_counts_testers_on_every_board(self):
        self.cfg["caps"]["max_review"] = 2        # N-1 = 1 tester
        self.write_cfg()
        other = paths.board_db("other", self.home)
        db.create_board(other)
        oconn = db.connect(other)
        self.addCleanup(oconn.close)
        task = db.new_task(oconn, title="t", branch="b", assignee="dev-frontend")
        oconn.execute("INSERT INTO task_runs (task_id, profile, status, started_at, run_id,"
                      " claim_lock) VALUES (?,?,?,?,?,?)",
                      (task, "blind-tester", "running", 1, "other-run", f"{self.t.host}:other-run"))
        oconn.commit()
        self.card_with("ready")
        self.step()
        self.assertEqual(self.blind_runs(), [])

    def test_without_one_on_the_other_board_the_tester_starts(self):
        self.cfg["caps"]["max_review"] = 2
        self.write_cfg()
        self.card_with("ready")
        self.step()
        self.assertEqual(len(self.blind_runs()), 1)


class HeavySeat(BlindBoard):
    """A tester never takes the last heavy seat: it needs heavy in use < max_heavy - 1."""

    def run_with(self, max_heavy, running):
        self.cfg["caps"].update(max_heavy=max_heavy, max_review=3, max_running=9)
        self.write_cfg()
        for _ in range(running):
            self.card_with("running")
        self.step()
        return len(self.blind_runs())

    def test_a_tester_starts_one_seat_short_of_the_last(self):
        self.assertEqual(self.run_with(3, 1), 1)       # 1 of 3 in use: 2 left, one stays free

    def test_it_waits_when_only_the_last_seat_is_left(self):
        self.assertEqual(self.run_with(3, 2), 0)       # 2 of 3 in use: exactly limit - 1

    def test_it_never_starts_at_a_limit_of_one(self):
        self.card_with("ready")
        self.assertEqual(self.run_with(1, 0), 0)


class ReadyRows(BlindBoard):
    def test_only_the_first_ready_card_of_a_branch_gets_a_tester(self):
        first = self.card_with("ready", "first")
        self.card_with("ready", "second")
        self.step()
        self.assertEqual(self.covered(), {first})

    def test_a_ready_card_behind_a_busy_branch_waits(self):
        busy = self.card_with("running", "busy")
        waiting = self.card_with("ready", "waiting")
        self.step()
        self.assertEqual(self.covered(), {busy})
        self.assertNotIn(waiting, self.covered())


class LateCards(BlindBoard):
    """A card that is validated before any tester seat freed drops out with one final skip."""

    def validated(self, body="Acceptance:\n- it works\n"):
        card = self.card_with("review", body=body)
        self.start = start = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.home / "origin",
                                            capture_output=True, text=True).stdout.strip()
        for n, (profile, outcome) in enumerate((("implementer", "completed"),
                                                ("validator", "completed"))):
            self.conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, outcome, started_at, run_id,"
                " claim_lock, metadata) VALUES (?,?,?,?,?,?,?,?)",
                (card, profile, "done", outcome, 1, f"{card}-{n}", f"h:{card}-{n}",
                 json.dumps({"start_sha": start})))
        self.conn.commit()
        return card

    def late(self):
        cfg = json.loads((self.home / "config.json").read_text())
        blind_dispatch.late(self.t, self.conn, cfg, int(time.time()), collections.Counter())

    def skips(self, card):
        return [json.loads(p[0]) for p in self.conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='blind_tester_skipped'",
            (card,))]

    def test_it_gets_one_final_skip_and_nothing_else(self):
        card = self.validated()
        for _ in range(3):
            self.late()
        (skip,) = self.skips(card)
        self.assertFalse(skip["final"])
        self.assertIn("seat", skip["reason"])
        self.assertEqual(self.blind_runs(), [])
        self.assertEqual(self.conn.execute("SELECT status FROM tasks WHERE id=?",
                                           (card,)).fetchone()[0], "review")

    def test_a_card_that_was_already_refused_gets_no_second_event(self):
        card = self.card_with("running", body="no acceptance statements here")
        self.step()                          # a candidate: refused, one final skip
        self.assertEqual(len(self.skips(card)), 1)
        for n, profile in enumerate(("implementer", "validator")):
            self.conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, outcome, started_at, run_id,"
                " claim_lock) VALUES (?,?,?,?,?,?,?)",
                (card, profile, "done", "completed", 1, f"{card}-{n}", f"h:{card}-{n}"))
        self.conn.execute("UPDATE tasks SET status='review' WHERE id=?", (card,))
        self.conn.commit()
        self.late()                          # validated: it was refused, so no second event
        self.assertEqual(len(self.skips(card)), 1)

    def test_a_card_sent_back_for_rework_still_gets_its_tester_on_the_pinned_base(self):
        card = self.validated()
        self.late()                                   # no seat freed: a non-final skip
        self.assertEqual(len(self.skips(card)), 1)
        self.conn.execute("UPDATE tasks SET status='running' WHERE id=?", (card,))
        self.conn.execute(      # the rework: a newer implementer run, the same card hash
            "INSERT INTO task_runs (task_id, profile, status, started_at, run_id, metadata)"
            " VALUES (?,?,?,?,?,?)",
            (card, "implementer", "running", 2, "impl-rework", json.dumps({"start_sha": "moved"})))
        self.conn.commit()
        self.step()                                   # a seat is free now
        (run,) = self.blind_runs()
        self.assertEqual(json.loads(run["metadata"])["start_sha"], self.start)
        self.assertEqual(len(self.skips(card)), 1)

    def test_a_failure_on_one_card_does_not_stop_the_pass(self):
        first, second = self.validated(), self.validated()
        real = blind_dispatch._late_one

        def boom(conn, task, now):
            if task["id"] == first:
                raise RuntimeError("late failed")
            return real(conn, task, now)
        with mock.patch.object(blind_dispatch, "_late_one", boom):
            self.late()                       # `tick._guarded` takes the raise, the next card runs
        self.assertEqual(len(self.skips(second)), 1)

    def test_a_card_whose_tester_ran_gets_no_skip(self):
        card = self.validated()
        self.conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, started_at, run_id, claim_lock,"
            " metadata) VALUES (?,?,?,?,?,?,?)",
            (card, "blind-tester", "done", 1, "bt", "h:bt",
             json.dumps({"card_hash": bundles.card_hash(
                 self.conn.execute("SELECT body FROM tasks WHERE id=?", (card,)).fetchone()[0])})))
        self.conn.commit()
        self.late()
        self.assertEqual(self.skips(card), [])

    def test_the_normal_case_the_validator_ends_last_and_the_verdict_passes_in_one_pass(self):
        self.cfg["caps"].update(max_review=1)       # no tester seat: N-1 = 0
        self.write_cfg()
        self.conn.execute("UPDATE tasks SET status='ready', body=? WHERE id=?",
                          ("Acceptance:\n- it works\n", self.tid))
        self.conn.commit()
        self.script["implementer"] = [[tt.IMPL_DONE]]
        for _ in range(20):
            self.tick.once()
            if self.task()["status"] == "done":
                break
        self.assertEqual(self.task()["status"], "done")
        self.assertEqual(self.blind_runs(), [])
        (skip,) = self.skips(self.tid)
        self.assertFalse(skip["final"])
        self.assertIn("seat", skip["reason"])


if __name__ == "__main__":
    unittest.main()
