# mutation-check: targets-predate-range 4d253485 -- a flake fix renames a test whose subject
#   already behaved on base ::
#   OtherFailures.test_a_timeout_during_the_opus_attempt_goes_back_to_ready
"""M155 (redesign D11, D12, spec 4.4): a Sonnet attempt with 2 fix rounds, then one fresh run on
`routing.implementer_restart` with the failure notes and 2 fix rounds of its own, then triage.
The tick tests run the real review loop with scripted roles at the shipped defaults for
`caps.failure_limit` and `runaway` (4 rounds, same-class 3, identical findings every round); the
switch is `redesign.escalation`."""
import json
import time
import unittest
from types import SimpleNamespace


from capsule import blocks, bundles, config, db, dispatch, landing, restart, runaway
from capsule.db import event
from capsule.models import Result
from capsule.tests import test_landing as tl
from capsule.tests import test_tick as tt
from capsule.tests.test_tick import CHECKPOINT, IMPL_DONE, REV_FAIL, REV_PASS, TickCase

OPUS = {"provider": "claude", "account": "default", "model": "claude-opus-restart-test",
        "fallback": None, "turn_budget": 60}
N = 9   # scripted rounds per role, more than the ladder uses


class Ladder(TickCase):
    def setUp(self):
        super().setUp()
        self.cfg.pop("runaway")          # the defaults: max_rounds 4, same_class_rounds 3
        self.cfg["korin"] = dict(self.cfg["korin"], enabled=False)
        self.jobs = []
        self.hooks["implementer"] = lambda job, sp, run: self.jobs.append(job)
        self.rounds = []   # the `round` input of every reviewer job
        self.hooks["reviewer"] = lambda job, sp, run: self.rounds.append(job["inputs"]["round"])
        self.script["implementer"] = [[IMPL_DONE] for _ in range(N)]
        self.script["validator"] = [[tt.VAL_PASS] for _ in range(N)]
        self.script["guide"] = [[tt.GUIDE_OK] for _ in range(N)]
        self.script["reviewer"] = [[REV_FAIL]] * N
        self.sonnet = self.cfg["routing"]["implementer"]["model"]

    def switch(self, on=True, route=True, **opus):
        self.cfg["redesign"] = {"escalation": on}
        if route:
            self.cfg["routing"]["implementer_restart"] = dict(OPUS, **opus)
        self.write_cfg()
        self.tick = self.make_tick()

    def drive(self, until, n=60):
        for _ in range(n):
            self.tick.once()
            if until():
                return
        self.fail("the card never reached the expected state")

    def models(self):
        return [r["model"] for r in self.runs("implementer")]

    def triaged(self):
        return self.task()["status"] == "triage"

    def kinds(self, kind):
        return self.conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind=?",
                                 (self.tid, kind)).fetchall()


