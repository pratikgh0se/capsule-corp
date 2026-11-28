"""R31: the red check and landing of blind tests (`capsule/blind/red.py`), driven through
`red.step` with real git repos in a temp dir, a real blind repo (`capsule blind init`'s creator),
the real harness runner and a stand-in for the strong runner that runs the run line on the host
(`test_blind_run.Container`). No docker, no model.

Z2 oracles: the container's results (results.jsonl and the summary line, as in the blind run) and,
for landing, the blind repo's `main` sha read with git against the card's `blind_commit` and
the `blind_landed` event (three data paths). Z1: the section 9 `red_check` rows."""
import json
import tempfile
import unittest
from pathlib import Path

from capsule import blocks, bundles, db, paths, workspace
from capsule.blind import dispatch, launch, red, settle
from capsule.tests import tmpclean
from capsule.tests.test_blind_red import exempting
from capsule.tests.test_blind_run import BOARD, Container, blind_repo, examples, make_ws, sh

BODY = "Goal.\n\nAcceptance:\n- the first thing works\n- the second thing works\n"


def source(body):
    return ("import unittest\n\nfrom harness import run\n\n\nclass Greeting(unittest.TestCase):\n"
            "    def test_greets(self):\n        " + body + "\n")


FAILING = source('self.assertEqual(run("cat hello.txt").stdout, "hello\\n")')
PASSING = source('self.assertEqual(run("cat hello.txt").stdout, "hi\\n")')
EARLY = source("raise ValueError")


class RedBox(Container):
    """The strong runner's stand-in for a red check: its workspace is whatever `start` is given."""

    def __init__(self, **kw):
        super().__init__(None, **kw)
        self.execs = 0

    def start(self, run_id, ws, **kw):
        self.ws = Path(ws)
        return super().start(run_id, ws, **kw)

    def exec(self, name, cmd, timeout):
        self.execs += 1
        self.timeouts = getattr(self, "timeouts", []) + [timeout]
        return super().exec(name, cmd, timeout)


class StubTick:
    host = "testhost"

    def __init__(self, root, strong):
        self.home, self.strong, self.root = Path(root) / "home", strong, Path(root)

    def _spool_root(self, cfg):
        return self.root / "spool"

    def _guarded(self, conn, cfg, task_id, now, counts, work, run_id=None):
        work()
        return True


