# mutation-check: targets-predate-range b957739c9 -- renames only, no behaviour pinned ::
#   AmendVerb.test_event_and_outbox_line
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capsule import amend, db, paths
from capsule.__main__ import main


def setup_board(status="ready"):
    home = Path(tempfile.mkdtemp())
    board = "sample-app"
    board_db = paths.board_db(board, home)
    db.create_board(board_db)
    conn = db.connect(board_db)
    tid = db.new_task(conn, title="card", body="old body", file_set=["a.py", "b.py"],
                       status=status, now=10)
    conn.close()
    return home, board, tid


def row(home, board, tid):
    conn = db.connect(paths.board_db(board, home))
    try:
        return conn.execute("SELECT status, body, file_set FROM tasks WHERE id=?", (tid,)).fetchone()
    finally:
        conn.close()


def events(home, board, tid, kind):
    conn = db.connect(paths.board_db(board, home))
    try:
        return conn.execute(
            "SELECT run_id, payload FROM task_events WHERE task_id=? AND kind=?", (tid, kind)).fetchall()
    finally:
        conn.close()


def outbox_rows(home, board, tid):
    conn = db.connect(paths.board_db(board, home))
    try:
        return conn.execute("SELECT body FROM outbox WHERE thread_key=?", (tid,)).fetchall()
    finally:
        conn.close()


def write_body(text="new body text"):
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False)
    f.write(text)
    f.close()
    return f.name


def add_review_run(home, board, tid, role="guide", claim_expires=None, ended_at=None):
    """A review run (validator/reviewer/guide) row: these hold no lease (tick.py), so `tasks.status`
    stays whatever it was (typically `review`), never `running`, while one is live."""
    conn = db.connect(paths.board_db(board, home))
    try:
        conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, claim_lock, claim_expires, started_at,"
            " run_id, ended_at) VALUES (?,?,?,?,?,?,?,?)",
            (tid, role, "running", f"host:{role}", claim_expires, 50, f"{role}-run", ended_at))
        conn.commit()
    finally:
        conn.close()


