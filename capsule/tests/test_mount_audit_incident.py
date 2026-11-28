"""R33 mount audit, the incident side (blind spec 4.4 item 8): a hit opens one review_infra
incident (fingerprint = check name + card, the path only in the event text), a refused start holds
the card whichever container it was, and a repeat hit after a release is counted and paged. Fake
docker only; never real docker. The container-level cases are in test_mount_audit."""
import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capsule import db, incident_blind, incident_inputs, incidents, intent, lease, tick
from capsule.runners import docker
from capsule.tests import test_contract_jobs as tcj
from capsule.tests import test_tick as tt
from capsule.tests.test_mount_audit import CHECK, NOW, RUN, Fake, listing


def setUpModule():   # the runner never falls back to another image, so name one (statement 3)
    patch = mock.patch.dict(os.environ, {"OPS_WORKER_IMAGE": "stub-image:1"})
    patch.start()
    unittest.addModuleCleanup(patch.stop)


class Incident(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        db.create_board(self.dir / "kanban.db")
        self.conn = db.connect(self.dir / "kanban.db")
        self.addCleanup(self.conn.close)
        self.tid = db.new_task(self.conn, title="card", assignee="dev-frontend", branch="epic/x")

    def tick_error(self, tid, message):
        db.event(self.conn, tid, "tick_error",
                 {"error": f"capsule: tick error on {tid}: RuntimeError: {message}"}, now=NOW)

    def opened(self):
        incidents.from_events(self.conn, board="b", spool_root=self.dir, now=NOW)
        return incidents.listing(self.conn)

    def test_a_hit_opens_one_review_infra_incident_with_no_path_in_its_evidence(self):
        incidents.from_events(self.conn, board="b", now=NOW)
        self.tick_error(self.tid, f"{CHECK}: /home/p/x-blind")
        (row,) = self.opened()
        self.assertEqual(row["kind"], "review_infra")
        self.assertIn(CHECK, row["role"])
        self.assertIn(self.tid, row["role"])
        self.assertNotIn("x-blind", row["evidence"])
        evidence = json.loads(row["evidence"])
        self.assertFalse(any(e.startswith("/") and "blind" in e for e in evidence))

    def test_fingerprint_is_check_name_plus_card_whatever_the_path(self):
        other = db.new_task(self.conn, title="other", assignee="dev-frontend", branch="epic/y")
        incidents.from_events(self.conn, board="b", now=NOW)
        self.tick_error(self.tid, f"{CHECK}: /a/x-blind")
        self.tick_error(self.tid, f"{CHECK}: /b/blind-out")
        self.tick_error(other, f"{CHECK}: /a/x-blind")
        rows = self.opened()
        self.assertEqual(len(rows), 2)                  # same card dedupes, another card does not
        self.assertEqual(len({r["fingerprint"] for r in rows}), 2)

    def test_an_ordinary_tick_error_is_still_a_crash(self):
        incidents.from_events(self.conn, board="b", now=NOW)
        self.tick_error(self.tid, "boom")
        self.assertEqual([r["kind"] for r in self.opened()], ["crash"])

    def test_a_refused_start_holds_the_card_and_re_raises(self):
        class Refusing:
            def start(self, *args, **kw):
                raise RuntimeError(f"{CHECK}: /home/p/x-blind")

        with self.assertRaises(RuntimeError):
            incident_blind.start(self.conn, Refusing(), {"id": self.tid}, NOW, RUN, self.dir / "w")
        task = self.conn.execute("SELECT status, block_kind FROM tasks WHERE id=?",
                                 (self.tid,)).fetchone()
        self.assertEqual((task["status"], task["block_kind"]), ("blocked", "needs_input"))

    def test_the_shipped_rows_for_a_hit_and_an_unreadable_list_match_what_the_cases_observe(self):
        rows = {r["id"]: r for r in intent.load("mount_audit")}
        repo = self.dir / "x-blind"
        seen = {}
        for case, mounts in (("blind-mount-stops-the-run", listing("/w", str(repo))),
                             ("unreadable-mount-list-is-never-clean", "[]")):
            fake = Fake(mounts)
            runner = docker.StrongRunner()
            runner._run, runner._sleep, runner.blind_repo = fake, lambda seconds: None, str(repo)
            seen[case] = {"run_started": True, "blind_incident": False, "container_removed": None}
            try:
                incident_blind.start(self.conn, runner, {"id": self.tid}, NOW, RUN,
                                     self.dir / "w")
            except (RuntimeError, subprocess.CalledProcessError) as exc:
                seen[case].update(run_started=False, blind_incident=CHECK in str(exc),
                                  container_removed=not fake.exists)
                if CHECK in str(exc):
                    self.tick_error(self.tid, str(exc))
        (inc,) = self.opened()
        held = self.conn.execute("SELECT block_kind FROM tasks WHERE id=?", (self.tid,)).fetchone()
        seen["blind-mount-stops-the-run"].update(incident_kind=inc["kind"],
                                                 card_held=held[0] == "needs_input")
        for case, observed in seen.items():
            for key, want in rows[case]["expect"].items():
                self.assertEqual(observed[key], want, f"{case}: {key}")

    def test_another_start_failure_re_raises_without_a_hold(self):
        class Failing:
            def start(self, *args, **kw):
                raise RuntimeError("docker exploded")

        with self.assertRaises(RuntimeError):
            incident_blind.start(self.conn, Failing(), {"id": self.tid}, NOW, RUN, self.dir / "w")
        self.assertIsNone(self.conn.execute("SELECT block_kind FROM tasks WHERE id=?",
                                            (self.tid,)).fetchone()["block_kind"])


class TickPaths(unittest.TestCase):
    """The card's containers started the way the tick starts them."""
    tick_error, opened = Incident.tick_error, Incident.opened

    def setUp(self):
        Incident.setUp(self)
        self.repo = self.dir / "x-blind"
        self.repo.mkdir()
        self.tick = tick.Tick(self.dir, runners=lambda tier, role: None)
        self.task = self.conn.execute("SELECT * FROM tasks WHERE id=?", (self.tid,)).fetchone()

    def runner(self):
        runner = docker.StrongRunner()
        runner._run, runner._sleep = Fake(listing(str(self.repo))), lambda seconds: None
        runner.blind_repo = str(self.repo)
        return runner

    def held(self):
        return self.conn.execute("SELECT status, block_kind FROM tasks WHERE id=?",
                                 (self.tid,)).fetchone()

    def contract_step(self):
        """One `contracts.step` of the card (the contract job's thread starts the container and
        fails at once, so a long grace costs nothing), through the tick's own guard."""
        jobs, counts = self.tick.contracts, {"tick_errors": 0}
        cfg = {"board": "b", "contract_commands": [], "caps": {"max_running": 1, "max_review": 1}}
        jobs.grace = 10
        ok = self.tick._guarded(
            self.conn, cfg, self.tid, NOW, counts, lambda: jobs.step(
                self.tick, self.conn, cfg, self.task, None, self.dir / "w", NOW, RUN), run_id=RUN)
        return ok, counts

    def test_a_hit_on_the_contract_container_holds_the_card_and_records_the_error(self):
        self.tick.strong = self.runner()
        ok, counts = self.contract_step()
        self.assertFalse(ok)
        self.assertEqual(tuple(self.held()), ("blocked", "needs_input"))
        self.assertEqual(counts["tick_errors"], 1)
        events = self.errors()
        self.assertEqual(len(events), 1)
        self.assertIn(CHECK, events[0]["payload"])

    def test_any_other_start_failure_still_backs_off_and_does_not_hold(self):
        class Down:
            def start(self, *args, **kw):
                raise RuntimeError("docker is down")
        self.tick.strong = Down()
        ok, counts = self.contract_step()
        self.assertFalse(ok)
        self.assertEqual(counts["tick_errors"], 1)
        self.assertEqual(tuple(self.held()), ("ready", None))
        self.assertTrue(self.tick.contracts.running(self.task, None, NOW))   # waits its backoff

    def test_only_the_runners_own_refusal_holds_not_a_quote_of_it(self):
        text = f"{CHECK}: /x-blind [run capsule-abc]"
        quoting = [ValueError(text), RuntimeError(f"contract job failed: RuntimeError: {text}"),
                   RuntimeError(f"the probe said {text}"), RuntimeError(f"{CHECK}: "),
                   subprocess.CalledProcessError(1, f"RuntimeError: {CHECK}: /x-blind"),
                   subprocess.CalledProcessError(125, ["docker", "run", text])]
        for exc in quoting:
            class Raising:
                def start(self, *args, **kw):
                    raise exc
            self.tick.strong = Raising()
            with self.subTest(exc=repr(exc)):
                ok, counts = self.contract_step()
                self.assertFalse(ok)
                self.assertEqual(tuple(self.held()), ("ready", None))      # backs off, no hold
                self.assertTrue(self.tick.contracts.running(self.task, None, NOW))
            self.tick.contracts.backoff.clear()
            self.assertFalse(incident_blind.hold(self.conn, self.tid, exc, RUN, NOW))
        self.assertTrue(incident_blind.hold(self.conn, self.tid, RuntimeError(text), RUN, NOW))
        self.assertEqual(tuple(self.held()), ("blocked", "needs_input"))

    def test_the_real_refusal_holds_through_both_doors(self):
        class Refusing:
            def start(self, *args, **kw):
                raise RuntimeError(f"{CHECK}: /x-blind [run capsule-abc]")
        self.tick.strong = Refusing()
        ok, counts = self.contract_step()
        self.assertEqual(tuple(self.held()), ("blocked", "needs_input"))
        self.conn.execute("UPDATE tasks SET status='ready', block_kind=NULL WHERE id=?",
                          (self.tid,))
        with self.assertRaises(RuntimeError):
            incident_blind.start(self.conn, Refusing(), self.task, NOW, RUN, self.dir / "w")
        self.assertEqual(tuple(self.held()), ("blocked", "needs_input"))

    LEAK = "/srv/BLINDLEAK-7f3a/spool"
    SAFE = f"{CHECK}: path-6b70333149f8 [run capsule-abc]"       # sha256 of LEAK, first 12 hex

    class Leaking:
        def start(self, *args, **kw):
            raise RuntimeError(f"{CHECK}: {TickPaths.LEAK} [run capsule-abc]")

    def assert_block_is_masked(self):
        """The hold of a running card, read on every surface its reason reaches."""
        events = [r[0] for r in self.conn.execute(
            "SELECT payload FROM task_events WHERE kind='blocked'")]
        summaries = [r[0] for r in self.conn.execute("SELECT summary FROM task_runs")]
        notices = [r[0] for r in self.conn.execute(
            "SELECT body FROM outbox WHERE body LIKE '%(needs_input)%'")]
        for name, found in (("event", events), ("summary", summaries), ("outbox", notices)):
            self.assertEqual(len(found), 1, name)
            self.assertNotIn("BLINDLEAK", found[0], name)
            self.assertNotIn("/srv/", found[0], name)
            self.assertIn(self.SAFE, found[0], name)
        self.assertEqual(json.loads(events[0])["reason"], self.SAFE)
        self.assertEqual(summaries[0], self.SAFE)

    def claim(self):
        lease.claim(self.conn, self.tid, ttl=60, role="implementer", now=NOW, run_id=RUN)

    def test_a_held_refusal_masks_the_path_when_hold_is_called_directly(self):
        self.claim()
        exc = RuntimeError(f"{CHECK}: {self.LEAK} [run capsule-abc]")
        self.assertTrue(incident_blind.hold(self.conn, self.tid, exc, RUN, NOW))
        self.assert_block_is_masked()

    def test_a_held_refusal_masks_the_path_through_the_real_start_route(self):
        self.claim()
        with self.assertRaises(RuntimeError):
            incident_blind.start(self.conn, self.Leaking(), self.task, NOW, RUN, self.dir / "w")
        self.assert_block_is_masked()

    def test_a_held_refusal_masks_the_path_through_the_contract_step_route(self):
        self.claim()
        self.tick.strong = self.Leaking()
        ok, counts = self.contract_step()
        self.assertFalse(ok)
        self.assert_block_is_masked()

    def hit(self, run_id):
        cfg, counts = {"board": "b"}, {"tick_errors": 0}
        runner = self.runner()
        return self.tick._guarded(
            self.conn, cfg, self.tid, NOW, counts, lambda: incident_blind.start(
                self.conn, runner, self.task, NOW, run_id, self.dir / "w"), run_id=run_id)

    def errors(self):
        return self.conn.execute(
            "SELECT payload FROM task_events WHERE kind='tick_error'").fetchall()

    def pages(self):
        return self.conn.execute("SELECT COUNT(*) FROM outbox WHERE body LIKE ?",
                                 (f"capsule: tick error on {self.tid}:%",)).fetchone()[0]

    def test_a_repeat_after_a_release_is_a_new_event_and_page_on_the_same_fingerprint(self):
        incidents.from_events(self.conn, board="b", now=NOW)           # the watermark
        self.assertFalse(self.hit("a" * 32))
        first = self.opened()
        self.assertEqual(self.pages(), 1)
        self.conn.execute("UPDATE tasks SET status='ready', block_kind=NULL WHERE id=?",
                          (self.tid,))
        self.assertFalse(self.hit("b" * 32))              # someone released it; the same path
        self.assertEqual(self.pages(), 2)
        events = self.errors()
        self.assertEqual(len(events), 2)
        self.assertEqual(len({e["payload"] for e in events}), 2)
        again = self.opened()
        self.assertEqual([r["fingerprint"] for r in again], [r["fingerprint"] for r in first])
        evidence = json.loads(again[0]["evidence"])
        self.assertEqual(len([e for e in evidence if e.startswith("event:")]), 2)   # both events
        digest = hashlib.sha256(str(self.repo).encode()).hexdigest()[:12]
        self.assertNotIn(str(self.repo), events[1]["payload"])        # the event text has no path
        self.assertIn(f"{CHECK}: path-{digest} [run capsule-{'b' * 12}]", events[1]["payload"])
        payload = json.loads(events[1]["payload"])
        self.assertEqual(incident_blind.mount_path(payload), f"path-{digest}")

    def all_text(self):
        """Every task_events payload, task_runs summary and error, and outbox body of the board."""
        return [r[0] for r in self.conn.execute(
            "SELECT payload FROM task_events UNION ALL SELECT summary FROM task_runs"
            " UNION ALL SELECT error FROM task_runs UNION ALL SELECT body FROM outbox") if r[0]]

    def add_blind_run(self):
        self.conn.execute("INSERT INTO task_runs (task_id, profile, status, started_at, run_id)"
                          " VALUES (?, 'blind-tester', 'completed', 1, ?)", (self.tid, "c" * 32))

    def assert_no_raw_path_on_the_board(self):
        self.claim()
        self.tick.strong = self.Leaking()
        with contextlib.redirect_stderr(io.StringIO()):
            self.contract_step()
        self.assertTrue(self.errors())
        self.assertTrue(self.all_text())
        for text in self.all_text():
            self.assertNotIn("BLINDLEAK", text)
            self.assertNotIn("/srv/", text)
        payload = json.loads(self.errors()[0]["payload"])
        self.assertIn(self.SAFE.split(" [run")[0], payload["error"])
        self.assertEqual(incident_blind.classify({"task_id": self.tid}, payload),
                         ("review_infra", f"{CHECK}:{self.tid}", CHECK))
        self.assertEqual(incident_blind.mount_path(payload), "path-6b70333149f8")

    def test_the_raw_path_reaches_no_row_of_a_card_without_blind_runs(self):
        self.assert_no_raw_path_on_the_board()

    def test_the_raw_path_reaches_no_row_of_a_card_with_a_blind_run(self):
        self.add_blind_run()
        self.assert_no_raw_path_on_the_board()

    def test_tick_text_of_a_refusal_is_the_masked_text_and_the_log_line_has_the_path(self):
        exc = RuntimeError(f"{CHECK}: {self.LEAK} [run capsule-abc]")
        for blind in (False, True):
            if blind:
                self.add_blind_run()
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                got = incident_inputs.tick_text(self.conn, exc, self.dir, self.tid)
            self.assertEqual(got, "implementer-has-no-blind: path-6b70333149f8 [run capsule-abc]")
            line = err.getvalue()
            self.assertEqual(len(line.splitlines()), 1)
            for part in (CHECK, self.tid, self.LEAK):
                self.assertIn(part, line)
        with contextlib.redirect_stderr(io.StringIO()):
            got = incident_inputs.tick_text(self.conn, RuntimeError(f"{CHECK}: /a/b"), self.dir,
                                            self.tid)
        self.assertEqual(got, "implementer-has-no-blind: path-" + hashlib.sha256(b"/a/b")
                         .hexdigest()[:12])                          # no run tag, none added

    def test_a_path_with_a_newline_or_a_lone_surrogate_is_still_masked(self):
        for path, digest in (("/srv/BLIND\nLEAK/x", None), ("/srv/BLIND\udcffLEAK", None)):
            exc = RuntimeError(f"{CHECK}: {path} [run capsule-abc]")
            self.assertTrue(incident_blind.refusal(exc))
            want = hashlib.sha256(path.encode("utf-8", "surrogatepass")).hexdigest()[:12]
            masked = f"{CHECK}: path-{want} [run capsule-abc]"
            self.assertEqual(incident_blind.masked(exc), masked)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(incident_inputs.tick_text(self.conn, exc, self.dir, self.tid),
                                 masked)
            payload = {"error": f"c: tick error on t: RuntimeError: {masked}"}
            self.assertEqual(incident_blind.mount_path(payload), f"path-{want}")
        bare = RuntimeError(f"{CHECK}: /a\nb")
        self.assertEqual(incident_blind.masked(bare), f"{CHECK}: path-"
                         + hashlib.sha256(b"/a\nb").hexdigest()[:12])

    def test_tick_text_of_another_error_is_as_before(self):
        exc = RuntimeError("quoted text")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(incident_inputs.tick_text(self.conn, exc, self.dir, self.tid),
                             "quoted text")
            self.add_blind_run()
            self.assertEqual(incident_inputs.tick_text(self.conn, exc, self.dir, self.tid),
                             "withheld: the run carries blind rows")
        self.assertEqual(err.getvalue(), "")


class RefusingRunner(tt.FakeRunner):
    """Starts the implementer; refuses the contract container (no `requests_dir`) with a blind
    mount hit, and records the refusals (a list: a contract job runs on a copy of the runner, and
    a copy shares the list, not a counter)."""

    def __init__(self):
        super().__init__()
        self.refused = []

    def start(self, run_id, ws, **kw):
        if "requests_dir" in kw:
            return super().start(run_id, ws, **kw)
        self.refused.append(run_id)
        raise RuntimeError(f"{CHECK}: /x-blind [run capsule-{run_id[:12]}]")


class HoldThroughTheTick(tcj.ContractJobsCase):
    """The hold is what stops a retry: only a card that leaves review is not reviewed again."""

    def test_a_held_card_is_not_reviewed_again_and_no_container_starts_again(self):
        self.runner = RefusingRunner()
        tick = self.make_tick()
        tick.contracts.grace = 10                  # the job fails at once; wait for the thread
        self.pump(tick, lambda: self.runner.refused)
        self.assertEqual(len(self.runner.refused), 1)
        task = self.task()
        self.assertEqual((task["status"], task["block_kind"]), ("blocked", "needs_input"))
        for _ in range(3):                         # real passes: `_review` -> `_start_reviews`
            tick.once()
        self.assertEqual(len(self.runner.refused), 1)   # no second container start
        self.assertEqual(len(self.started()), 1)   # no second contract job
        self.assertEqual(self.task()["status"], "blocked")
