"""R35 land-time blind step: what can go wrong around the run and what a person does about it.
Waits (nothing counts as a failure), the mount audit, `no blind tests yet`, a person's release (one
landing attempt, the same cause only), the cause every hold carries and the exits that work.
Fixtures in `lander_world`."""
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from capsule import bundles, db, incidents, interventions, lander, lander_blind
from capsule.blind import dispatch, gate
from capsule.blind import run as blind_run
from capsule.runners import docker, sandbox
from capsule.tests.lander_world import BOARD, HOURS_72, PASSING, TESTS, Landing, Strong
from capsule.tests.test_blind_run import THREE, blind_repo, record
from capsule.tests.test_lander import BRANCH
from capsule.tests.test_lander_blind import SKIPPING

AUDIT = f"{docker.BLIND_CHECK}: /somewhere [run capsule-abcdef123456]"
ACCEPT = "- the page greets twice\n"


class Unable(Landing):
    def test_docker_down_is_a_wait_nothing_runs_and_nothing_counts_as_a_failure(self):
        strong = Strong(up=False)
        result = self.land(strong)
        self.assertEqual((result["verdict"], result["hold"]), ("wait", "lander_wait_docker"))
        self.assertEqual((strong.started, incidents.listing(self.conn)), ([], []))
        lander.record(self.conn, self.card, result, limit=3)
        task = self.task()
        self.assertEqual((task["status"], task["block_kind"], task["failure_count"]),
                         ("blocked", "transient", 0))

    def test_a_wait_names_what_a_person_does_and_never_says_it_is_retried(self):
        self.landed(Strong(up=False))
        said = self.events("blocked")[-1]["reason"]
        self.assertIn(lander_blind.WAIT_NEXT, said)
        self.assertNotRegex(said, r"is retried|lifts by itself")

    def test_a_transient_wait_whose_cause_is_not_docker_does_not_name_docker(self):
        self.assertNotIn("docker", lander_blind.WAIT_NEXT.lower())

    def test_a_container_that_will_not_start_is_a_wait(self):
        strong = Strong(start_error=subprocess.CalledProcessError(1, "docker", "", "no image"))
        self.assertEqual(self.land(strong)["verdict"], "wait")

    def test_a_container_gone_with_the_daemon_is_a_wait(self):
        result = self.land(Strong(dies=True))
        self.assertEqual((result["verdict"], incidents.listing(self.conn)), ("wait", []))

    def test_a_container_gone_while_docker_still_answers_is_a_wait_without_an_incident(self):
        strong = Strong(gone=True)
        result = self.land(strong)
        self.assertEqual((result["verdict"], result["hold"]), ("wait", "lander_wait_docker"))
        self.assertTrue(strong.started)
        self.assertEqual((incidents.listing(self.conn), self.task()["failure_count"]), ([], 0))

    def test_a_container_gone_before_the_first_test_is_a_wait_and_nothing_is_exec_d(self):
        strong = Strong(gone_early=True)
        result = self.land(strong)
        self.assertEqual((result["verdict"], strong.timeouts), ("wait", []))
        self.assertTrue(strong.started)
        self.assertEqual((incidents.listing(self.conn), self.task()["failure_count"]), ([], 0))

    def test_a_container_that_cannot_be_stopped_after_the_run_does_not_change_the_verdict(self):
        # stop-timeout: the post-run stop is best effort (the reaper removes the container), so
        # the verdict is the run's own (the failing test), not a wait and not a crash.
        strong = Strong(stop_error=RuntimeError("docker rm -f capsule-x failed (rc=1)"))
        result = self.land(strong)
        self.assertEqual((result["verdict"], result["hold"]), ("held", "blind tests failed"))
        self.assertTrue(strong.stopped)
        self.assertEqual([r["root_key"].split(": ")[-1] for r in incidents.listing(self.conn)],
                         ["#.test_fails"])     # the run's own failure, nothing about the stop

    def test_a_git_timeout_or_os_error_in_the_step_is_a_wait(self):
        for error in (subprocess.TimeoutExpired("git", 1), OSError("no git")):
            with mock.patch.object(lander_blind, "_state", side_effect=error):
                result = self.land(Strong())
            self.assertEqual((result["verdict"], incidents.listing(self.conn)), ("wait", []))

    def test_a_timeout_is_a_hold_with_an_incident_never_a_pass(self):
        result = self.land(Strong(boom=subprocess.TimeoutExpired("x", 1)))
        self.assertEqual(result["verdict"], "held")
        self.assertIn("exit code 124", self.checks(result)["blind"]["detail"])
        self.assertEqual(len(incidents.listing(self.conn)), 1)

    def test_no_time_left_in_the_landing_is_a_wait(self):
        for left in (-1e6, 0.5):         # none, and less than the one second a timeout needs
            strong = Strong()
            plan = lander_blind.plan(self.conn, self.cfg, self.card, False)
            status, _ = lander_blind.check(plan, self.conn, self.cfg, None, runner=strong,
                                           deadline=time.monotonic() + left, timeout=100)
            self.assertEqual((status, strong.started), ("wait", []))

    def test_a_run_given_less_than_a_second_by_its_timeout_is_a_wait_nothing_runs(self):
        for timeout in (0, 0.5):         # plenty of the deadline is left: the timeout is too short
            strong = Strong()
            plan = lander_blind.plan(self.conn, self.cfg, self.card, False)
            status, _ = lander_blind.check(plan, self.conn, self.cfg, None, runner=strong,
                                           deadline=time.monotonic() + 1e6, timeout=timeout)
            self.assertEqual((status, strong.started), ("wait", []))

    def test_results_that_disagree_with_the_summary_are_a_hold_and_an_incident(self):
        repo = blind_repo(self.dir / "r2", self.card, THREE,
                          ("test_greets", "test_fails", "test_third"))
        record(self.conn, self.card, repo)
        self.cfg["blind_tests"] = {"repo": str(repo), "adapter": "unittest"}
        result = self.land(Strong(tamper=lambda t: t.replace("Ran 3 tests", "Ran 2 tests")))
        self.assertEqual(result["verdict"], "held")
        self.assertEqual(self.incident_roles(), [f"blind_runner:{self.card}"])

    def test_a_run_line_that_cannot_be_built_is_a_hold_that_says_what_is_true(self):
        for error in (IndexError("x"), KeyError("run"), ValueError("bad toml")):
            with mock.patch.object(blind_run, "_run_line", side_effect=error):
                strong = Strong()
                result = self.land(strong)
            self.assertEqual((result["verdict"], strong.started), ("held", []))
            said = self.checks(result)["blind"]["detail"]
            self.assertIn("no blind run line could be built from blind.toml", said)
            self.assertIn("a bad adapter", said)
            self.assertNotIn("malformed", said)


