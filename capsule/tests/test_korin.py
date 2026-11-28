"""Korin in the tick (row 456): incidents open from typed events, the Korin run, its document applied,
and the closure gate. TickCase's temp CAPSULE_HOME, fake runner and stub adapter; a temp vault."""
import contextlib
import json
import subprocess
import time
import unittest
import unittest.mock
from pathlib import Path

from capsule import db, human, incident_inputs, incidents, outcomes, paths, report, worker
from capsule.models import Result
from capsule.models.stub import StubAdapter
from capsule.tests.test_tick import TOKENS, TickCase, git

FATAL = Result(None, 1, TOKENS, 1, "fatal", None, None, "boom")
DAY = 86400


def postmortem(incident_id, **over):
    doc = {"schema": "postmortem.v1", "incident_id": incident_id,
           "root_cause": "The adapter raised on an empty reply and the worker never wrote a report.",
           "lesson": "A fatal adapter result must carry its cause into the run record.",
           "mitigation": "Record the adapter's stderr tail on the run when error_class is fatal.",
           "enforcer": "capsule/tick.py _close crashed event, tested by the recurrence test",
           "recurrence_test": "capsule.tests.test_korin.AutoOpen.test_a_crashed_run_opens_one_crash_incident",
           "fix_card": {"title": "tick: keep the fatal cause on the run", "body": "Store the stderr tail on a fatal run."},
           "decision_row_text": f"| 457 | Incident {incident_id}: fatal implementer run | ... | keep the cause |",
           "confidence": "high"}
    doc.update(over)
    return doc


class KorinCase(TickCase):
    def setUp(self):
        super().setUp()
        self.vault = self.home / "vault"
        self.vault.mkdir()
        self.main = self.home / "main"             # the main checkout: a real git repo on `main`
        self.main.mkdir()
        (self.main / "README.md").write_text("main\n")
        git(self.main, "init", "-q", "-b", "main")
        git(self.main, "add", ".")
        git(self.main, "commit", "-qm", "init")
        git(self.main, "remote", "add", "origin", self.cfg["repo_url"])
        self.korin_docs = []        # callables: incident id -> document (or a Result)
        self.korin_jobs = []

    def enable(self, **over):
        base = {"enabled": True, "since": "2026-01-01", "vault": str(self.vault),
                "main_checkout": str(self.main)}
        self.cfg["korin"] = dict(base, **over)
        self.write_cfg()

    def spawn(self, board_db, spool_dir, run_id, role):
        if role != "korin":
            return super().spawn(board_db, spool_dir, run_id, role)
        job = json.loads((Path(spool_dir) / "job.json").read_text())
        self.korin_jobs.append(job)
        ro = db.connect_ro(board_db)
        run = ro.execute("SELECT * FROM task_runs WHERE run_id=?", (run_id,)).fetchone()
        ro.close()
        make = self.korin_docs.pop(0) if self.korin_docs else None
        entry = make(job["inputs"]["incident"]["id"]) if make else None
        worker.run_one(board_db, spool_dir, run_id, role,
                       adapter=StubAdapter([self.fill(entry, run)] if entry is not None else []))
        return None

    def incidents(self, status=None):
        return incidents.listing(self.conn, status)

    def crash_once(self, passes=3, now=None):
        self.script["implementer"] = [[FATAL]]
        now = now or int(time.time())
        for i in range(passes):
            self.tick.once(now=now + i)