class AmendVerb(unittest.TestCase):
    def test_body_only(self):
        home, board, tid = setup_board()
        backups = Path(tempfile.mkdtemp())
        body_path = write_body("brand new body")
        result = amend.amend(home, board, tid, body_file=body_path, reason="fix typo",
                              now=100, backup_dir=backups)
        r = row(home, board, tid)
        self.assertEqual(r["body"], "brand new body")
        self.assertEqual(json.loads(r["file_set"]), ["a.py", "b.py"])
        self.assertIn("event_id", result)

    def test_clears_block_recurrences(self):
        """I3/t_8e00b598: an amended contract's next block is a fresh one, not a loop recurrence."""
        home, board, tid = setup_board(status="blocked")
        conn = db.connect(paths.board_db(board, home))
        conn.execute("UPDATE tasks SET block_kind='needs_input', block_recurrences=1 WHERE id=?", (tid,))
        conn.commit()
        conn.close()
        backups = Path(tempfile.mkdtemp())
        amend.amend(home, board, tid, body_file=write_body("clarified the ask"), reason="clarify",
                    now=100, backup_dir=backups)
        conn = db.connect(paths.board_db(board, home))
        self.assertEqual(conn.execute("SELECT block_recurrences FROM tasks WHERE id=?", (tid,)).fetchone()[0], 0)
        conn.close()

    def test_file_set_only(self):
        home, board, tid = setup_board()
        backups = Path(tempfile.mkdtemp())
        result = amend.amend(home, board, tid, file_set=["c.py", "d.py"], reason="scope change",
                              now=100, backup_dir=backups)
        r = row(home, board, tid)
        self.assertEqual(r["body"], "old body")
        self.assertEqual(json.loads(r["file_set"]), ["c.py", "d.py"])
        self.assertIn("event_id", result)

    def test_both(self):
        home, board, tid = setup_board()
        backups = Path(tempfile.mkdtemp())
        body_path = write_body("both changed")
        result = amend.amend(home, board, tid, body_file=body_path, file_set=["e.py"],
                              reason="rescope", now=100, backup_dir=backups)
        r = row(home, board, tid)
        self.assertEqual(r["body"], "both changed")
        self.assertEqual(json.loads(r["file_set"]), ["e.py"])
        self.assertIn("event_id", result)

    def test_running_refused(self):
        home, board, tid = setup_board(status="running")
        backups = Path(tempfile.mkdtemp())
        body_path = write_body()
        with self.assertRaises(amend.AmendRefused) as ctx:
            amend.amend(home, board, tid, body_file=body_path, reason="nope",
                        now=100, backup_dir=backups)
        self.assertIn("running", str(ctx.exception))
        r = row(home, board, tid)
        self.assertEqual(r["body"], "old body")
        self.assertEqual(list(backups.iterdir()), [])  # refused before any backup was taken

    def test_live_review_run_refused_with_a_retry_after(self):
        """I8 review finding: a review run (validator/reviewer/guide) holds no lease, so `tasks.status`
        stays `review`, never `running` - the `running` check alone would silently let an amend land
        mid-round, and the round would then be judged against a card body it never saw."""
        home, board, tid = setup_board(status="review")
        add_review_run(home, board, tid, role="guide", claim_expires=250)
        backups = Path(tempfile.mkdtemp())
        body_path = write_body()
        with self.assertRaises(amend.AmendRefused) as ctx:
            amend.amend(home, board, tid, body_file=body_path, reason="nope",
                        now=100, backup_dir=backups)
        self.assertIn("review run", str(ctx.exception))
        self.assertEqual(ctx.exception.retry_after, 150)   # claim_expires (250) - now (100)
        r = row(home, board, tid)
        self.assertEqual(r["body"], "old body")
        self.assertEqual(list(backups.iterdir()), [])  # refused before any backup was taken

    def test_live_review_run_with_no_claim_expires_falls_back_to_the_default(self):
        home, board, tid = setup_board(status="review")
        add_review_run(home, board, tid, role="validator", claim_expires=None)
        with self.assertRaises(amend.AmendRefused) as ctx:
            amend.amend(home, board, tid, body_file=write_body(), reason="nope", now=100,
                        backup_dir=Path(tempfile.mkdtemp()))
        self.assertEqual(ctx.exception.retry_after, amend.REVIEW_RETRY_AFTER_DEFAULT)

    def test_an_ended_review_run_does_not_refuse(self):
        home, board, tid = setup_board(status="review")
        add_review_run(home, board, tid, role="guide", claim_expires=250, ended_at=90)
        backups = Path(tempfile.mkdtemp())
        result = amend.amend(home, board, tid, body_file=write_body("new body"), reason="ok",
                             now=100, backup_dir=backups)
        self.assertIn("event_id", result)
        self.assertEqual(row(home, board, tid)["body"], "new body")

    def test_missing_body_file_refused(self):
        home, board, tid = setup_board()
        backups = Path(tempfile.mkdtemp())
        with self.assertRaises(amend.AmendRefused) as ctx:
            amend.amend(home, board, tid, body_file="/no/such/file.md", reason="x",
                        now=100, backup_dir=backups)
        self.assertIn("not found", str(ctx.exception))

    def test_empty_body_file_refused(self):
        home, board, tid = setup_board()
        backups = Path(tempfile.mkdtemp())
        body_path = write_body("   \n  ")
        with self.assertRaises(amend.AmendRefused) as ctx:
            amend.amend(home, board, tid, body_file=body_path, reason="x",
                        now=100, backup_dir=backups)
        self.assertIn("empty", str(ctx.exception))

    def test_neither_body_nor_file_set_refused(self):
        home, board, tid = setup_board()
        with self.assertRaises(amend.AmendRefused):
            amend.amend(home, board, tid, reason="x", now=100)

    def test_backup_created(self):
        home, board, tid = setup_board()
        backups = Path(tempfile.mkdtemp())
        body_path = write_body()
        result = amend.amend(home, board, tid, body_file=body_path, reason="x",
                              now=100, backup_dir=backups)
        files = list(backups.iterdir())
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].name.startswith(f"{board}-"))
        self.assertEqual(str(files[0]), result["backup"])
        # the backup is a real, readable copy of the board *before* the write
        old_conn = db.connect(files[0])
        try:
            pre_body = old_conn.execute("SELECT body FROM tasks WHERE id=?", (tid,)).fetchone()[0]
        finally:
            old_conn.close()
        self.assertEqual(pre_body, "old body")

    def test_event_and_outbox_line(self):
        home, board, tid = setup_board()
        backups = Path(tempfile.mkdtemp())
        body_path = write_body("changed")
        amend.amend(home, board, tid, body_file=body_path, file_set=["z.py"],
                    reason="the reason text", who="owner", now=100, backup_dir=backups)
        evs = events(home, board, tid, "contract_amended")
        self.assertEqual(len(evs), 1)
        run_id, payload_raw = evs[0]
        self.assertIsNone(run_id)
        payload = json.loads(payload_raw)
        self.assertEqual(payload["issuer"], "owner")
        self.assertEqual(payload["reason"], "the reason text")
        self.assertEqual(payload["old_file_set"], ["a.py", "b.py"])
        self.assertEqual(payload["new_file_set"], ["z.py"])
        self.assertIn("body_hash_before", payload)
        self.assertIn("body_hash_after", payload)
        self.assertNotEqual(payload["body_hash_before"], payload["body_hash_after"])

        rows = outbox_rows(home, board, tid)
        self.assertEqual(len(rows), 1)
        self.assertIn(tid, rows[0]["body"])
        self.assertIn("contract amended", rows[0]["body"])

    OLD = "Guide ruling: do ruling items 1-12.\n"
    NARROW = "Amend note 4: fix the marker. Change nothing else.\n"

    def narrowing_card(self):
        home, board, tid = setup_board()
        conn = db.connect(paths.board_db(board, home))
        conn.execute("UPDATE tasks SET body=? WHERE id=?", (self.OLD, tid))
        conn.commit()
        conn.close()
        return home, board, tid

    def test_an_appended_note_that_narrows_scope_without_a_keeps_line_is_refused(self):
        home, board, tid = self.narrowing_card()
        backups = Path(tempfile.mkdtemp())
        with self.assertRaises(amend.AmendRefused) as ctx:
            amend.amend(home, board, tid, body_file=write_body(self.OLD + self.NARROW), reason="n4",
                        now=100, backup_dir=backups)
        self.assertIn("nothing else", str(ctx.exception))
        self.assertIn("Keeps:", str(ctx.exception))
        self.assertEqual(row(home, board, tid)["body"], self.OLD)
        self.assertEqual(list(backups.iterdir()), [])
        self.assertEqual(events(home, board, tid, "contract_amended"), [])

    def test_a_narrowing_note_with_a_keeps_line_is_accepted(self):
        home, board, tid = self.narrowing_card()
        new = self.OLD + self.NARROW + "Keeps: ruling items 1-12\n"
        amend.amend(home, board, tid, body_file=write_body(new), reason="n5",
                    now=100, backup_dir=Path(tempfile.mkdtemp()))
        self.assertEqual(row(home, board, tid)["body"], new)

    def test_a_narrowing_note_with_a_supersedes_line_is_accepted(self):
        home, board, tid = self.narrowing_card()
        new = self.OLD + self.NARROW + "Supersedes: none\n"
        amend.amend(home, board, tid, body_file=write_body(new), reason="n5", now=100,
                    backup_dir=Path(tempfile.mkdtemp()))
        self.assertEqual(row(home, board, tid)["body"], new)

    def test_a_plain_appended_note_needs_no_keeps_line(self):
        home, board, tid = self.narrowing_card()
        new = self.OLD + "Note: fix the marker.\n"
        amend.amend(home, board, tid, body_file=write_body(new), reason="n", now=100,
                    backup_dir=Path(tempfile.mkdtemp()))
        self.assertEqual(row(home, board, tid)["body"], new)

    def test_every_narrowing_phrase_is_refused_without_a_keeps_line(self):
        for phrase in ("nothing else", "change only", "only these", "no other change"):
            with self.subTest(phrase=phrase):
                home, board, tid = self.narrowing_card()
                with self.assertRaises(amend.AmendRefused) as ctx:
                    amend.amend(home, board, tid, reason="n", now=100,
                                body_file=write_body(self.OLD + f"Note: {phrase}.\n"),
                                backup_dir=Path(tempfile.mkdtemp()))
                self.assertIn(phrase, str(ctx.exception))

    def test_a_narrowing_phrase_in_a_non_prefix_edit_is_found_by_line_diff(self):
        home, board, tid = self.narrowing_card()
        new = "Intro.\n" + self.OLD + "Change only the marker.\n"
        with self.assertRaises(amend.AmendRefused) as ctx:
            amend.amend(home, board, tid, body_file=write_body(new), reason="n", now=100,
                        backup_dir=Path(tempfile.mkdtemp()))
        self.assertIn("Change only", str(ctx.exception))

    def test_a_phrase_already_in_the_old_body_is_not_a_new_note(self):
        home, board, tid = setup_board()
        conn = db.connect(paths.board_db(board, home))
        conn.execute("UPDATE tasks SET body=? WHERE id=?", ("Change nothing else.\nA\n", tid))
        conn.commit()
        conn.close()
        amend.amend(home, board, tid, body_file=write_body("Change nothing else.\nB\n"), reason="n",
                    now=100, backup_dir=Path(tempfile.mkdtemp()))
        self.assertEqual(row(home, board, tid)["body"], "Change nothing else.\nB\n")

    def test_a_body_that_requires_a_host_only_check_from_the_implementer_is_refused(self):
        for line in ("Run mutation_check --base origin/main.",
                     "Run mutation_check --base=origin/main.",
                     "Paste the mutation_check run.",
                     "Run git fetch before you start.",
                     "Rebase onto origin/main first.",
                     "Must be rebased onto origin/main.",
                     "Set every marker to the origin/main sha.",
                     "Set every marker to the sha of origin/main.",
                     "Paste the precheck run in your report.",
                     "Rebasing onto origin/main is required.",
                     "Pasted precheck run required.",
                     "Set the origin/main marker sha.",
                     "Pasting the mutation_check run.",
                     "The mutation_check run must be pasted.",
                     "Do a clean origin/main rebase first.",
                     "Set markers to the `origin/main` sha.",
                     "Use origin/main's sha.",
                     "Use the origin/main commit sha.",
                     "paste a passing mutation_check --base origin/main run"):
            with self.subTest(line=line):
                home, board, tid = self.narrowing_card()
                backups = Path(tempfile.mkdtemp())
                before = row(home, board, tid)["file_set"]
                with self.assertRaises(amend.AmendRefused) as ctx:
                    amend.amend(home, board, tid, reason="n", now=100, backup_dir=backups,
                                file_set=["c.py"],
                                body_file=write_body(self.OLD + f"Fix it.\n{line}\n"))
                self.assertIn(line, str(ctx.exception))
                self.assertEqual(row(home, board, tid)["file_set"], before)
                self.assertEqual(row(home, board, tid)["body"], self.OLD)
                self.assertEqual(list(backups.iterdir()), [])
                self.assertEqual(events(home, board, tid, "contract_amended"), [])

    def test_a_host_only_line_marked_host_or_host_only_passes(self):
        for line in ("Host: run mutation_check --base origin/main.",
                     "host_only: git fetch, then rebase onto origin/main."):
            with self.subTest(line=line):
                home, board, tid = self.narrowing_card()
                new = self.OLD + line + "\n"
                amend.amend(home, board, tid, body_file=write_body(new), reason="n", now=100,
                            backup_dir=Path(tempfile.mkdtemp()))
                self.assertEqual(row(home, board, tid)["body"], new)

    def test_a_default_base_precheck_citation_passes(self):
        home, board, tid = self.narrowing_card()
        new = self.OLD + "Run `python3 bin/precheck-gates.py` before you report done.\n"
        amend.amend(home, board, tid, body_file=write_body(new), reason="n", now=100,
                    backup_dir=Path(tempfile.mkdtemp()))
        self.assertEqual(row(home, board, tid)["body"], new)

    def test_no_lint_lets_a_host_only_demand_in_and_records_it(self):
        home, board, tid = self.narrowing_card()
        amend.amend(home, board, tid, body_file=write_body(self.OLD + "Run git fetch.\n"),
                    reason="n", now=100, backup_dir=Path(tempfile.mkdtemp()), lint=False)
        (ev,) = events(home, board, tid, "contract_amended")
        self.assertIs(json.loads(ev["payload"])["lint_skipped"], True)

    def test_an_old_demand_line_kept_in_the_body_is_refused(self):
        home, board, tid = self.narrowing_card()
        old = self.OLD + "Run git fetch.\n"
        conn = db.connect(paths.board_db(board, home))
        conn.execute("UPDATE tasks SET body=? WHERE id=?", (old, tid))
        conn.commit()
        conn.close()
        with self.assertRaises(amend.AmendRefused) as ctx:
            amend.amend(home, board, tid, body_file=write_body(old + "Plain note.\n"), reason="n",
                        now=100, backup_dir=Path(tempfile.mkdtemp()))
        self.assertIn("Run git fetch.", str(ctx.exception))

    def test_a_file_set_only_amend_skips_the_lint(self):
        home, board, tid = self.narrowing_card()
        conn = db.connect(paths.board_db(board, home))
        conn.execute("UPDATE tasks SET body=? WHERE id=?", (self.OLD + "Run git fetch.\n", tid))
        conn.commit()
        conn.close()
        amend.amend(home, board, tid, file_set=["c.py"], reason="r", now=100,
                    backup_dir=Path(tempfile.mkdtemp()))
        self.assertEqual(json.loads(row(home, board, tid)["file_set"]), ["c.py"])

    def test_no_lint_records_the_skip_in_the_event(self):
        home, board, tid = self.narrowing_card()
        amend.amend(home, board, tid, body_file=write_body(self.OLD + self.NARROW), reason="n4",
                    now=100, backup_dir=Path(tempfile.mkdtemp()), lint=False)
        (ev,) = events(home, board, tid, "contract_amended")
        self.assertIs(json.loads(ev["payload"])["lint_skipped"], True)

    def test_an_indented_keeps_line_does_not_count(self):
        home, board, tid = self.narrowing_card()
        with self.assertRaises(amend.AmendRefused):
            amend.amend(home, board, tid, reason="n", now=100,
                        body_file=write_body(self.OLD + self.NARROW + "  Keeps: none\n"),
                        backup_dir=Path(tempfile.mkdtemp()))

    def test_a_null_old_body_is_amended_without_a_traceback(self):
        home, board, tid = setup_board()
        conn = db.connect(paths.board_db(board, home))
        conn.execute("UPDATE tasks SET body=NULL WHERE id=?", (tid,))
        conn.commit()
        conn.close()
        amend.amend(home, board, tid, body_file=write_body("plain"), reason="n", now=100,
                    backup_dir=Path(tempfile.mkdtemp()))
        self.assertEqual(row(home, board, tid)["body"], "plain")

    def test_a_linted_amend_has_no_lint_skipped_key(self):
        home, board, tid = setup_board()
        amend.amend(home, board, tid, body_file=write_body("plain"), reason="n", now=100,
                    backup_dir=Path(tempfile.mkdtemp()))
        (ev,) = events(home, board, tid, "contract_amended")
        self.assertNotIn("lint_skipped", json.loads(ev["payload"]))


