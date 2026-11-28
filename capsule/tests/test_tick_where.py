"""A `tick_error` event names where the exception was raised (`where`: repo-relative file:line and
function, no message text), so a crash whose text is withheld for a blind run is still traceable."""
import json
from unittest import mock

from capsule import epic_sync, incident_inputs, lease, probe_detail, review_scope, workspace
from capsule.tests import test_tick


def raises_type_error():
    raise TypeError("quotes a blind row")


def parses_bad_json():
    json.loads("{not json")


class TickErrorWhere(test_tick.TickCase):
    def record(self):
        counts = {"tick_errors": 0}
        self.tick._guarded(self.conn, self.cfg, self.tid, 1, counts, raises_type_error)
        row = self.conn.execute(
            "SELECT payload FROM task_events WHERE kind='tick_error'").fetchone()
        return json.loads(row[0])

    def test_where_names_the_raising_function_by_repo_relative_path(self):
        payload = self.record()
        self.assertRegex(payload["where"],
                         r"^capsule/tests/test_tick_where\.py:\d+ raises_type_error$")
        self.assertIn("quotes a blind row", payload["error"])

    def test_the_text_stays_withheld_for_a_run_with_blind_rows(self):
        self.conn.execute("INSERT INTO task_runs (task_id, profile, status, run_id, started_at)"
                          " VALUES (?, 'validator', 'done', 'r1', 1)", (self.tid,))
        self.conn.commit()
        with mock.patch.object(incident_inputs, "_blind_run", return_value=True):
            payload = self.record()
        self.assertIn(incident_inputs.WITHHELD_TEXT, payload["error"])
        self.assertNotIn("quotes a blind row", json.dumps(payload))
        self.assertIn("raises_type_error", payload["where"])

    def test_a_stdlib_raise_site_is_skipped_for_the_capsule_caller(self):
        try:
            parses_bad_json()
        except ValueError as exc:
            where = probe_detail.where(exc)
        self.assertRegex(where, r"^capsule/tests/test_tick_where\.py:\d+ parses_bad_json$")

    def test_a_trace_wholly_outside_the_repo_gives_a_basename_only(self):
        with mock.patch.object(probe_detail, "ROOT", "/nonexistent-root"):
            try:
                raises_type_error()
            except TypeError as exc:
                where = probe_detail.where(exc)
        self.assertRegex(where, r"^test_tick_where\.py:\d+ raises_type_error$")
        self.assertNotIn("/", where)


class StartShaWithoutMetadata(test_tick.TickCase):
    """`Tick._start_sha` json.loads'd a NULL `metadata` (TypeError out of json/__init__.py on a
    card whose first implementer run row carries none)."""
    def add(self, metadata):
        self.conn.execute("INSERT INTO task_runs (task_id, profile, status, metadata, started_at)"
                          " VALUES (?, 'implementer', 'done', ?, 1)", (self.tid, metadata))
        self.conn.commit()

    def test_a_run_with_no_metadata_is_skipped_for_the_next_that_pinned_a_sha(self):
        self.conn.execute("DELETE FROM task_runs WHERE task_id=?", (self.tid,))
        self.add(None)
        self.add(json.dumps({"start_sha": "abc123"}))
        self.assertEqual(self.tick._start_sha(self.conn, self.tid), "abc123")

    def test_no_pinned_sha_is_a_plain_error_not_a_type_error(self):
        self.conn.execute("DELETE FROM task_runs WHERE task_id=?", (self.tid,))
        self.add(None)
        with self.assertRaisesRegex(RuntimeError, "no implementer run"):
            self.tick._start_sha(self.conn, self.tid)


class PinnedStartSha(test_tick.TickCase):
    """The claim writes the run's metadata in its own transaction; every reader takes the earliest
    run that pinned a `start_sha`."""
    def test_the_claimed_run_row_is_never_without_its_metadata(self):
        self.conn.execute("DELETE FROM task_runs WHERE task_id=?", (self.tid,))
        self.conn.execute("UPDATE tasks SET status='ready', claim_lock=NULL, current_run_id=NULL"
                          " WHERE id=?", (self.tid,))
        self.conn.commit()
        meta = json.dumps({"start_sha": "def456"})
        c = lease.claim(self.conn, self.tid, ttl=60, role="implementer", now=5, metadata=meta)
        row = self.conn.execute("SELECT metadata FROM task_runs WHERE id=?", (c.run_pk,)).fetchone()
        self.assertEqual(row[0], meta)

    def test_half_merge_check_goes_on_with_no_start_when_no_sha_is_pinned(self):
        def none_pinned(conn, task_id):
            raise RuntimeError("no implementer run recorded a start_sha")
        task = self.conn.execute("SELECT * FROM tasks WHERE id=?", (self.tid,)).fetchone()
        with mock.patch.object(epic_sync, "_half_merge", return_value="") as half:
            held = epic_sync.hold_half_merge(self.conn, task, "/ws", None, 1, none_pinned)
        self.assertIsNone(held)
        half.assert_called_once_with("/ws", None)

    def test_open_round_touched_uses_the_first_run_that_pinned_a_sha(self):
        self.conn.execute("DELETE FROM task_runs WHERE task_id=?", (self.tid,))
        for meta in (None, json.dumps({"start_sha": "abc123"})):
            self.conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, metadata, started_at)"
                " VALUES (?, 'implementer', 'done', ?, 1)", (self.tid, meta))
        self.conn.commit()
        with mock.patch.object(workspace, "changed", return_value=["a.py"]) as changed:
            self.assertEqual(review_scope.open_round_touched(self.conn, self.tid, "/ws"), ["a.py"])
        changed.assert_called_once_with("/ws", "abc123")
