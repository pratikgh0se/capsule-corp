"""R31 senior attack review: one test (or a few) per way a hostile tester, or a product under test,
could get a blind test landed that should not, or could wedge the tick. Same fixtures as
test_blind_red_tick.py (temp repos and the fake strong runner; no docker, no model).

Z1 intent examples: a tester that keeps writing until the host reads, a test that never touches
the product, a command that is not there, a `load_tests` that hides a test, rows forged at exit,
a disagreeing base, a file nobody can read, a slow clone, a running implementer, an unprotected
judge file: each must end in a bounce or a wait, never in a landing."""
import io
import json
import os
import subprocess
import time
import unittest
from pathlib import Path
from unittest import mock

from capsule import workspace
from capsule.blind import checkout, lint, red, settle
from capsule.blind import run as blind_run
from capsule.tests.test_blind_red import CARD, GOOD, Dirs
from capsule.tests.test_blind_red_tick import RedCase, source


def folder(rel):
    return f"cards/{CARD}/{rel}"


class SwappedFiles(Dirs):
    """Item 1: a tester container that is still alive can swap a file for a link."""

    def test_a_file_is_opened_without_following_a_link_even_after_the_check(self):
        clone = self.clone()
        target = self.root / "host-secret"
        target.write_text("HOST KEY")
        link = clone / folder("test_it.py")
        link.unlink()
        os.symlink(target, link)
        real = os.lstat
        with mock.patch.object(lint.os, "lstat", side_effect=lambda p, **kw: (
                os.stat(p) if str(p).endswith("test_it.py") else real(p, **kw))):
            files, problems = lint.collect(clone, CARD)       # lstat says "regular file"
        self.assertNotIn(folder("test_it.py"), files)
        self.assertNotIn(b"HOST KEY", b"".join(files.values()))
        self.assertEqual(problems, [f"{folder('test_it.py')}: cannot be read (a link, special"
                                    " file or no permission)"])

    def test_a_file_that_cannot_be_read_is_a_problem_and_does_not_raise(self):
        clone = self.clone()
        os.chmod(clone / folder("test_it.py"), 0)
        self.addCleanup(os.chmod, clone / folder("test_it.py"), 0o600)
        files, problems = lint.collect(clone, CARD)
        self.assertIn(f"{folder('test_it.py')}: cannot be read (a link, special file or no"
                      " permission)", problems)
        self.assertNotIn(folder("test_it.py"), files)


