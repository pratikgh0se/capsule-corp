# mutation-check: targets-predate-range b957739c9 -- renames only, no behaviour pinned ::
#   HumanFix.test_a_file_the_tick_already_moved_to_done_counts,
#   HumanFix.test_handover_tagged_files_are_not_human_fixes,
#   HumanFix.test_owner_files_in_the_window_count_and_a_missing_author_is_owner
"""Card t_41bb09e8 (Evals E4): `capsule/outcomes.py` writes one `outcome` per landed card per window
(14 d headline, 30 d tracked) and opens `escaped_bug` when a revert, a naming fix commit or a
defect incident follows the landing. Two oracles (Z2): the code's verdict against the example's
hand-judged answer, and git's own `revert` against a hand-written revert message.
`human_fix` counts the owner's intervention.v1 files naming the card in the window (cos never)."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capsule import db, human, incidents, intent, outcomes, paths, report, spool
from capsule.tests import tmpclean

DAY = 86400
LAND = 1_700_000_000


class Case(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(tmpclean.sweep, self.dir)
        self.repo = self.dir / "repo"
        self.repo.mkdir()
        db.create_board(self.dir / "kanban.db")
        self.conn = db.connect(self.dir / "kanban.db")
        self.addCleanup(self.conn.close)
        patch = mock.patch.object(outcomes, "SINCE", LAND)   # cut-off at the shared landing
        patch.start()
        self.addCleanup(patch.stop)
        self.git("init", "-q", "-b", "main")
        self.n = 0

    def git(self, *args, day=0):
        stamp = f"@{LAND + day * DAY} +0000"
        env = dict(os.environ, GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp, GIT_AUTHOR_NAME="t",
                   GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        return subprocess.run(["git", "-C", str(self.repo), *args], env=env, check=True,
                              capture_output=True, text=True).stdout.strip()

    def commit(self, message, day=0, name="f.txt"):
        self.n += 1
        (self.repo / name).write_text(f"{self.n}\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message, day=day)
        return self.git("rev-parse", "HEAD")

    def card(self, title, sha):
        """A done card whose `completed` event (at LAND) names commit `sha`."""
        tid = db.new_task(self.conn, title=title, assignee="dev", status="done", now=LAND - DAY)
        db.event(self.conn, tid, "completed", {"commit": sha, "gates": {}}, now=LAND)
        self.conn.execute("UPDATE tasks SET completed_at=? WHERE id=?", (LAND, tid))
        return tid

    def landed(self, title="parser card"):
        """A done card whose landing commit is on main and whose `completed` event is at LAND."""
        sha = self.commit(f"capsule: {title} (t_pending)")
        return self.card(title, sha), sha

    def run_step(self, days):
        return outcomes.step(self.conn, checkout=self.repo, board="b", now=LAND + days * DAY,
                             home=self.dir / "home")

    def human(self, tid, day, author="owner", kind="reset", tag=None, at=None):
        """A real intervention.v1 spool file on card `tid`, `day` days after LAND (or at `at`)."""
        human.write(self.dir / "home", "b", tid, kind, "redo it", tag=tag, author=author,
                    now=LAND + day * DAY if at is None else at,
                    **({"check": "c", "ttl": "1d", "fix_card": "t_1", "issuer": "p"}
                       if kind == "override" else {}))

    def raw_human(self, tid, day, folder="", **extra):
        """A hand-written spool file (keys as given), optionally under human/<folder>."""
        d = spool.human_dir(self.dir / "home", "b") / folder
        d.mkdir(parents=True, exist_ok=True)
        doc = {"task_id": tid, "kind": "reset", "text": "x", "at": LAND + day * DAY, **extra}
        (d / f"raw{self.n}.json").write_text(json.dumps(doc))
        self.n += 1

    def human_fix(self, tid, window=14):
        return [p["human_fix"] for t, p in self.rows(tid) if p["window"] == window][0]

    def rows(self, tid=None):
        sql = "SELECT task_id, payload FROM task_events WHERE kind='outcome'"
        sql += " AND task_id=?" if tid else ""
        return [(r[0], json.loads(r[1])) for r in self.conn.execute(sql, (tid,) if tid else ())]

    def signals(self, tid, window=14):
        return [p["signals"] for t, p in self.rows(tid) if p["window"] == window][0]

    def escaped(self):
        return self.conn.execute(
            "SELECT task_id, root_key FROM incidents"
            " WHERE root_key LIKE 'escaped_bug:%'").fetchall()


class Detect(Case):
    def test_clean_card_gets_a_clean_outcome_per_window(self):
        tid, sha = self.landed()
        self.commit("unrelated work", day=2)
        self.run_step(31)
        clean = {"escaped": False, "signals": [], "commit": sha, "human_fix": 0}
        self.assertEqual(self.rows(tid),
                         [(tid, {"window": 14, **clean}), (tid, {"window": 30, **clean})])
        self.assertEqual(self.escaped(), [])

    def test_a_real_git_revert_of_the_landing_escapes(self):
        tid, sha = self.landed()
        self.git("revert", "--no-edit", sha, day=4)
        self.run_step(15)
        want = {"window": 14, "escaped": True, "signals": ["revert"], "commit": sha,
                "human_fix": 0}
        self.assertEqual(self.rows(tid), [(tid, want)])
        self.assertEqual([r[0] for r in self.escaped()], [tid])

    def test_a_revert_of_a_non_head_commit_of_the_card_escapes(self):
        tid = db.new_task(self.conn, title="two part", status="done", now=LAND - DAY)
        first = self.commit(f"capsule: two part 1 ({tid})", name="a.txt")
        head = self.commit(f"capsule: two part 2 ({tid})", name="b.txt")
        db.event(self.conn, tid, "completed", {"commit": head}, now=LAND)
        self.git("revert", "--no-edit", first, day=4)
        self.run_step(15)
        self.assertEqual(self.signals(tid), ["revert"])

    def merge_card(self, merge_day):
        """A card landed by `merge --no-ff` on `merge_day`; returns (tid, tip, merge)."""
        tid = db.new_task(self.conn, title="branchy", status="done", now=LAND - DAY)
        self.commit("base work")
        self.git("checkout", "-q", "-b", "feat")
        tip = self.commit(f"capsule: branchy ({tid})")
        db.event(self.conn, tid, "completed", {"commit": tip}, now=LAND)
        self.git("checkout", "-q", "main")
        self.commit("other work on main", name="other.txt")
        self.git("merge", "--no-ff", "-q", "-m", "Merge feat", "feat", day=merge_day)
        return tid, tip, self.git("rev-parse", "HEAD")

    def test_a_revert_of_the_no_ff_merge_escapes(self):
        tid, tip, merge = self.merge_card(0)
        self.git("revert", "-m", "1", "--no-edit", merge, day=4)
        self.run_step(15)
        self.assertEqual(self.signals(tid), ["revert"])

    def test_the_window_starts_when_the_commit_reaches_main(self):
        tid, tip, merge = self.merge_card(10)
        self.commit(f"fix the branchy parser ({tid})", day=20)
        self.run_step(15)                       # 14 d after `completed`, but only 5 d after arrival
        self.assertEqual(self.rows(tid), [])
        self.run_step(25)
        self.assertEqual(self.signals(tid), ["followup"])

    def test_a_fix_commit_naming_the_card_escapes_but_only_inside_the_window(self):
        tid, sha = self.landed()
        self.commit("fix typo elsewhere", day=3)
        self.commit(f"fix parser crash found after ({tid})", day=20)
        self.run_step(31)
        self.assertEqual([(p["window"], p["signals"]) for _, p in self.rows(tid)],
                         [(14, []), (30, ["followup"])])

    def test_a_fix_only_counts_with_the_card_in_the_subject_and_a_real_fix_word(self):
        a, _ = self.landed("a")
        b, _ = self.landed("b")
        c, _ = self.landed("c")
        d, _ = self.landed("d")
        self.commit(f"fix a typo\n\nfound while reading {a}", day=2)
        self.commit(f"count fixture rows ({b})", day=3)
        self.commit(f"hotfix the parser ({c})", day=3)
        self.run_step(15)
        self.assertEqual([self.signals(t) for t in (a, b, c, d)], [[], [], ["followup"], []])

    def test_an_incident_on_the_card_after_done_escapes(self):
        tid, _ = self.landed()
        incidents.open_incident(self.conn, board="b", kind="crash", key="boom", task_id=tid,
                                now=LAND + DAY)
        self.run_step(15)
        self.assertEqual(self.signals(tid), ["incident"])

    def test_an_incident_before_landing_does_not_count(self):
        tid, _ = self.landed()
        incidents.open_incident(self.conn, board="b", kind="crash", key="boom", task_id=tid,
                                now=LAND - 5)
        self.run_step(15)
        self.assertEqual(self.signals(tid), [])

    def test_human_acts_after_done_are_not_defects_but_a_crash_is(self):
        """accept and screen-ok open `human_intervention` incidents through the real
        `from_events`; a human fix is a count, not an escape signal, so a reset stays clean too."""
        acts, _ = self.landed("acts")
        broke, _ = self.landed("broke")
        for kind in ("accept", "screen-ok", "reset"):
            db.event(self.conn, acts, "intervention", {"kind": kind}, now=LAND + DAY)
        db.event(self.conn, broke, "crashed", {"error_class": "oom", "exit": 1}, now=LAND + DAY)
        opened = incidents.from_events(self.conn, board="b", now=LAND + DAY)
        self.assertGreaterEqual(len(opened), 4)
        self.run_step(15)
        self.assertEqual((self.signals(acts), self.signals(broke)), ([], ["incident"]))

    def test_a_card_whose_commit_is_not_on_main_is_skipped_not_called_clean(self):
        tid = db.new_task(self.conn, title="x", status="done", now=LAND)
        db.event(self.conn, tid, "completed", {"commit": "0" * 40}, now=LAND)
        self.run_step(40)
        self.assertEqual(self.rows(), [])


class HumanFix(Case):
    """human_fix = the owner's intervention.v1 files naming the card inside its window (row 610)."""

    def test_owner_files_in_the_window_count_and_a_missing_author_is_owner(self):
        tid, _ = self.landed()
        self.human(tid, 3)
        self.human(tid, 5, author="owner")
        legacy = spool.human_dir(self.dir / "home", "b") / "legacy.json"
        legacy.write_text(json.dumps({"task_id": tid, "kind": "reset", "text": "x",
                                      "at": LAND + 6 * DAY}))
        self.run_step(31)
        self.assertEqual((self.human_fix(tid), self.human_fix(tid, 30)), (3, 3))
        self.assertEqual(self.signals(tid), [])      # a measured count, not an escape signal

    def test_a_cos_file_never_counts(self):
        tid, _ = self.landed()
        self.human(tid, 3, author="cos")
        self.run_step(15)
        self.assertEqual(self.human_fix(tid), 0)

    def test_a_file_outside_the_window_does_not_count(self):
        tid, _ = self.landed()
        self.human(tid, -2)                 # before it reached main
        self.human(tid, 20)                 # after 14 d, inside 30 d
        self.run_step(31)
        self.assertEqual((self.human_fix(tid), self.human_fix(tid, 30)), (0, 1))

    def test_a_file_naming_another_card_does_not_count(self):
        tid, _ = self.landed("one")
        other, _ = self.landed("two")
        self.human(other, 3)
        self.run_step(15)
        self.assertEqual((self.human_fix(tid), self.human_fix(other)), (0, 1))


    def test_only_kinds_that_send_work_back_count(self):
        tid, _ = self.landed()
        for kind in outcomes.HUMAN_FIX_KINDS:
            self.human(tid, 3, kind=kind)
        for kind in ("accept", "screen-ok", "unblock", "park"):
            self.human(tid, 4, kind=kind)
        self.run_step(15)
        self.assertEqual(self.human_fix(tid), len(outcomes.HUMAN_FIX_KINDS))
        self.assertEqual(set(outcomes.HUMAN_FIX_KINDS), {"reset", "screen-fix", "override"})

    def test_handover_tagged_files_are_not_human_fixes(self):
        tid, _ = self.landed()
        self.human(tid, 3, tag="migration-reset")
        self.raw_human(tid, 4, tag="handover-route", author="owner")
        self.run_step(15)
        self.assertEqual(self.human_fix(tid), 0)

    def test_the_window_is_open_at_the_start_and_closed_at_the_end(self):
        tid, _ = self.landed()
        self.human(tid, 0)                       # exactly at landed: not inside
        self.human(tid, 14)                      # exactly landed + 14 d: inside
        self.run_step(31)
        self.assertEqual((self.human_fix(tid), self.human_fix(tid, 30)), (1, 1))

    def test_an_unknown_or_null_author_is_not_owner(self):
        tid, _ = self.landed()
        self.raw_human(tid, 3, author=None)
        self.raw_human(tid, 3, author="ghost")
        self.run_step(15)
        self.assertEqual(self.human_fix(tid), 0)

    def test_a_file_the_tick_already_moved_to_done_counts(self):
        tid, _ = self.landed()
        self.raw_human(tid, 3, folder="done", author="owner")
        self.run_step(15)
        self.assertEqual(self.human_fix(tid), 1)