class FullLadder(Ladder):
    def setUp(self):
        super().setUp()
        self.switch()

    def test_three_sonnet_rounds_then_three_opus_rounds_then_triage_at_the_defaults(self):
        self.drive(self.triaged)
        for _ in range(4):
            self.tick.once()
        task = self.task()
        self.assertEqual((task["status"], task["block_kind"]), ("triage", "restart_failed"))
        self.assertEqual(self.models(), [self.sonnet] * 3 + [OPUS["model"]] * 3)
        self.assertEqual([j["route"]["model"] for j in self.jobs], self.models())
        self.assertEqual(self.jobs[3]["route"]["turn_budget"], OPUS["turn_budget"])
        self.assertEqual(self.kinds("runaway"), [])
        self.assertEqual(self.kinds("failure_limit"), [])

    def test_reviewer_rounds_count_from_the_restart_so_opus_gets_rounds_1_2_3(self):
        self.drive(self.triaged)
        self.assertEqual(self.rounds, [1, 2, 3, 1, 2, 3])

    def test_the_hand_off_failure_goes_back_to_ready_not_to_triage(self):
        self.drive(lambda: len(self.jobs) == 4)
        self.assertEqual(self.kinds("failure_limit"), [])
        self.assertEqual(self.task()["block_kind"], None)

    def test_failed_ladder_reason_is_plain_and_reaches_the_outbox(self):
        self.drive(self.triaged)
        (ev,) = [json.loads(r[0]) for r in self.conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='blocked'", (self.tid,))]
        self.assertEqual(ev["kind"], "restart_failed")
        self.assertIn("Opus restart", ev["reason"])
        self.assertIn("dead branch", ev["reason"])
        self.assertIn(self.tid, (self.home / "outbox.log").read_text())

    def test_a_passing_opus_fix_round_finishes_the_card(self):
        self.script["reviewer"] = [[REV_FAIL]] * 4 + [[REV_PASS]]
        self.drive(lambda: self.task()["status"] in ("done", "triage", "blocked"))
        self.assertEqual(self.task()["status"], "done")
        self.assertEqual(self.models()[-1], OPUS["model"])
        self.assertEqual(len(self.models()), 5)

    def test_restart_run_gets_notes_and_no_prior_runs_or_duplicate_findings(self):
        self.drive(lambda: len(self.jobs) == 4)
        for job in self.jobs[:3]:
            self.assertNotIn("restart_notes", job["inputs"])
        self.assertTrue(self.jobs[1]["inputs"]["prior_runs"])
        restart_inputs = self.jobs[3]["inputs"]
        self.assertEqual(len(restart_inputs["restart_notes"]), 3)
        self.assertTrue(all(n.startswith("Review failed:") and "dead branch" in n
                            for n in restart_inputs["restart_notes"]))
        self.assertNotIn("review_findings", restart_inputs)
        self.assertEqual(restart_inputs["prior_runs"], [])

    def test_the_restart_run_keeps_a_blind_finding_and_drops_the_other_findings(self):
        blind = "blind:t_x.test_a failed\ngiven: g\nexpect: e\nobserved: o"
        was = self.tick._open_findings
        self.tick._open_findings = lambda *a, **k: [*was(*a, **k), blind]
        self.drive(lambda: len(self.jobs) == 4)
        self.assertEqual(self.jobs[3]["inputs"]["review_findings"], [blind])
        self.assertEqual(len(self.jobs[3]["inputs"]["restart_notes"]), 3)
        self.assertIn(blind, self.jobs[1]["inputs"]["review_findings"])

    def test_a_validator_run_is_no_prior_run_of_the_implementer_in_gate_mode_only(self):
        self.conn.execute("INSERT INTO task_runs (task_id, profile, run_id, started_at, status)"
                          " VALUES (?, 'validator', 'v1', 1, 'done')", (self.tid,))
        root = self.tick._spool_root(self.cfg)
        (root / "v1").mkdir(parents=True)
        run = {"task_id": self.tid, "profile": "implementer", "run_id": "now", "id": 10 ** 6}
        for mode, want in (("gate", []), ("report", ["v1"]), ("off", ["v1"])):
            cfg = dict(self.cfg, redesign={"blind_tests": mode})
            got = restart.apply(self.conn, cfg, run, {}, root)["prior_runs"]
            self.assertEqual([p["run_id"] for p in got], want, mode)

    def test_opus_fix_rounds_see_only_opus_attempt_runs(self):
        self.drive(lambda: len(self.jobs) == 6)
        opus_impl = self.runs("implementer")[3]
        sonnet = {r["run_id"] for r in self.runs() if r["id"] < opus_impl["id"]}
        for job in self.jobs[4:6]:
            self.assertNotIn("restart_notes", job["inputs"])
            self.assertTrue(job["inputs"]["review_findings"])
            seen = {p["run_id"] for p in job["inputs"]["prior_runs"]}
            self.assertTrue(seen)
            self.assertFalse(seen & sonnet)
        first = {p["run_id"] for p in self.jobs[4]["inputs"]["prior_runs"]}
        self.assertIn(opus_impl["run_id"], first)

    def test_a_checkpoint_continuation_of_the_restart_keeps_its_notes(self):
        self.script["implementer"] = [[IMPL_DONE]] * 3 + [[CHECKPOINT]] + [[IMPL_DONE]] * 4
        self.drive(lambda: len(self.jobs) == 5)
        restart_run, cont = self.runs("implementer")[3:5]
        self.assertEqual(cont["resumed_from"], restart_run["run_id"])
        self.assertEqual(self.jobs[4]["route"]["model"], OPUS["model"])
        self.assertEqual(len(self.jobs[4]["inputs"]["restart_notes"]), 3)
        # only the restart attempt's own earlier segment, never a Sonnet run
        self.assertEqual([p["run_id"] for p in self.jobs[4]["inputs"]["prior_runs"]],
                         [restart_run["run_id"]])

    def test_a_tripped_round_counts_so_a_guide_continue_buys_no_extra_run(self):
        event(self.conn, self.tid, "runaway", {"trigger": "runs", "detail": "x", "pass": True})
        self.assertEqual(restart._failed_rounds(self.conn, self.tid), 1)
        for _ in range(2):
            event(self.conn, self.tid, "failed",
                  {"reason": "Review failed:\n- x", "failure_count": 1, "limit": 9})
        self.assertTrue(restart.active(self.conn, self.cfg, self.tid))   # 1 tripped + 2 failed = 3

    def test_a_human_reset_gives_the_card_a_fresh_sonnet_ladder(self):
        self.drive(self.triaged)
        blocks.reset_failures(self.conn, self.tid, by="human")
        route = restart.route(self.conn, self.cfg, self.tid, "implementer")
        self.assertEqual(route["model"], self.sonnet)

    def test_other_roles_keep_their_routes(self):
        self.drive(lambda: len(self.jobs) == 4)
        for role in ("reviewer", "validator", "guide"):
            got = restart.route(self.conn, self.cfg, self.tid, role)
            self.assertEqual(got, config.route(self.cfg, role))


FATAL = Result(None, 1, tt.TOKENS, 1, "fatal", None, None, "boom")


class OtherFailures(Ladder):
    """A crash or a timeout during the Opus attempt counts against `failure_count` too: the
    ladder's cap applies to them, so one does not triage the card at once (switch off: same as
    before)."""

    def seed_three_failed_rounds(self):
        for _ in range(3):
            event(self.conn, self.tid, "failed",
                  {"reason": "Review failed:\n- x", "failure_count": 1, "limit": 9})
        self.conn.execute("UPDATE tasks SET failure_count=3 WHERE id=?", (self.tid,))
        self.conn.commit()

    def test_a_crash_during_the_opus_attempt_goes_back_to_ready(self):
        self.switch()
        self.seed_three_failed_rounds()
        self.script["implementer"] = [[FATAL]] + [[IMPL_DONE]] * 4
        self.drive(lambda: self.runs("implementer") and self.runs("implementer")[0]["outcome"])
        self.assertEqual(self.runs("implementer")[0]["model"], OPUS["model"])
        self.assertEqual(self.task()["failure_count"], 4)
        self.assertNotEqual(self.task()["status"], "triage")
        self.assertEqual(self.kinds("failure_limit"), [])

    def test_a_crash_with_the_switch_off_still_triages_at_the_cap(self):
        self.switch(on=False)
        self.conn.execute("UPDATE tasks SET failure_count=2 WHERE id=?", (self.tid,))
        self.conn.commit()
        self.script["implementer"] = [[FATAL]]
        self.drive(self.triaged)
        self.assertEqual(self.task()["failure_count"], 3)

    def test_a_timeout_during_the_opus_attempt_goes_back_to_ready(self):
        self.switch()
        self.seed_three_failed_rounds()
        killed = []
        now = int(time.time())
        self.tick = self.make_tick(spawn=lambda *a: SimpleNamespace(pid=51000),
                                   alive=lambda pid: pid not in killed, clock=lambda: now)
        self.tick._kill = killed.append
        self.tick.once(now)
        self.assertEqual(self.models(), [OPUS["model"]])
        self.runner.up = False
        self.assertEqual(self.tick.once(now + 7200)["timed_out"], 1)
        self.assertEqual(self.task()["failure_count"], 4)
        self.assertEqual(self.task()["status"], "ready")


class LandingFailure(tl.Landing):
    """The Opus attempt passed review and the landing check is red once: with three review
    failures already on the card that one failure must not triage it (switch off: it does)."""

    def setUp(self):
        super().setUp()
        self.sha = self.put("a.txt", "work\n", "card work")
        self.cfg["contract_commands"] = ["exit 1"]
        self.cfg["repo_url"] = str(self.repo)
        self.cfg["caps"] = {"failure_limit": 3}
        self.cfg["routing"] = {"implementer_restart": dict(OPUS)}
        self.conn.execute("UPDATE tasks SET failure_count=3 WHERE id=?", (self.tid,))
        self.conn.commit()

    def card(self):
        return self.conn.execute("SELECT status, failure_count FROM tasks WHERE id=?",
                                 (self.tid,)).fetchone()

    def check(self):
        landing._check(self.conn, self.cfg, self.home, tl.NOW, self.release, self.tid, tl.BRANCH,
                       self.sha)

    def test_on_a_red_landing_goes_back_to_ready_on_both_landing_paths(self):
        self.cfg["redesign"] = {"escalation": True}
        self.land()
        self.assertEqual(tuple(self.card()), ("ready", 4))
        self.conn.execute("UPDATE tasks SET status='done' WHERE id=?", (self.tid,))
        self.conn.commit()
        self.check()
        self.assertEqual(tuple(self.card()), ("ready", 5))

    def test_off_a_red_landing_still_triages_at_the_cap_on_both_landing_paths(self):
        self.cfg["redesign"] = {"escalation": False}
        self.land()
        self.assertEqual(tuple(self.card()), ("triage", 4))
        self.conn.execute("UPDATE tasks SET status='done', failure_count=3 WHERE id=?", (self.tid,))
        self.conn.commit()
        self.check()
        self.assertEqual(tuple(self.card()), ("triage", 4))


class RestartAccount(Ladder):
    def test_the_dispatcher_honours_the_restart_routes_own_account_pause(self):
        self.switch(account="opus-acct")
        db.guard_set(self.conn, "pause:account:opus-acct", {"until": int(time.time()) + 3600})
        for _ in range(40):
            self.tick.once()
        self.assertEqual(self.models(), [self.sonnet] * 3)
        self.assertEqual(self.task()["status"], "ready")
        self.assertEqual(restart.account(self.conn, self.cfg, self.tid, "implementer"), "opus-acct")
        ready = dispatch.ready(self.conn, self.cfg, self.tick.host_tiers, time.time(), self.home)
        self.assertEqual(ready, [])
        db.guard_set(self.conn, "pause:account:opus-acct", {"until": 0})
        self.drive(lambda: len(self.models()) == 4)
        self.assertEqual(self.models()[3], OPUS["model"])

    def test_a_card_before_the_restart_is_checked_on_the_sonnet_account(self):
        self.switch(account="opus-acct")
        self.assertEqual(restart.account(self.conn, self.cfg, self.tid, "implementer"), "default")


class Settings(unittest.TestCase):
    CFG = {"redesign": {"escalation": True}, "routing": {"implementer_restart": OPUS},
           "caps": {"failure_limit": 3}}

    def test_ladder_on_lifts_the_limits_to_the_ladder_length(self):
        ks = runaway.settings(self.CFG)
        self.assertEqual((ks["max_rounds"], ks["same_class_rounds"]), (6, 6))
        self.assertEqual(restart.failure_cap(self.CFG), 6)

    def test_ladder_on_keeps_larger_and_zero_values(self):
        cfg = dict(self.CFG, runaway={"max_rounds": 9, "same_class_rounds": 0})
        ks = runaway.settings(cfg)
        self.assertEqual((ks["max_rounds"], ks["same_class_rounds"]), (9, 0))

    def test_switch_off_or_no_route_leaves_every_limit_alone(self):
        for cfg in ({"caps": {"failure_limit": 3}},
                    {"redesign": {"escalation": False}, "routing": {"implementer_restart": OPUS},
                     "caps": {"failure_limit": 3}},
                    {"redesign": {"escalation": True}, "routing": {},
                     "caps": {"failure_limit": 3}}):
            self.assertEqual(runaway.settings(cfg), runaway.DEFAULTS)
            self.assertEqual(restart.failure_cap(cfg), 3)


class Off(Ladder):
    def setUp(self):
        super().setUp()
        self.cfg["caps"]["failure_limit"] = 9
        self.cfg["runaway"] = {"max_rounds": 99, "same_class_rounds": 0}

    def test_off_runs_every_round_on_the_implementer_route_with_no_restart_input(self):
        for kw in ({"on": False}, {"on": False, "route": False}):
            self.switch(**kw)
            self.assertEqual(restart.route(self.conn, self.cfg, self.tid, "implementer"),
                             config.route(self.cfg, "implementer"))
        self.drive(lambda: len(self.jobs) == 4)
        want = config.route(self.cfg, "implementer")
        self.assertEqual([j["route"] for j in self.jobs], [want] * 4)
        self.assertTrue(all("restart_notes" not in j["inputs"] for j in self.jobs))
        self.assertNotEqual(self.task()["block_kind"], "restart_failed")

    def test_switch_on_without_a_restart_route_is_refused_by_config_validation(self):
        self.cfg["redesign"] = {"escalation": True}
        with self.assertRaises(ValueError):
            config.validate(self.cfg)
        self.cfg["routing"]["implementer_restart"] = dict(OPUS)
        config.validate(self.cfg)

    def test_switch_on_refuses_a_restart_model_equal_to_the_implementer_model(self):
        self.cfg["redesign"] = {"escalation": True}
        same = dict(OPUS, model=self.cfg["routing"]["implementer"]["model"])
        self.cfg["routing"]["implementer_restart"] = same
        with self.assertRaisesRegex(ValueError, "must differ"):
            config.validate(self.cfg)
        self.cfg["redesign"] = {"escalation": False}
        config.validate(self.cfg)   # off: the route is only checked for shape


class Brief(unittest.TestCase):
    TASK = {"id": "t_1", "title": "card", "body": "do the thing"}

    def test_restart_notes_are_in_the_prompt_and_absent_otherwise(self):
        plain = bundles.implementer(self.TASK)["prompt"]
        self.assertNotIn("Fresh restart", plain)
        notes = ["Review failed:\n- app.py:12 dead branch"]
        brief = bundles.implementer(self.TASK, restart_notes=notes)["prompt"]
        self.assertIn("Fresh restart", brief)
        self.assertIn("app.py:12 dead branch", brief)

    def test_brief_says_the_sonnet_diff_may_be_discarded_and_long_notes_are_trimmed(self):
        brief = bundles.implementer(self.TASK, restart_notes=["Review failed:\n- x"])["prompt"]
        self.assertIn("may discard that diff and start again from the card's start point", brief)
        self.assertIn("trimmed to 3000 characters", brief)
        self.assertNotIn("verbatim", brief.split("## Fresh restart")[1])

    def test_an_empty_note_list_changes_nothing(self):
        self.assertEqual(bundles.implementer(self.TASK, restart_notes=[]),
                         bundles.implementer(self.TASK))


if __name__ == "__main__":
    unittest.main()