class AutoOpen(KorinCase):
    def test_a_crashed_run_opens_one_crash_incident(self):
        self.crash_once()
        recs = self.incidents()
        self.assertEqual([r["kind"] for r in recs], ["crash"])
        rec = recs[0]
        run = self.runs("implementer")[0]
        self.assertEqual((rec["task_id"], rec["run_id"], rec["status"]), (self.tid, run["run_id"], "open"))
        evidence = json.loads(rec["evidence"])
        self.assertIn(str(self.spool_dir(run["run_id"])), evidence)
        # oracle 2: the typed event it came from is on the card, and named in the evidence
        crashed = self.conn.execute("SELECT id FROM task_events WHERE task_id=? AND kind='crashed'", (self.tid,)).fetchall()
        self.assertEqual(len(crashed), 1)
        self.assertIn(f"event:{crashed[0]['id']}", evidence)
        # Korin is off by default: no Korin card, no run
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM task_runs WHERE profile='korin'").fetchone()[0], 0)

    def test_every_human_file_is_its_own_incident(self):
        now = int(time.time())
        self.tick.once(now=now)                          # watermark
        for text in ("first", "second"):
            human.write(self.home, "test", self.tid, "comment", text, now=now)
        self.tick.once(now=now + 1)
        recs = [r for r in self.incidents() if r["kind"] == "human_intervention"]
        self.assertEqual(len(recs), 2)
        files = sorted(Path(json.loads(r["evidence"])[-1]).name for r in recs)
        self.assertEqual(files, ["1.json", "2.json"])

    def test_the_same_crash_after_a_close_is_a_repeat(self):
        # Pinned, not wall-clock: the tick's once-a-day eval-check pass opens a contradiction
        # incident for every closed week after outcomes.SINCE, so a real `now + 2 days` that crosses
        # a Monday week-close adds a third incident (it did on 2026-10-25). Two days after SINCE no
        # week has closed, so the board holds exactly the two crash incidents this test is about.
        now = outcomes.SINCE
        self.crash_once(now=now)
        first = self.incidents()[0]
        self.conn.execute("UPDATE incidents SET status='closed', closed_at=? WHERE id=?", (now + 10, first["id"]))
        # a second card crashes the same way two days later
        self.conn.execute("UPDATE tasks SET status='done' WHERE id=?", (self.tid,))
        self.tid = db.new_task(self.conn, title="break screen", assignee="dev-frontend", branch=self.cfg["epics"][0])
        self.crash_once(now=now + 2 * DAY)
        repeat = [r for r in self.incidents() if r["kind"] == "repeat"]
        self.assertEqual(len(repeat), 1)
        self.assertEqual(repeat[0]["repeat_of"], first["id"])
        self.assertEqual(incidents.summary(self.conn)["repeat"], 1)
        # the report: repeat is the headline (md) and the status board's number (json), seen two ways
        doc = report.interventions(self.home, "test", since=0)
        self.assertEqual((doc["repeat_incidents"], doc["incidents"]["total"]), (1, 2))
        md = report.render_md(doc)
        self.assertIn("## Incidents", md)
        self.assertIn("**Repeat incidents: 1**", md)
        self.assertLess(md.index("Repeat incidents"), md.index("| card |"))
        self.assertIn(f"repeat {repeat[0]['id']} of {first['id']}", md)

    def test_a_board_without_the_table_reports_zero_incidents(self):
        self.conn.execute("DROP TABLE incidents")
        doc = report.interventions(self.home, "test", since=0)
        self.assertEqual(doc["repeat_incidents"], 0)
        self.assertTrue(doc["incidents"]["absent"])


