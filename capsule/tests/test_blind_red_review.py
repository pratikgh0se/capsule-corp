"""R31 second review and hunt: one test per finding, same fixtures as test_blind_red_attacks.py
(temp repos, the fake strong runner, no docker, no model).

Z1 intent examples: the tests must be red-checked on the base the implementer also saw; a rerun
cannot drop landed tests or hide a test by name; an exemption needs the board's own list and a
guide ruling; a card is triaged only from ready or review; a start that hangs cannot eat the
budget; forged results end in an incident."""
import json
import subprocess
import time
import unittest
from pathlib import Path
from unittest import mock

from capsule import bundles, config, db, incidents, workspace
from capsule.blind import red, settle
from capsule.blind import run as blind_run
from capsule.tests.test_blind_red import CARD, GOOD, Dirs, exempting
from capsule.tests.test_blind_red_attacks import PASSING_SOURCE, folder
from capsule.tests.test_blind_red_tick import BODY, FAILING, RedBox, RedCase
from capsule.tests.test_blind_run import Container, RunCase

OTHER = "e" * 40


def set_start(conn, run_id, sha):
    conn.execute("UPDATE task_runs SET metadata=json_set(metadata,'$.start_sha',?) WHERE run_id=?",
                 (sha, run_id))


class Bases(RedCase):
    def implementer(self, sha):
        self.conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, started_at, run_id, metadata)"
            " VALUES (?,?,?,?,?,?)", (self.card, "implementer", "done", 0, "f" * 32,
                                      json.dumps({"start_sha": sha})))

    def test_round_two_on_the_implementers_base_is_compared_with_the_red_base(self):
        self.make_round(PASSING_SOURCE)
        self.step()                                      # round 1: bounced, base H1 = self.base
        self.implementer(OTHER)                          # the implementer started on H2
        clone = self.make_round(FAILING)
        set_start(self.conn, f"{self.n:032x}", OTHER)    # round 2's tester is on H2 too
        before = len(self.box.started)
        self.step()
        got = self.events("blind_red_checked")[-1]
        self.assertEqual(got["verdict"], "inconclusive")
        self.assertIn("differ", got["reason"])
        self.assertEqual((len(self.box.started), self.main()), (before, self.main0))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM incidents").fetchone()[0], 1)
        self.assertFalse(clone.exists())                 # settled: its clone is removed

    def test_a_round_whose_own_base_differs_from_the_implementers_is_compared_too(self):
        self.implementer(OTHER)
        self.make_round()
        self.step()
        self.assertEqual(self.events("blind_red_checked")[0]["verdict"], "inconclusive")


class LandedBase(RunCase):
    def landed(self, sha):
        db.event(self.conn, self.card, "blind_landed", {"start_sha": sha, "tests": []}, now=1)

    def test_tests_red_checked_on_another_base_than_the_implementers_are_inconclusive(self):
        self.landed(OTHER)
        box = Container(self.ws)
        rows = self.go(box)
        self.assertEqual([r["cmd"] for r in rows], ["blind:inconclusive"])
        self.assertEqual((box.started, [i["task_id"] for i in incidents.listing(self.conn)]),
                         ([], [self.card]))

    def test_tests_red_checked_on_the_same_base_run_as_before(self):
        self.landed(self.base)
        rows = self.go(Container(self.ws))
        self.assertEqual(len(rows), 2)
        self.assertEqual(incidents.listing(self.conn), [])