class MountAudit(Landing):
    source = PASSING

    def test_an_audit_hit_on_the_blind_container_is_a_hold_with_the_workers_incident(self):
        for _ in range(2):
            result = self.land(Strong(start_error=RuntimeError(AUDIT)))
            self.assertEqual((result["verdict"], result["hold"]), ("held", "mount audit hit"))
        found = incidents.listing(self.conn)
        self.assertEqual([(i["kind"], i["role"]) for i in found],
                         [("review_infra", f"{docker.BLIND_CHECK}:{self.card}")])     # one print
        self.assertEqual(self.task()["failure_count"], 0)
        self.assertNotIn("/somewhere", self.dump())

    def test_an_audit_hit_on_the_suites_container_is_a_hold_and_its_copy_is_removed(self):
        made = []
        real = tempfile.mkdtemp

        def mkdtemp(*a, **kw):
            made.append(real(*a, **kw))
            return made[-1]

        with mock.patch.object(sandbox, "session", sandbox.REAL_SESSION), \
                mock.patch.object(sandbox.tempfile, "mkdtemp", mkdtemp):
            result = self.land(Strong(start_error=RuntimeError(AUDIT)))
        self.assertEqual(result["verdict"], "held")
        self.assertEqual(self.incident_roles(), [f"{docker.BLIND_CHECK}:{self.card}"])
        self.assertTrue(made)
        self.assertFalse(any(Path(p).exists() for p in made))

    def test_any_other_runtime_error_is_not_swallowed(self):
        with self.assertRaises(RuntimeError):
            lander_blind.audit_hold(self.conn, self.cfg, self.card, RuntimeError("boom"))