class Backlog(Case):
    def test_a_card_completed_before_since_gets_no_outcome_and_no_incident(self):
        old_sha = self.commit("capsule: old card (t_old)")
        old = self.card("old", old_sha)
        self.conn.execute(
            "UPDATE task_events SET created_at=? WHERE task_id=? AND kind='completed'",
            (LAND - DAY, old))
        self.git("revert", "--no-edit", old_sha, day=2)
        edge, edge_sha = self.landed("edge")             # completed exactly at SINCE
        self.git("revert", "--no-edit", edge_sha, day=3)
        self.run_step(31)
        self.assertEqual({t for t, _ in self.rows()}, {edge})
        self.assertEqual([r[0] for r in self.escaped()], [edge])


class Windows(Case):
    def test_nothing_is_written_while_the_window_is_open(self):
        tid, _ = self.landed()
        self.run_step(13)
        self.assertEqual(self.rows(), [])
        self.run_step(15)
        self.assertEqual([p["window"] for _, p in self.rows(tid)], [14])

    def test_reruns_write_exactly_one_outcome_per_card_per_window(self):
        tid, sha = self.landed()
        self.git("revert", "--no-edit", sha, day=2)
        for _ in range(3):
            self.run_step(35)
        self.assertEqual(sorted(p["window"] for _, p in self.rows(tid)), [14, 30])
        self.assertEqual(len(self.escaped()), 1)       # 14 d and 30 d both escape: one incident

    def test_two_escaped_cards_with_near_identical_titles_open_two_incidents(self):
        for title in ("C06 fix round 1", "C06 fix round 2"):
            _, sha = self.landed(title)
            self.git("revert", "--no-edit", sha, day=2)
        self.run_step(15)
        self.assertEqual(len({r[0] for r in self.escaped()}), 2)
        self.assertEqual(len(self.escaped()), 2)