class Names(RedCase):
    def bounced(self):
        return self.events("blind_red_checked")[-1]

    def test_a_rerun_cannot_drop_a_landed_test(self):
        self.make_round()
        self.step()
        self.assertEqual(len(self.events("blind_landed")), 1)
        self.make_round(FAILING.replace("test_greets", "test_other"), names=("test_other",))
        self.step()
        self.assertEqual(self.bounced()["verdict"], "bounced")
        self.assertIn(f"test {self.card}.test_greets is named", self.bounced()["problems"][0])

    def test_a_test_is_matched_by_its_full_id_not_its_method_name(self):
        self.make_round(covered=[{"statement": 1, "tests": ["t_other.test_greets"]},
                                 {"statement": 2, "tests": [f"{self.card}.test_greets"]}])
        self.step()
        self.assertEqual(self.bounced()["verdict"], "bounced")
        self.assertIn("test t_other.test_greets is named", self.bounced()["problems"][0])

    def test_a_bare_method_name_in_the_report_says_to_use_the_full_id(self):
        self.make_round(covered=[{"statement": 1, "tests": ["test_greets"]},
                                 {"statement": 2, "tests": [f"{self.card}.test_greets"]}])
        self.step()
        problems = self.events("blind_red_checked")[-1]["problems"]
        self.assertEqual(problems, [f"test test_greets is a bare name: write its full id"
                                    f" {self.card}.test_greets in the examples and the report"])
        self.assertEqual(self.events("blind_landed"), [])

    def test_a_stale_round_still_stops_the_testers_container(self):
        self.make_round(card_hash="0" * 64)
        self.step()
        self.assertEqual(self.box.stopped, ["capsule-000000000000"])
        self.assertEqual(self.bounced()["verdict"], "stale")


class Exemption(RedCase):
    def rule(self):
        db.event(self.conn, self.card, "guide_ruling", {
            "role": "blind-tester", "choice": "red-exempt", "applied": True,
            "reason": "statement 1 and statement 2", "card_hash": self.hash}, now=1)

    def test_a_ruling_and_a_tester_label_alone_exempt_nothing(self):
        self.make_round(PASSING_SOURCE)
        self.rule()                                  # the tester's `covered` names the test
        self.step()
        self.assertEqual((self.events("blind_landed"), self.main()), ([], self.main0))

    def landed_with(self, covered):
        test = f"{self.card}.test_greets"
        db.event(self.conn, self.card, "guide_ruling", {
            "role": "blind-tester", "choice": "red-exempt", "applied": True,
            "reason": "statement 3", "card_hash": self.hash}, now=1)
        self.make_round(PASSING_SOURCE, mutate=exempting(self.card, [test]), covered=[
            {"statement": n, "tests": [test]} for n in covered])
        self.step()
        return len(self.events("blind_landed"))

    def test_a_test_listed_under_a_ruled_statement_only_is_exempt(self):
        self.assertEqual(self.landed_with([3]), 1)

    def test_a_test_also_listed_under_an_unruled_statement_is_not_exempt(self):
        self.assertEqual(self.landed_with([3, 1]), 0)
        self.assertEqual(self.events("blind_red_checked")[0]["outcomes"],
                         {f"{self.card}.test_greets": "passes-on-base"})

    def test_the_boards_list_without_a_ruling_exempts_nothing(self):
        self.make_round(PASSING_SOURCE, mutate=exempting(self.card, [f"{self.card}.test_greets"]))
        self.step()
        self.assertEqual(self.events("blind_landed"), [])

    def test_the_method_name_in_the_list_is_not_the_full_id(self):
        self.make_round(PASSING_SOURCE, mutate=exempting(self.card, ["test_greets"]))
        self.rule()
        self.step()
        self.assertEqual(self.events("blind_landed"), [])

    def test_a_malformed_list_is_a_lint_problem_and_a_bounce(self):
        self.make_round(PASSING_SOURCE, mutate=exempting(self.card, "test_greets"))
        self.step()
        got = self.events("blind_red_checked")[0]
        self.assertEqual(got["verdict"], "bounced")
        self.assertIn("red_exempt is not a list of test ids", got["problems"][0])
        self.assertEqual((self.box.started, self.events("blind_landed")), ([], []))

    def test_a_list_with_a_non_string_is_one_too(self):
        self.make_round(PASSING_SOURCE, mutate=exempting(self.card, [1, ""]))
        self.step()
        got = self.events("blind_red_checked")[0]
        self.assertIn("red_exempt is not a list", got["problems"][0])