class NoTests(Landing):
    def setUp(self):
        super().setUp()
        self.conn.execute("UPDATE tasks SET blind_commit=NULL WHERE id=?", (self.card,))

    def test_a_card_that_would_get_a_tester_with_no_tests_is_held_with_the_plain_reason(self):
        strong = Strong()
        result = self.land(strong)
        self.assertEqual((result["verdict"], result["hold"]), ("held", "no blind tests yet"))
        self.assertEqual(strong.started, [])
        lander.record(self.conn, self.card, result, limit=3, board=BOARD)
        held = self.events("blocked")[-1]
        self.assertTrue(held["reason"].startswith("no blind tests yet; to go on"))
        for verb in ("`accept`", "`close`",
                     f"capsule human {BOARD} {self.card} amend '<reason>' --body-file <path>"):
            self.assertIn(verb, held["reason"])
        self.assertNotRegex(held["reason"], r"lifts by itself|unblock|retried")
        self.assertTrue(held["cause"].startswith(f"{gate.CAUSE}:"))
        self.assertEqual(self.task()["status"], "blocked")

    def test_a_persons_live_release_lands_it_and_the_record_says_without_blind_tests(self):
        self.held_for(lander_blind.PENDING, 200)
        self.accepted(100)
        result = self.land(Strong())
        self.assertEqual(result["verdict"], "checked")
        self.assertIn("without blind tests", self.checks(result)["blind"]["detail"])

    def test_a_card_that_would_get_no_tester_lands_as_before(self):
        self.conn.execute("UPDATE tasks SET body='no acceptance here' WHERE id=?", (self.card,))
        result = self.land(Strong())
        self.assertEqual(result["verdict"], "checked")
        self.assertNotIn("blind", self.checks(result))

    def test_a_card_that_is_gone_from_the_board_is_not_a_crash_in_the_record(self):
        result = self.land(Strong())
        self.conn.execute("DELETE FROM tasks WHERE id=?", (self.card,))
        lander.record(self.conn, self.card, result, limit=3)
        self.assertIsNone(self.task())          # nothing to move, and no crash


class Released(Landing):
    """A person's release (S17, S28, S29): one landing attempt, the same cause only."""
    source = PASSING

    def broken(self):
        return Strong(tamper=lambda t: t.replace("Ran 2 tests", "Ran 1 test"))

    def test_without_a_release_the_inconclusive_run_holds(self):
        self.assertEqual(self.land(self.broken())["verdict"], "held")

    def test_a_live_release_for_that_reason_lands_the_card_and_the_record_says_so(self):
        self.held_for(lander_blind.INCONCLUSIVE, 200)
        self.accepted(100)
        result = self.land(self.broken())
        self.assertEqual(result["verdict"], "checked")
        self.assertEqual(self.checks(result)["blind"]["detail"],
                         lander_blind.RELEASED + lander_blind.INCONCLUSIVE)

    def test_a_release_for_another_reason_does_not(self):
        self.held_for(lander_blind.PENDING, 200)
        self.accepted(100)
        self.assertEqual(self.land(self.broken())["verdict"], "held")

    def test_an_expired_release_does_not(self):
        self.held_for(lander_blind.INCONCLUSIVE, HOURS_72 + 500)
        self.accepted(HOURS_72 + 100)
        self.assertEqual(self.land(self.broken())["verdict"], "held")

    def test_an_accept_by_anyone_but_a_person_does_not(self):
        self.held_for(lander_blind.INCONCLUSIVE, 200)
        self.accepted(100, author="cos")
        self.assertEqual(self.land(self.broken())["verdict"], "held")

    def test_a_release_does_not_land_a_card_whose_test_really_fails(self):
        self.commit_tests(TESTS)
        record(self.conn, self.card, self.blind)
        self.held_for(lander_blind.INCONCLUSIVE, 200)
        self.accepted(100)
        self.assertEqual(self.land(Strong())["verdict"], "held")

    def test_a_release_does_not_land_a_run_with_a_failing_row_that_then_timed_out(self):
        self.commit_tests(TESTS)
        record(self.conn, self.card, self.blind)
        self.held_for(lander_blind.INCONCLUSIVE, 200)
        self.accepted(100)
        result = self.land(Strong(late=True))
        self.assertEqual(result["verdict"], "held")
        self.assertNotIn("person's release", str(result["checks"]))

    def test_a_release_is_spent_by_the_first_landing_attempt(self):
        self.held_for(lander_blind.INCONCLUSIVE, 200)
        self.accepted(100)
        self.assertEqual(self.landed(self.broken())["verdict"], "checked")
        self.assertEqual(self.landed(self.broken())["verdict"], "held")     # not reused

    def test_a_release_does_not_survive_a_land_time_failed_hold(self):
        self.held_for(lander_blind.INCONCLUSIVE, 300)
        self.accepted(200)
        self.commit_tests(TESTS)
        record(self.conn, self.card, self.blind)
        self.assertEqual(self.landed(Strong())["hold"], lander_blind.FAILED)   # carries a cause
        self.commit_tests(PASSING)
        record(self.conn, self.card, self.blind)
        self.assertEqual(self.land(self.broken())["verdict"], "held")

    def test_a_release_given_for_the_validators_skip_is_honoured_at_the_lander(self):
        self.commit_tests(SKIPPING)
        record(self.conn, self.card, self.blind)
        self.held_for(lander_blind.SKIPPED, 200)
        self.accepted(100)
        result = self.land(Strong())
        self.assertEqual(result["verdict"], "checked")
        self.assertEqual(self.checks(result)["blind"]["detail"],
                         lander_blind.RELEASED + lander_blind.SKIPPED)

    def test_a_release_for_an_inconclusive_validator_hold_is_honoured_for_a_skip_at_land(self):
        self.commit_tests(SKIPPING)
        record(self.conn, self.card, self.blind)
        self.held_for(lander_blind.INCONCLUSIVE, 200)
        self.accepted(100)
        result = self.land(Strong())
        self.assertEqual(result["verdict"], "checked")
        self.assertEqual(self.checks(result)["blind"]["detail"],
                         lander_blind.RELEASED + lander_blind.SKIPPED)

    def test_an_accept_after_a_lander_only_inconclusive_hold_is_honoured(self):
        self.assertEqual(self.landed(self.broken())["verdict"], "held")    # no validator hold
        self.accepted(0)
        self.assertEqual(self.land(self.broken())["verdict"], "checked")