class TickAttacks(RedCase):
    def bounced(self):
        got, = self.events("blind_red_checked")
        return got

    def test_the_tester_container_is_stopped_before_its_files_are_read(self):
        order = []
        real = lint.collect
        self.box.stop = lambda name: order.append(("stop", name))
        self.make_round()
        with mock.patch.object(lint, "collect",
                               side_effect=lambda *a: order.append(("collect",)) or real(*a)):
            self.step()
        self.assertEqual(order[:2], [("stop", "capsule-000000000000"), ("collect",)])

    def test_a_file_nobody_can_read_bounces_with_a_reason_and_is_no_tick_error(self):
        def lock(clone):
            notes = clone / "cards" / self.card / "notes.txt"
            notes.write_text("x")
            os.chmod(notes, 0)
        self.make_round(mutate=lock)
        self.step()
        self.assertIn("notes.txt: cannot be read", self.bounced()["problems"][0])
        self.assertEqual((self.events("blind_red_retry"), self.main()), ([], self.main0))

    def test_a_test_that_fails_without_calling_the_product_does_not_land(self):
        self.make_round(source('self.fail("always")'))
        self.step()
        self.assertEqual(self.bounced()["outcomes"],
                         {f"{self.card}.test_greets": "errors-before-first-call"})
        self.assertEqual(self.main(), self.main0)

    def test_a_call_then_a_crash_is_inconclusive_not_red(self):
        self.make_round(source('run("true")\n        raise ValueError("boom")'))
        self.step()
        self.assertEqual(self.bounced()["outcomes"], {f"{self.card}.test_greets": "inconclusive"})
        self.assertEqual(self.main(), self.main0)

    def test_a_command_that_is_not_there_is_inconclusive_not_red(self):
        self.make_round(source('self.assertEqual(run("no_such_command_zz9").stdout, "x")'))
        self.step()
        self.assertEqual(self.bounced()["outcomes"], {f"{self.card}.test_greets": "inconclusive"})
        self.assertEqual(self.main(), self.main0)

    def test_a_test_named_in_an_example_or_the_report_that_never_ran_bounces(self):
        self.make_round(names=("test_greets", "test_hidden"))
        self.step()
        problems = self.bounced()["problems"]
        self.assertEqual(len(problems), 1)
        self.assertIn(f"test {self.card}.test_hidden is named in", problems[0])
        self.assertEqual((self.events("blind_landed"), self.main()), ([], self.main0))

    def test_a_disagreeing_base_is_inconclusive_with_an_incident_and_no_container(self):
        self.make_round()
        self.conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, started_at, run_id, metadata)"
            " VALUES (?,?,?,?,?,?)", (self.card, "implementer", "done", 0, "f" * 32,
                                      json.dumps({"start_sha": "e" * 40})))
        self.step()
        got = self.bounced()
        self.assertEqual(got["verdict"], "inconclusive")
        self.assertIn("differ", got["reason"])
        self.assertEqual((self.box.started, self.main()), ([], self.main0))
        row = self.conn.execute("SELECT kind, task_id, status FROM incidents").fetchone()
        self.assertEqual(tuple(row), ("review_infra", self.card, "open"))
        self.assertEqual(settle.rounds(self.conn, self.card, self.hash), 0)

    def test_an_agreeing_base_is_no_incident(self):
        self.make_round()
        self.conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, started_at, run_id, metadata)"
            " VALUES (?,?,?,?,?,?)", (self.card, "implementer", "done", 0, "f" * 32,
                                      json.dumps({"start_sha": self.base})))
        self.step()
        self.assertEqual(len(self.events("blind_landed")), 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM incidents").fetchone()[0], 0)

    def test_a_running_implementer_is_not_triaged_until_it_stops(self):
        self.conn.execute("UPDATE tasks SET status='running' WHERE id=?", (self.card,))
        self.make_round(PASSING_SOURCE)
        self.step()
        self.make_round(PASSING_SOURCE)
        self.step()
        self.assertEqual([e["verdict"] for e in self.events("blind_red_checked")],
                         ["bounced", "triage_pending"])
        self.assertEqual((self.card_row()["status"], self.events("block_loop_detected")),
                         ("running", []))
        self.step()                                               # still running: still waiting
        self.assertEqual(self.card_row()["status"], "running")
        self.conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (self.card,))
        self.step()
        self.assertEqual((self.card_row()["status"], self.card_row()["block_kind"]),
                         ("triage", "needs_input"))
        self.assertEqual([e["verdict"] for e in self.events("blind_red_checked")][-1], "triaged")
        self.assertEqual(len(self.events("block_loop_detected")), 1)
        self.step()                                                # triaged once, never again
        self.assertEqual(len(self.events("block_loop_detected")), 1)


PASSING_SOURCE = source('self.assertEqual(run("cat hello.txt").stdout, "hi\\n")')


class BoundedClone(RedCase):
    def test_the_clone_and_checkout_get_the_time_left_not_their_own_600_seconds(self):
        seen = []
        real, build = workspace._git, checkout.build
        with mock.patch.object(workspace, "_git", side_effect=lambda *a, **kw: (
                seen.append(kw.get("timeout")) or real(*a, **kw))), \
                mock.patch.object(checkout, "build", side_effect=lambda *a, **kw: (
                    seen.append(kw.get("timeout")) or build(*a, **kw))):
            ws = red._checkout(str(self.project), self.base, self.root / "r" / "base",
                               time.monotonic() + 30)
        self.assertEqual(ws, self.root / "r" / "base")          # the checkout is at the path given
        self.assertTrue((ws / "hello.txt").exists() and (ws / ".git").exists())
        self.assertEqual(len(seen), 2)                          # the clone and the checkout
        self.assertTrue(all(0 < t <= 30 for t in seen), seen)

    def test_a_spent_budget_gives_the_clone_one_second_at_most_and_the_card_a_retry(self):
        seen = []
        with mock.patch.object(workspace, "_git", side_effect=lambda *a, **kw: (
                seen.append(kw.get("timeout")) or (_ for _ in ()).throw(TimeoutError("slow")))):
            self.make_round()
            original = red.RED_BUDGET
            self.addCleanup(setattr, red, "RED_BUDGET", original)
            red.RED_BUDGET = 2
            self.step()
        self.assertTrue(all(t <= 2 for t in seen), seen)
        self.assertEqual(len(self.events("blind_red_retry")), 1)
        self.assertEqual(self.events("blind_landed"), [])

    def test_a_docker_failure_of_the_red_check_is_stored_without_the_mount_source(self):
        argv = ["docker", "run", "-d", "-v", "/srv/BLINDLEAK/clone:/blind:ro", "img"]
        with mock.patch.object(workspace, "_git",
                               side_effect=subprocess.CalledProcessError(125, argv)):
            self.make_round()
            with mock.patch("sys.stderr", new_callable=io.StringIO) as log:
                self.step()
        self.assertEqual(log.getvalue().count("/srv/BLINDLEAK/clone:/blind:ro"), 1)   # the log
        (retry,) = self.events("blind_red_retry")
        self.assertIn("<masked>:/blind:ro", retry["reason"])
        self.assertNotIn("BLINDLEAK", retry["reason"])