class ConfigKey(unittest.TestCase):
    def test_red_exempt_is_not_a_board_setting(self):
        with self.assertRaises(ValueError) as got:
            config.validate_blind_tests({"repo": "/r", "adapter": "unittest",
                                         "red_exempt": ["t_1.test_a"]})
        self.assertIn("red_exempt: unknown key", str(got.exception))


class Triage(RedCase):
    def two_rounds(self):
        for _ in range(2):
            self.make_round(PASSING_SOURCE)
            self.step()

    def test_a_blocked_card_keeps_its_block_and_is_triaged_when_it_is_ready(self):
        self.conn.execute("UPDATE tasks SET status='blocked', block_kind='spec_stale' WHERE id=?",
                          (self.card,))
        self.two_rounds()
        self.assertEqual(self.events("blind_red_checked")[-1]["verdict"], "triage_pending")
        row = self.card_row()
        self.assertEqual((row["status"], row["block_kind"]), ("blocked", "spec_stale"))
        self.assertEqual(self.events("block_loop_detected"), [])
        self.step()
        self.assertEqual(self.card_row()["status"], "blocked")           # still waiting
        self.conn.execute("UPDATE tasks SET status='review' WHERE id=?", (self.card,))
        self.step()
        self.assertEqual(self.card_row()["status"], "triage")
        self.assertEqual(self.events("blind_red_checked")[-1]["verdict"], "triaged")

    def test_a_card_that_is_finished_meanwhile_needs_no_triage(self):
        self.conn.execute("UPDATE tasks SET status='running' WHERE id=?", (self.card,))
        self.two_rounds()
        self.conn.execute("UPDATE tasks SET status='done' WHERE id=?", (self.card,))
        self.step()
        self.assertEqual(self.events("blind_red_checked")[-1]["verdict"], "stale")
        self.assertEqual(settle.waiting(self.conn), [])
        self.assertEqual(self.events("block_loop_detected"), [])

    def pending(self):
        self.conn.execute("UPDATE tasks SET status='running' WHERE id=?", (self.card,))
        self.two_rounds()
        self.assertEqual(self.events("blind_red_checked")[-1]["verdict"], "triage_pending")

    def test_a_pending_round_for_a_replaced_acceptance_text_triages_nothing(self):
        self.pending()
        self.conn.execute("UPDATE tasks SET body=?, status='review' WHERE id=?",
                          (BODY + "- a third thing\n", self.card))
        self.step()
        self.assertEqual(self.events("blind_red_checked")[-1]["verdict"], "stale")
        self.assertEqual((self.card_row()["status"], self.events("block_loop_detected")),
                         ("review", []))
        self.assertEqual(settle.waiting(self.conn), [])

    def test_a_guides_reset_leaves_the_round_pending(self):
        self.pending()
        db.event(self.conn, self.card, "failure_reset", {"by": "guide"}, now=5)
        self.conn.execute("UPDATE tasks SET status='review' WHERE id=?", (self.card,))
        self.step()
        self.assertEqual(self.events("blind_red_checked")[-1]["verdict"], "triaged")
        self.assertEqual(self.card_row()["status"], "triage")

    def test_a_guides_reset_does_not_drop_a_round_that_still_waits(self):
        self.pending()
        db.event(self.conn, self.card, "failure_reset", {"by": "guide"}, now=5)
        self.step()                                       # the card is still running
        self.assertEqual(self.events("blind_red_checked")[-1]["verdict"], "triage_pending")
        self.assertEqual(len(settle.waiting(self.conn)), 1)

    def test_a_pending_round_is_dropped_by_a_human_reset_not_triaged_after_it(self):
        self.pending()
        db.event(self.conn, self.card, "failure_reset", {"by": "human"}, now=5)
        self.conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (self.card,))
        self.step()
        self.assertEqual(self.events("blind_red_checked")[-1]["verdict"], "stale")
        self.assertEqual((self.card_row()["status"], self.events("block_loop_detected")),
                         ("ready", []))


