"""Korin watch closure (spec 2026-10-24 section 6): incident_watch.py and its gate."""
import json
import tempfile
import unittest
from pathlib import Path

from capsule import db, incident_watch, incidents
from capsule.tests import test_incidents as ti
from capsule.tests.test_incidents import DAY, T0, good_doc


class WatchClosure(ti.Gate):
    """closure = watch: oracle A is the incidents table, oracle B the test on main."""

    def fill_watch(self):
        doc = good_doc(self.inc, closure="watch", confidence="low")
        rel = incidents.note_rel(incidents.get(self.conn, self.inc))
        inc = incidents.get(self.conn, self.inc)
        (self.vault / rel).write_text(incidents.render_note(inc, doc, self.card, now=T0))
        incidents.apply_postmortem(self.conn, self.inc, doc, fix_card=self.card, note_path=rel,
                                   now=T0)
        self.conn.execute("UPDATE tasks SET status='done' WHERE id=?", (self.card,))
        db.event(self.conn, self.card, "completed", {"commit": "abc"}, now=T0)
        incidents.set_decision_row(self.conn, self.inc, 457)
        note = self.vault / incidents.DEFAULT_DECISIONS_NOTE
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text(f"| 457 | Incident {self.inc}: transient | x |\n")

    def gate(self, at):
        return incidents.close(self.conn, self.inc, vault=self.vault, run_test=self.run_test,
                               check_new=self.check_new, watch_days=14, now=at)

    def test_apply_stores_closure_and_fixed_is_the_default(self):
        self.fill_watch()
        self.assertEqual(incidents.get(self.conn, self.inc)["closure"], "watch")
        other, _ = self.open(key="other: key")
        incidents.apply_postmortem(self.conn, other, good_doc(other), fix_card=self.card,
                                   note_path="n.md")
        self.assertEqual(incidents.get(self.conn, other)["closure"], "fixed")

    def test_the_window_blocks_until_watch_days_pass(self):
        self.fill_watch()
        self.test_ok = True
        with self.assertRaises(incidents.CloseRefused) as cm:
            self.gate(T0 + 14 * DAY - 1)
        self.assertEqual(len(cm.exception.missing), 1, cm.exception.missing)
        self.assertIn("watch window open", cm.exception.missing[0])
        self.assertEqual(self.gate(T0 + 14 * DAY)["status"], "closed")

    def test_a_detector_test_may_predate_the_incident(self):
        self.fill_watch()
        self.test_ok, self.new_ok = True, False        # check_new would refuse a fixed closure
        self.assertEqual(self.gate(T0 + 14 * DAY)["status"], "closed")
        self.assertEqual(self.tests, [good_doc(self.inc)["recurrence_test"]])

    def test_a_failing_detector_test_blocks(self):
        self.fill_watch()
        self.test_ok = False
        with self.assertRaises(incidents.CloseRefused) as cm:
            self.gate(T0 + 14 * DAY)
        self.assertEqual(cm.exception.missing, ["recurrence test failed on main"])

    def test_a_later_row_of_the_same_fingerprint_blocks_the_gate(self):
        self.fill_watch()
        self.test_ok = True
        row = incidents.get(self.conn, self.inc)
        self.conn.execute("INSERT INTO incidents (id, board, kind, opened_at, fingerprint, status)"
                          " VALUES ('inc_0000beef', 'b', 'crash', ?, ?, 'open')",
                          (T0 + 3 * DAY, row["fingerprint"]))
        self.assertIn("reopened", " ".join(incident_watch.gaps(self.conn, row, 14, T0 + 15 * DAY)))

    def test_an_undone_evidence_card_blocks(self):
        self.fill_watch()
        self.conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (self.card,))
        self.test_ok = True
        with self.assertRaises(incidents.CloseRefused) as cm:
            self.gate(T0 + 14 * DAY)
        self.assertIn("is not done", cm.exception.missing[0])

    def test_note_and_outbox_say_watch_never_fixed(self):
        self.fill_watch()
        inc = incidents.get(self.conn, self.inc)
        watch_doc = good_doc(self.inc, closure="watch", confidence="low")
        note = incidents.render_note(inc, watch_doc, self.card, now=T0, watch_days=14)
        self.assertIn("status: watching", note)
        self.assertIn("Closure: watch (14 days, detector `capsule.tests.test_worker", note)
        fixed = incidents.render_note(inc, good_doc(self.inc), self.card, now=T0)
        self.assertIn("status: fix_pending", fixed)
        self.assertNotIn("Closure: watch", fixed)
        self.test_ok = True
        self.gate(T0 + 14 * DAY)
        text = json.dumps([dict(r) for r in self.conn.execute("SELECT * FROM outbox")])
        self.assertIn("closed (watch, no recurrence in 14 days)", text)

    def reopen(self, at, key="implementer: fatal: boom"):
        return incidents.open_incident(self.conn, board="b", kind="crash", key=key,
                                       evidence=["event:2"], now=at)

    def test_a_recurrence_during_the_watch_is_a_repeat_and_supersedes_it(self):
        for at in (T0 + 60, T0 + 3 * DAY):
            self.setUp()
            self.fill_watch()
            new, action = self.reopen(at)
            self.assertEqual(action, "repeat")
            row = incidents.get(self.conn, new)
            self.assertEqual((row["kind"], row["repeat_of"]), ("repeat", self.inc))
            old = incidents.get(self.conn, self.inc)
            self.assertEqual((old["status"], old["closure"]), ("superseded", incident_watch.FAILED))
            self.assertIsNone(old["closed_at"])
            self.assertEqual(incidents.summary(self.conn)["repeat"], 1)
            self.assertEqual([r["id"] for r in incidents.listing(self.conn, "open")], [new])
            with self.assertRaises(incidents.CloseRefused) as cm:
                self.gate(T0 + 15 * DAY)
            self.assertEqual(cm.exception.missing, ["already superseded"])

    def test_a_failed_watch_is_not_open_not_closed_and_not_a_lesson(self):
        from capsule import prior_verdicts, statusview
        self.fill_watch()
        self.conn.execute("UPDATE incidents SET root_cause='unproven' WHERE id=?", (self.inc,))
        self.reopen(T0 + 60)
        counts = incidents.summary(self.conn)
        self.assertEqual((counts["open"], counts["closed"], counts["total"]), (1, 0, 2))
        self.assertEqual(counts["by_status"]["superseded"], 1)
        card = {"ids": {self.inc}, "tasks": set(), "key": "", "rows": set(),
                "marks": (set(), set())}
        self.assertEqual(prior_verdicts._incident_hits(self.conn, card, T0 + DAY, ()), [])
        self.assertEqual([r["id"] for r in incidents.listing(self.conn, "superseded")], [self.inc])
        self.assertNotIn(self.inc, [r["id"] for r in incidents.listing(self.conn, "open")])
        open_kinds = statusview._incident_problems(self.conn, "b", {}, {})
        self.assertEqual(str(open_kinds).count("repeat"), 1, open_kinds)
        self.assertNotIn("crash", str(open_kinds))

    def problems(self, doc, row, directory):
        return incidents.postmortem_problems(doc, row, directory=directory, conn=self.conn)

    def test_a_watch_is_refused_when_the_fingerprint_already_recurred(self):
        row = incidents.get(self.conn, self.inc)
        doc = good_doc(self.inc, closure="watch", confidence="low")
        empty = tempfile.mkdtemp()
        self.assertEqual(self.problems(doc, row, empty), [])
        _, action = self.reopen(T0 + DAY)      # no watch yet, so this is its own incident
        self.assertEqual(action, "opened")
        probs = self.problems(doc, row, empty)
        self.assertTrue(any("already reopened" in p for p in probs), probs)
        fixed = good_doc(self.inc)
        self.assertEqual(self.problems(fixed, row, empty), [])

    def test_a_failed_watch_is_never_a_clean_close(self):
        self.fill_watch()
        self.reopen(T0 + 60)
        text = json.dumps([dict(r) for r in self.conn.execute("SELECT * FROM outbox")])
        self.assertIn("REPEAT incident", text)
        self.assertNotIn("closed (watch", text)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE kind='incident_closed'").fetchone()[0], 0)

    def test_watches_cannot_chain(self):
        self.fill_watch()
        first, _ = self.reopen(T0 + 3 * DAY)
        self.conn.execute("UPDATE incidents SET closure='watch' WHERE id=?", (first,))
        second, action = self.reopen(T0 + 5 * DAY)
        self.assertEqual(action, "repeat")
        self.assertEqual(incidents.summary(self.conn)["repeat"], 2)

    def test_an_alert_refiring_during_a_watch_is_a_repeat(self):
        cfg = {"korin": {"enabled": True}}
        inc, _ = self.open(kind="silent_failure", key="unowned_work_idle", now=T0 - 3 * DAY)
        self.conn.execute("UPDATE incidents SET closure='watch' WHERE id=?", (inc,))
        for at in (T0, T0 + 60):
            action = incidents.record_alert_firing(self.conn, cfg, alert="UnownedWorkIdle",
                                                   key="unowned_work_idle", board="b", now=at)
        self.assertEqual(action, "repeat")
        self.assertEqual(incidents.get(self.conn, inc)["closure"], incident_watch.FAILED)
        self.assertEqual(incidents.summary(self.conn)["repeat"], 1)

    def test_watch_is_barred_for_repeat_and_escaped_bug(self):
        empty = tempfile.mkdtemp()
        row = incidents.get(self.conn, self.inc)
        watch = good_doc(self.inc, closure="watch", confidence="medium")
        for kind in incidents.WATCH_BARRED:
            probs = incidents.postmortem_problems(watch, {**row, "kind": kind}, directory=empty)
            self.assertTrue(any("watch" in p for p in probs), (kind, probs))
        low = good_doc(self.inc, closure="watch", confidence="low")
        self.assertEqual(incidents.postmortem_problems(low, row, directory=empty), [])


    def test_the_watch_days_setting(self):
        self.assertEqual(incidents.settings({})["watch_days"], 14)
        ks = incidents.settings({"korin": {"watch_days": 7}})
        self.assertEqual((ks["watch_days"], incidents.gate_args(ks)["watch_days"]), (7, 7))