class TickPass(Case):
    def args(self, counts):
        return (self.conn, {"board": "b"}, {"main_checkout": self.repo}, counts)

    def call(self, days, counts):
        outcomes.tick_pass(*self.args(counts), spool_root=None, human_dir=None,
                           now=LAND + days * DAY)

    def crash(self, tid, days):
        db.event(self.conn, tid, "crashed", {"error_class": "oom", "exit": 1},
                 now=LAND + days * DAY)

    def test_the_daily_step_runs_once_a_day(self):
        self.landed()
        counts = {"tick_errors": 0}
        with mock.patch.object(outcomes, "step", wraps=outcomes.step) as step:
            self.call(15, counts)
            self.call(15, counts)
            self.assertEqual((counts["outcomes"], step.call_count), (0, 1))
            self.call(31, counts)
            self.assertEqual(step.call_count, 2)
        self.assertEqual(len(self.rows()), 2)

    def test_the_tick_path_finds_the_home_and_counts_a_human_fix(self):
        tid, _ = self.landed()
        home = self.dir / "home"
        self.human(tid, 3)
        db.create_board(paths.board_db("b", home))
        outcomes.tick_pass(*self.args({"tick_errors": 0}), spool_root=None,
                           human_dir=spool.human_dir(home, "b"), now=LAND + 15 * DAY)
        self.assertEqual(self.human_fix(tid), 1)

    def test_the_first_call_writes_and_a_throttled_call_still_reports_its_incidents(self):
        tid, _ = self.landed()
        first = {"tick_errors": 0}
        self.call(15, first)
        self.assertEqual((first["outcomes"], first["incidents"]), (1, 0))
        self.crash(tid, 15)
        again = {"tick_errors": 0}
        self.call(15, again)
        self.assertEqual((again["outcomes"], again["incidents"]), (0, 1))

    def test_a_failing_outcomes_pass_is_a_tick_error_and_incidents_still_count(self):
        tid, _ = self.landed()
        self.crash(tid, 1)
        counts = {"tick_errors": 0}
        with mock.patch.object(outcomes, "step", side_effect=RuntimeError("boom")):
            self.call(15, counts)
        got = (counts["tick_errors"], counts["incidents"], counts["outcomes"])
        self.assertEqual(got, (1, 1, 0))