class Retries(RedCase):
    def forge(self):
        return RedBox(tamper=lambda text: text.replace("failures=1", "failures=0"))

    def test_any_third_runner_failure_opens_an_incident(self):
        self.make_round()
        for _ in range(3):
            self.step(RedBox(boom=RuntimeError("docker is gone")))
        found = incidents.listing(self.conn)
        self.assertEqual([(i["kind"], i["task_id"]) for i in found],
                         [("review_infra", self.card)])
        self.assertNotIn("docker is gone", found[0]["evidence"])     # no run text (D161, S25)

    def test_forged_results_open_an_incident_on_the_third_failure_only(self):
        self.make_round()
        for _ in range(2):
            self.step(self.forge())
        self.assertEqual(self.conn.execute("SELECT count(*) FROM incidents").fetchone()[0], 0)
        self.step(self.forge())
        found = incidents.listing(self.conn)
        self.assertEqual([(i["kind"], i["role"]) for i in found],
                         [("review_infra", f"blind_red:{self.card}")])
        self.assertEqual(self.card_row()["status"], "triage")
        self.assertEqual(self.events("blind_landed"), [])

    def test_a_failing_triage_cannot_roll_back_the_count(self):
        self.make_round()
        boom = RedBox(boom=RuntimeError("docker is gone"))
        self.step(boom)
        self.step(boom)
        with mock.patch.object(settle, "finish", side_effect=RuntimeError("db is locked")):
            with self.assertRaises(RuntimeError):
                self.step(boom)
        self.assertEqual([e["attempt"] for e in self.events("blind_red_retry")], [1, 2, 3])
        self.step(boom)                                   # the next pass triages, no fourth run
        self.assertEqual(self.card_row()["status"], "triage")


class BoundedStart(unittest.TestCase):
    class Fake:
        def __init__(self):
            self.calls, self.sleeps = [], []
            self._run = lambda cmd, **kw: self.calls.append(kw.get("timeout"))
            self._sleep = self.sleeps.append

    def test_long_calls_and_sleeps_get_the_time_left_and_the_hooks_come_back(self):
        fake = self.Fake()
        original = (fake._run, fake._sleep)
        with red._bounded(fake, time.monotonic() + 20):
            fake._run(["docker", "run"], timeout=300)
            fake._run(["docker", "exec"], timeout=30)
            fake._sleep(45)
        self.assertTrue(0 < fake.calls[0] <= 20, fake.calls)
        self.assertEqual(fake.calls[1], 30)               # a quick call is not touched
        self.assertTrue(fake.sleeps[0] <= 20, fake.sleeps)
        self.assertEqual((fake._run, fake._sleep), original)

    def test_a_start_after_the_deadline_times_out_at_once(self):
        fake = self.Fake()
        with red._bounded(fake, time.monotonic() - 1):
            with self.assertRaises(subprocess.TimeoutExpired):
                fake._run(["docker", "run"], timeout=300)
        self.assertEqual(fake.calls, [])

    def test_a_runner_without_the_hooks_is_left_alone(self):
        box = Container(None)
        with red._bounded(box, time.monotonic() + 1):
            self.assertFalse(hasattr(box, "_run"))


