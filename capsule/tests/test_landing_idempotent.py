"""R12 S7b idempotent landing: a kill at each step of a landing, then a restart, lands the commit
exactly once; a card whose patch is already on main is recorded and skipped. Real git over a temp
bare remote (test_landing_step's `Step` world); nothing touches the real origin. A killed landing
restarts through the tick path (`step()`: the pass spawns the real worker), as in production."""
import json
import os
import stat
import subprocess
import unittest
from unittest import mock

from capsule import blocks, db, incidents, intent, lander, landing, landing_reverts, outbox, paths
from capsule.db import guard_get
from capsule.tests.test_landing_step import NOW, PASS, Step
from capsule.tests.test_lander import BRANCH, git


class Killed(BaseException):
    """A kill: not an Exception, so nothing in the lander catches it on the way out."""


class KilledLanding(Step):
    """R12 S7b, Z1 rows in contracts/intent/idempotent_landing.jsonl. Z2: the remote's own log
    (plain git) and the journal file read back as plain json, not through `journal.read`."""
    KILLS = ("_push", "_deploy", "_marker", "_complete", "boundary.mark", "guard_set")
    WANT = {
        "land_killed_at_each_step": {
            "main_has_commit_once": True, "card_done": True, "landed_lines": 1,
            "watch_closes": "verified"},
        "land_patch_already_on_main": {
            "outcome": "already_on_main", "pushed": False, "commits_added": 0, "card_done": True,
            "completed_once": True, "second_pass": "idle"},
        "land_conflict_not_verbatim_on_main": {
            "outcome": "not_checked", "main_unchanged": True, "card_on_main": False},
    }

    def kill_at(self, where):
        """The patch that raises `Killed` at one step of `_land` (the guard write only for the
        card's own `lander.landed` state)."""
        if where == "guard_set":
            real = landing.guard_set

            def die(conn, key, *args, **kw):
                if key.startswith(landing.LANDED):
                    raise Killed(key)
                return real(conn, key, *args, **kw)
            return mock.patch.object(landing, "guard_set", die)
        target = landing.boundary if where == "boundary.mark" else landing
        return mock.patch.object(target, where.split(".")[-1], side_effect=Killed(where))

    def lines(self, step=None):
        """The journal's lines as the file holds them."""
        path = self.home / "landing" / "b" / self.tid / "journal.jsonl"
        got = [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []
        return [x for x in got if step in (None, x["step"])]

    def done(self):
        return self.conn.execute("SELECT status FROM tasks WHERE id=?",
                                 (self.tid,)).fetchone()[0] == "done"

    def on_main(self):
        return git(self.remote, "log", "main", "--format=%s").splitlines().count("card work")

    def completions(self):
        return [json.loads(p) for (p,) in self.conn.execute(
            "SELECT payload FROM task_events WHERE kind='completed' ORDER BY id")]

    def killed(self, where):
        with self.kill_at(where), self.assertRaises(Killed):
            self.land(fetch=True)

    def kill_then_restart(self, where):
        """The restart through the land CLI (skips the tick's choice of card)."""
        self.killed(where)
        return self.land(fetch=True)

    def each_kill(self, replayed):
        """One result per kill point, each in a fresh world, restarted through the tick;
        `replayed`: main moved first, so the card is replayed onto it and the pushed sha is not
        the card's own."""
        for where in self.KILLS:
            self.setUp()
            if replayed:
                self.rival()
            self.killed(where)
            self.step(NOW + 10)
            yield {"main_has_commit_once": self.on_main() == 1, "card_done": self.done(),
                   "landed_lines": len(self.lines("landed")), "watch_closes": self.step(NOW + 20)}

    @staticmethod
    def agree(seen):
        """Per key the value every result shares, else the list of them (so a miss shows)."""
        return {k: seen[0][k] if all(r[k] == seen[0][k] for r in seen) else [r[k] for r in seen]
                for k in seen[0]}

    def unlike_main(self, text, reverted):
        """Main gets the card's change as another commit (`text` is what that commit writes),
        maybe reverts it, then changes the file again, so the card's own commit conflicts."""
        self.rival("a.txt", text)
        if reverted:
            git(self.dir / "other", "revert", "--no-edit", "HEAD")
            git(self.dir / "other", "push", "-q", "origin", "main")
        self.rival("a.txt", text + "more\n")

    def not_landed(self, text, reverted):
        self.setUp()
        self.unlike_main(text, reverted)
        before = self.remote_main()
        return {"outcome": self.step(NOW + 20), "main_unchanged": self.remote_main() == before,
                "card_on_main": self.on_main() > 0}

    def replay(self, case):
        if case == "land_killed_at_each_step":
            return self.agree([*self.each_kill(False), *self.each_kill(True)])
        if case == "land_conflict_not_verbatim_on_main":
            return self.agree([self.not_landed("    work\n", False),    # indented, not equal
                               self.not_landed("work\n", True)])        # equal, then reverted
        self.rival("a.txt", "work\n")                # the card's patch, landed by hand
        self.rival("a.txt", "work\nmore\n")          # and main has changed those lines since
        main = self.remote_main()
        outcome = self.step(NOW + 20)
        return {"outcome": outcome, "pushed": self.remote_main() != main,
                "commits_added": int(git(self.remote, "rev-list", "--count", f"{main}..main")),
                "card_done": self.done(),
                "completed_once": len(self.completions()) == 2,
                "second_pass": self.step(NOW + 30)}

    def test_a_kill_at_each_step_then_a_tick_restart_lands_the_commit_exactly_once(self):
        for replayed in (False, True):
            for got, where in zip(self.each_kill(replayed), self.KILLS):
                self.assertEqual(got, self.WANT["land_killed_at_each_step"], (where, replayed))
                self.assertEqual(len(self.completions()), 2, (where, replayed))   # finish, landing

    def test_a_kill_before_the_landed_state_is_recovered_by_the_tick_when_a_check_is_advisory(self):
        """The landing's own `completed` event carries its check rows; a drifting deploy check
        makes one of them advisory, so the card no longer looks approved to the tick."""
        self.cfg["lander"]["deploy_check"] = "exit 1"
        self.killed("guard_set")
        self.assertIn("advisory", self.completions()[-1]["gates"].values())
        self.assertIsNone(self.state())
        self.assertEqual(self.step(NOW + 10), "already_on_main")
        self.assertEqual((self.state(), len(self.lines("landed")), self.on_main()),
                         ("pushed", 1, 1))

    def second_card(self):
        """A second done card (its own branch off main, one commit, completed after the first)."""
        git(self.repo, "checkout", "-q", "-b", "card2", "main")
        sha = self.put("c.txt", "two\n", "second card")
        git(self.repo, "checkout", "-q", BRANCH)
        tid = db.new_task(self.conn, title="t2", assignee="dev", branch="card2", status="done")
        db.event(self.conn, tid, "completed", {"commit": sha, "gates": PASS}, now=NOW + 1)
        self.conn.commit()
        return tid

    def human_reverts_head(self, fetch):
        """A human reverts main's tip (the pushed commit); `fetch`: the dev repo has seen it."""
        other = self.dir / "other"
        subprocess.run(["git", "clone", "-q", str(self.remote), str(other)], check=True,
                       capture_output=True)
        git(other, "revert", "--no-edit", "HEAD")
        git(other, "push", "-q", "origin", "main")
        if fetch:
            git(self.repo, "fetch", "-q", "origin")

    def reverts_asked(self, tid):
        return self.conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND "
                                 "kind='landing' AND payload LIKE '%reverted_on_main%'",
                                 (tid,)).fetchone()[0]

    def test_a_card_a_human_reverted_after_an_unrecorded_push_is_closed_and_left_alone(self):
        """Stale or fresh dev repo, the tick flags the revert once, closes the push
        (`push_abandoned`) and never re-picks the card; the second card gets the slot."""
        for fresh in (False, True):
            self.setUp()
            second = self.second_card()
            self.killed("_deploy")
            self.human_reverts_head(fresh)
            seen = [self.step(NOW + 10 + 10 * i) for i in range(3)]
            self.assertEqual(seen, ["landed", "verified", "idle"] if fresh
                             else ["reverted_on_main", "landed", "verified"], fresh)
            self.assertEqual(self.reverts_asked(self.tid), 1, fresh)
            self.assertEqual([x["step"] for x in self.lines()], ["push", "push_abandoned"], fresh)
            self.assertEqual((self.state(), self.reverts_asked(second)), (None, 0), fresh)
            self.assertEqual(self.on_main(), 1)     # card work: once, from the first push
            self.assertEqual(git(self.remote, "log", "main", "--format=%s").count("second card"),
                             1, fresh)

    def test_a_card_whose_lander_worker_keeps_crashing_is_not_recovered_again(self):
        self.killed("_deploy")
        db.guard_set(self.conn, landing.CRASHED + self.tid,
                     {"sha": self.sha, "count": landing.CRASH_LIMIT})
        self.assertEqual(self.step(NOW + 10), "idle")
        self.assertEqual([x["step"] for x in self.lines()], ["push"])

    def test_a_close_after_an_unrecorded_push_still_records_the_push_and_watches_it(self):
        """Killed after the push, the card held, then a person's close: the push is recorded."""
        self.killed("_deploy")
        self.conn.execute("UPDATE tasks SET status='blocked', block_kind='transient' WHERE id=?",
                          (self.tid,))
        self.conn.commit()
        self.assertTrue(blocks.close(self.conn, self.tid, "landed by hand", now=NOW + 5))
        self.assertEqual(self.step(NOW + 10), "already_on_main")
        self.assertEqual(self.step(NOW + 20), "verified")
        self.assertEqual(self.guard()["sha"], self.sha)
        self.assertEqual(([x["step"] for x in self.lines()], self.done()),
                         (["push", "landed"], True))

    def card(self):
        return tuple(self.conn.execute("SELECT status, block_kind, failure_count FROM tasks "
                                       "WHERE id=?", (self.tid,)).fetchone())

    def hold_card(self):
        self.conn.execute("UPDATE tasks SET status='blocked', block_kind='transient' WHERE id=?",
                          (self.tid,))
        self.conn.commit()

    def close_card(self, at=NOW + 5):
        """The card held, then a person closes it; its state after."""
        self.hold_card()
        self.assertTrue(blocks.close(self.conn, self.tid, "landed by hand", now=at))
        return self.card()

    def killed_then_closed(self):
        """Killed after the push (so one push is owed), the card held, a person closes it."""
        self.killed("_deploy")
        return self.close_card()

    def escaped(self):
        return [r["kind"] for r in incidents.listing(self.conn)]

    def test_a_closed_cards_owed_push_is_recorded_with_no_suites_and_never_moves_it(self):
        """Wait (sandbox down) or red suites on the card's commit do not hold the record: the
        push is recorded from the journal, the card stays done and closed, and the after-push
        watch alone judges the pushed sha."""
        suites = list(self.cfg["contract_commands"])
        for case, env, commands in (("wait", {"AGENT_OPS_NO_DOCKER": "1"}, suites),
                                    ("red", {}, ["exit 1"])):
            with self.subTest(case):
                self.setUp()
                before, ran = self.killed_then_closed(), len(self.checked())
                self.cfg["contract_commands"] = commands
                with mock.patch.dict(os.environ, env):
                    self.assertEqual(self.step(NOW + 10), "already_on_main")
                    watch = self.step(NOW + 20)
                self.assertEqual(len(self.checked()), ran)     # the card's commit ran no suites
                self.assertEqual(self.guard()["sha"], self.sha)          # recorded, `pushed`
                self.assertEqual(len(self.lines("landed")), 1)
                self.assertEqual((self.card(), blocks.was_closed(self.conn, self.tid)),
                                 (before, True))
                self.assertEqual(watch, "verify_held" if case == "wait" else "closed_red")
                self.assertEqual(self.card(), before)

    def asked(self):
        return [b for (b,) in self.conn.execute("SELECT body FROM outbox")]

    def test_a_red_closed_cards_push_asks_a_person_and_never_reverts(self):
        """Red on a closed card's push: one escaped_bug incident, one ask in the outbox, main
        untouched, and the watch ends in `closed_red` so no later pass repeats it."""
        self.killed_then_closed()
        main = self.remote_main()
        self.cfg["contract_commands"] = ["exit 1"]
        self.assertEqual([self.step(NOW + 10), self.step(NOW + 20), self.step(NOW + 30)],
                         ["already_on_main", "closed_red", "idle"])
        self.assertEqual((self.escaped(), self.state()), (["escaped_bug"], "closed_red"))
        self.assertEqual((self.remote_main(), len(self.asked())), (main, 1))
        self.assertEqual(self.step(NOW + 40), "idle")
        self.assertEqual((self.escaped(), len(self.asked())), (["escaped_bug"], 1))   # once

    def test_the_closed_red_ask_says_in_plain_words_what_happened_and_what_to_do(self):
        self.killed_then_closed()
        self.cfg["contract_commands"] = ["exit 1"]
        self.step(NOW + 10), self.step(NOW + 20)
        (text,) = self.asked()
        for part in ('"t"', self.sha[:8], "A person closed this card", "failed the tests",
                     "Main was left as it is and nothing was undone", "undo it by hand, or accept",
                     "Closing the incident changes nothing on main"):
            self.assertIn(part, text)
        self.assertNotIn("settles it", text)

    def test_a_kill_before_the_closed_red_guard_write_repeats_the_ask_by_its_key(self):
        """The ask goes first, so a kill after it cannot lose it; the repeat carries the same
        Slack key (it edits the posted message), and the watch then ends in `closed_red`."""
        db.guard_set(self.conn, outbox.SLACK_FLAG, True)
        self.killed_then_closed()
        self.cfg["contract_commands"] = ["exit 1"]
        self.step(NOW + 10)
        real = landing.guard_set

        def die(conn, key, value=None, *a, **kw):
            if isinstance(value, dict) and value.get("state") == "closed_red":
                raise Killed("before the closed_red guard write")
            return real(conn, key, value, *a, **kw)
        with mock.patch.object(landing, "guard_set", die), self.assertRaises(Killed):
            landing.verify(self.conn, self.tid, cfg=self.cfg, release=self.release, home=self.home)
        self.assertEqual((self.state(), len(self.asked())), ("pushed", 2))   # file + slack row
        self.assertEqual(self.step(NOW + 20), "closed_red")
        keys = {k for (k,) in self.conn.execute("SELECT dedup_key FROM outbox_key")}
        self.assertEqual((self.state(), keys), ("closed_red", {f"needs:{self.tid}"}))

    def break_fetch_keep_push(self):
        """The release checkout cannot fetch origin, but can still push to it."""
        good = git(self.release, "remote", "get-url", "origin").strip()
        git(self.release, "remote", "set-url", "--push", "origin", good)
        git(self.release, "remote", "set-url", "origin", "/nonexistent/origin.git")
        return good

    def expire_pause(self):
        """The worker stamps the pause with the wall clock (the fixture's NOW is not it)."""
        seen = guard_get(self.conn, landing_reverts.FETCHFAIL + self.tid)[0]
        db.guard_set(self.conn, landing_reverts.FETCHFAIL + self.tid, {**seen, "until": 0})

    def landing_events(self, tid):
        return [json.loads(p).get("outcome") for (p,) in self.conn.execute(
            "SELECT payload FROM task_events WHERE kind='landing' AND task_id=?", (tid,))]

    def test_a_failing_fetch_backs_a_closed_card_off_alone_and_a_younger_card_lands(self):
        """The failure is logged, asked once and not repeated each pass; the closed card is
        skipped while it backs off, so the card behind it lands; then it is retried."""
        self.killed_then_closed()
        self.young_card(NOW + 6)
        young = self.conn.execute("SELECT id FROM tasks WHERE id!=?", (self.tid,)).fetchone()[0]
        good = self.break_fetch_keep_push()
        seen = [self.step(NOW + 10), self.step(NOW + 20), self.step(NOW + 30)]
        self.assertEqual(seen, ["fetch_failed", "landed", "verified"])
        self.assertEqual((self.landing_events(self.tid), len(self.asked())), (["fetch_failed"], 1))
        self.assertEqual((self.subjects().count("young work"), self.state()), (1, None))
        git(self.release, "remote", "set-url", "origin", good)
        self.assertEqual(self.step(NOW + 100), "idle")           # still inside the pause
        self.expire_pause()
        self.assertEqual(self.step(NOW + 400), "already_on_main")   # retried, recorded
        self.assertEqual(self.landing_events(young), ["landed", "verified"])
        self.assertEqual(guard_get(self.conn, landing_reverts.FETCHFAIL + self.tid)[0], None)

    def test_a_second_failed_fetch_opens_one_incident_after_the_pause(self):
        self.killed_then_closed()
        self.break_fetch_keep_push()
        self.assertEqual([self.step(NOW + 10), self.step(NOW + 20)], ["fetch_failed", "idle"])
        self.assertEqual(self.escaped(), [])
        self.expire_pause()
        self.assertEqual(self.step(NOW + 400), "fetch_failed")
        self.assertEqual(self.step(NOW + 410), "idle")
        self.assertEqual((self.landing_events(self.tid), len(self.asked())),
                         (["fetch_failed", "fetch_failed"], 1))
        kinds = [r["kind"] for r in incidents.listing(self.conn)]
        self.assertEqual(kinds, ["silent_failure"])

    def young_card(self, at):
        young = self.put("c.txt", "young\n", "young work")
        tid = db.new_task(self.conn, title="y", assignee="dev", branch=BRANCH, status="done")
        db.event(self.conn, tid, "completed", {"commit": young, "gates": PASS}, now=at)
        self.conn.commit()

    def test_a_younger_card_lands_behind_a_red_closed_one(self):
        before = self.killed_then_closed()
        self.young_card(NOW + 6)
        self.cfg["contract_commands"] = ["test -e c.txt"]    # red on the closed card's push
        seen = [self.step(NOW + 10 + 10 * i) for i in range(5)]
        self.assertEqual(seen, ["already_on_main", "closed_red", "landed", "verified", "idle"])
        self.assertEqual((self.subjects().count("young work"), self.card()), (1, before))
        self.assertEqual([s for s in self.subjects() if s.startswith("Revert")], [])

    def test_a_later_cards_work_on_top_of_a_red_closed_push_is_never_reverted_from_under(self):
        """Any age: the old closed push is red on its own tree, a later card sits on top."""
        self.killed("_deploy")
        self.hold_card()
        self.young_card(NOW + 3)
        self.assertEqual([self.step(NOW + 4), self.step(NOW + 5)], ["landed", "verified"])
        self.close_card(NOW + 6)
        main, later = self.remote_main(), NOW + 30 * 86400
        self.cfg["contract_commands"] = ["test -e c.txt"]
        self.assertEqual([self.step(later), self.step(later + 10)],
                         ["already_on_main", "closed_red"])
        self.assertEqual((self.remote_main(), self.escaped()), (main, ["escaped_bug"]))

    def test_a_person_pushed_card_that_is_red_is_not_reverted(self):
        """The lander was killed before its push; a person pushed the same sha by hand."""
        with self.kill_at("_push"), self.assertRaises(Killed):
            self.land(fetch=True)
        git(self.repo, "push", "-q", "origin", f"{self.sha}:refs/heads/main")
        git(self.release, "fetch", "-q", "origin")
        self.close_card()
        main = self.remote_main()
        self.cfg["contract_commands"] = ["exit 1"]
        self.assertEqual([self.step(NOW + 10), self.step(NOW + 20)],
                         ["already_on_main", "closed_red"])
        self.assertEqual(self.remote_main(), main)

    def test_a_push_origin_no_longer_holds_is_not_recorded_from_a_stale_ref(self):
        self.killed("_deploy")
        base = self.lines("push")[0]["base"]
        self.close_card()
        git(self.remote, "update-ref", "refs/heads/main", base)    # origin dropped the push
        self.assertEqual([self.step(NOW + 10), self.step(NOW + 20)], ["nothing", "idle"])
        self.assertEqual(([x["step"] for x in self.lines()], self.state(), self.asked()),
                         (["push"], None, []))

    def test_a_kill_between_the_record_and_its_journal_line_raises_no_second_alarm(self):
        """Recorded (watch `pushed`) but killed before the `landed` line: the red watch asks
        once, the next pass only writes the line and never records again."""
        self.killed("_deploy")
        self.close_card()
        real = landing._journal

        def die(cfg, task_id, home, step, **fields):
            if step == "landed":
                raise Killed("before the landed line")
            return real(cfg, task_id, home, step, **fields)
        with mock.patch.object(landing, "_journal", die), self.assertRaises(Killed):
            self.land(fetch=True)
        self.assertEqual((self.state(), [x["step"] for x in self.lines()]), ("pushed", ["push"]))
        self.cfg["contract_commands"] = ["exit 1"]
        seen = [self.step(NOW + 10 + 10 * i) for i in range(4)]
        self.assertEqual(seen, ["closed_red", "already_on_main", "idle", "idle"])
        self.assertEqual(([x["step"] for x in self.lines()], self.escaped(), len(self.asked())),
                         (["push", "landed"], ["escaped_bug"], 1))

    def test_a_keeper_claim_taken_after_the_pre_check_defers_the_record(self):
        self.killed_then_closed()
        queue = paths.queue_dir(self.cfg.get("repo_name", self.cfg["board"]), self.home)
        queue.mkdir(parents=True, exist_ok=True)
        real = landing.acquire

        def claim_then_acquire(conn, task_id, now):      # the keeper claims right after _prerun
            (queue / "owners.json").write_text(json.dumps(
                {BRANCH: {"owner": lander.KEEPER, "heartbeat": NOW}}))
            return real(conn, task_id, now)
        with mock.patch.object(landing, "acquire", claim_then_acquire):
            self.assertEqual(self.land(fetch=True)["outcome"], "busy")
        self.assertEqual(([x["step"] for x in self.lines()], self.state()), (["push"], None))

    def test_the_no_commit_land_in_dry_run_does_not_move_a_closed_card(self):
        before = self.killed_then_closed()
        self.cfg["lander"]["mode"] = "dry-run"
        self.cfg["contract_commands"] = ["exit 1"]
        self.assertEqual(self.land(fetch=True)["outcome"], "dry-run")
        self.assertEqual(self.card(), before)

    def test_a_closed_cards_push_older_than_the_window_is_recorded(self):
        self.killed_then_closed()
        self.assertEqual(self.step(NOW + landing.WINDOW + 100), "already_on_main")
        self.assertEqual([x["step"] for x in self.lines()], ["push", "landed"])
        self.assertEqual(self.step(NOW + landing.WINDOW + 200), "verified")

    def test_an_unclosed_cards_owed_push_is_recorded_before_a_red_verdict_can_move_it(self):
        self.killed("_deploy")
        before, ran = self.card(), len(self.checked())
        self.cfg["contract_commands"] = ["exit 1"]
        self.assertEqual(self.step(NOW + 10), "already_on_main")
        self.assertEqual(len(self.checked()), ran)          # no suites ran on the card's commit
        self.assertEqual((self.guard()["sha"], len(self.lines("landed"))), (self.sha, 1))
        self.assertEqual(self.card(), before)

    def test_a_done_card_past_the_window_with_an_unrecorded_push_gets_one_notice(self):
        self.killed("_deploy")
        before, main = self.card(), self.remote_main()
        late = NOW + landing.WINDOW + 100
        self.assertEqual(self.step(late), "stale_push")
        self.assertEqual((len(self.asked()), self.lines("landed"), self.card()), (1, [], before))
        self.assertEqual(self.step(late + 10), "idle")
        self.assertEqual((len(self.asked()), self.remote_main()), (1, main))
        self.assertIn(self.tid, self.asked()[0])

    def test_a_closed_reworks_red_commit_does_not_hide_a_green_push(self):
        self.killed("_deploy")
        self.sha = self.put("b.txt", "more\n", "rework")
        self.finish(PASS, at=NOW + 2)
        self.conn.execute("UPDATE tasks SET status='blocked', block_kind='transient' WHERE id=?",
                          (self.tid,))
        self.conn.commit()
        self.assertTrue(blocks.close(self.conn, self.tid, "landed by hand", now=NOW + 5))
        before = self.card()
        self.cfg["contract_commands"] = ["test ! -e b.txt"]   # the push is green, the rework red
        self.assertEqual([self.step(NOW + 10), self.step(NOW + 20), self.step(NOW + 30)],
                         ["already_on_main", "verified", "idle"])
        self.assertEqual((self.subjects().count("rework"), self.card(), self.escaped()),
                         (0, before, []))

    def test_a_close_after_a_kill_before_the_push_lands_nothing(self):
        """No push happened, so there is nothing to recover: the closed card is left alone."""
        self.killed("_push")
        self.conn.execute("UPDATE tasks SET status='blocked', block_kind='transient' WHERE id=?",
                          (self.tid,))
        self.conn.commit()
        before = self.remote_main()
        self.assertTrue(blocks.close(self.conn, self.tid, "landed by hand", now=NOW + 5))
        self.assertEqual(self.step(NOW + 10), "idle")
        self.assertEqual((self.remote_main(), self.on_main(), self.done()), (before, 0, True))

    def test_a_card_reworked_after_an_unrecorded_push_still_lands_its_rework_once(self):
        """The old push is recorded (watch kept) but its landing does not stand in for the
        newer completion: that one goes through the normal check and land path."""
        self.killed("_deploy")
        old = self.sha
        self.sha = self.put("b.txt", "more\n", "rework")
        self.finish(PASS, at=NOW + 5)
        self.assertEqual(self.step(NOW + 10), "already_on_main")        # the old push, recorded
        self.assertEqual((self.state(), self.completions()[-1]["commit"]), ("pushed", self.sha))
        seen = [self.step(NOW + 20 + 10 * i) for i in range(3)]
        self.assertEqual(seen, ["verified", "landed", "verified"])
        subjects = git(self.remote, "log", "main", "--format=%s").splitlines()
        self.assertEqual((subjects.count("card work"), subjects.count("rework")), (1, 1))
        self.assertEqual(self.remote_main(), self.sha)
        self.assertEqual([x["sha"] for x in self.lines("landed")], [old, self.sha])

    def subjects(self):
        return git(self.remote, "log", "main", "--format=%s").splitlines()

    def rework(self, name, subject, at):
        """The card is reworked: a new commit on its branch and a newer `completed` event."""
        self.sha = self.put(name, subject + "\n", subject)
        self.finish(PASS, at=at)

    def guard(self):
        return db.guard_get(self.conn, landing.LANDED + self.tid)[0] or {}

    def test_a_card_reworked_twice_before_the_first_rework_lands_lands_both_once(self):
        """P1: the completion is what is keyed, so the second rework is not hidden behind the
        first one's record."""
        self.killed("_deploy")
        self.rework("b.txt", "rework", NOW + 5)
        self.assertEqual(self.step(NOW + 10), "already_on_main")     # the old push, recorded
        self.rework("d.txt", "rework2", NOW + 15)
        seen = [self.step(NOW + 20 + 10 * i) for i in range(4)]
        self.assertEqual(seen, ["verified", "landed", "verified", "idle"])
        counts = [self.subjects().count(s) for s in ("card work", "rework", "rework2")]
        self.assertEqual((counts, self.remote_main()), ([1, 1, 1], self.guard()["sha"]))

    def test_a_kill_in_a_reworks_landing_with_an_advisory_check_is_recovered_by_the_tick(self):
        """P2: the landed guard of the first landing is still there when the rework's landing is
        killed before its own guard write; the unclosed push is recorded all the same."""
        self.killed("_deploy")
        self.rework("b.txt", "rework", NOW + 5)
        self.assertEqual(self.step(NOW + 10), "already_on_main")
        self.assertEqual(self.step(NOW + 20), "verified")
        self.cfg["lander"]["deploy_check"] = "exit 1"                 # a drift: an advisory row
        with self.kill_at("guard_set"), self.assertRaises(Killed):
            self.land(fetch=True)
        self.assertEqual(self.remote_main(), self.sha)
        self.assertEqual([x["step"] for x in self.lines()][-2:], ["landed", "push"])
        seen = [self.step(NOW + 30 + 10 * i) for i in range(3)]
        self.assertEqual(seen, ["already_on_main", "verified", "idle"])
        self.assertEqual([x["step"] for x in self.lines()][-2:], ["push", "landed"])
        self.assertEqual((self.guard()["sha"], self.subjects().count("rework")),
                         (self.sha, 1))

    def test_a_card_amended_after_an_unrecorded_push_is_recorded_and_not_bounced(self):
        """P3: the amend has the same patch and a new sha, so the old card commit is no longer on
        the branch; the card's latest completion is what gets checked."""
        self.killed("_deploy")
        old = self.sha
        git(self.repo, "commit", "-q", "--amend", "-m", "card work, amended")
        self.sha = git(self.repo, "rev-parse", "HEAD")
        self.assertNotEqual(self.sha, old)
        self.finish(PASS, at=NOW + 5)
        seen = [self.step(NOW + 10 + 10 * i) for i in range(4)]
        self.assertEqual(seen, ["already_on_main", "verified", "already_on_main", "idle"])
        self.assertEqual(([x["step"] for x in self.lines()], self.on_main()),
                         (["push", "landed"], 1))
        self.assertEqual((self.guard()["sha"], self.guard()["state"]), (old, "verified"))
        self.assertEqual(self.conn.execute("SELECT status FROM tasks WHERE id=?",
                                           (self.tid,)).fetchone()[0], "done")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND "
                                           "kind='failure'", (self.tid,)).fetchone()[0], 0)

    def test_a_card_reworked_after_a_normal_landing_lands_its_rework_once(self):
        """The old caveat: a landed card whose latest completion is not the landed one is
        eligible again, and only its new change reaches main."""
        self.assertEqual(self.step(NOW + 1), "landed")
        self.assertEqual(self.step(NOW + 5), "verified")
        self.assertEqual(self.step(NOW + 6), "idle")
        self.rework("b.txt", "rework", NOW + 10)
        seen = [self.step(NOW + 20 + 10 * i) for i in range(3)]
        self.assertEqual(seen, ["landed", "verified", "idle"])
        self.assertEqual([self.subjects().count(s) for s in ("card work", "rework")], [1, 1])
        self.assertEqual((self.remote_main(), self.guard()["sha"]), (self.sha, self.sha))

    def test_the_lander_push_moves_the_release_checkouts_origin_main(self):
        """The tick's candidate check reads the release's `refs/remotes/origin/main`."""
        self.assertEqual(self.land(fetch=True)["outcome"], "landed")
        self.assertEqual(git(self.release, "rev-parse", "refs/remotes/origin/main"), self.sha)

    def test_the_journal_has_the_push_before_it_happens_and_one_landed_line_after(self):
        self.kill_then_restart("_deploy")
        self.assertEqual([x["step"] for x in self.lines()], ["push", "landed"])
        (push,) = self.lines("push")
        self.assertEqual((push["sha"], push["commits"]), (self.sha, [self.sha]))

    def test_a_kill_before_the_push_pushes_it_on_restart(self):
        got = self.kill_then_restart("_push")
        self.assertEqual((got["outcome"], got["pushed"]), ("landed", True))
        self.assertEqual([x["step"] for x in self.lines()], ["push", "push", "landed"])

    def test_the_restart_after_a_kill_reports_already_on_main_and_pushes_nothing(self):
        got = self.kill_then_restart("_complete")
        self.assertEqual((got["outcome"], got["pushed"]), ("already_on_main", False))
        self.assertEqual(self.remote_main(), self.sha)

    def test_a_landing_without_a_journal_line_is_not_taken_for_ours(self):
        """A human pushed it: the old path (an ask, nothing recorded), never a made-up landing."""
        git(self.repo, "push", "-q", str(self.remote), f"{self.sha}:refs/heads/main")
        got = self.land(fetch=True)
        self.assertEqual(got["outcome"], "already_on_main")
        self.assertEqual((self.state(), self.lines()), (None, []))

    def test_an_old_landing_is_not_credited_to_a_later_completion_a_human_pushed(self):
        self.assertEqual(self.land(fetch=True)["outcome"], "landed")
        later = self.put("b.txt", "more\n", "rework")
        git(self.repo, "push", "-q", str(self.remote), f"{later}:refs/heads/main")   # by hand
        self.sha = later
        self.finish(PASS, at=NOW + 5)
        got = self.land(fetch=True)
        self.assertEqual(got["outcome"], "already_on_main")
        self.assertEqual(len(self.lines("push")), 1)

    def test_a_push_that_never_reached_main_is_not_recorded_when_a_human_lands_another_sha(self):
        """Killed before the push, then the card was rebuilt and a human pushed the new commit:
        the pending push line names a sha that is not on main, so it is not ours to record."""
        self.killed("_push")
        (pending,) = self.lines("push")
        alt = git(self.repo, "commit-tree", "-p", f"{self.sha}^", "-m", "rebuilt",
                  f"{self.sha}^{{tree}}")
        git(self.repo, "push", "-q", str(self.remote), f"{alt}:refs/heads/main")
        self.assertNotEqual(self.remote_main(), pending["sha"])
        self.sha = alt
        self.finish(PASS, at=NOW + 5)
        got = self.land(fetch=True)
        self.assertEqual((got["outcome"], got["detail"], self.lines("landed")),
                         ("already_on_main", "", []))

    def test_a_rejected_push_closes_its_line_so_a_later_human_push_is_not_credited(self):
        hook = self.remote / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n")
        os.chmod(hook, os.stat(hook).st_mode | stat.S_IXUSR)
        self.assertEqual(self.land(fetch=True)["outcome"], "push_failed")
        self.assertEqual([x["step"] for x in self.lines()], ["push", "push_failed"])
        hook.unlink()
        db.guard_set(self.conn, landing.PUSHFAIL, None)
        git(self.repo, "push", "-q", str(self.remote), f"{self.sha}:refs/heads/main")   # by hand
        got = self.land(fetch=True)
        self.assertEqual((got["outcome"], got["detail"], self.lines("landed")),
                         ("already_on_main", "", []))

    def test_the_cases(self):
        for case, want in self.WANT.items():
            self.setUp()
            self.assertEqual(self.replay(case), want, case)

    def test_the_idempotent_landing_examples(self):
        rows = intent.load("idempotent_landing")
        if not rows:
            self.skipTest("contracts/intent/idempotent_landing.jsonl is not shipped yet")
        for ex in rows:
            self.setUp()
            want = {k: v for k, v in ex["expect"].items() if k != "note"}
            seen = self.replay(ex["given"]["document"]["case"])
            self.assertEqual({k: seen[k] for k in want}, want, ex["id"])


if __name__ == "__main__":
    unittest.main()