class Forgery(unittest.TestCase):
    def rows(self, *results):
        return "".join(json.dumps({"id": f"t.{i}", "result": r}) + "\n"
                       for i, r in enumerate(results))

    def parse(self, rows, status, code, ran):
        return blind_run._parse(rows, f"Ran {ran} tests in 0.1s\n\n{status}\n", code)[1]

    def test_more_fail_rows_than_the_summary_counts_is_refused(self):
        forged = self.rows("fail", "fail")
        self.assertIn("more fail and error rows", self.parse(forged, "FAILED (failures=1)", 1, 2))
        self.assertIsNone(self.parse(forged, "FAILED (failures=2)", 1, 2))

    def test_error_rows_and_unexpected_successes_count_toward_the_summary(self):
        self.assertIsNone(self.parse(self.rows("error", "fail"), "FAILED (failures=1, errors=1)",
                                     1, 2))
        self.assertIsNone(self.parse(self.rows("fail", "fail"),
                                     "FAILED (failures=1, unexpected successes=1)", 1, 2))
        self.assertIn("more fail", self.parse(self.rows("error", "error"),
                                             "FAILED (errors=1)", 1, 2))


class ClassifyLost(unittest.TestCase):
    def test_exit_codes_that_mean_nothing_ran_are_never_red(self):
        for code, want in ((1, "ok"), (2, "ok"), (124, "inconclusive"), (126, "inconclusive"),
                           (127, "inconclusive")):
            row = {"result": "fail", "observed": {"cmd": "x", "exit": code}}
            self.assertEqual(red.classify([{"t.a": row}] * 5), {"t.a": want}, code)

    def test_a_fail_with_no_observation_and_an_error_after_a_call(self):
        self.assertEqual(red.classify([{"t.a": {"result": "fail"}}] * 5),
                         {"t.a": "errors-before-first-call"})
        seen = {"result": "error", "observed": {"cmd": "x", "exit": 1}}
        self.assertEqual(red.classify([{"t.a": seen}] * 5), {"t.a": "inconclusive"})


class LintAttacks(Dirs):
    def problems(self, body, extra=None):
        files, found = lint.collect(self.clone({folder("test_it.py"): body, **(extra or {})}),
                                    CARD)
        return found + lint.problems(files, CARD, "h" * 64)

    def test_every_way_of_loading_code_by_name_is_refused(self):
        for line, word in (("exec('x = 1')", "exec"), ("eval('1')", "eval"),
                           ("compile('1', 'f', 'eval')", "compile"),
                           ("__import__('os')", "__import__"),
                           ("import builtins\nbuiltins.__import__('os')", "__import__"),
                           ("import importlib", "importlib"),
                           ("from importlib import import_module", "importlib"),
                           ("import runpy", "runpy"), ("import pkgutil", "pkgutil"),
                           ("import imp", "imp")):
            found = self.problems(GOOD + line + "\n")
            self.assertTrue(any(word in f and "run time" in f for f in found), (line, found))

    def test_a_file_named_after_a_project_package_or_the_stdlib_is_refused(self):
        for name in ("capsule.py", "src/x.py", "harness.py", "json.py", "bin/x.py", "tests/x.py"):
            found = self.problems(GOOD, {folder(name): "x = 1\n"})
            self.assertTrue(any("in its place" in f for f in found), (name, found))

    def test_ordinary_files_pass(self):
        files = {folder("helper.py"): "x = 1\n", folder("data/sample.txt"): "x"}
        self.assertEqual(self.problems(GOOD + "import json\n", files), [])


class ProtectedJudges(unittest.TestCase):
    def test_the_red_check_and_the_lint_are_protected_paths(self):
        mine = ["capsule/blind/red.py", "capsule/blind/lint.py", "capsule/blind/settle.py"]
        self.assertEqual(workspace.touches_protected(mine), mine)
        self.assertEqual(workspace.touches_protected(["capsule/blind/dispatch.py"]), [])


if __name__ == "__main__":
    unittest.main()