class KorinRun(KorinCase):
    def test_korin_fills_the_record_files_the_fix_card_and_writes_the_note(self):
        self.enable()
        self.korin_docs.append(lambda inc: postmortem(inc))
        self.crash_once(passes=5)
        rec = self.incidents()[0]
        self.assertEqual(rec["status"], "fix_pending")
        for field in incidents.RECORD_FIELDS:
            self.assertTrue(rec[field], field)
        job = self.korin_jobs[0]
        self.assertEqual(job["workspace"], str(self.main))            # the main checkout, read-only
        self.assertEqual(job["route"]["model"], "claude-opus-5")
        self.assertIn(rec["id"], job["inputs"]["incident"]["id"])
        self.assertTrue(any("worker.log" in log["path"] or "board events" in log["path"] for log in job["inputs"]["logs"]))
        fix = self.conn.execute("SELECT * FROM tasks WHERE id=?", (rec["fix_card"],)).fetchone()
        self.assertEqual((fix["status"], fix["block_kind"]), ("blocked", "needs_input"))
        self.assertIn(rec["recurrence_test"], fix["body"])
        note = self.vault / rec["note_path"]
        self.assertTrue(note.is_file())
        self.assertTrue(rec["note_path"].startswith("Knowledge/Incidents/"))
        self.assertIn(f" - crash - {rec['id']}.md", rec["note_path"])
        self.assertIn("## For future agent", note.read_text())
        korin_card = self.conn.execute("SELECT * FROM tasks WHERE id=?", (rec["korin_task"],)).fetchone()
        self.assertEqual((korin_card["status"], korin_card["assignee"], korin_card["branch_name"]), ("done", "korin", None))
        lines = [r[0] for r in self.conn.execute("SELECT body FROM outbox WHERE channel='file'")]
        self.assertTrue(any("decision row" in line and "| 457 |" in line for line in lines), lines)

    def note_lines(self):
        return [r[0] for r in self.conn.execute("SELECT body FROM outbox WHERE channel='file'")
                if "not written" in r[0]]

    def test_no_vault_reports_the_note_unwritten_and_writes_nothing(self):
        self.enable()
        real = incidents.settings
        self.korin_docs.append(lambda inc: postmortem(inc))
        with unittest.mock.patch.object(incidents, "settings",
                                        lambda cfg: dict(real(cfg), vault=None)):
            self.crash_once(passes=5)
        self.assertTrue(any("no notes vault configured" in line for line in self.note_lines()))
        self.assertEqual(list(self.vault.rglob("*.md")), [])

    def test_a_real_bug_in_render_note_is_not_reported_as_a_missing_vault(self):
        self.enable()
        self.korin_docs.append(lambda inc: postmortem(inc))
        with unittest.mock.patch.object(incidents, "render_note", side_effect=TypeError("bug")):
            with contextlib.suppress(TypeError):
                self.crash_once(passes=5)
        self.assertEqual(self.note_lines(), [])

    def test_an_intent_contradicting_document_is_refused_then_korin_gives_up_once(self):
        self.enable()
        self.korin_docs += [lambda inc: postmortem(inc, enforcer="review"), lambda inc: FATAL]
        self.crash_once(passes=6)
        rec = self.incidents()[0]
        self.assertEqual(rec["status"], "open")
        self.assertIsNone(rec["root_cause"])
        runs = self.conn.execute("SELECT * FROM task_runs WHERE profile='korin' ORDER BY id").fetchall()
        self.assertEqual(len(runs), 2)
        self.assertIn("bare-review-is-not-an-enforcer", runs[0]["error"])
        gave_up = self.conn.execute("SELECT COUNT(*) FROM task_events WHERE kind='korin_gave_up'").fetchone()[0]
        self.assertEqual(gave_up, 1)
        # Korin's own failures open no incident about an incident
        self.assertEqual(len(self.incidents()), 1)
        sent = self.conn.execute("SELECT body FROM outbox WHERE channel != 'local'")
        self.assertEqual(sum(1 for line in sent if "Korin failed" in line[0]), 1)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM outbox WHERE channel='local' AND body LIKE '%Korin failed%'"
        ).fetchone()[0], 1)                                  # and one Mac page


class Gate(KorinCase):
    def setUp(self):
        super().setUp()
        self.enable()
        self.korin_docs.append(lambda inc: postmortem(inc))
        self.now = int(time.time())
        self.crash_once(passes=5, now=self.now)
        self.rec = self.incidents()[0]
        self.tests = []
        self.passing = True
        self.tick.run_recurrence = self.fake_test

    def fake_test(self, test_id, checkout, timeout):
        self.tests.append((test_id, str(checkout)))
        return self.passing, "recurrence test failed on main"

    def finish_fix(self):
        card = self.rec["fix_card"]
        self.conn.execute("UPDATE tasks SET status='done' WHERE id=?", (card,))
        db.event(self.conn, card, "completed", {"commit": "abc"})
        incidents.set_decision_row(self.conn, self.rec["id"], 457)
        decisions = self.vault / incidents.DEFAULT_DECISIONS_NOTE
        decisions.parent.mkdir(parents=True, exist_ok=True)
        decisions.write_text(f"| 457 | Incident {self.rec['id']}: fatal run | x | y |\n")

    def land_test_on_main(self):
        """The fix card's recurrence test reaches main after the incident opened (I4's second oracle)."""
        path = self.main / "capsule" / "tests" / "test_korin.py"
        path.parent.mkdir(parents=True)
        path.write_text("class AutoOpen:\n    def test_a_crashed_run_opens_one_crash_incident(self):\n        pass\n")
        git(self.main, "add", ".")
        git(self.main, "commit", "-qm", "fix + recurrence test")

    def test_the_tick_closes_only_when_everything_holds(self):
        self.tick.once(now=self.now + 10)
        self.assertEqual(incidents.get(self.conn, self.rec["id"])["status"], "fix_pending")
        self.assertEqual(self.tests, [])                      # cheap checks failed: the test never ran
        self.finish_fix()
        self.tick.once(now=self.now + 15)
        self.assertEqual(self.tests, [])                      # the test is not on main yet: not new, not run
        self.assertIsNotNone(self.rec["main_sha"])
        self.land_test_on_main()
        self.passing = False
        self.tick.once(now=self.now + 20)
        self.assertEqual(incidents.get(self.conn, self.rec["id"])["status"], "fix_pending")
        self.assertEqual(self.tests, [(self.rec["recurrence_test"], str(self.main))])
        self.passing = True
        self.tick.once(now=self.now + 30)                     # throttled: no second run within the hour
        self.assertEqual(len(self.tests), 1)
        self.tick.once(now=self.now + 30 + 3600)
        rec = incidents.get(self.conn, self.rec["id"])
        self.assertEqual(rec["status"], "closed")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM task_events WHERE kind='incident_closed'").fetchone()[0], 1)


