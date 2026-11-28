"""R35 (blind spec B6): the lander runs a card's blind tests again at land time, in `gate` mode,
against the exact commit it is about to push: a green run lands, a failing or untrustworthy one
holds. Fixtures in `lander_world`. Oracle 1 is the verdict and its `checks`; oracle 2 is the card's
row in the board, the incidents and the refs (nothing is pushed here: `run` never pushes)."""
import json
import re
import time
import unittest
from unittest import mock

from capsule import db, incidents, lander, lander_blind
from capsule.blind import run as blind_run
from capsule.runners import docker, sandbox
from capsule.tests.lander_world import BOARD, PASSING, TESTS, Landing, Strong
from capsule.tests.test_blind_run import blind_repo, record
from capsule.tests.test_lander import git

SKIPPING = TESTS.replace('self.assertEqual(run("echo no").exit, 1)', 'self.skipTest("later")')
SETUP_FAILS = """import unittest

from harness import run


class Greeting(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        raise RuntimeError("boom")

    def test_greets(self):
        self.assertEqual(run("cat hello.txt").stdout, "hi\\n")
"""
MARKER = "SECRET-MARKER-7f3a"


class Green(Landing):
    source = PASSING

    def test_green_blind_run_on_the_commit_to_push_checks_the_card(self):
        strong = Strong()
        before = self.refs()
        result = self.land(strong)
        self.assertEqual((result["verdict"], result["hold"]), ("checked", None))
        self.assertEqual(self.checks(result)["blind"]["status"], "pass")
        self.assertEqual(list(self.checks(result))[-2:], ["blind", "deploy"])
        self.assertEqual(strong.heads, [result["commit"]])     # the commit the lander would push
        self.assertEqual(self.refs(), before)               # nothing moved

    def test_a_card_behind_main_is_blind_run_at_the_replayed_commit(self):
        self.behind("b.txt")
        strong = Strong()
        result = self.land(strong)
        self.assertNotEqual(result["commit"], result["card_commit"])      # replayed onto main
        self.assertEqual((result["verdict"], strong.heads), ("checked", [result["commit"]]))

    def test_the_container_has_the_validators_limits_and_one_writable_folder(self):
        strong = Strong()
        self.land(strong)
        (_, ws, kw), = strong.started
        self.assertEqual((kw["ro"], kw["git_dir"], kw["tmpdir"]), (True, None, "/tmp"))
        self.assertEqual(sorted(m.split(":", 1)[1] for m in kw["mounts"]), ["/blind:ro", "/out"])
        self.assertGreater(kw["lifetime"], sandbox.MARGIN)        # a killed lander leaves none
        mounts = {m.split(":")[0] for m in kw["mounts"]}
        own = mounts | {str(ws)}  # the audit exempts only these
        self.assertEqual({str(p) for p in kw["allow_blind"]}, own)
        args = " ".join(docker.run_args("x", ws, "img", **{k: v for k, v in kw.items()
                                                            if k != "allow_blind"}))
        for want in ("--network=none", ":/workspace:ro", "--cap-drop ALL"):
            self.assertIn(want, args)
        self.assertNotIn("docker.sock", args)

    def test_the_snapshot_is_removed_after_the_run(self):
        strong = Strong()
        self.land(strong)
        self.assertFalse(strong.snaps[0].parent.exists())
        self.assertEqual(strong.stopped, ["capsule-" + strong.started[0][0][:12]])

    def test_the_snapshot_is_removed_when_the_run_raises(self):
        strong = Strong(boom=ValueError("boom"))
        with self.assertRaises(ValueError):
            self.land(strong)
        self.assertFalse(strong.snaps[0].parent.exists())

    def test_the_run_uses_the_recorded_commit_not_the_blind_head(self):
        self.commit_tests(TESTS)                               # blind main now fails the card
        self.assertEqual(self.land(Strong())["verdict"], "checked")

    def test_the_card_is_not_blind_run_in_off_and_report(self):
        for mode in ("off", "report"):
            self.cfg["redesign"] = {"blind_tests": mode}
            strong = Strong()
            result = self.land(strong)
            self.assertEqual((strong.started, result["verdict"]), ([], "checked"))
            self.assertNotIn("blind", self.checks(result))

    def test_a_commit_already_on_main_is_a_no_op_with_no_blind_run(self):
        strong = Strong()
        result = self.land(strong, commit=git(self.repo, "rev-parse", "main"))
        self.assertEqual(strong.started, [])
        self.assertNotIn("blind", self.checks(result))

    def test_a_worktree_the_tests_write_is_never_a_pass(self):
        strong = Strong(after=lambda ws: (ws / "planted.txt").write_text("x"))
        result = self.land(strong)
        self.assertEqual((result["verdict"], self.checks(result)["blind"]["status"]),
                         ("held", "hold"))
        self.assertEqual(self.incident_roles(), [f"blind_runner:{self.card}"])

    def test_a_skipped_test_is_never_a_pass(self):
        self.commit_tests(SKIPPING)
        record(self.conn, self.card, self.blind)
        result = self.land(Strong())
        self.assertEqual((result["verdict"], self.checks(result)["blind"]["status"]),
                         ("held", "hold"))
        self.assertEqual(result["hold"], lander_blind.SKIPPED)
        self.assertEqual(self.incident_roles(), [f"blind_runner:{self.card}"])

    def test_a_landed_test_with_no_result_is_never_a_pass(self):
        repo = blind_repo(self.dir / "r2", self.card, PASSING,
                          ("test_greets", "test_fails", "test_ghost"))   # an example, no test
        record(self.conn, self.card, repo)
        self.cfg["blind_tests"] = {"repo": str(repo), "adapter": "unittest"}
        db.event(self.conn, self.card, "blind_landed", {
            "blind_commit": blind_run._recorded_commit(self.conn, self.card),
            "card_hash": "h", "start_sha": "s", "exempt": False,
            "acceptance": ["the page greets"],
            "tests": [f"{self.card}.{n}" for n in ("test_greets", "test_fails", "test_ghost")]})
        result = self.land(Strong())
        self.assertEqual(result["verdict"], "held")
        self.assertEqual(self.incident_roles(), [f"blind_runner:{self.card}"])

    def test_the_run_gets_the_least_of_the_timeout_the_blind_limit_and_the_time_left(self):
        for timeout, want in ((50, 50), (5000, blind_run.BLIND_TIMEOUT)):
            strong = Strong()
            self.land(strong, timeout=timeout)
            self.assertEqual(strong.timeouts, [want])

    def test_the_run_is_cut_to_the_time_left_in_the_landings_deadline(self):
        limit = 100
        now = time.monotonic()
        late = mock.Mock(monotonic=lambda: now + limit * 4 - 5, time=time.time)  # gate, 2 suites, 1
        strong = Strong()
        with mock.patch.object(lander_blind, "time", late):
            result = self.land(strong, timeout=limit)
        self.assertEqual(len(strong.timeouts), 1, result)
        self.assertTrue(1 <= strong.timeouts[0] <= 5, strong.timeouts)

    def test_a_card_whose_tests_went_stale_is_held_not_run(self):
        db.event(self.conn, self.card, "blind_landed", {
            "blind_commit": blind_run._recorded_commit(self.conn, self.card),
            "card_hash": "h", "start_sha": "s", "exempt": False,
            "acceptance": ["an older statement"], "tests": []})
        strong = Strong()
        result = self.land(strong)
        self.assertEqual((result["hold"], strong.started), ("no blind tests yet", []))