class RedCase(unittest.TestCase):
    """A card with a tester round waiting for its red check."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(tmpclean.sweep, self.root)
        db.create_board(self.root / "kanban.db")
        self.conn = db.connect(self.root / "kanban.db")
        self.addCleanup(self.conn.close)
        self.project = make_ws(self.root)
        self.base = workspace.head(self.project)
        self.card = db.new_task(self.conn, title="card", body=BODY, assignee="dev", branch="main")
        self.hash = bundles.card_hash(BODY)
        self.repo = blind_repo(self.root, self.card, FAILING)       # a repo to land into ...
        sh("git", "-C", str(self.repo), "rm", "-rq", f"cards/{self.card}")   # ... with no tests yet
        sh("git", "-C", str(self.repo), *workspace.IDENTITY, "commit", "-q", "-m", "no tests yet")
        self.main0 = sh("git", "-C", str(self.repo), "rev-parse", "main").strip()
        self.cfg = {"board": BOARD, "redesign": {"blind_tests": "report"},
                    "repo_url": str(self.project),
                    "blind_tests": {"repo": str(self.repo), "adapter": "unittest"}}
        self.box = RedBox()
        self.tick = StubTick(self.root, self.box)
        self.n = 0

    def make_round(self, source=FAILING, names=("test_greets",), card_hash=None, mutate=None,
                     covered=None, seed=None, start_sha=True):
        """A finished tester run whose clone holds `source` as its test, with a valid report."""
        self.n += 1
        run_id = f"{self.n:032x}"
        clone = self.root / "work" / run_id
        launch._blind_clone(self.repo, clone, self.card, seed)
        folder = clone / "cards" / self.card
        (folder / "test_it.py").write_text(source)
        (folder / "examples.jsonl").write_text(examples(self.card, names))
        (folder / "HEADER.json").write_text(json.dumps({
            "card": self.card, "card_hash": card_hash or self.hash, "statements": [1, 2]}))
        if mutate:
            mutate(clone)
        listed = [f"{self.card}.{n}" for n in names]
        doc = {"covered": covered or [{"statement": 1, "tests": listed},
                                      {"statement": 2, "tests": [f"{self.card}.{names[0]}"]}],
               "not_testable": []}
        meta = {"card_hash": card_hash or self.hash, "blind_clone": str(clone),
                **({"start_sha": self.base} if start_sha else {})}
        self.conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at,"
            " run_id, handoff, metadata) VALUES (?,?,?,?,?,?,?,?,?)",
            (self.card, "blind-tester", "done", "completed", 1, 2, run_id, json.dumps(doc),
             json.dumps(meta)))
        return clone

    def step(self, box=None):
        if box:
            self.box = self.tick.strong = box
        red.step(self.tick, self.conn, self.cfg, 100, {})

    def events(self, kind):
        return [json.loads(r["payload"]) for r in self.conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id",
            (self.card, kind))]

    def card_row(self):
        return self.conn.execute("SELECT * FROM tasks WHERE id=?", (self.card,)).fetchone()

    def main(self):
        return sh("git", "-C", str(self.repo), "rev-parse", "main").strip()


class Landing(RedCase):
    def test_a_test_that_fails_every_time_on_the_base_lands_on_main(self):
        clone = self.make_round()
        self.step()
        landed = self.events("blind_landed")[0]
        self.assertEqual(self.box.execs, red.RUNS)                         # 5 runs
        self.assertEqual(self.card_row()["blind_commit"], self.main())    # card, git, event
        self.assertEqual(landed["blind_commit"], self.main())
        parent = sh("git", "-C", str(self.repo), "rev-parse", f"{self.main()}^").strip()
        self.assertEqual(parent, self.main0)
        self.assertEqual((landed["tests"], landed["start_sha"], landed["card_hash"]),
                         ([f"{self.card}.test_greets"], self.base, self.hash))
        self.assertEqual(landed["acceptance"], ["the first thing works", "the second thing works"])
        files = sh("git", "-C", str(self.repo), "ls-tree", "-r", "--name-only", "main").split()
        self.assertEqual([f for f in files if "cards/t_" in f], [
            f"cards/{self.card}/HEADER.json", f"cards/{self.card}/examples.jsonl",
            f"cards/{self.card}/test_it.py"])                              # the folder, no harness
        self.assertEqual(self.events("blind_red_checked")[0]["verdict"], "landed")
        self.assertFalse(clone.exists())
        self.assertEqual(self.card_row()["status"], "ready")
        self.assertEqual(sh("git", "-C", str(self.repo), "status", "--porcelain"), "")

    def test_landing_sends_a_card_held_for_its_blind_tests_back_to_review(self):
        self.conn.execute("UPDATE tasks SET status='review' WHERE id=?", (self.card,))
        self.conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, started_at, run_id, handoff)"
            " VALUES (?, 'implementer', 'done', 1, 'impl1', ?)",
            (self.card, json.dumps({"status": "done"})))
        blocks.block(self.conn, self.card, kind="needs_input", reason="no blind tests yet",
                     cause="blind_gate:abc:no blind tests yet", offer="accept|reset", now=50)
        self.assertEqual(self.card_row()["status"], "blocked")
        self.make_round()
        self.step()
        self.assertEqual((self.card_row()["status"], self.card_row()["block_recurrences"]),
                         ("review", 0))
        self.assertEqual(self.events("blind_hold_released"), [{"reason": "no blind tests yet"}])
        self.assertEqual([e["by"] for e in self.events("request_review")], ["accept"])
        self.assertEqual(self.events("blind_landed")[0]["blind_commit"], self.main())

    def hold_for_tests(self, status="review"):
        self.cfg["redesign"]["blind_tests"] = "gate"
        self.conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, self.card))
        self.conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, started_at, run_id, handoff)"
            " VALUES (?, 'implementer', 'done', 1, 'impl1', ?)",
            (self.card, json.dumps({"status": "done"})))
        blocks.block(self.conn, self.card, kind="needs_input", reason="no blind tests yet",
                     cause="blind_gate:abc:no blind tests yet", offer="accept|reset", now=50)

    def test_in_gate_mode_landing_releases_a_card_held_for_no_blind_tests(self):
        self.hold_for_tests()
        self.make_round()
        self.step()
        self.assertEqual(self.card_row()["status"], "review")
        self.assertEqual(len(self.events("blind_hold_released")), 1)

    def test_landing_releases_only_a_card_still_held_by_the_hold(self):
        self.hold_for_tests()
        self.conn.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES"
                          " (?, 'failure_limit', '{}', 60)", (self.card,))
        self.make_round()
        self.step()
        self.assertEqual(self.card_row()["status"], "blocked")
        self.assertEqual(self.events("blind_hold_released"), [])

    def test_landing_leaves_a_card_held_for_anything_else_held(self):
        blocks.block(self.conn, self.card, kind="needs_input", reason="a question", now=50)
        self.make_round()
        self.step()
        self.assertEqual(self.card_row()["status"], "blocked")
        self.assertEqual(self.events("blind_hold_released"), [])

    def test_the_red_check_runs_on_the_base_not_the_branch_head_in_a_sealed_container(self):
        (self.project / "hello.txt").write_text("hello\n")        # the head would make it pass
        sh("git", "-C", str(self.project), "add", "-A")
        sh("git", "-C", str(self.project), *workspace.IDENTITY, "commit", "-q", "-m", "moved on")
        self.make_round()
        self.step()
        self.assertEqual(len(self.box.started), red.RUNS)           # a container for each run
        for run_id, ws, kw in self.box.started:
            self.assertEqual((kw["ro"], [m.rsplit(":/", 1)[1] for m in kw["mounts"]]),
                             (True, ["blind:ro", "out"]))
        self.assertEqual(self.box.stopped, ["capsule-000000000000"]
                         + ["capsule-" + s[0][:12] for s in self.box.started])
        self.assertEqual((len(self.events("blind_landed")), Path(ws).exists()), (1, False))
        self.assertFalse(list((self.root / "spool").glob("*/blind")))      # snapshots are removed

    def test_the_commit_that_was_checked_lands_not_the_branch_tip_of_the_moment(self):
        def move_branch(ws):
            sh("git", "-C", str(self.repo), "update-ref", f"refs/heads/card/{self.card}",
               self.main0)
        self.make_round()
        self.step(RedBox(after=move_branch))
        landed = self.events("blind_landed")[0]["blind_commit"]
        self.assertNotEqual(landed, self.main0)
        self.assertEqual(self.main(), landed)

    def test_an_exempt_statement_may_pass_on_base_and_still_lands(self):
        self.make_round(PASSING, mutate=exempting(self.card, [f"{self.card}.test_greets"]))
        ruling = {"role": "blind-tester", "choice": "red-exempt", "applied": True,
                  "reason": "a characterisation test for statement 1 and statement 2",
                  "card_hash": self.hash}
        db.event(self.conn, self.card, "guide_ruling", ruling, now=1)
        self.step()
        self.assertEqual(self.events("blind_landed")[0]["exempt"], [1, 2])
        self.assertEqual(red._exempt_statements(self.conn, self.card), {1, 2})
        self.conn.execute("UPDATE tasks SET body=body || '- a third thing works\n' WHERE id=?",
                          (self.card,))              # the ruling held for the text it was asked on
        self.assertEqual(red._exempt_statements(self.conn, self.card), set())

    def test_without_a_ruling_naming_the_statement_it_does_not(self):
        self.make_round(PASSING)
        db.event(self.conn, self.card, "guide_ruling", {
            "role": "blind-tester", "choice": "red-exempt", "applied": True,
            "reason": "fine", "card_hash": self.hash}, now=1)
        db.event(self.conn, self.card, "guide_ruling", {
            "role": "guide", "choice": "red-exempt", "applied": True,
            "reason": "statement 1", "card_hash": self.hash}, now=1)
        self.step()
        self.assertEqual(self.events("blind_landed"), [])
        self.assertEqual(self.events("blind_red_checked")[0]["outcomes"],
                         {f"{self.card}.test_greets": "passes-on-base"})

    def test_off_changes_nothing(self):
        self.make_round()
        self.cfg["redesign"]["blind_tests"] = "off"
        self.step()
        self.assertEqual((self.box.execs, self.events("blind_red_checked"), self.main()),
                         (0, [], self.main0))


class Bounces(RedCase):
    def bounced(self):
        got, = self.events("blind_red_checked")
        return got

    def bounced_all(self):
        return self.events("blind_red_checked")

    def test_a_test_that_passes_on_the_base_is_bounced_by_name_and_nothing_lands(self):
        self.make_round(PASSING)
        self.step()
        got = self.bounced()
        self.assertEqual((got["verdict"], got["round"], got["outcomes"]),
                         ("bounced", 1, {f"{self.card}.test_greets": "passes-on-base"}))
        self.assertEqual((self.main(), self.card_row()["blind_commit"]), (self.main0, None))
        self.assertEqual(self.card_row()["status"], "ready")

    def test_an_error_before_the_first_call_is_its_own_outcome(self):
        self.make_round(EARLY)
        self.step()
        self.assertEqual(self.bounced()["outcomes"],
                         {f"{self.card}.test_greets": "errors-before-first-call"})

    def test_mixed_results_on_the_base_are_inconclusive(self):
        calls = {"n": 0}

        def toggle(ws):
            calls["n"] += 1
            (Path(ws) / "hello.txt").write_text("hello\n" if calls["n"] % 2 else "hi\n")
        self.make_round()
        self.step(RedBox(after=toggle))
        self.assertEqual(self.bounced()["outcomes"], {f"{self.card}.test_greets": "inconclusive"})

    def test_a_test_without_an_example_row_is_named_and_does_not_land(self):
        self.make_round(names=("test_other",))
        self.step()
        problems = self.bounced()["problems"]
        self.assertEqual(problems[0], f"no example row for test {self.card}.test_greets")
        self.assertIn(f"{self.card}.test_other is named in the examples", problems[1])
        self.assertEqual(self.main(), self.main0)

    def test_a_lint_failure_is_a_round_and_never_starts_a_container(self):
        self.make_round(mutate=lambda clone: (clone / "cards" / "t_other").mkdir(parents=True)
                          or (clone / "cards" / "t_other" / "x.py").write_text("x = 1\n"))
        self.step()
        self.assertEqual(self.bounced()["round"], 1)
        self.assertIn("outside cards/", self.bounced()["problems"][0])
        self.assertEqual((self.box.started, self.main()), ([], self.main0))

    def test_a_second_lint_failure_triages_with_a_count_never_a_test_name(self):
        self.make_round(names=("test_secret_rule_name",))
        self.step()
        self.make_round(names=("test_secret_rule_name",), seed=self.bounced()["tested"])
        self.step()
        self.assertIn("test_secret_rule_name", " ".join(self.bounced_all()[0]["problems"]))
        got = self.events("block_loop_detected")[0]["reason"]
        self.assertEqual(got, "could not write tests that pass the lint (2 problems)")
        logged = " ".join(r[0] for r in self.conn.execute("SELECT body FROM outbox"))
        self.assertNotIn("secret_rule", logged)
        self.assertNotIn("secret_rule", json.dumps(self.events("blind_red_checked")[-1]["reason"]))

    def test_what_goes_back_is_names_and_outcomes_never_product_output(self):
        self.make_round(PASSING)
        self.step()
        seed, extra, skip = dispatch._rerun_inputs(
            self.conn, self.card_row(), self.hash, ["a", "b"], 5)
        self.assertEqual(extra, {"red_check": {"round": 1, "problems": [], "tests": {
            f"{self.card}.test_greets": "passes-on-base"}}})
        self.assertEqual((seed, skip), (self.bounced()["tested"], None))
        self.assertNotIn("hi", json.dumps(self.bounced()))
        prompt = bundles.render_blind_tester_brief(
            {"id": self.card, "title": "t", "acceptance": ["a"], **extra}, self.root)["prompt"]
        self.assertIn(f"- {self.card}.test_greets: passes-on-base", prompt)

    def test_the_next_round_gets_a_new_tester_run_and_the_second_bounce_triages(self):
        self.assertFalse(dispatch._known(self.conn, self.card, self.hash))
        self.make_round(PASSING)
        self.step()
        self.assertFalse(dispatch._known(self.conn, self.card, self.hash))    # a new run is due
        clone = self.make_round(PASSING, seed=self.bounced()["tested"])
        self.assertTrue((clone / "cards" / self.card / "test_it.py").is_file())  # its own work back
        self.step()
        row = self.card_row()
        self.assertEqual((row["status"], row["block_kind"]), ("triage", "needs_input"))
        got = self.events("block_loop_detected")[0]
        self.assertEqual(got["reason"],
                         "could not write a failing test for statement 1 (also statements 2)")
        self.assertEqual(self.main(), self.main0)

    def test_a_round_for_an_old_card_hash_is_stale_and_not_a_round(self):
        self.make_round(card_hash="0" * 64)
        self.step()
        got, = self.events("blind_red_checked")
        rounds = settle.rounds(self.conn, self.card, self.hash)
        self.assertEqual((got["verdict"], self.box.execs, rounds),
                         ("stale", 0, 0))


class RunnerFailures(RedCase):
    def test_a_runner_failure_is_not_a_round_and_is_retried_on_a_later_pass(self):
        self.make_round()
        self.step(RedBox(boom=RuntimeError("docker is gone")))
        self.assertEqual(self.events("blind_red_checked"), [])
        self.assertEqual([e["attempt"] for e in self.events("blind_red_retry")], [1])
        self.step(RedBox())
        self.assertEqual(self.events("blind_landed")[0]["blind_commit"], self.main())

    def test_the_third_runner_failure_triages_with_a_fixed_phrase_not_the_runner_text(self):
        marker = "BLINDMARK_c41f given: x expect: y"      # a runner failure can quote blind tests
        self.make_round()
        for _ in range(3):
            self.step(RedBox(boom=RuntimeError(f"docker is gone {marker}")))
        row = self.card_row()
        self.assertEqual(row["status"], "triage")
        reason = self.events("block_loop_detected")[0]["reason"]
        self.assertIn("could not run 3 times", reason)
        self.assertNotIn("BLINDMARK", reason)
        sent = " ".join(r[0] for r in self.conn.execute("SELECT body FROM outbox"))
        self.assertNotIn("BLINDMARK", sent)                 # nor the outbox log Korin tails
        self.assertEqual(settle.rounds(self.conn, self.card, self.hash), 0)
        self.step(RedBox())                                              # settled: nothing more
        self.assertEqual(self.events("blind_landed"), [])

    def test_a_disagreeing_runner_is_a_runner_failure(self):
        self.make_round()
        self.step(RedBox(tamper=lambda text: text.replace("Ran 1 test", "Ran 4 tests")))
        self.assertEqual(len(self.events("blind_red_retry")), 1)
        self.assertEqual(self.events("blind_red_checked"), [])

    def test_a_card_gets_at_most_the_red_budget_of_the_pass(self):
        self.make_round()
        box = RedBox()
        original = red.RED_BUDGET
        self.addCleanup(setattr, red, "RED_BUDGET", original)
        red.RED_BUDGET = 0
        self.step(box)
        self.assertEqual((box.execs, len(self.events("blind_red_retry"))), (0, 1))
        red.RED_BUDGET = 300
        self.step(RedBox())
        self.assertTrue(all(t <= 300 for t in self.box.timeouts))

    def test_at_most_one_red_check_runs_a_pass(self):
        self.make_round()
        other = db.new_task(self.conn, title="o", body=BODY, assignee="dev", branch="main")
        self.conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, outcome, started_at, run_id,"
            " handoff, metadata) SELECT ?, profile, status, outcome, started_at, ?, handoff,"
            " metadata FROM task_runs", (other, "f" * 32))
        self.step()
        self.assertEqual(self.box.execs, red.RUNS)


class BaseSha(RedCase):
    def test_the_base_is_the_earliest_start_sha_of_any_profile(self):
        self.assertIsNone(red.base_sha(self.conn, self.card))
        for profile, sha in (("blind-tester", "a" * 40), ("implementer", "b" * 40)):
            self.conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, started_at, metadata)"
                " VALUES (?,?,?,?,?)",
                (self.card, profile, "done", 1, json.dumps({"start_sha": sha})))
        self.conn.execute("INSERT INTO task_runs (task_id, profile, status, started_at, metadata)"
                          " VALUES (?, 'guide', 'done', 1, '{}')", (self.card,))
        self.assertEqual(red.base_sha(self.conn, self.card), "a" * 40)

    def test_the_red_check_uses_it(self):
        self.make_round(start_sha=False)
        self.step()
        self.assertEqual(len(self.events("blind_red_retry")), 1)    # no base: a runner failure
        self.assertIn("no start_sha", self.events("blind_red_retry")[0]["reason"])


class StaleTests(RedCase):
    def landed(self):
        self.make_round()
        self.step()
        self.assertTrue(self.card_row()["blind_commit"])

    def test_a_changed_acceptance_text_marks_the_tests_stale_and_gives_only_its_diff(self):
        self.landed()
        new = BODY.replace("second thing works", "second thing really works")
        task = {"id": self.card, "body": new}
        seed, extra, skip = dispatch._rerun_inputs(self.conn, task, bundles.card_hash(new),
                                             dispatch.acceptance(new), 7)
        self.assertEqual((seed, skip), (self.card_row()["blind_commit"], None))
        self.assertEqual(list(extra), ["acceptance_diff"])
        self.assertIn("-2. the second thing works", extra["acceptance_diff"])
        self.assertIn("+2. the second thing really works", extra["acceptance_diff"])
        self.assertNotIn("first thing", "\n".join(l for l in extra["acceptance_diff"].splitlines()
                                                   if l[:1] in "+-"))
        stale, = self.events("blind_tests_stale")
        self.assertEqual((stale["acceptance_changed"], stale["landed_hash"]), (True, self.hash))
        dispatch._rerun_inputs(self.conn, task, bundles.card_hash(new), dispatch.acceptance(new), 8)
        self.assertEqual(len(self.events("blind_tests_stale")), 1)         # once per new hash

    def test_a_body_change_that_leaves_the_acceptance_text_alone_starts_no_run(self):
        self.landed()
        new = BODY + "\nA note for the reader.\n"
        _, extra, skip = dispatch._rerun_inputs(self.conn, {"id": self.card, "body": new},
                                          bundles.card_hash(new), dispatch.acceptance(new), 7)
        self.assertEqual((extra, skip),
                         ({}, "the acceptance text is unchanged since the tests landed"))
        self.assertEqual(self.events("blind_tests_stale")[0]["acceptance_changed"], False)

    def test_a_card_with_nothing_landed_is_never_stale(self):
        self.assertEqual(dispatch._rerun_inputs(self.conn, self.card_row(), self.hash, ["a"], 1),
                         (None, {}, None))
        self.assertEqual(self.events("blind_tests_stale"), [])


if __name__ == "__main__":
    unittest.main()
