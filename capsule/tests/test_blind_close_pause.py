"""A tester run that ends in a pausing error class pauses the board and is released for a later
retry, as an implementer run does (blind spec 3.1; follow-up of the rate_limit-only pause).
Z1 intent: a lost login or a host wall must not end the tester run `failed` with
`blind_tester_failed`, and must not open an incident; a `fatal` run still does."""
import time

from capsule import blocks, db, incidents
from capsule.blind import close, dispatch
from capsule.blind.dispatch import MAX_TRIES, ROLE
from capsule.runners import host_wall
from capsule.tests.test_tick import TickCase


class BlindPause(TickCase):
    def notices(self):
        """How many pause notices the file channel holds."""
        return self.conn.execute("SELECT COUNT(*) FROM outbox WHERE channel='file'"
                                 " AND body LIKE '% paused: %'").fetchone()[0]

    def closed(self, error_class, prior=None, tag="", after=300):
        """Close a tester run whose run.json carries `error_class` (the row already says `prior`
        when given); (its row, event kinds, now). `tag` tells two runs apart; the run is for
        card hash "h"."""
        now = int(time.time())
        self.tick.cfg = self.cfg                 # `once` sets it; `close` is called directly
        pk = self.conn.execute(
            "INSERT INTO task_runs (task_id, profile, run_id, started_at, status, claim_lock,"
            " metadata) VALUES (?, ?, ?, ?, 'running', 'l', '{\"card_hash\": \"h\"}')",
            (self.tid, ROLE, f"t{error_class}{prior}{tag}", now)).lastrowid
        self.conn.execute("UPDATE task_runs SET error_class=? WHERE id=?", (prior, pk))
        run = self.conn.execute("SELECT * FROM task_runs WHERE id=?", (pk,)).fetchone()
        sp = self.home / f"spool-{error_class}{prior}{tag}"
        sp.mkdir()
        done = {"error_class": error_class, "retry_after": now + after}
        close.close(self.tick, self.conn, self.cfg, run, sp, done, now, {})
        row = self.conn.execute("SELECT * FROM task_runs WHERE id=?", (pk,)).fetchone()
        kinds = [r[0] for r in self.conn.execute(
            "SELECT kind FROM task_events WHERE task_id=?", (self.tid,))]
        return row, kinds, now

    def test_every_pause_class_pauses_and_writes_no_failure(self):
        for error_class in blocks.PAUSE_CLASSES:
            with self.subTest(error_class):
                db.guard_set(self.conn, "pause:board", {"until": 0}, now=0)
                row, kinds, now = self.closed(error_class)
                self.assertEqual((row["status"], row["outcome"]), ("released", "rate_limited"))
                paused = db.guard_get(self.conn, "pause:board")[0]["until"] > now
                self.assertEqual(paused, error_class != host_wall.TIMEOUT_CLASS)  # see pause_scope
                self.assertNotIn("blind_tester_failed", kinds)
                self.assertEqual(self.notices(), int(error_class != host_wall.TIMEOUT_CLASS))
                self.conn.execute("DELETE FROM outbox")
                # the host wall's own outage incident is the only one a pause may open
                self.assertEqual([i["role"] for i in incidents.listing(self.conn)
                                  if i["role"] != "host_wall"], [])

    def test_releases_that_paused_nothing_exhaust_the_tester(self):
        """A start-up timeout pauses nothing, so MAX_TRIES of them end the retries; the cap adds
        no incident (the page comes at TIMEOUT_PAGE, above MAX_TRIES)."""
        db.guard_set(self.conn, "pause:board", {"until": 0}, now=0)
        for n in range(MAX_TRIES):
            self.assertFalse(dispatch._known(self.conn, self.tid, "h"), n)
            self.closed(host_wall.TIMEOUT_CLASS, tag=str(n))
        self.assertTrue(dispatch._known(self.conn, self.tid, "h"))
        self.assertEqual(incidents.listing(self.conn), [])

    def test_a_tester_killed_at_its_cap_is_retried_then_exhausted(self):
        """D210: a `timed_out` tester run counts like a failed one: the card gets a rerun
        until MAX_TRIES of them end the retries."""
        now = int(time.time())
        for n in range(MAX_TRIES):
            self.assertFalse(dispatch._known(self.conn, self.tid, "h"), n)
            self.conn.execute(
                "INSERT INTO task_runs (task_id, profile, run_id, started_at, status, claim_lock,"
                " metadata) VALUES (?, ?, ?, ?, 'timed_out', 'l', '{\"card_hash\": \"h\"}')",
                (self.tid, ROLE, f"to{n}", now))
        self.assertTrue(dispatch._known(self.conn, self.tid, "h"))

    def test_releases_that_paused_the_board_never_exhaust_the_tester(self):
        for error_class in ("rate_limit", "auth_required", host_wall.ERROR_CLASS):
            with self.subTest(error_class):
                for n in range(MAX_TRIES + 2):
                    db.guard_set(self.conn, "pause:board", {"until": 0}, now=0)   # it ended
                    self.closed(error_class, tag=str(n))
                    self.assertFalse(dispatch._known(self.conn, self.tid, "h"), n)
                self.assertEqual({i["role"] for i in incidents.listing(self.conn)} - {"host_wall"},
                                 set())          # only the host wall's own outage incident

    def test_a_late_close_after_retry_after_never_counts(self):
        """A slow tick closes the run after its retry_after: its pause wrote nothing live, yet
        a rate limit or lost login is no failure of the run."""
        for error_class in ("rate_limit", "auth_required"):
            with self.subTest(error_class):
                db.guard_set(self.conn, "pause:board", {"until": 0}, now=0)
                for n in range(MAX_TRIES + 1):
                    row, _, _ = self.closed(error_class, tag=str(n), after=-60)
                    self.assertEqual((row["status"], row["outcome"]),
                                     ("released", "rate_limited"))
                    self.assertFalse(dispatch._known(self.conn, self.tid, "h"), n)

    def test_invalid_output_with_a_stored_pause_class_pauses_once(self):
        for error_class in ("rate_limit", "auth_required"):
            with self.subTest(error_class):
                db.guard_set(self.conn, "pause:board", {"until": 0}, now=0)
                self.conn.execute("DELETE FROM outbox")
                row, kinds, now = self.closed(error_class, prior="invalid_output")
                self.assertEqual((row["status"], row["outcome"]), ("released", "rate_limited"))
                self.assertNotIn("blind_tester_failed", kinds)
                self.assertGreater(db.guard_get(self.conn, "pause:board")[0]["until"], now)
                self.assertEqual(self.notices(), 1)

    def test_invalid_output_with_a_stored_fatal_still_fails(self):
        row, kinds, _ = self.closed("fatal", prior="invalid_output")
        self.assertEqual(row["status"], "failed")
        self.assertIn("blind_tester_failed", kinds)
        self.assertEqual(self.notices(), 0)

    def test_a_fatal_class_still_ends_failed(self):
        row, kinds, _ = self.closed("fatal")
        self.assertEqual(row["status"], "failed")
        self.assertIn("blind_tester_failed", kinds)
