# mutation-check: targets-predate-range b957739c9 -- names out of tests, no behaviour pinned ::
#   Spool.test_a_worker_unblock_verb_on_its_own_running_card_gets_no_notice
import json, os, tempfile, threading, time, unittest
from pathlib import Path
from unittest import mock
from capsule import blocks, db, lease, outbox, paths, spool
from capsule.jsonschema import load_all

SCHEMAS = load_all(paths.SCHEMAS)

def setup(tc=None):
    d = Path(tempfile.mkdtemp()); db.create_board(d / "k.db"); conn = db.connect(d / "k.db")
    if tc is not None:
        # R28: close every connection this helper opens (ResourceWarning), via the calling
        # test's own addCleanup. `tc` is omitted by callers that run `setup()` on a background
        # thread - a sqlite connection is thread-affine, so it must be closed from the thread
        # that made it (that caller closes it itself, in its own `finally`), never from the
        # main thread's test-cleanup machinery.
        tc.addCleanup(conn.close)
    tid = db.new_task(conn, title="t"); c = lease.claim(conn, tid, ttl=900, role="dev-frontend")
    run = conn.execute("SELECT * FROM task_runs WHERE id=?", (c.run_pk,)).fetchone()
    return conn, spool.make(d / "spool", c.run_id), run, c

def req(c, **kw):
    return dict({"schema": "request.v1", "run_id": c.run_id, "task_id": c.task_id, "claim_lock": c.claim_lock}, **kw)