class ClosureMigration(unittest.TestCase):
    def test_an_old_board_gains_the_column_once_and_keeps_its_rows(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "k.db"
            db.create_board(path)
            conn = db.connect(path)
            inc, _ = incidents.open_incident(conn, board="b", kind="crash", key="k", now=T0)
            old = db._incidents_body().replace(", closure TEXT", "")
            cols = ", ".join(c for c in db.INCIDENTS_COLUMNS if c != "closure")
            conn.execute(f"CREATE TABLE incidents_old ({old})")
            conn.execute(f"INSERT INTO incidents_old ({cols}) SELECT {cols} FROM incidents")
            conn.execute("DROP TABLE incidents")
            conn.execute("ALTER TABLE incidents_old RENAME TO incidents")
            self.assertTrue(db.migrations_pending(conn))
            db.migrate(conn)
            db.migrate(conn)
            names = [r[1] for r in conn.execute("PRAGMA table_info(incidents)")]
            self.assertEqual(names.count("closure"), 1)
            self.assertFalse(db.migrations_pending(conn))
            self.assertIsNone(incidents.get(conn, inc)["closure"])
            conn.close()

    def test_an_old_check_without_superseded_is_widened_once(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "k.db"
            db.create_board(path)
            conn = db.connect(path)
            inc, _ = incidents.open_incident(conn, board="b", kind="crash", key="k", now=T0)
            old = db._incidents_body().replace(",'superseded'", "")
            cols = ", ".join(db.INCIDENTS_COLUMNS)
            conn.execute(f"CREATE TABLE incidents_old ({old})")
            conn.execute(f"INSERT INTO incidents_old ({cols}) SELECT {cols} FROM incidents")
            conn.execute("DROP " + "TABLE incidents")
            conn.execute("ALTER TABLE incidents_old RENAME TO incidents")
            self.assertTrue(db.migrations_pending(conn))
            db.migrate(conn)
            db.migrate(conn)
            self.assertFalse(db.migrations_pending(conn))
            conn.execute("UPDATE incidents SET status='superseded' WHERE id=?", (inc,))
            self.assertEqual(incidents.get(conn, inc)["status"], "superseded")
            conn.close()