class Wording(Dirs):
    def test_the_lint_lets_a_plain_file_not_stand_for_a_package(self):
        files, found = red.lint.collect(self.clone({
            folder("test_it.py"): GOOD + "import capsule.tick\n", folder("capsule"): "x"}), CARD)
        problems = found + red.lint.problems(files, CARD, "h" * 64)
        self.assertTrue(any("imports capsule" in p for p in problems), problems)

    def test_builtins_and_a_dunder_import_attribute_are_refused_but_re_compile_is_not(self):
        def problems(extra):
            clone = self.clone({folder("test_it.py"): GOOD + extra})
            files, found = red.lint.collect(clone, CARD)
            return found + red.lint.problems(files, CARD, "h" * 64)
        self.assertTrue(any("builtins" in p for p in problems("import builtins\n")))
        self.assertTrue(any("__import__" in p for p in problems("x.__import__('os')\n")))
        self.assertEqual(problems("import re\nre.compile('a')\ncur.eval()\ncur.exec()\n"), [])

    def test_the_testers_dispatched_system_prompt_carries_the_id_rule(self):
        card = {"id": CARD, "title": "t", "acceptance": ["a"]}
        system = bundles.render_blind_tester_brief(card, Path("/nonexistent"))["system"]
        self.assertIn("full id, `<card id>.<test method name>`", system)
        self.assertIn("(example: `t_5219f8cf.test_greets`)", system)
        self.assertIn("never by the bare method name", system)

    def test_the_brief_describes_the_outcomes_as_they_are_decided(self):
        card = {"id": CARD, "title": "t", "acceptance": ["a"],
                "red_check": {"round": 1, "tests": {f"{CARD}.test_a": "inconclusive"},
                              "problems": []}}
        prompt = bundles.render_blind_tester_brief(card, Path("/nonexistent"))["prompt"]
        self.assertIn("exit 124, 126 or 127", prompt)
        self.assertIn("broke after a command ran", prompt)

    def test_expected_failures_do_not_raise_the_count(self):
        rows = "".join(json.dumps({"id": f"t.{i}", "result": "fail"}) + "\n" for i in range(2))
        out = "Ran 2 tests in 0.1s\n\nFAILED (failures=1, expected failures=3)\n"
        self.assertIn("more fail and error rows", blind_run._parse(rows, out, 1)[1])


class DuplicateNames(Dirs):
    def problems(self, files):
        clone = self.clone({folder(rel): body for rel, body in files.items()})
        files, found = red.lint.collect(clone, CARD)
        return found + red.lint.problems(files, CARD, "h" * 64)

    def test_a_test_name_defined_twice_is_refused_with_its_name(self):
        twice = GOOD + "\n\nclass U(unittest.TestCase):\n    def test_a(self):\n        run('x')\n"
        found = self.problems({"test_it.py": twice})
        self.assertEqual(len(found), 1)
        self.assertIn("test test_a is defined 2 times (test_it.py)", found[0])
        self.assertIn("unique within a card", found[0])

    def test_a_load_tests_hidden_twin_in_another_file_is_refused(self):
        hidden = ("import unittest\n\nfrom harness import run\n\n\nclass H(unittest.TestCase):\n"
                  "    def test_a(self):\n        run('y')\n\n\n"
                  "def load_tests(loader, tests, pattern):\n    return unittest.TestSuite()\n")
        found = self.problems({"test_hidden.py": hidden})
        self.assertEqual(len(found), 1)
        self.assertIn("test test_a is defined 2 times (test_hidden.py, test_it.py)", found[0])

    def test_distinct_names_and_non_test_methods_pass(self):
        more = (GOOD + "\n\nclass U(unittest.TestCase):\n    def test_b(self):\n"
                "        run('x')\n    def helper(self):\n        pass\n\n"
                "    def test_a_variant(self):\n        run('z')\n")
        self.assertEqual(self.problems({"test_it.py": more}), [])

    def test_the_contract_says_names_are_unique_within_a_card(self):
        card = {"id": CARD, "title": "t", "acceptance": ["a"]}
        system = bundles.render_blind_tester_brief(card, Path("/nonexistent"))["system"]
        self.assertIn("Test names are unique within a card", system)


class Protected(unittest.TestCase):
    def test_run_and_preflight_are_protected_too(self):
        names = ["capsule/blind/run.py", "capsule/blind/preflight.py"]
        self.assertEqual(workspace.touches_protected(names), names)


if __name__ == "__main__":
    unittest.main()