class Rate(Case):
    def test_pending_until_a_card_has_a_14_day_outcome_then_the_share_that_escaped(self):
        self.landed("a")
        _, sha = self.landed("b")
        self.git("revert", "--no-edit", sha, day=1)
        self.assertEqual(outcomes.rate(self.conn, 14), "pending")
        self.run_step(10)
        self.assertEqual(outcomes.rate(self.conn, 14), "pending")
        self.run_step(15)
        self.assertEqual(outcomes.rate(self.conn, 14), 0.5)
        self.assertEqual(outcomes.rate(self.conn, 30), "pending")

    def test_the_report_carries_the_14_day_field(self):
        home = self.dir / "home"
        db.create_board(paths.board_db("b", home))
        doc = report.interventions(home, "b", now=LAND)
        self.assertEqual(doc["escaped_defect_rate_14d"], "pending")


class IntentExamples(Case):
    def test_ships_one_to_three_standalone_examples(self):
        self.assertIn("outcomes", intent.STANDALONE)
        self.assertGreaterEqual(len(intent.load("outcomes")), 1)   # per-change cap, 2026-10-19
        self.assertEqual(intent.lint(paths.INTENT / "outcomes.jsonl", None, "outcomes"), [])

    def play(self, item, tid, sha):
        if item.get("human"):
            return self.human(tid, item["day"], item.get("author", "owner"), item["human"],
                              item.get("tag"))
        if item.get("revert"):
            return self.git("revert", "--no-edit", sha, day=item["day"])
        message = item["subject"].replace("CARD", tid)
        if item.get("body"):
            message += "\n\n" + item["body"].replace("CARD", tid)
        return self.commit(message, day=item["day"])

    def test_every_example_is_judged_by_the_real_detector(self):
        for ex in intent.load("outcomes"):
            doc = ex["given"]["document"]
            tid, sha = self.landed()
            for item in doc["later"]:
                self.play(item, tid, sha)
            self.run_step(doc["window"] + 1)
            got = [p for t, p in self.rows(tid) if p["window"] == doc["window"]][0]
            self.assertEqual(intent._differs(ex["expect"], got, None), [], ex["id"])
            self.setUp()


if __name__ == "__main__":
    unittest.main()
