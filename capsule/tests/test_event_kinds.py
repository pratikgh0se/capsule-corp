"""Row 569 item 1: a per-event-kind payload allowlist (`capsule/event_kinds.py`), enforced in
`db.event`. Oracle 1: the real `db.event` and real producers (`blocks.block`, `human` files through
the tick are covered by their own suites, which now run under the allowlist). Oracle 2 (Z2): an
AST walk of every `event(...)` call site in `capsule/` against the same allowlist, so a typo'd
key fails in CI even on a path no test drives."""
import ast
import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from capsule import blocks, db, event_kinds, intent, paths


def _call_sites():
    """[(file, line, kind, keys)] for every `event(conn, task, "<literal kind>", {<literal keys>})`;
    a dynamic kind or a non-dict payload is skipped here and listed in `DYNAMIC` below."""
    out = []
    for path in sorted((paths.REPO / "capsule").glob("*.py")) + sorted(
            (paths.REPO / "capsule" / "blind").glob("*.py")):   # R30: blind/dispatch.py emits too
        tree = ast.parse(path.read_text())
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call) or len(n.args) < 3:
                continue
            fn = n.func
            name = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else None
            if name != "event" or not (isinstance(n.args[2], ast.Constant) and isinstance(n.args[2].value, str)):
                continue
            payload = n.args[3] if len(n.args) > 3 else None
            if payload is None or (isinstance(payload, ast.Constant) and payload.value is None):
                out.append((path.name, n.lineno, n.args[2].value, set()))
            elif isinstance(payload, (ast.Dict, ast.IfExp)):
                dicts = [payload] if isinstance(payload, ast.Dict) else [payload.body, payload.orelse]
                keys = {k.value for d in dicts if isinstance(d, ast.Dict) for k in d.keys
                        if isinstance(k, ast.Constant)}     # a `**spread` key is None: skipped
                out.append((path.name, n.lineno, n.args[2].value, keys))
    return out


# Kinds whose call site builds the kind or the payload at run time; the real producer tests below
# and the whole suite (which runs through `db.event`) exercise them.
DYNAMIC = {"dependency_wait", "block_loop_detected", "failed", "first_screen_ok", "first_screen_fix",
           "epic_sync_skipped", "epic_push_refused", "decision_requested", "intake_ingested",
           "contract_amended", "assigned", "parked_by_human", "failure_limit", "blocked", "created",
           "worktree_mismatch", "epic_sync_conflict", "needs_input",
           "vault_context_verdict"}
# vault_context_cli builds its payload with a dict comprehension


class Allowlist(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        db.create_board(self.d / "k.db")
        self.conn = db.connect(self.d / "k.db")
        self.tid = db.new_task(self.conn, title="t")

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.d, ignore_errors=True)

    def last(self):
        return self.conn.execute("SELECT kind, payload FROM task_events ORDER BY id DESC LIMIT 1").fetchone()

    def event(self, *args, **kw):
        """db.event with stderr captured: (stderr text)."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            db.event(self.conn, self.tid, *args, **kw)
        return err.getvalue()

    def test_unknown_kind_is_named_on_stderr_and_the_row_is_still_written(self):
        said = self.event("parkd", {"by": "human"})
        self.assertIn("unknown event kind 'parkd'", said)
        self.assertEqual(self.last()[0], "parkd")      # a telemetry typo never crashes a tick pass

    def test_typo_in_a_payload_key_of_a_known_kind_is_named_on_stderr(self):
        said = self.event("parked_by_human", {"by": "human", "triger": "x"})
        self.assertRegex(said, r"'parked_by_human'.*\['triger'\]")
        self.assertEqual(json.loads(self.last()[1]), {"by": "human", "triger": "x"})

    def test_known_kind_with_allowed_keys_and_the_run_id_db_adds_is_written(self):
        payload = {"by": "human", "trigger": "x", "was": "capability"}
        self.assertEqual(self.event("parked_by_human", payload, run_id="r1"), "")
        kind, payload = self.last()
        self.assertEqual(kind, "parked_by_human")
        self.assertEqual(json.loads(payload), {"by": "human", "trigger": "x", "was": "capability", "run_id": "r1"})

    def test_a_kind_with_no_payload_is_written(self):
        self.assertEqual(self.event("request_review", None), "")
        self.assertEqual(self.last()[0], "request_review")

    def test_real_producer_blocks_block_writes_under_the_allowlist(self):
        with db.txn(self.conn):
            self.conn.execute("UPDATE tasks SET status='running' WHERE id=?", (self.tid,))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            blocks.block(self.conn, self.tid, kind="capability", reason="no tool", cause="invalid_document")
        self.assertEqual(err.getvalue(), "")
        kind, payload = self.last()
        self.assertEqual(kind, "blocked")
        self.assertEqual(set(json.loads(payload)), {"reason", "kind", "recurrences", "cause"})

    def test_problems_returns_messages_without_raising(self):
        self.assertEqual(event_kinds.problems("parked_by_human", {"by": "human"}), [])
        self.assertEqual(len(event_kinds.problems("nope", None)), 1)
        self.assertEqual(len(event_kinds.problems("parked_by_human", {"x": 1, "y": 2})), 1)


class CallSites(unittest.TestCase):
    """Oracle 2: the source text of every producer, read without running it."""

    def test_every_literal_call_site_is_inside_the_allowlist(self):
        sites = _call_sites()
        self.assertGreater(len(sites), 50)
        for file, line, kind, keys in sites:
            with self.subTest(site=f"{file}:{line}"):
                self.assertEqual(event_kinds.problems(kind, dict.fromkeys(keys)), [])

    def test_every_allowlisted_kind_has_a_producer(self):
        literal = {kind for _, _, kind, _ in _call_sites()}
        for kind in event_kinds.ALLOWED:
            with self.subTest(kind=kind):
                self.assertTrue(kind in literal or kind in DYNAMIC, f"{kind}: no call site (dead entry)")

    def test_decision_requested_allows_exactly_the_decision_v1_fields(self):
        schema = json.loads((paths.SCHEMAS / "decision.v1.json").read_text())
        self.assertEqual(event_kinds.ALLOWED["decision_requested"], frozenset(schema["properties"]))


class IntentExamples(unittest.TestCase):
    """Z1: `contracts/intent/event_payloads.jsonl`, each example run through the real check."""

    def test_every_example_matches_what_the_check_does(self):
        examples = intent.load("event_payloads")
        self.assertTrue(1 <= len(examples) <= 3)
        for ex in examples:
            doc = ex["given"]["document"]
            self.assertEqual(event_kinds.problems(doc["event"], dict.fromkeys(doc["keys"])) == [],
                             ex["expect"]["accepted"], ex["id"])