class Causes(Landing):
    """Every lander hold carries the validator's `cause` for its reason (S29)."""

    def cause_of_last_hold(self, strong):
        self.landed(strong)
        return self.events("blocked")[-1]["cause"]

    def test_a_failed_an_inconclusive_a_skipped_and_a_pending_hold_each_carry_their_cause(self):
        task = self.task()

        def want(reason):
            return lander_blind.cause(task, reason)

        self.assertEqual(self.cause_of_last_hold(Strong()), want(lander_blind.FAILED))
        self.assertEqual(self.cause_of_last_hold(Strong(late=True)),
                         want(lander_blind.INCONCLUSIVE))
        self.commit_tests(SKIPPING)
        record(self.conn, self.card, self.blind)
        self.assertEqual(self.cause_of_last_hold(Strong()), want(lander_blind.SKIPPED))
        self.conn.execute("UPDATE tasks SET blind_commit=NULL WHERE id=?", (self.card,))
        self.assertEqual(self.cause_of_last_hold(Strong()), want(lander_blind.PENDING))

    def test_an_audit_hold_carries_a_cause_and_is_offered_no_accept(self):
        self.landed(Strong(start_error=RuntimeError(AUDIT)))
        held = self.events("blocked")[-1]
        self.assertTrue(held["cause"].endswith("mount audit hit"))
        self.assertNotIn("`accept`", held["reason"])


class Exits(Landing):
    """The verbs a hold names really lift it (S15, S24)."""

    def human(self, kind, text=None, n=[0]):      # noqa: B006
        n[0] += 1
        with mock.patch.object(interventions, "routing_gap", return_value=(BRANCH, "dev", [])):
            interventions.apply(self.conn, self.cfg, self.card,
                                {"kind": kind, "text": text, "author": "owner"},
                                f"{n[0]}.json", "file", int(time.time()))

    def test_reset_sends_a_card_held_for_a_failing_test_back_for_a_new_implementer_round(self):
        self.landed(Strong())
        self.assertEqual(self.task()["status"], "blocked")
        self.human("reset")
        self.assertEqual((self.task()["status"], self.task()["failure_count"]), ("ready", 0))

    def test_amend_changes_the_card_hash_so_a_new_tester_is_due(self):
        old = bundles.card_hash(self.task()["body"])
        db.event(self.conn, self.card, "blind_tester_skipped",
                 {"reason": "x", "card_hash": old, "final": True})
        self.assertTrue(dispatch._known(self.conn, self.card, old))     # none is due for it now
        self.conn.execute("UPDATE tasks SET body=? WHERE id=?",
                          (self.body + ACCEPT, self.card))                              # `amend`
        self.assertFalse(dispatch._known(self.conn, self.card,
                                         bundles.card_hash(self.task()["body"])))

    def test_accept_by_a_person_lands_a_card_held_for_no_blind_tests(self):
        self.conn.execute("UPDATE tasks SET blind_commit=NULL WHERE id=?", (self.card,))
        first = self.land(Strong())
        self.assertEqual(first["hold"], "no blind tests yet")
        lander.record(self.conn, self.card, first, limit=3)
        self.assertIn("`accept`", self.events("blocked")[-1]["reason"])
        self.accepted(0)
        second = self.land(Strong())
        self.assertEqual(second["verdict"], "checked")
        self.assertIn("without blind tests", self.checks(second)["blind"]["detail"])


if __name__ == "__main__":
    unittest.main()