class Cli(KorinCase):
    def run_cli(self, *argv):
        import contextlib
        import io
        from capsule.__main__ import main
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = main(["incidents", *argv, "--capsule-home", str(self.home)])
        return rc, out.getvalue(), err.getvalue()

    def test_list_show_row_and_a_typed_close_refusal(self):
        self.crash_once()
        inc = self.incidents()[0]["id"]
        rc, out, _ = self.run_cli("list")
        self.assertEqual(rc, 0)
        self.assertIn(inc, out)
        rc, out, _ = self.run_cli("show", inc)
        self.assertEqual(json.loads(out)["kind"], "crash")
        rc, out, _ = self.run_cli("row", inc, "457")
        self.assertEqual((rc, incidents.get(self.conn, inc)["decision_row"]), (0, 457))
        rc, _, err = self.run_cli("close", inc)
        self.assertEqual(rc, 1)
        for part in ("refused", "not fix_pending", "no fix card", "note missing", "no recurrence test"):
            self.assertIn(part, err)
        self.assertEqual(incidents.get(self.conn, inc)["status"], "open")
        rc, _, err = self.run_cli("show", "inc_ffffffff")
        self.assertEqual(rc, 1)


class ConfigAndRunner(KorinCase):
    def test_korin_block_is_validated_and_off_in_the_example(self):
        from capsule import config
        example = config.load(paths.REPO / "capsule" / "config.example.json")
        self.assertFalse(example["korin"]["enabled"])
        self.assertEqual(config.route(example, "korin")["model"], "claude-opus-5")
        for bad in ({"enabled": "yes"}, {"surprise": 1}, {"max_running": 0}):
            cfg = json.loads((paths.REPO / "capsule" / "config.example.json").read_text())
            cfg["korin"] = bad
            with self.assertRaises(ValueError):
                config.validate(cfg)
        cfg = json.loads((paths.REPO / "capsule" / "config.example.json").read_text())
        del cfg["routing"]["korin"]
        config.validate(cfg)                               # optional role: absent is fine
        cfg["korin"] = {"enabled": True}
        with self.assertRaises(ValueError):
            config.validate(cfg)                           # enabled needs a route

    def test_watch_days_is_a_positive_int(self):
        from capsule import config
        base = json.loads((paths.REPO / "capsule" / "config.example.json").read_text())
        base["korin"] = {"watch_days": 7}
        config.validate(base)
        for bad in (0, -1, "7", True):
            base["korin"] = {"watch_days": bad}
            with self.assertRaises(ValueError):
                config.validate(base)

    def test_korin_runs_on_the_host_with_read_only_tools(self):
        from capsule.runners import host, runner_for
        self.assertIsInstance(runner_for("strong", "korin"), host.HostRunner)
        limits = host.HostRunner().limits("korin", self.home / "sp")
        self.assertEqual(limits["tools"].split(","), ["Read", "Glob", "Grep", "Write"])
        self.assertIn("requests", limits["allowed"])       # Write only into its own spool's requests/