class Spool(unittest.TestCase):
    def old(self, p):
        os.utime(p, (time.time() - 60,) * 2); return p

    def test_comment_applied_and_moved(self):
        conn, sp, run, c = setup(self)
        self.old(spool.write_doc(sp, "requests", req(c, verb="comment", text="hi")))
        self.assertEqual(spool.apply(conn, sp, run, role_verbs={"comment"}, schemas=SCHEMAS), ["applied"])
        self.assertEqual(conn.execute("SELECT body FROM task_comments").fetchone()[0], "hi")
        self.assertEqual(len(list((sp / "done").iterdir())), 1)

    def test_stale_lock_rejected(self):
        conn, sp, run, c = setup(self)
        self.old(spool.write_doc(sp, "requests", req(c, verb="comment", text="x", claim_lock="h:other")))
        self.assertEqual(spool.apply(conn, sp, run, role_verbs={"comment"}, schemas=SCHEMAS), ["rejected"])

    def test_verb_outside_contract_rejected(self):
        conn, sp, run, c = setup(self)
        self.old(spool.write_doc(sp, "requests", req(c, verb="reassign", assignee="x")))
        self.assertEqual(spool.apply(conn, sp, run, role_verbs={"comment"}, schemas=SCHEMAS), ["rejected"])

    def test_garbage_files_are_invalid_not_fatal(self):
        conn, sp, run, c = setup(self)
        for name, data in (("1.json", b"{not json"), ("2.json", b"\xff\xfe"), ("3.json", b"[]")):
            (sp / "requests" / name).write_bytes(data)
            self.old(sp / "requests" / name)
        (sp / "requests" / "4.json").mkdir()
        self.old(spool.write_doc(sp, "requests", req(c, verb="comment", text="ok")))
        got = spool.apply(conn, sp, run, role_verbs={"comment"}, schemas=SCHEMAS)
        self.assertEqual(got, ["invalid", "invalid", "invalid", "applied"])
        self.assertEqual(conn.execute("SELECT error_class FROM task_runs").fetchone()[0], "invalid_output")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM requests WHERE status='invalid'").fetchone()[0], 3)

    def test_links_and_fifos_are_invalid_and_never_read(self):
        """Final review I2 mounts requests/ into the implementer's container, so it can plant a
        link to a host file (or a FIFO); the tick must never read one into the board."""
        conn, sp, run, c = setup(self)
        secret = Path(tempfile.mkdtemp()) / "secret"
        secret.write_text("TOP-SECRET")
        (sp / "requests" / "1.json").symlink_to(secret)
        (sp / "requests" / "2.json").symlink_to(sp / "nowhere")
        os.mkfifo(sp / "requests" / "3.json")
        for n in (1, 2, 3):
            os.utime(sp / "requests" / f"{n}.json", (time.time() - 60,) * 2, follow_symlinks=False)
        got = spool.apply(conn, sp, run, role_verbs={"comment"}, schemas=SCHEMAS)
        self.assertEqual(got, ["invalid", "invalid", "invalid"])
        bodies = " ".join(r[0] or "" for r in conn.execute("SELECT body FROM requests"))
        self.assertNotIn("TOP-SECRET", bodies)
        self.assertEqual(sorted(p.name for p in (sp / "done").iterdir()), ["1.json", "2.json", "3.json"])

    def _apply_with_last_moment_swap(self, make):
        """Fix round 2, N1: the container wins the race - the entry is a plain, settled file when
        the tick looks and becomes `make(path)` (a link or a FIFO) right before the read, whichever
        of the old check (`Path.is_file`) or the new open (`os.open`) comes first. Returns the
        statuses and the stored request bodies; fails if `apply` blocks."""
        swapped = []

        def swap(p):
            if Path(p).name == "1.json" and Path(p).parent.name == "requests" and not swapped:
                swapped.append(Path(p))
                tmp = Path(p).with_name(".swap")
                make(tmp)
                os.replace(tmp, p)
        real_is_file, real_open = Path.is_file, os.open

        def is_file(self):
            r = real_is_file(self)
            swap(self)
            return r

        def os_open(p, *a, **kw):
            swap(p)
            return real_open(p, *a, **kw)
        out = {}

        def body():   # sqlite objects stay in the thread that made them
            conn, sp, run, c = setup()   # tc omitted: this thread closes it itself, below
            try:
                p = sp / "requests" / "1.json"
                p.write_text("{}")
                os.utime(p, (time.time() - 60,) * 2)
                out["got"] = spool.apply(conn, sp, run, role_verbs={"comment"}, schemas=SCHEMAS)
                out["bodies"] = " ".join(r[0] or "" for r in conn.execute("SELECT body FROM requests"))
            finally:
                conn.close()
        with mock.patch.object(Path, "is_file", is_file), mock.patch.object(spool.os, "open", os_open):
            t = threading.Thread(target=body, daemon=True)
            t.start()
            t.join(5)
        if t.is_alive():   # unstick the reader blocked on the FIFO before failing
            for p in swapped:
                try:
                    os.close(os.open(p, os.O_WRONLY | os.O_NONBLOCK))
                except OSError:
                    pass
            t.join(5)
            self.fail("spool.apply blocked on a FIFO swapped in after its check")
        self.assertTrue(swapped)
        return out["got"], out["bodies"]

    def test_symlink_swapped_in_before_the_read_is_never_followed(self):
        secret = Path(tempfile.mkdtemp()) / "secret"
        secret.write_text("TOP-SECRET")
        got, bodies = self._apply_with_last_moment_swap(lambda tmp: tmp.symlink_to(secret))
        self.assertEqual(got, ["invalid"])
        self.assertNotIn("TOP-SECRET", bodies)

    def test_fifo_swapped_in_before_the_read_never_blocks(self):
        got, _ = self._apply_with_last_moment_swap(os.mkfifo)
        self.assertEqual(got, ["invalid"])

    def test_archived_name_reused_as_a_directory_does_not_wedge_apply(self):
        """Fix round 2 (Minor): `done/1.json` already exists, then `requests/1.json/` arrives as a
        non-empty directory; it is archived under a fresh name, not left to raise every tick."""
        conn, sp, run, c = setup(self)
        p = sp / "requests" / "1.json"
        p.write_text("{bad")
        os.utime(p, (time.time() - 60,) * 2)
        self.assertEqual(spool.apply(conn, sp, run, role_verbs={"comment"}, schemas=SCHEMAS), ["invalid"])
        p.mkdir()
        (p / "x").write_text("x")
        os.utime(p, (time.time() - 60,) * 2)
        self.assertEqual(spool.apply(conn, sp, run, role_verbs={"comment"}, schemas=SCHEMAS), ["invalid"])
        self.assertEqual(list((sp / "requests").iterdir()), [])
        self.assertEqual(len(list((sp / "done").iterdir())), 2)

    def test_fresh_file_waits(self):
        conn, sp, run, c = setup(self)
        spool.write_doc(sp, "requests", req(c, verb="comment", text="x"))
        (sp / "requests" / ".9.tmp").write_text("{")
        self.assertEqual(spool.apply(conn, sp, run, role_verbs={"comment"}, schemas=SCHEMAS), [])

    def test_block_verb_routes(self):
        conn, sp, run, c = setup(self)
        self.old(spool.write_doc(sp, "requests", req(c, verb="block", kind="needs_input", text="which?")))
        spool.apply(conn, sp, run, role_verbs={"block"}, schemas=SCHEMAS)
        self.assertEqual(conn.execute("SELECT status FROM tasks").fetchone()[0], "blocked")

    def test_decision_event_and_outbox_row(self):
        conn, sp, run, c = setup(self)
        doc = {"schema": "decision.v1", "run_id": c.run_id, "task_id": c.task_id, "claim_lock": c.claim_lock,
               "question": "which?", "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
               "recommended": "a"}
        self.old(spool.write_doc(sp, "requests", doc))
        self.assertEqual(spool.apply(conn, sp, run, role_verbs={"comment"}, schemas=SCHEMAS), ["applied"])
        self.assertEqual(conn.execute("SELECT kind FROM task_events WHERE kind='decision_requested'").fetchone()[0],
                          "decision_requested")
        row = conn.execute("SELECT channel, thread_key, body FROM outbox").fetchone()
        self.assertEqual(row["channel"], "file")
        self.assertEqual(row["thread_key"], c.task_id)
        self.assertIn("which?", row["body"])

    def test_a_decision_on_a_slack_board_is_a_red_decide_line(self):
        conn, sp, run, c = setup()
        outbox.set_slack(conn, True)
        doc = {"schema": "decision.v1", "run_id": c.run_id, "task_id": c.task_id, "claim_lock": c.claim_lock,
               "question": "Which break length?", "options": [{"id": "a", "label": "5 min"},
                                                               {"id": "b", "label": "10 min"}],
               "recommended": "a"}
        self.old(spool.write_doc(sp, "requests", doc))
        spool.apply(conn, sp, run, role_verbs={"comment"}, schemas=SCHEMAS)
        body = conn.execute("SELECT body FROM outbox WHERE channel='slack'").fetchone()[0]
        board_name = blocks._board_name(conn)
        self.assertEqual(body, f"\U0001F534 Decide {c.task_id}: Which break length?\n"
                               "options: a) 5 min \u00b7 b) 10 min\nrecommended: a\n"
                               f"reply: `python3 -m capsule human {board_name} {c.task_id} answer 'a'`")

    def test_read_report_validates(self):
        conn, sp, run, c = setup(self)
        spool.write_doc(sp, "report", {"schema": "report.v1"})
        doc, errs = spool.read_report(sp, SCHEMAS)
        self.assertIsNone(doc); self.assertTrue(errs)

    def test_missing_verb_field_is_invalid_batch_continues(self):
        conn, sp, run, c = setup(self)
        self.old(spool.write_doc(sp, "requests", req(c, verb="comment")))  # no "text"
        self.old(spool.write_doc(sp, "requests", req(c, verb="artefact")))  # no "path"
        self.old(spool.write_doc(sp, "requests", req(c, verb="reassign")))  # no "assignee"
        self.old(spool.write_doc(sp, "requests", req(c, verb="comment", text="ok")))
        got = spool.apply(conn, sp, run, role_verbs={"comment", "artefact", "reassign"}, schemas=SCHEMAS)
        self.assertEqual(got, ["invalid", "invalid", "invalid", "applied"])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM task_comments").fetchone()[0], 1)
        self.assertEqual(len(list((sp / "done").iterdir())), 4)
        self.assertEqual(conn.execute("SELECT error_class FROM task_runs").fetchone()[0], "invalid_output")

    def test_untrack_verb_applied_and_event_recorded(self):
        """Row 563, t_d2b36dd4: before this verb existed, a worker had no way to untrack a staged
        file (an unknown-verb `reassign`-like rejection was the only outcome). `spool.py` never
        touches a workspace itself (no `ws` here) - it validates the path and records an event,
        the same shape `artefact` uses; the host `git rm --cached` is the tick's job."""
        conn, sp, run, c = setup(self)
        self.old(spool.write_doc(sp, "requests", req(c, verb="untrack", path=".hypothesis/x.gz")))
        self.assertEqual(spool.apply(conn, sp, run, role_verbs={"untrack"}, schemas=SCHEMAS), ["applied"])
        payload = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='untrack'", (c.task_id,)).fetchone()[0])
        self.assertEqual(payload["path"], ".hypothesis/x.gz")

    def test_untrack_verb_outside_contract_rejected(self):
        conn, sp, run, c = setup(self)
        self.old(spool.write_doc(sp, "requests", req(c, verb="untrack", path="x.txt")))
        self.assertEqual(spool.apply(conn, sp, run, role_verbs={"comment"}, schemas=SCHEMAS), ["rejected"])

    def test_untrack_unsafe_paths_are_invalid(self):
        """Safety rules (row 563, t_d2b36dd4): relative, inside the clone, non-root, no `..`, no
        absolute path, never under (any-case) `.git/` (C2/M1, review of t_d2b36dd4). `*` and `:/`
        are NOT here - they validate (ordinary relative names) and stay harmless only because
        `untrack` runs git with `--literal-pathspecs` (see test_tick.py::UntrackVerb)."""
        for bad in ("../escape.txt", "/etc/passwd", ".git/config", ".", ".GIT/config"):
            with self.subTest(bad):
                conn, sp, run, c = setup(self)
                self.old(spool.write_doc(sp, "requests", req(c, verb="untrack", path=bad)))
                self.assertEqual(spool.apply(conn, sp, run, role_verbs={"untrack"}, schemas=SCHEMAS), ["invalid"])

    def test_report_schema_in_requests_is_invalid(self):
        conn, sp, run, c = setup(self)
        self.old(spool.write_doc(sp, "requests", {"schema": "report.v1"}))
        got = spool.apply(conn, sp, run, role_verbs={"comment"}, schemas=SCHEMAS)
        self.assertEqual(got, ["invalid"])

    def test_settled_directory_entry_is_invalid_and_moved(self):
        conn, sp, run, c = setup(self)
        d = sp / "requests" / "1.json"
        d.mkdir()
        self.old(d)
        self.old(spool.write_doc(sp, "requests", req(c, verb="comment", text="ok")))
        got = spool.apply(conn, sp, run, role_verbs={"comment"}, schemas=SCHEMAS)
        self.assertEqual(got, ["invalid", "applied"])
        self.assertFalse(d.exists())
        self.assertTrue((sp / "done" / "1.json").is_dir())

    def test_a_worker_unblock_verb_on_its_own_running_card_gets_no_notice(self):
        """Fix round 2, item 1: pins `spool.py`'s `by="worker"` wiring on the `unblock` verb - the
        card stays `running` (unblock refuses it), the refusal event exists, but no outbox row
        reaches the owner. Flipping `spool.py:131` to `by="human"` must make this fail."""
        conn, sp, run, c = setup(self)
        self.old(spool.write_doc(sp, "requests", req(c, verb="unblock")))
        self.assertEqual(spool.apply(conn, sp, run, role_verbs={"unblock"}, schemas=SCHEMAS), ["applied"])
        self.assertEqual(conn.execute("SELECT status FROM tasks WHERE id=?", (c.task_id,)).fetchone()[0], "running")
        payload = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='unblock_refused'",
            (c.task_id,)).fetchone()[0])
        self.assertIn("running", payload["reason"])
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM outbox WHERE thread_key=?", (c.task_id,)).fetchone()[0], 0)

    def test_unblock_reassign_raise_tier_honour_target_task(self):
        conn, sp, run, c = setup(self)
        other = db.new_task(conn, title="other", risk_class="light")
        blocks.block(conn, other, kind="needs_input", reason="x")
        self.assertEqual(conn.execute("SELECT status FROM tasks WHERE id=?", (other,)).fetchone()[0], "blocked")

        self.old(spool.write_doc(sp, "requests", req(c, verb="unblock", target_task=other)))
        self.assertEqual(spool.apply(conn, sp, run, role_verbs={"unblock"}, schemas=SCHEMAS), ["applied"])
        self.assertEqual(conn.execute("SELECT status FROM tasks WHERE id=?", (other,)).fetchone()[0], "ready")
        self.assertEqual(conn.execute("SELECT status FROM tasks WHERE id=?", (c.task_id,)).fetchone()[0], "running")

        self.old(spool.write_doc(sp, "requests", req(c, verb="reassign", target_task=other, assignee="dev-x")))
        self.assertEqual(spool.apply(conn, sp, run, role_verbs={"reassign"}, schemas=SCHEMAS), ["applied"])
        self.assertEqual(conn.execute("SELECT assignee FROM tasks WHERE id=?", (other,)).fetchone()[0], "dev-x")
        self.assertIsNone(conn.execute("SELECT assignee FROM tasks WHERE id=?", (c.task_id,)).fetchone()[0])

        self.old(spool.write_doc(sp, "requests", req(c, verb="raise-tier", target_task=other)))
        self.assertEqual(spool.apply(conn, sp, run, role_verbs={"raise-tier"}, schemas=SCHEMAS), ["applied"])
        self.assertEqual(conn.execute("SELECT tier FROM tasks WHERE id=?", (other,)).fetchone()[0], "strong")


class UntrackIntentExamples(unittest.TestCase):
    """Z1/I4 (review of t_d2b36dd4): contracts/intent/untrack.jsonl replayed through `spool.apply`,
    the tick's own entry point (mirrors test_autoamend.py's/test_epic_sync.py's production replay,
    not just `intent.lint_all`'s shape check)."""

    def old(self, p):
        os.utime(p, (time.time() - 60,) * 2)
        return p

    def test_every_example_matches_its_oracle(self):
        lines = [json.loads(x) for x in (paths.INTENT / "untrack.jsonl").read_text().splitlines() if x.strip()]
        self.assertGreaterEqual(len(lines), 3)
        for ex in lines:
            with self.subTest(ex["id"]):
                conn, sp, run, c = setup(self)
                self.old(spool.write_doc(sp, "requests", req(c, **ex["given"]["document"])))
                got = spool.apply(conn, sp, run, role_verbs={"untrack"}, schemas=SCHEMAS)
                self.assertEqual(got == ["applied"], ex["expect"]["applied"], ex["id"])
