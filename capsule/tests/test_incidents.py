# mutation-check: targets-predate-range b957739c9 -- fixture rename only, no behaviour pinned ::
#   FromEvents.test_cos_routine_nudges_open_no_incident_but_owner_and_cos_override_do,
#   FromEvents.test_cos_unblock_of_a_korin_fix_card_hold_opens_no_incident,
#   FromEvents.test_cos_unblock_of_a_lander_wait_opens_no_incident_but_a_faked_or_stale_one_does,
#   FromEvents.test_cos_unblock_of_a_routed_korin_fix_card_opens_no_incident
"""Korin's record and closure gate (row 456): capsule/incidents.py. Temp boards and a temp vault only."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from capsule import db, incident_attach, incidents, paths, routine_nudges
from capsule.tests.fixtures.board_ddl import board_ddl

DAY = 86400
T0 = 1_790_000_000 - 1_790_000_000 % DAY + 3600      # 01:00 UTC on some day


def good_doc(incident_id, **over):
    doc = {"schema": "postmortem.v1", "run_id": "0" * 32, "task_id": "t_0000aaaa", "claim_lock": "h:x",
           "incident_id": incident_id,
           "root_cause": "The worker exited before writing run.json because the adapter raised on an empty reply.",
           "lesson": "An adapter error must still end in a run.json.",
           "mitigation": "Wrap the adapter call; write run.json in a finally block.",
           "enforcer": "capsule/worker.py run_one finally-block, tested by the recurrence test",
           "recurrence_test": "capsule.tests.test_worker.RunJson.test_written_on_adapter_error",
           "fix_card": {"title": "worker: run.json on adapter error", "body": "Write run.json even when the adapter raises."},
           "decision_row_text": f"| 457 | Incident {incident_id}: worker crash | ... | run.json in finally |",
           "confidence": "high"}
    doc.update(over)
    return doc


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.board = self.dir / "kanban.db"
        db.create_board(self.board)
        self.conn = db.connect(self.board)
        self.addCleanup(self.conn.close)
        self.vault = self.dir / "vault"
        (self.vault / "Knowledge" / "Incidents").mkdir(parents=True)

    def open(self, kind="crash", key="implementer: fatal: boom", now=T0, **kw):
        return incidents.open_incident(self.conn, board="b", kind=kind, key=key, evidence=kw.pop("evidence", ["event:1"]),
                                       now=now, **kw)


class Fingerprint(Base):
    def test_same_cause_on_another_run_hashes_the_same(self):
        a = incidents.fingerprint("crash", "implementer: fatal: run 1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d died on t_1234abcd line 42")
        b = incidents.fingerprint("crash", "Implementer:  fatal: run ffffffffffffffffffffffffffff0000 died on t_9999eeee line 7")
        self.assertEqual(a, b)
        self.assertEqual(len(a), 16)

    def test_kind_and_cause_both_count(self):
        self.assertNotEqual(incidents.fingerprint("crash", "x"), incidents.fingerprint("hang", "x"))
        self.assertNotEqual(incidents.fingerprint("crash", "reviewer: timed out"),
                            incidents.fingerprint("crash", "validator: timed out"))
        with self.assertRaises(ValueError):
            incidents.fingerprint("meltdown", "x")


class OpenRule(Base):
    def test_same_day_dedupes_and_attaches_the_evidence(self):
        a, act_a = self.open(evidence=["event:1"])
        b, act_b = self.open(now=T0 + 3600, evidence=["event:2"])
        self.assertEqual((a, act_a, act_b), (b, "opened", "deduped"))
        self.assertEqual(json.loads(incidents.get(self.conn, a)["evidence"]), ["event:1", "event:2"])
        # oracle 2: the table itself holds one row
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0], 1)

    def test_next_day_opens_a_new_incident(self):
        a, _ = self.open()
        b, act = self.open(now=T0 + DAY)
        self.assertNotEqual(a, b)
        self.assertEqual(act, "opened")

    def test_human_interventions_and_overrides_are_one_per_event(self):
        a, _ = self.open(kind="human_intervention", key="reset: t_0000aaaa")
        b, act = self.open(kind="human_intervention", key="reset: t_0000aaaa", now=T0 + 60)
        self.assertNotEqual(a, b)
        self.assertEqual(act, "opened")
        c, _ = self.open(kind="override", key="override: make test")
        d, _ = self.open(kind="override", key="override: make test", now=T0 + 60)
        self.assertNotEqual(c, d)

    def test_closed_within_30_days_opens_a_repeat_linked_to_the_original(self):
        a, _ = self.open()
        self.conn.execute("UPDATE incidents SET status='closed', closed_at=? WHERE id=?", (T0 + DAY, a))
        b, act = self.open(now=T0 + 10 * DAY)
        rec = incidents.get(self.conn, b)
        self.assertEqual((act, rec["kind"], rec["repeat_of"]), ("repeat", "repeat", a))
        self.assertEqual(rec["fingerprint"], incidents.get(self.conn, a)["fingerprint"])
        lines = [r[0] for r in self.conn.execute("SELECT body FROM outbox WHERE channel='file'")]
        self.assertTrue(any("REPEAT" in line and a in line for line in lines), lines)

    def test_closed_longer_ago_or_still_open_is_not_a_repeat(self):
        a, _ = self.open()
        self.conn.execute("UPDATE incidents SET status='closed', closed_at=? WHERE id=?", (T0, a))
        _, act = self.open(now=T0 + 31 * DAY)
        self.assertEqual(act, "opened")
        c, _ = self.open(kind="hang", key="reviewer: timed out")
        _, act = self.open(kind="hang", key="reviewer: timed out", now=T0 + 2 * DAY)
        self.assertEqual(act, "opened")                  # c is open, not closed: a new incident, not a repeat

    def test_repeat_is_never_opened_by_a_caller(self):
        with self.assertRaises(ValueError):
            self.open(kind="repeat")


class FromEvents(Base):
    def setUp(self):
        super().setUp()
        self.tid = db.new_task(self.conn, title="card", assignee="dev-frontend", branch="epic/x")

    def ev(self, kind, payload, task=None):
        db.event(self.conn, task or self.tid, kind, payload, now=T0)

    def test_a_board_without_a_watermark_only_gets_one(self):
        self.conn.execute("DELETE FROM guard_state WHERE key=?", (incidents.WATERMARK,))
        self.ev("timed_out", {"role": "implementer"})
        self.assertEqual(incidents.from_events(self.conn, board="b", now=T0), [])
        self.assertEqual(incidents.listing(self.conn), [])

    def test_each_typed_event_opens_its_kind(self):
        incidents.from_events(self.conn, board="b", now=T0)
        self.ev("crashed", {"role": "implementer", "error_class": "fatal", "error": "boom", "run_id": "a" * 32})
        self.ev("timed_out", {"role": "reviewer"})
        self.ev("blocked", {"kind": "review_infra", "reason": "review run failed twice"})
        self.ev("blocked", {"kind": "needs_input", "reason": "a question"})      # not an incident
        self.ev("contradiction", {"role": "reviewer", "examples": [{"id": "pass-with-findings"}]})
        self.ev("intervention", {"kind": "reset", "file": "1.json", "src": "1.json"})
        self.ev("intervention", {"kind": "reset", "file": "2.json", "src": "2.json"})
        self.ev("intervention", {"kind": "override", "file": "3.json", "src": "3.json"})
        self.ev("override", {"check": "make test"})
        self.ev("tick_error", {"error": f"capsule: tick error on {self.tid}: KeyError: 'x'"})
        spool = self.dir / "spool"
        ids = incidents.from_events(self.conn, board="b", spool_root=spool, human_dir=self.dir / "human", now=T0)
        kinds = sorted(incidents.get(self.conn, i)["kind"] for i in set(ids))
        self.assertEqual(kinds, ["contradiction", "crash", "crash", "hang", "human_intervention", "human_intervention",
                                 "override", "silent_failure"])
        crash = next(r for r in incidents.listing(self.conn) if r["run_id"])
        self.assertIn(str(spool / ("a" * 32)), json.loads(crash["evidence"]))
        human = sorted(e for r in incidents.listing(self.conn, "open") if r["kind"] == "human_intervention"
                       for e in json.loads(r["evidence"]) if not e.startswith("event:"))
        self.assertEqual(human, [str(self.dir / "human" / "done" / f"{n}.json") for n in (1, 2)])
        # oracle 2: every incident with a task left an incident_opened event on it
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM task_events WHERE kind='incident_opened'").fetchone()[0],
                         len(ids))
        self.assertEqual(incidents.from_events(self.conn, board="b", now=T0), [])       # watermark moved

    def test_cos_routine_nudges_open_no_incident_but_owner_and_cos_override_do(self):
        incidents.from_events(self.conn, board="b", now=T0)
        for n, kind in enumerate(incidents.ROUTINE_COS_KINDS):      # includes reset and amend
            self.ev("intervention", {"kind": kind, "file": f"c{n}.json",
                                     "src": f"c{n}.json", "author": "cos"})
        self.assertEqual(incidents.from_events(self.conn, board="b", now=T0), [])
        count = self.conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE kind='intervention'").fetchone()[0]
        self.assertEqual(count, len(incidents.ROUTINE_COS_KINDS))    # the events are still recorded
        self.ev("intervention", {"kind": "reset", "file": "p.json", "src": "p.json",
                                 "author": "owner"})
        self.ev("intervention", {"kind": "override", "file": "o.json", "src": "o.json",
                                 "author": "cos"})
        kinds = sorted(incidents.get(self.conn, i)["kind"]
                       for i in set(incidents.from_events(self.conn, board="b", now=T0)))
        self.assertEqual(kinds, ["human_intervention"])
        self.ev("override", {"check": "make test", "issuer": "cos"})
        kinds = sorted(incidents.get(self.conn, i)["kind"]
                       for i in set(incidents.from_events(self.conn, board="b", now=T0)))
        self.assertEqual(kinds, ["override"])

    def test_cos_unblock_of_a_korin_fix_card_hold_opens_no_incident(self):
        """inc_b8de7b6c: `file_fix_card` holds every fix card on purpose; the CoS unblock that
        routes it is the planned step. Only that case is routine: the owner or another hold
        counts."""
        incidents.from_events(self.conn, board="b", now=T0)
        hold = {"kind": "needs_input", "incident": "inc_x",
                "reason": "Korin fix card for incident inc_x: unblock it to dispatch"}
        def unblock(who, n):
            return {"kind": "unblock", "file": f"u{n}.json", "src": f"u{n}.json", "author": who}

        def kinds():
            ids = incidents.from_events(self.conn, board="b", now=T0)
            return [incidents.get(self.conn, i)["kind"] for i in set(ids)]

        self.ev("blocked", hold)
        self.ev("intervention", unblock("cos", 1))
        self.assertEqual(incidents.from_events(self.conn, board="b", now=T0), [])
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE kind='intervention'").fetchone()[0], 1)
        self.ev("blocked", hold)
        self.ev("intervention", unblock("owner", 2))
        self.assertEqual(kinds(), ["human_intervention"])
        self.ev("blocked", {"kind": "needs_input", "reason": "which branch?"})
        self.ev("intervention", unblock("cos", 3))
        self.assertEqual(kinds(), ["human_intervention"])
        # a lift ends the Korin hold: a later cos unblock is an ordinary human action
        for n, lift in enumerate(("unblocked", "failure_reset", "parked_by_human"), 4):
            fresh = db.new_task(self.conn, title=lift, assignee="dev-frontend", branch="epic/x")
            self.ev("blocked", hold, task=fresh)
            self.ev(lift, {}, task=fresh)
            self.ev("intervention", unblock("cos", n), task=fresh)
            self.assertEqual(kinds(), ["human_intervention"], lift)

    def test_cos_unblock_of_a_lander_wait_opens_no_incident_but_a_faked_or_stale_one_does(self):
        incidents.from_events(self.conn, board="b", now=T0)
        text = "landing check gates: x"

        def unblocked(who, n, cause, reason="lander_wait_env", later=None):
            task = db.new_task(self.conn, title=str(n), assignee="dev", branch="epic/x")
            self.ev("blocked", {"kind": "transient", "reason": f"{reason}: {text}",
                                **({"cause": cause} if cause else {})}, task=task)
            if later:
                self.ev(later, {}, task=task)
            self.ev("intervention", {"kind": "unblock", "file": f"w{n}.json", "src": f"w{n}.json",
                                     "author": who}, task=task)
            ids = incidents.from_events(self.conn, board="b", now=T0)
            return [incidents.get(self.conn, i)["kind"] for i in set(ids)]

        self.assertEqual(unblocked("cos", 1, "lander_wait_env"), [])
        self.assertEqual(unblocked("cos", 2, "lander_wait_docker"), [])
        human = ["human_intervention"]
        self.assertEqual(unblocked("owner", 3, "lander_wait_env"), human)
        self.assertEqual(unblocked("cos", 4, "lander_wait_rebase"), human)
        # a worker's `block` verb writes free text and no cause: the words prove nothing
        self.assertEqual(unblocked("cos", 5, None), human)
        # a later hold outranks the wait, even one that writes no `blocked` event
        for n, later in ((6, "contradiction_loop"), (7, "failure_limit")):   # open their own too
            self.assertIn("human_intervention", unblocked("cos", n, "lander_wait_env", later=later))

    def test_cos_unblock_of_a_routed_korin_fix_card_opens_no_incident(self):
        """inc_85fada36: a `fix_card_routed` event between the hold and the unblock keeps the
        match, and `is_routine_nudge` uses the same predicate as the classifier."""
        incidents.from_events(self.conn, board="b", now=T0)
        hold = {"kind": "needs_input", "incident": "inc_x",
                "reason": "Korin fix card for incident inc_x: unblock it to dispatch"}

        def unblock(who, n, hold_payload, task):
            self.ev("blocked", hold_payload, task=task)
            self.ev("fix_card_routed", {"incident": "inc_x"}, task=task)
            self.ev("intervention", {"kind": "unblock", "file": f"u{n}.json",
                                     "src": f"u{n}.json", "author": who}, task=task)
            return self.conn.execute("SELECT MAX(id) FROM task_events").fetchone()[0]

        def opened(eid, task):
            inc, _ = incidents.open_incident(self.conn, board="b", kind="human_intervention",
                                             key=f"unblock: {eid}", evidence=[f"event:{eid}"],
                                             task_id=task, now=T0)
            return incidents.get(self.conn, inc)

        cos = unblock("cos", 1, hold, self.tid)
        self.assertEqual(incidents.from_events(self.conn, board="b", now=T0), [])
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE kind='intervention'").fetchone()[0], 1)
        self.assertTrue(routine_nudges.is_routine_nudge(self.conn, opened(cos, self.tid)))
        owner_task = db.new_task(self.conn, title="p", assignee="dev-frontend", branch="epic/x")
        owner_hold = unblock("owner", 2, hold, owner_task)
        ids = incidents.from_events(self.conn, board="b", now=T0)
        self.assertEqual([incidents.get(self.conn, i)["kind"] for i in set(ids)],
                         ["human_intervention"])
        self.assertFalse(routine_nudges.is_routine_nudge(self.conn, opened(owner_hold, owner_task)))
        plain_task = db.new_task(self.conn, title="q", assignee="dev-frontend", branch="epic/x")
        unblock("cos", 3, {"kind": "needs_input", "reason": "which branch?"}, plain_task)
        ids = incidents.from_events(self.conn, board="b", now=T0)
        self.assertEqual([incidents.get(self.conn, i)["kind"] for i in set(ids)],
                         ["human_intervention"])

    def test_invalid_document_and_cause_tagged_review_infra_open_their_own_kinds(self):
        """Row 507(b) fix round 1 (card t_67a95702): an implementer's `invalid_document` block
        opens its own kind (never `review_infra`, which means a review role); a review role's
        `review_infra` block opens `review_infra` ONLY when its payload is tagged
        `cause: invalid_document`, else the broader `silent_failure` it always got."""
        incidents.from_events(self.conn, board="b", now=T0)
        self.ev("blocked", {"kind": "invalid_document", "reason": "run x: #/options/0/label: longer than 300"})
        self.ev("blocked", {"kind": "review_infra", "reason": "run y: bad decision.v1", "cause": "invalid_document"})
        self.ev("blocked", {"kind": "review_infra", "reason": "review run failed twice"})
        ids = incidents.from_events(self.conn, board="b", now=T0)
        kinds = sorted(incidents.get(self.conn, i)["kind"] for i in set(ids))
        self.assertEqual(kinds, ["invalid_document", "review_infra", "silent_failure"])

    def test_a_deferred_pending_contradiction_opens_nothing_only_the_routed_one_does(self):
        """I2 (ruling race fix, tick.py `_contradiction`): a contradiction found while a sibling
        review run is still live is recorded once as `routed: "pending"` and again, once routed,
        as `routed: "guide"`. Only the routed event may open an incident - else one contradiction
        opens two (the pending one and its later twin), possibly straddling midnight so the
        same-day dedupe never catches it."""
        incidents.from_events(self.conn, board="b", now=T0)
        self.ev("contradiction", {"role": "reviewer", "examples": [{"id": "pass-with-findings"}],
                                  "routed": "pending"})
        self.assertEqual(incidents.from_events(self.conn, board="b", now=T0), [])
        self.assertEqual(incidents.listing(self.conn), [])
        self.ev("contradiction", {"role": "reviewer", "examples": [{"id": "pass-with-findings"}],
                                  "routed": "guide"})
        ids = incidents.from_events(self.conn, board="b", now=T0 + DAY)   # next day: no dedupe hiding a second open
        self.assertEqual(len(ids), 1)
        self.assertEqual(incidents.listing(self.conn)[0]["kind"], "contradiction")

    def test_worktree_mismatch_opens_a_crash_incident(self):
        """Row 465 (t_685fd87b, review I4): the typed `worktree_mismatch` event (tick.Tick._close)
        must reach Korin like any other run failure, not disappear silently."""
        incidents.from_events(self.conn, board="b", now=T0)
        self.ev("worktree_mismatch", {"role": "implementer",
                                      "error": "assigned workspace /tmp/wrong is not a worktree of the repo"})
        ids = incidents.from_events(self.conn, board="b", now=T0)
        self.assertEqual(len(ids), 1)
        inc = incidents.get(self.conn, ids[0])
        self.assertEqual(inc["kind"], "crash")
        self.assertIn("worktree_mismatch", inc["root_key"])
        self.assertEqual(inc["role"], "implementer")

    def test_contradiction_loop_opens_its_own_incident(self):
        """t_99136481 (card t_0086cfd4): tick.py `_apply_ruling`'s loop-cap branch used to leave
        a TODO instead of ever calling this module; `contradiction_loop` now opens a `contradiction`
        incident whose fingerprint (the `loop:` key) never dedupes with the plain contradiction
        that came before it."""
        incidents.from_events(self.conn, board="b", now=T0)
        self.ev("contradiction", {"role": "reviewer", "examples": [{"id": "fail-without-findings"}]})
        plain_id, = incidents.from_events(self.conn, board="b", now=T0)
        self.ev("contradiction_loop", {"role": "reviewer", "examples": ["fail-without-findings"], "resets": 2,
                                       "reason": "the fail is wrong"})
        ids = incidents.from_events(self.conn, board="b", now=T0)
        self.assertEqual(len(ids), 1)
        loop_id = ids[0]
        self.assertNotEqual(loop_id, plain_id)
        inc = incidents.get(self.conn, loop_id)
        self.assertEqual(inc["kind"], "contradiction")
        self.assertEqual(inc["role"], "reviewer")
        self.assertIn("loop", inc["root_key"])

    def test_events_on_korin_cards_open_nothing(self):
        incidents.from_events(self.conn, board="b", now=T0)
        korin = db.new_task(self.conn, title="Korin", assignee="korin", status="todo")
        self.ev("timed_out", {"role": "korin"}, task=korin)
        self.assertEqual(incidents.from_events(self.conn, board="b", now=T0), [])


class Postmortem(Base):
    def test_a_good_document_has_no_problems(self):
        a, _ = self.open()
        self.assertEqual(incidents.postmortem_problems(good_doc(a), incidents.get(self.conn, a)), [])

    def test_wrong_incident_bad_test_id_and_row_text_without_the_id_are_refused(self):
        a, _ = self.open()
        inc = incidents.get(self.conn, a)
        probs = incidents.postmortem_problems(good_doc("inc_ffffffff", recurrence_test="run the tests",
                                                       decision_row_text="| 457 | a crash | x |"), inc)
        self.assertEqual(len(probs), 3, probs)

    def test_intent_examples_are_live(self):
        """contracts/intent/korin.jsonl is the second oracle after the schema: each example, applied to
        a document that is otherwise good, is refused; its expect side is accepted."""
        examples = incidents.intent_examples()
        self.assertEqual(len(examples), 10)
        self.assertEqual({ex["id"] for ex in examples},
                          {"bare-review-is-not-an-enforcer", "a-brief-line-is-not-an-enforcer",
                           "repeat-needs-confidence", "escaped-bug-needs-confidence",
                           "unowned-idle-twice-in-a-day-is-an-incident",
                           "watch-needs-low-or-medium-confidence",
                           "watch-not-allowed-for-repeat", "watch-not-allowed-for-escaped-bug",
                           "reworded-review-is-not-an-enforcer",
                           "reworded-alert-fired-is-not-a-root-cause"})
        for ex in examples:
            with self.subTest(ex["id"]):
                kind = ((ex["given"].get("inputs") or {}).get("incident") or {}).get("kind", "crash")
                if kind == "repeat":
                    a, _ = self.open()
                    self.conn.execute("UPDATE incidents SET status='closed', closed_at=? WHERE id=?", (T0, a))
                    a, _ = self.open(now=T0 + DAY)
                else:
                    a, _ = self.open(kind=kind, key=ex["id"])
                inc = incidents.get(self.conn, a)
                bad = good_doc(a, **{k: v for k, v in ex["given"]["document"].items() if k != "schema"})
                self.assertTrue(any(ex["id"] in p for p in incidents.postmortem_problems(bad, inc)), ex["id"])
                self.assertEqual(incidents.postmortem_problems(good_doc(a), inc), [])

    def test_note_is_ai_first(self):
        a, _ = self.open()
        inc = incidents.get(self.conn, a)
        text = incidents.render_note(inc, good_doc(a), "t_0000bbbb", now=T0)
        self.assertTrue(text.startswith("---\ntype: incident\n"))
        for part in ("## For future agent", "[[Capsule Corp - Zenkai Rules]]", f"incident: {a}", "ai-first: true",
                     "## Root cause", "## Evidence", "event:1"):
            self.assertIn(part, text)
        self.assertEqual(incidents.note_rel(inc), f"Knowledge/Incidents/{_date(T0)} - crash - {a}.md")

    def test_test_hint_and_texts_follow_the_id_form(self):
        a, _ = self.open()
        inc = incidents.get(self.conn, a)
        forms = (
            ("capsule.tests.test_x.Case.test_y",
             "`python3 -m unittest capsule.tests.test_x.Case.test_y` passes"),
            ("tests/test_x.sh", "`bash tests/test_x.sh` passes"),
            ("tests/test_x.sh::label",
             "`bash tests/test_x.sh` passes with a `PASS label` line"))
        for test_id, hint in forms:
            with self.subTest(test_id):
                self.assertEqual(incidents._test_hint(test_id), hint)
                doc = good_doc(a, recurrence_test=test_id)
                self.assertIn(hint, incidents.fix_card_body(inc, doc))
                note = incidents.render_note(inc, doc, "t_0000bbbb", now=T0)
                self.assertIn(f"{hint} on main", note)
                if test_id.startswith("tests/"):
                    self.assertNotIn("python3 -m unittest tests/", note)


def _date(ts):
    import time
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


class Gate(Base):
    def setUp(self):
        super().setUp()
        self.inc, _ = self.open()
        self.card = db.new_task(self.conn, title="fix", status="blocked")
        self.tests = []

    def run_test(self, test_id, checkout, timeout):
        self.tests.append(test_id)
        return self.test_ok, "recurrence test failed on main"

    def fill(self):
        doc = good_doc(self.inc)
        rel = incidents.note_rel(incidents.get(self.conn, self.inc))
        (self.vault / rel).write_text(incidents.render_note(incidents.get(self.conn, self.inc), doc, self.card, now=T0))
        incidents.apply_postmortem(self.conn, self.inc, doc, fix_card=self.card, note_path=rel, now=T0)

    def check_new(self, test_id, checkout, open_sha):
        return self.new_ok, "recurrence test is not new on main"

    new_ok = True

    def close(self):
        return incidents.close(self.conn, self.inc, vault=self.vault, run_test=self.run_test,
                               check_new=self.check_new, now=T0 + 10)

    def test_an_open_incident_is_refused_with_every_gap_listed(self):
        self.test_ok = True
        with self.assertRaises(incidents.CloseRefused) as cm:
            self.close()
        text = "; ".join(cm.exception.missing)
        for part in ("not fix_pending", "record fields empty", "no fix card", "decision_row not set", "note missing",
                     "no recurrence test"):
            self.assertIn(part, text)

    def test_each_check_on_its_own_blocks_the_close(self):
        self.fill()
        self.test_ok = True
        with self.assertRaises(incidents.CloseRefused) as cm:
            self.close()
        self.assertEqual(len(cm.exception.missing), 2, cm.exception.missing)     # card not done, no row
        self.conn.execute("UPDATE tasks SET status='done' WHERE id=?", (self.card,))
        incidents.set_decision_row(self.conn, self.inc, 457)
        with self.assertRaises(incidents.CloseRefused) as cm:
            self.close()
        self.assertEqual(len(cm.exception.missing), 2)
        self.assertIn("completed event no", cm.exception.missing[0])   # status alone is one oracle, not two
        db.event(self.conn, self.card, "completed", {"commit": "abc"}, now=T0)
        self.assertIn("decisions note not found", cm.exception.missing[-1])        # I5: one proof is not enough
        decisions = self.vault / incidents.DEFAULT_DECISIONS_NOTE
        decisions.parent.mkdir(parents=True)
        decisions.write_text(f"| 457 | Incident {self.inc}: worker crash | x |\n")
        self.test_ok = False
        with self.assertRaises(incidents.CloseRefused) as cm:
            self.close()
        self.assertEqual(cm.exception.missing, ["recurrence test failed on main"])
        self.test_ok, self.new_ok = True, False
        with self.assertRaises(incidents.CloseRefused) as cm:
            self.close()
        self.assertEqual(cm.exception.missing, ["recurrence test is not new on main"])   # I4
        self.new_ok = True
        decisions.write_text("| 457 | Incident inc_other | x |\n")
        with self.assertRaises(incidents.CloseRefused) as cm:
            self.close()
        self.assertIn("decision row 457", cm.exception.missing[0])
        decisions.write_text(f"| 457 | Incident {self.inc}: worker crash | x |\n")
        rec = self.close()
        self.assertEqual((rec["status"], rec["closed_at"]), ("closed", T0 + 10))
        self.assertEqual(self.tests[-1], "capsule.tests.test_worker.RunJson.test_written_on_adapter_error")

    def test_a_note_that_does_not_name_the_incident_is_missing(self):
        self.fill()
        (self.vault / incidents.get(self.conn, self.inc)["note_path"]).write_text("some other note")
        self.test_ok = True
        with self.assertRaises(incidents.CloseRefused) as cm:
            self.close()
        self.assertTrue(any("does not name" in m for m in cm.exception.missing))


class Settings(unittest.TestCase):
    def test_defaults_keep_korin_off(self):
        s = incidents.settings({})
        self.assertFalse(s["enabled"])
        self.assertEqual(s["main_checkout"], paths.REPO)
        self.assertEqual(s["max_running"], 1)


if __name__ == "__main__":
    unittest.main()


class FixRound1(Base):
    """Review fix round 1 (I1, I2, I4, I5, I6-adjacent, I7 at module level)."""

    def test_i1_different_causes_do_not_collide(self):
        fp = incidents.fingerprint
        self.assertNotEqual(fp("crash", "KeyError: 'cafe1'", role="implementer"),
                            fp("crash", "KeyError: 'bead2'", role="implementer"))
        self.assertNotEqual(fp("crash", "fatal: exit 1: boom", role="implementer"),
                            fp("crash", "fatal: exit 137: boom", role="implementer"))
        self.assertNotEqual(fp("hang", "timed out", role="reviewer"), fp("hang", "timed out", role="validator"))
        self.assertNotEqual(fp("human_intervention", "reset: t_1a2b3c4d"), fp("human_intervention", "reset: t_9f8e7d6c"))
        self.assertNotEqual(fp("override", "make test: t_1a2b3c4d"), fp("override", "make test: t_9f8e7d6c"))

    def test_i1_the_same_cause_across_runs_and_tempdirs_matches(self):
        fp = incidents.fingerprint
        a = fp("crash", "fatal: exit 1: run 0123456789abcdef0123456789abcdef on t_1a2b3c4d:"
                        " FileNotFoundError: /private/var/folders/_l/jd5t82/T/tmpk3j9x_q/job.json pid 4411",
               role="implementer")
        b = fp("crash", "fatal: exit 1: run fedcba9876543210fedcba9876543210 on t_9f8e7d6c:"
                        " FileNotFoundError: /private/var/folders/_l/jd5t82/T/tmpzz0w_ab/job.json pid 97",
               role="implementer")
        self.assertEqual(a, b)
        self.assertEqual(fp("crash", "at /tmp/tmpk3j9x_q/x.py"), fp("crash", "at /tmp/tmpzz0w_ab1/x.py"))

    def test_i2_migrate_sets_the_watermark_so_only_later_events_open(self):
        import sqlite3
        p = self.dir / "old.db"
        raw = sqlite3.connect(p)
        raw.executescript(board_ddl())
        raw.execute("INSERT INTO tasks (id, title, status, created_at) VALUES ('t_0000aaaa', 'x', 'ready', 1)")
        raw.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES"
                    " ('t_0000aaaa', 'timed_out', '{\"role\": \"implementer\"}', 1)")
        raw.commit()
        raw.close()
        conn = db.connect(p)
        self.addCleanup(conn.close)
        db.migrate(conn)
        db.event(conn, "t_0000aaaa", "timed_out", {"role": "reviewer"}, now=T0)
        ids = incidents.from_events(conn, board="b", now=T0)          # the first pass after deploy
        self.assertEqual([incidents.get(conn, i)["kind"] for i in ids], ["hang"])
        self.assertEqual(len(incidents.listing(conn)), 1)            # the pre-migration event opened nothing

    def test_m1_a_same_day_recurrence_after_a_close_is_a_repeat(self):
        a, _ = self.open()
        self.conn.execute("UPDATE incidents SET status='closed', closed_at=? WHERE id=?", (T0 + 60, a))
        b, act = self.open(now=T0 + 120)
        self.assertEqual((act, incidents.get(self.conn, b)["repeat_of"]), ("repeat", a))


class CrossBoardAttach(Base):
    """`incidents attach`: the fix card may live on another board, read read-only (U2)."""

    def setUp(self):
        super().setUp()
        self.home = self.dir / "home"
        self.mine = paths.board_db("b", self.home)
        self.other = paths.board_db("factory", self.home)
        for p in (self.mine, self.other):
            p.parent.mkdir(parents=True)
            db.create_board(p)
        self.conn.close()
        self.conn = db.connect(self.mine)
        self.oconn = db.connect(self.other)
        self.addCleanup(self.oconn.close)
        self.inc, _ = self.open()

    def attach(self, card, **kw):
        return incident_attach.attach(self.conn, self.inc, card, board="b", home=self.home, **kw)

    def test_other_board_card_is_stored_with_its_board_and_never_written(self):
        card = db.new_task(self.oconn, title="factory fix", status="ready")
        before = self.oconn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]
        self.assertEqual(self.attach(f"factory:{card}"), f"factory:{card}")
        self.assertEqual(incidents.get(self.conn, self.inc)["fix_card"], f"factory:{card}")
        rec = incidents.get(self.conn, self.inc)
        self.assertEqual(rec["status"], "open")     # Korin still runs
        count = self.oconn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]
        self.assertEqual(count, before)

    def test_gate_reads_the_other_boards_card_status_and_completed_event(self):
        card = db.new_task(self.oconn, title="factory fix", status="ready")
        self.attach(f"factory:{card}")
        inc = incidents.get(self.conn, self.inc)
        text = "; ".join(incidents.missing(self.conn, inc, vault=self.vault, home=self.home))
        self.assertIn("fix card factory:" + card + " is not done (status ready", text)
        self.oconn.execute("UPDATE tasks SET status='done' WHERE id=?", (card,))
        status = incidents.fix_card_status
        self.assertEqual(status(self.conn, self.home, f"factory:{card}"), ("done", False))
        db.event(self.oconn, card, "completed", {"commit": "abc"}, now=T0)
        self.assertEqual(status(self.conn, self.home, f"factory:{card}"), ("done", True))
        text = "; ".join(incidents.missing(self.conn, inc, vault=self.vault, home=self.home))
        self.assertNotIn("is not done", text)

    def test_a_closed_or_superseded_incident_refuses_attach(self):
        card = db.new_task(self.oconn, title="factory fix", status="ready")
        for status in ("closed", "superseded"):
            self.conn.execute("UPDATE incidents SET status=? WHERE id=?", (status, self.inc))
            with self.assertRaisesRegex(ValueError, f"already {status}"):
                self.attach(f"factory:{card}")
        self.assertIsNone(incidents.get(self.conn, self.inc)["fix_card"])

    def test_unknown_card_or_board_is_refused(self):
        with self.assertRaises(LookupError):
            self.attach("factory:t_00000000")
        with self.assertRaises(LookupError):
            self.attach("nowhere:t_00000000")
        with self.assertRaises(ValueError):
            self.attach("factory/t_00000000")
        self.assertIsNone(incidents.get(self.conn, self.inc)["fix_card"])

    def test_same_board_card_gets_an_event_and_the_own_board_prefix_is_dropped(self):
        card = db.new_task(self.conn, title="fix", status="blocked")
        self.assertEqual(self.attach(f"b:{card}"), card)
        ev = self.conn.execute("SELECT kind FROM task_events WHERE task_id=?", (card,)).fetchall()
        self.assertIn("fix_card_attached", [r["kind"] for r in ev])

    def test_open_moves_to_fix_pending_only_when_the_record_is_filled(self):
        card = db.new_task(self.conn, title="fix", status="blocked")
        doc = good_doc(self.inc)
        rel = incidents.note_rel(incidents.get(self.conn, self.inc))
        incidents.apply_postmortem(self.conn, self.inc, doc, fix_card=card, note_path=rel, now=T0)
        self.conn.execute("UPDATE incidents SET status='open' WHERE id=?", (self.inc,))
        new = db.new_task(self.oconn, title="real fix", status="ready")
        self.attach(f"factory:{new}")
        self.assertEqual(incidents.get(self.conn, self.inc)["status"], "fix_pending")

    def test_apply_postmortem_keeps_an_attached_card(self):
        card = db.new_task(self.oconn, title="real fix", status="ready")
        self.attach(f"factory:{card}")
        auto = db.new_task(self.conn, title="auto", status="blocked")
        rel = incidents.note_rel(incidents.get(self.conn, self.inc))
        incidents.apply_postmortem(self.conn, self.inc, good_doc(self.inc), fix_card=auto,
                                   note_path=rel, now=T0)
        rec = incidents.get(self.conn, self.inc)
        self.assertEqual(rec["fix_card"], f"factory:{card}")
        self.assertEqual(rec["status"], "fix_pending")

    def test_file_fix_card_keeps_an_attached_card_and_files_none(self):
        card = db.new_task(self.oconn, title="real fix", status="ready")
        self.attach(f"factory:{card}")
        inc = incidents.get(self.conn, self.inc)
        before = self.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        fix = incidents.file_fix_card(self.conn, {}, inc, good_doc(self.inc), None, None, T0)
        self.assertEqual(fix, f"factory:{card}")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], before)

    def test_swapped_out_card_is_parked_not_left_unblockable(self):
        auto = db.new_task(self.conn, title="auto", status="blocked")
        self.conn.execute("UPDATE tasks SET block_kind='needs_input' WHERE id=?", (auto,))
        self.conn.execute("UPDATE incidents SET fix_card=? WHERE id=?", (auto, self.inc))
        new = db.new_task(self.oconn, title="real fix", status="ready")
        self.attach(f"factory:{new}")
        row = self.conn.execute("SELECT status, block_kind FROM tasks WHERE id=?",
                                (auto,)).fetchone()
        self.assertEqual((row["status"], row["block_kind"]), ("blocked", "parked"))

    def test_replace_only_a_blocked_card_with_no_runs(self):
        auto = db.new_task(self.conn, title="auto", status="blocked")
        self.conn.execute("UPDATE incidents SET fix_card=? WHERE id=?", (auto, self.inc))
        new = db.new_task(self.oconn, title="real fix", status="ready")
        self.assertEqual(self.attach(f"factory:{new}"), f"factory:{new}")
        # a cross-board card is never swapped out by attach
        other = db.new_task(self.oconn, title="again", status="ready")
        with self.assertRaises(ValueError):
            self.attach(f"factory:{other}")
        # a card that ran, or is no longer blocked, is kept
        ran = db.new_task(self.conn, title="ran", status="blocked")
        self.conn.execute("UPDATE incidents SET fix_card=? WHERE id=?", (ran, self.inc))
        self.conn.execute("INSERT INTO task_runs (task_id, started_at, status)"
                          " VALUES (?, ?, 'failed')", (ran, T0))
        with self.assertRaises(ValueError):
            self.attach(f"factory:{other}")
        self.assertEqual(incidents.get(self.conn, self.inc)["fix_card"], ran)

    def test_fixcard_refuses_a_board_form_value(self):
        card = db.new_task(self.oconn, title="factory fix", status="ready")
        self.attach(f"factory:{card}")
        with self.assertRaises(ValueError) as cm:
            incidents.route_fix_card(self.conn, {"epics": ["e"]}, self.inc, epic="e")
        self.assertIn("own board", str(cm.exception))


class AlertFiring(Base):
    """Row 470: a Grafana alert (e.g. `UnownedWorkIdle`) that fires twice within 24h opens one
    incident; a lone firing, or a repeat more than 24h later, only remembers it. Row 472 follow-up
    (I3): `record_alert_firing` returns `open_incident`'s own action ("opened"/"deduped"/"repeat"),
    never a fixed "incident_opened" - a repeat that lands on an incident already open (whether via
    open_incident's own same-day rule or this function's own any-still-open check below) is
    "deduped", not a second opening."""

    def test_first_firing_only_records_no_incident(self):
        action = incidents.record_alert_firing(self.conn, {"korin": {"enabled": True}},
                                               alert="UnownedWorkIdle", key="unowned_work_idle",
                                               board="b", now=T0)
        self.assertEqual(action, "recorded")
        self.assertEqual(incidents.listing(self.conn), [])

    def test_second_firing_within_24h_opens_an_incident(self):
        incidents.record_alert_firing(self.conn, {"korin": {"enabled": True}}, alert="UnownedWorkIdle",
                                      key="unowned_work_idle", board="b", now=T0)
        action = incidents.record_alert_firing(self.conn, {"korin": {"enabled": True}}, alert="UnownedWorkIdle",
                                                key="unowned_work_idle", board="b", now=T0 + 3600)
        self.assertEqual(action, "opened")
        rows = incidents.listing(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "silent_failure")
        self.assertIn("unowned_work_idle", rows[0]["root_key"])

    def test_evidence_and_run_id_flow_through_to_the_opened_incident(self):
        incidents.record_alert_firing(self.conn, {"korin": {"enabled": True}}, alert="UnownedWorkIdle",
                                      key="unowned_work_idle", board="b", now=T0)
        incidents.record_alert_firing(self.conn, {"korin": {"enabled": True}}, alert="UnownedWorkIdle",
                                      key="unowned_work_idle", board="b", now=T0 + 60,
                                      evidence=["unowned:3", "last_dispatch_at:100"], run_id="r" * 32)
        rows = incidents.listing(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["run_id"], "r" * 32)
        self.assertEqual(json.loads(rows[0]["evidence"]), ["unowned:3", "last_dispatch_at:100"])

    def test_a_repeat_while_the_incident_is_still_open_attaches_evidence_instead_of_reopening(self):
        incidents.record_alert_firing(self.conn, {"korin": {"enabled": True}}, alert="UnownedWorkIdle",
                                      key="unowned_work_idle", board="b", now=T0)
        incidents.record_alert_firing(self.conn, {"korin": {"enabled": True}}, alert="UnownedWorkIdle",
                                      key="unowned_work_idle", board="b", now=T0 + 60,
                                      evidence=["first:evidence"])
        action = incidents.record_alert_firing(self.conn, {"korin": {"enabled": True}}, alert="UnownedWorkIdle",
                                               key="unowned_work_idle", board="b", now=T0 + 120,
                                               evidence=["second:evidence"])
        self.assertEqual(action, "deduped")
        rows = incidents.listing(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0]["evidence"]), ["first:evidence", "second:evidence"])

    def test_second_firing_crossing_utc_midnight_still_dedupes_to_one_incident(self):
        """Row 472 (I3): `open_incident`'s own dedupe is same-UTC-day only; a still-open incident
        must not become two just because the third firing lands on the NEXT UTC calendar day
        (mirrors the reviewer's 22:00/23:30/01:00 example)."""
        t1, t2, t3 = T0 - 10800, T0 - 5400, T0     # 22:00, 23:30 (day D-1), then 01:00 (day D)
        incidents.record_alert_firing(self.conn, {"korin": {"enabled": True}}, alert="UnownedWorkIdle",
                                      key="unowned_work_idle", board="b", now=t1)
        action2 = incidents.record_alert_firing(self.conn, {"korin": {"enabled": True}}, alert="UnownedWorkIdle",
                                                 key="unowned_work_idle", board="b", now=t2)
        action3 = incidents.record_alert_firing(self.conn, {"korin": {"enabled": True}}, alert="UnownedWorkIdle",
                                                 key="unowned_work_idle", board="b", now=t3)
        self.assertEqual(action2, "opened")
        self.assertEqual(action3, "deduped")
        self.assertEqual(len(incidents.listing(self.conn)), 1)

    def test_second_firing_after_24h_only_records(self):
        incidents.record_alert_firing(self.conn, {"korin": {"enabled": True}}, alert="UnownedWorkIdle",
                                      key="unowned_work_idle", board="b", now=T0)
        action = incidents.record_alert_firing(self.conn, {"korin": {"enabled": True}}, alert="UnownedWorkIdle",
                                                key="unowned_work_idle", board="b", now=T0 + 86401)
        self.assertEqual(action, "recorded")
        self.assertEqual(incidents.listing(self.conn), [])

    def test_second_firing_with_korin_disabled_opens_nothing(self):
        incidents.record_alert_firing(self.conn, {}, alert="UnownedWorkIdle", key="unowned_work_idle",
                                      board="b", now=T0)
        action = incidents.record_alert_firing(self.conn, {}, alert="UnownedWorkIdle", key="unowned_work_idle",
                                                board="b", now=T0 + 60)
        self.assertEqual(action, "korin_disabled")
        self.assertEqual(incidents.listing(self.conn), [])

    def test_a_different_alert_name_has_its_own_independent_memory(self):
        incidents.record_alert_firing(self.conn, {"korin": {"enabled": True}}, alert="UnownedWorkIdle",
                                      key="unowned_work_idle", board="b", now=T0)
        action = incidents.record_alert_firing(self.conn, {"korin": {"enabled": True}}, alert="SomeOtherAlert",
                                                key="other", board="b", now=T0 + 60)
        self.assertEqual(action, "recorded")
        self.assertEqual(incidents.listing(self.conn), [])

    def test_third_firing_right_after_the_second_does_not_double_open(self):
        incidents.record_alert_firing(self.conn, {"korin": {"enabled": True}}, alert="UnownedWorkIdle",
                                      key="unowned_work_idle", board="b", now=T0)
        incidents.record_alert_firing(self.conn, {"korin": {"enabled": True}}, alert="UnownedWorkIdle",
                                      key="unowned_work_idle", board="b", now=T0 + 60)
        action = incidents.record_alert_firing(self.conn, {"korin": {"enabled": True}}, alert="UnownedWorkIdle",
                                                key="unowned_work_idle", board="b", now=T0 + 120)
        # dedupe (this function's own still-open check, row 472 I3) attaches to the same
        # incident, not a new one - and says so honestly ("deduped"), never "incident_opened"
        self.assertEqual(action, "deduped")
        self.assertEqual(len(incidents.listing(self.conn)), 1)