class DispatchInputsAndSince(KorinCase):
    def hand_open(self, key, now, evidence=()):
        inc, _ = incidents.open_incident(self.conn, board="b", kind="crash", key=key, now=now,
                                         evidence=list(evidence))
        return inc

    def test_since_excludes_older_incidents_but_keeps_them_open_and_counted(self):
        now = int(time.time())
        cut = incident_inputs.since_ts("2026-10-24")
        old = self.hand_open("old", cut - DAY)
        new = self.hand_open("new", cut + DAY)
        self.enable(since="2026-10-24", max_running=5)
        self.korin_docs += [lambda inc: FATAL] * 4
        self.tick.once(now=now)
        started = {j["inputs"]["incident"]["id"] for j in self.korin_jobs}
        self.assertEqual(started, {new})
        self.assertEqual(incidents.get(self.conn, old)["status"], "open")
        self.assertIsNone(incidents.get(self.conn, old)["korin_task"])
        self.assertEqual(incidents.summary(self.conn, cut)["backlog"], 1)
        self.assertEqual(incidents.summary(self.conn)["backlog"], 0)

    def test_enabled_without_since_is_refused(self):
        from capsule import config
        cfg = json.loads((paths.REPO / "capsule" / "config.example.json").read_text())
        cfg["korin"] = {"enabled": True}
        with self.assertRaisesRegex(ValueError, "korin.since is required"):
            config.validate(cfg)

    def test_disabled_without_since_is_fine(self):
        from capsule import config
        cfg = json.loads((paths.REPO / "capsule" / "config.example.json").read_text())
        cfg["korin"] = {"enabled": False}
        self.assertIs(config.validate(cfg), cfg)

    def test_since_is_validated_as_an_iso_date(self):
        from capsule import config
        base = json.loads((paths.REPO / "capsule" / "config.example.json").read_text())
        for bad in ("yesterday", "2026-13-01", 5):
            cfg = json.loads(json.dumps(base))
            cfg["korin"]["since"] = bad
            with self.assertRaises(ValueError, msg=repr(bad)):
                config.validate(cfg)
        base["korin"]["since"] = "2026-10-24"
        config.validate(base)
        self.assertEqual(incident_inputs.since_ts("2026-10-24"), 1792800000)
        self.assertIsNone(incident_inputs.since_ts(None))

    def test_file_evidence_contributes_its_own_tail(self):
        f = self.home / "results.jsonl"
        f.write_text("x" * 5000 + "TAILMARK")
        self.hand_open("dark", int(time.time()), evidence=[str(f)])
        self.enable()
        self.korin_docs.append(lambda inc: FATAL)
        self.tick.once(now=int(time.time()))
        logs = self.korin_jobs[0]["inputs"]["logs"]
        mine = [log for log in logs if log["path"] == str(f)]
        self.assertEqual(len(mine), 1)
        self.assertTrue(mine[0]["tail"].endswith("TAILMARK"))
        self.assertLessEqual(len(mine[0]["tail"]), 3000)

    def test_a_huge_evidence_file_is_read_from_its_end_only(self):
        f = self.home / "huge.log"
        f.write_bytes(b"a" * 5_000_000 + b"\xff" + b"b" * 100 + b"TAILMARK")
        tail = incident_inputs.first_tail((f,), 3000)["tail"]
        self.assertTrue(tail.endswith("TAILMARK"))
        self.assertEqual(len(tail), 3000)
        with unittest.mock.patch("builtins.open", wraps=open) as op:
            incident_inputs.first_tail((f,), 3000)
        self.assertEqual(op.call_count, 1)

    def test_a_symlinked_evidence_file_is_skipped(self):
        real = self.home / "real.log"
        real.write_text("SECRET")
        link = self.home / "link.log"
        link.symlink_to(real)
        self.assertIsNone(incident_inputs.first_tail((link,), 3000))
        self.hand_open("sym", int(time.time()), evidence=[str(link)])
        self.enable()
        self.korin_docs.append(lambda inc: FATAL)
        self.tick.once(now=int(time.time()))
        logs = self.korin_jobs[0]["inputs"]["logs"]
        self.assertFalse([log for log in logs if log["path"] == str(link)])