class Failing(Landing):
    def test_one_failing_test_holds_the_card_and_names_only_its_id(self):
        result = self.land(Strong())
        ident = f"{self.card}.test_fails"
        self.assertEqual((result["verdict"], result["hold"]), ("held", lander_blind.FAILED))
        self.assertEqual(self.checks(result)["blind"]["status"], "hold")
        self.assertEqual(self.checks(result)[f"blind:{ident}"]["status"], "hold")
        lander.record(self.conn, self.card, result, limit=3)
        task = self.task()
        self.assertEqual((task["status"], task["block_kind"]), ("blocked", "needs_input"))
        everything = json.dumps(result) + self.dump()
        for text in ("a user runs", "behaves", "echo no", "observed", "assertEqual"):
            self.assertNotIn(text, everything)
        self.assertIn(ident, everything)

    def test_the_hold_names_the_verbs_that_lift_it_and_never_unblock(self):
        self.landed(Strong())
        said = self.events("blocked")[-1]["reason"]
        for verb in ("`reset`", "amend '<reason>'", "`close`"):
            self.assertIn(verb, said)
        self.assertNotIn("unblock", said)
        sent = " ".join(str(tuple(r)) for r in self.conn.execute("SELECT * FROM outbox"))
        self.assertNotIn("unblock", sent)

    def test_the_hold_says_when_reset_alone_does_not_help_and_never_that_it_retries(self):
        self.landed(Strong())
        said = self.events("blocked")[-1]["reason"]
        self.assertIn("`reset` alone repeats this", said)    # its text or its tests are at fault
        self.assertIn("`amend` the text first", said)
        self.assertNotRegex(said, r"retried|retries by itself|lifts by itself")

    def test_the_hold_names_the_whole_amend_command_with_the_board_and_the_card(self):
        self.landed(Strong())
        said = self.events("blocked")[-1]["reason"]
        self.assertIn(f"capsule human {BOARD} {self.card} amend '<reason>' --body-file <path>",
                      said)

    def test_the_failure_opens_one_incident_naming_the_card_and_ids(self):
        self.land(Strong())
        self.land(Strong())
        found = incidents.listing(self.conn)
        self.assertEqual(len(found), 1)           # the same failure on the same card: one print
        self.assertEqual((found[0]["kind"], found[0]["task_id"], found[0]["role"]),
                         ("review_infra", self.card, f"blind_land:{self.card}"))
        self.assertIn(f"{self.card}.test_fails", found[0]["evidence"])

    def test_tests_that_passed_at_the_validator_and_fail_after_main_moved_have_an_incident(self):
        db.event(self.conn, self.card, "blind_gated",
                 {"failing": [], "counts": {"pass": 2}, "leaks": 0, "held": "", "detail": ""})
        self.behind("b.txt")
        result = self.land(Strong())
        self.assertEqual(result["verdict"], "held")
        self.assertEqual(self.incident_roles(), [f"blind_land:{self.card}"])
        lander.record(self.conn, self.card, result, limit=3)
        self.assertIn("`reset`", self.events("blocked")[-1]["reason"])
        self.assertEqual(self.task()["failure_count"], 0)       # a hold, never a counted failure

    def test_a_runner_holder_row_is_renamed_so_no_test_file_or_class_shows(self):
        self.commit_tests(SETUP_FAILS)
        record(self.conn, self.card, self.blind)
        result = self.land(Strong())
        everything = json.dumps(result) + self.dump()
        self.assertNotIn("test_it", everything)
        self.assertNotIn("Greeting", everything)

    def test_a_marker_in_the_runners_status_line_reaches_nothing(self):
        for tamper in (lambda t: t.replace("\nFAILED", f"\nFAILED {MARKER}"),
                       lambda t: re.sub(r"Ran \d+ tests?", MARKER, t)):
            self.landed(Strong(tamper=tamper))
            self.assertNotIn(MARKER, self.dump())

    def test_the_parse_messages_never_carry_the_runners_status_text(self):
        def rows(*pairs):
            return "".join(json.dumps({"id": i, "result": s}) + "\n" for i, s in pairs)

        cases = (
            (rows(("a", "pass")), f"Ran 1 test in 0s\n\nFAILED {MARKER} (failures=1)", 0),
            (rows(("a", "pass")), f"Ran 1 test in 0s\n\nOK {MARKER}\nsomething", 1),
            (rows(("a", "fail"), ("b", "fail")), f"Ran 2 tests\n\nFAILED {MARKER}", 1),
            (rows(("a", "skipped")), f"Ran 1 test\n\nOK {MARKER}", 0),
            (rows(("a", "pass")), f"{MARKER}\nno summary here", 0),
        )
        for results, text, code in cases:
            _, problem = blind_run._parse(results, text, code)
            self.assertTrue(problem, text)
            self.assertNotIn(MARKER, problem)

    def test_a_failing_row_in_a_run_that_then_times_out_is_kept_as_a_failure(self):
        result = self.land(Strong(late=True))
        ident = f"{self.card}.test_fails"
        self.assertEqual(result["verdict"], "held")
        self.assertEqual(self.checks(result)["blind"]["detail"].split(": ")[0],
                         lander_blind.INCONCLUSIVE)
        self.assertEqual(self.checks(result)[f"blind:{ident}"]["status"], "hold")   # row kept
        self.assertEqual(sorted(self.incident_roles()),
                         [f"blind_land:{self.card}", f"blind_runner:{self.card}"])

    def test_a_timeout_after_a_failing_row_is_not_offered_an_accept(self):
        self.landed(Strong(late=True))
        said = self.events("blocked")[-1]
        self.assertNotIn("`accept`", said["reason"])
        self.assertEqual(said["cause"], lander_blind.cause(self.task(), lander_blind.INCONCLUSIVE))


class Selected(Landing):
    """Test selection (D273) narrows the ordinary suites only: the blind gate and the hold for a
    card without blind tests behave as when selection is off."""
    source = PASSING

    def picked(self):
        return mock.patch("capsule.lander_select.parts", return_value=[])

    def test_the_landing_asks_the_selector_once_with_the_commit_it_checks(self):
        with self.picked() as parts:
            result = self.land(Strong())
        parts.assert_called_once()
        self.assertEqual(parts.call_args.args[3:], (result["base"], result["commit"]))

    def test_a_selected_landing_still_runs_the_blind_gate(self):
        strong = Strong()
        with self.picked():
            result = self.land(strong)
        self.assertEqual((result["verdict"], self.checks(result)["blind"]["status"]),
                         ("checked", "pass"))
        self.assertEqual(len(strong.started), 1)

    def test_a_selected_landing_still_holds_a_card_with_no_blind_tests(self):
        self.conn.execute("UPDATE tasks SET blind_commit=NULL WHERE id=?", (self.card,))
        with self.picked():
            result = self.land(Strong())
        self.assertEqual((result["verdict"], result["hold"]), ("held", "no blind tests yet"))


if __name__ == "__main__":
    unittest.main()