class AmendCLI(unittest.TestCase):
    """Test-honesty audit item 6: `python3 -m capsule human <board> <task> amend ...` (capsule/__main__.py)
    - argument parsing, the refusal exit code, and the success path, all under a temp home and backup dir."""

    def setUp(self):
        self.home, self.board, self.tid = setup_board()
        self.backups = Path(tempfile.mkdtemp())

    def cli(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        argv = ["human", self.board, self.tid, "amend", "the reason", "--author", "cos",
                "--capsule-home", str(self.home), *extra]
        with mock.patch.dict("os.environ", {"CAPSULE_BACKUP_DIR": str(self.backups)}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_file_set_is_split_on_commas_and_replaces_the_list(self):
        rc, out, err = self.cli("--file-set", "c.py,d/e.py")
        self.assertEqual((rc, err), (0, ""))
        r = row(self.home, self.board, self.tid)
        self.assertEqual(json.loads(r["file_set"]), ["c.py", "d/e.py"])
        self.assertEqual(r["body"], "old body")
        result = json.loads(out)
        self.assertEqual(Path(result["backup"]).parent, self.backups)
        self.assertTrue(Path(result["backup"]).is_file())
        (ev,) = events(self.home, self.board, self.tid, "contract_amended")
        payload = json.loads(ev["payload"])
        self.assertEqual((payload["issuer"], payload["reason"]), ("cos", "the reason"))
        self.assertEqual(payload["new_file_set"], ["c.py", "d/e.py"])

    def test_body_file_replaces_the_body_and_keeps_the_file_set(self):
        rc, out, err = self.cli("--body-file", write_body("cli body"))
        self.assertEqual((rc, err), (0, ""))
        r = row(self.home, self.board, self.tid)
        self.assertEqual((r["body"], json.loads(r["file_set"])), ("cli body", ["a.py", "b.py"]))
        self.assertIn("event_id", json.loads(out))

    def assert_refused(self, rc, out, err, fragment):
        self.assertEqual((rc, out), (1, ""))
        self.assertTrue(err.startswith("capsule human amend: "), err)
        self.assertIn(fragment, err)
        r = row(self.home, self.board, self.tid)
        self.assertEqual((r["body"], json.loads(r["file_set"])), ("old body", ["a.py", "b.py"]))
        self.assertEqual(events(self.home, self.board, self.tid, "contract_amended"), [])
        self.assertEqual(list(self.backups.iterdir()), [])     # refused before the backup

    def test_a_missing_body_file_exits_1_with_no_write(self):
        missing = str(self.backups.parent / "no-such-body.md")
        self.assert_refused(*self.cli("--body-file", missing, "--file-set", "x.py"), f"--body-file {missing}: not found")

    def test_an_empty_body_file_exits_1_with_no_write(self):
        self.assert_refused(*self.cli("--body-file", write_body("  \n")), ": empty")

    def test_a_narrowing_note_without_a_keeps_line_exits_1_with_no_write(self):
        body = write_body("old body\nChange nothing else.\n")
        self.assert_refused(*self.cli("--body-file", body), "Keeps:")

    def test_no_lint_flag_lets_the_narrowing_note_in(self):
        body = write_body("old body\nChange nothing else.\n")
        rc, out, err = self.cli("--body-file", body, "--no-lint")
        self.assertEqual((rc, err), (0, ""))
        (ev,) = events(self.home, self.board, self.tid, "contract_amended")
        self.assertTrue(json.loads(ev["payload"])["lint_skipped"])

    def test_neither_flag_exits_1(self):
        self.assert_refused(*self.cli(), "nothing to amend")


if __name__ == "__main__":
    unittest.main()