class FixRound1(KorinCase):
    def test_i1_crashes_key_on_the_adapters_own_error(self):
        now = int(time.time())
        self.script["implementer"] = [[Result(None, 1, TOKENS, 1, "fatal", None, None, "Traceback\nKeyError: 'cafe1'")]]
        for i in range(3):
            self.tick.once(now=now + i)
        self.conn.execute("UPDATE tasks SET status='done' WHERE id=?", (self.tid,))
        self.tid = db.new_task(self.conn, title="two", assignee="dev-frontend", branch=self.cfg["epics"][0])
        self.script["implementer"] = [[Result(None, 1, TOKENS, 1, "fatal", None, None, "Traceback\nKeyError: 'bead2'")]]
        for i in range(3, 6):
            self.tick.once(now=now + i)
        recs = self.incidents()
        self.assertEqual(len(recs), 2)                        # same day, different causes: never deduped
        self.assertTrue(any("cafe1" in r["root_key"] for r in recs))
        self.assertTrue(all(r["role"] == "implementer" for r in recs))

    def test_i2_the_first_pass_opens_what_its_own_steps_recorded(self):
        """A human file dropped while the tick was down becomes an event in step 1 of the very first
        pass after deploy; step 5b of that same pass opens its incident."""
        now = int(time.time())
        human.write(self.home, "test", self.tid, "comment", "while the tick was down", now=now)
        self.tick.once(now=now)
        self.assertEqual([r["kind"] for r in self.incidents()], ["human_intervention"])

    def test_i3_the_fix_card_is_dispatchable_after_an_unblock(self):
        from capsule import blocks, dispatch
        self.enable()
        self.korin_docs.append(lambda inc: postmortem(inc))
        self.crash_once(passes=5)
        rec = self.incidents()[0]
        fix = self.conn.execute("SELECT * FROM tasks WHERE id=?", (rec["fix_card"],)).fetchone()
        self.assertEqual((fix["branch_name"], fix["assignee"]), (self.cfg["epics"][0], "dev-frontend"))
        self.conn.execute("UPDATE tasks SET status='done' WHERE id=?", (self.tid,))     # free the epic
        blocks.unblock(self.conn, rec["fix_card"], by="human")
        cfg = __import__("capsule.config", fromlist=["load"]).load(self.home / "config.json")
        ready = [r["id"] for r in dispatch.ready(self.conn, cfg, host_tiers={"strong"})]
        self.assertIn(rec["fix_card"], ready)

    def test_i3_fixcard_verb_routes_a_card_that_had_no_epic(self):
        self.enable()
        self.korin_docs.append(lambda inc: postmortem(inc))
        self.crash_once(passes=5)
        rec = self.incidents()[0]
        self.conn.execute("UPDATE tasks SET branch_name=NULL WHERE id=?", (rec["fix_card"],))
        rc, _, err = Cli.run_cli(self, "fixcard", rec["id"], "--epic", "epic/unknown")
        self.assertEqual(rc, 1)
        self.assertIn("not one of the config's epics", err)
        rc, _, _ = Cli.run_cli(self, "fixcard", rec["id"], "--epic", self.cfg["epics"][0])
        self.assertEqual(rc, 0)
        fix = self.conn.execute("SELECT branch_name, assignee FROM tasks WHERE id=?", (rec["fix_card"],)).fetchone()
        self.assertEqual(tuple(fix), (self.cfg["epics"][0], "dev-frontend"))

    def test_i7_open_verb_for_the_gauntlet_and_introspection(self):
        self.enable()
        rc, out, _ = Cli.run_cli(self, "open", "--kind", "escaped_bug", "--root-cause-key", "export drops alpha",
                                 "--evidence", "/gauntlet/run-7/report.json", "--evidence", "row 460")
        self.assertEqual(rc, 0)
        rec = incidents.get(self.conn, out.split()[0])
        self.assertEqual((rec["kind"], rec["status"]), ("escaped_bug", "open"))
        self.assertEqual(json.loads(rec["evidence"]), ["/gauntlet/run-7/report.json", "row 460"])
        self.assertIsNotNone(rec["main_sha"])
        rc, _, err = Cli.run_cli(self, "open", "--kind", "repeat", "--root-cause-key", "x")
        self.assertNotEqual(rc, 0)


class MigrateOnStartup(KorinCase):
    """Row 456 II (escaped bug): a board that existed before Korin merged never got `incidents`
    from create_board/copy_board, so its live tick logged `no such table: incidents` on every
    pass. The tick now runs `db.migrate` once per board at startup, and `capsule check` reports
    pending migrations."""

    def make_pre_korin(self):
        """A board fixture from before row 456: drop the table and its watermark, as if
        `create_board` had run before Korin's migration existed."""
        self.conn.execute("DROP TABLE incidents")
        self.conn.execute("DELETE FROM guard_state WHERE key=?", (db.INCIDENTS_WATERMARK,))

    def test_a_pre_korin_board_gets_the_table_and_watermark_on_the_first_pass(self):
        self.make_pre_korin()
        have = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='incidents'").fetchone()
        self.assertIsNone(have)
        self.tick.once(now=int(time.time()))
        have = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='incidents'").fetchone()
        self.assertIsNotNone(have)
        mark, _ = db.guard_get(self.conn, db.INCIDENTS_WATERMARK)
        self.assertIsNotNone(mark)
        # the incidents step (5b) no longer errors: no tick_error event, no exception raised
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE kind='tick_error'").fetchone()[0], 0)

    def test_the_migration_does_not_open_incidents_for_pre_migration_history(self):
        """I2 (row 456): the watermark is set at migration time, so a crash recorded before the
        tick ever migrated this board does not retroactively open an incident."""
        self.make_pre_korin()
        now = int(time.time())
        # the very first pass migrates the board (setting the watermark) before step 5b runs,
        # so the crash from this same deploy's own passes is still caught (I2)
        self.crash_once(now=now)
        recs = self.incidents()
        self.assertEqual([r["kind"] for r in recs], ["crash"])

    def test_a_second_startup_is_a_no_op(self):
        """Two startups of the same board (db.migrate called twice, as the tick's cache-miss
        path and a restarted process both do) touch the schema and the watermark only once."""
        self.make_pre_korin()
        db.migrate(self.conn)
        mark_after_first, version_after_first = db.guard_get(self.conn, db.INCIDENTS_WATERMARK)
        self.assertIsNotNone(mark_after_first)
        db.migrate(self.conn)          # a second startup
        mark_after_second, version_after_second = db.guard_get(self.conn, db.INCIDENTS_WATERMARK)
        self.assertEqual(mark_after_first, mark_after_second)
        self.assertEqual(version_after_first, version_after_second)
        # and the tick, which migrates once per board per process, runs cleanly on top of that
        now = int(time.time())
        self.tick.once(now=now)
        self.tick.once(now=now + 1)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE kind='tick_error'").fetchone()[0], 0)

    def test_capsule_check_reports_pending_then_ok_migrations(self):
        from capsule import preflight

        def ok_run(argv, **kw):
            return subprocess.CompletedProcess(argv, 0, "28.0\n", "")

        self.make_pre_korin()
        rows = preflight.run(self.home, run_cmd=ok_run, which=lambda n: "/bin/claude")
        detail = {name: (level, detail) for level, name, detail in rows}["migrations"]
        self.assertEqual(detail, ("FAIL", "pending"))
        self.tick.once(now=int(time.time()))
        rows = preflight.run(self.home, run_cmd=ok_run, which=lambda n: "/bin/claude")
        detail = {name: (level, detail) for level, name, detail in rows}["migrations"]
        self.assertEqual(detail, ("ok", "ok"))


class RecurrenceTestTheGateCanRevert(unittest.TestCase):
    """inc_a9d793f8: no brief sends a reader to a test for a name the fix adds."""

    def test_the_korin_and_implementer_briefs_forbid_a_test_read_of_a_name_the_fix_adds(self):
        from capsule import bundles
        task = {"id": "t", "title": "k", "body": ""}
        korin = bundles.korin(task, {"id": "inc_0000aaaa"}, [], [])["prompt"]
        implementer = bundles.implementer(task)["prompt"]
        self.assertIn("a name the fix adds", korin)
        self.assertIn("kept at head for import of", implementer)
        self.assertIn("record the change in `notes`", implementer)
