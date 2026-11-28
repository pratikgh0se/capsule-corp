"""inc_789f7e81: every runaway trip names the verdict runs of the round that tripped it, and the
incident it opens attaches their spools."""
import json

from capsule import db, incidents, runaway
from capsule.tests.test_cost import BoardCase

FAIL = json.dumps({"verdict": "fail", "findings": [
    {"file": "a.py", "line": 1, "class": "c", "text": "x"}]})


class RoundEvidence(BoardCase):
    def setUp(self):
        super().setUp()
        self.cfg = {"board": "b"}
        self.tid = self.card(status="review")

    def round_(self, start, roles=("reviewer", "validator", "guide")):
        """One failed round: a completed implementer run, then one run per role."""
        impl = self.run_row(self.tid, start=start)
        self.conn.execute("UPDATE task_runs SET outcome='completed' WHERE id=?", (impl["id"],))
        ids = {r: self.run_row(self.tid, role=r, start=start, handoff=FAIL)["run_id"]
               for r in roles}
        return impl, ids

    def trip(self, impl, cfg=None):
        task = self.conn.execute("SELECT * FROM tasks WHERE id=?", (self.tid,)).fetchone()
        self.assertTrue(runaway.step(self.conn, {**self.cfg, **(cfg or {})}, task, impl, 300))
        row = self.conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind="
                                "'runaway' ORDER BY id DESC", (self.tid,)).fetchone()
        return json.loads(row[0])

    def test_a_rounds_trip_lists_the_tripping_round_verdict_spools_as_incident_evidence(self):
        incidents.from_events(self.conn, board="b", now=1)
        rounds = [self.round_(10 * n) for n in range(1, 5)]
        impl, ids = rounds[-1]
        spool = self.dir / "spool"
        self.assertEqual(self.trip(impl)["trigger"], "rounds")
        opened = incidents.from_events(self.conn, board="b", spool_root=spool, now=2)
        (evidence,) = {tuple(json.loads(incidents.get(self.conn, i)["evidence"])) for i in opened}
        want = {str(spool / r) for r in (impl["run_id"], *ids.values())}
        self.assertEqual({e for e in evidence if e.startswith(str(spool))}, want)

    def trigger_of(self, cfg, rounds=1):
        for n in range(rounds):
            impl, ids = self.round_(10 * (n + 1))
        event = self.trip(impl, {"runaway": cfg})
        self.assertEqual(sorted(event["verdict_runs"]), sorted(ids.values()))
        return event["trigger"]

    def test_an_owner_rounds_trip_holds_verdict_runs(self):
        self.assertEqual(self.trigger_of({"owner_rounds": 1}), "owner_rounds")

    def test_a_rounds_trip_holds_verdict_runs(self):
        self.assertEqual(self.trigger_of({"max_rounds": 2}, rounds=2), "rounds")

    def test_a_runs_trip_holds_verdict_runs(self):
        self.assertEqual(self.trigger_of({"floor_runs": 1}), "runs")

    def test_a_tokens_trip_holds_verdict_runs(self):
        self.run_row(self.tid, tin=10, start=1)
        self.assertEqual(self.trigger_of({"floor_tokens": 1, "floor_runs": 99}), "tokens")

    def test_a_same_class_trip_holds_verdict_runs(self):
        self.assertEqual(self.trigger_of({"same_class_rounds": 1}), "same_class")

    def test_a_role_retried_in_the_round_contributes_only_its_latest_run(self):
        impl, ids = self.round_(10)
        newer = self.run_row(self.tid, role="reviewer", start=11, handoff=FAIL)["run_id"]
        self.assertEqual(sorted(self.trip(impl, {"runaway": {"max_rounds": 1}})["verdict_runs"]),
                         sorted([newer, ids["validator"], ids["guide"]]))

    def test_a_rate_limited_run_and_a_ruling_run_do_not_appear(self):
        impl, ids = self.round_(10)
        limited = self.run_row(self.tid, role="reviewer", start=12, handoff=FAIL)
        self.conn.execute("UPDATE task_runs SET outcome='rate_limited' WHERE id=?",
                          (limited["id"],))
        ruling = self.run_row(self.tid, role="guide", start=13, handoff=FAIL)
        self.conn.execute("UPDATE task_runs SET metadata=? WHERE id=?",
                          (json.dumps({"ruling_of": 1}), ruling["id"]))
        got = self.trip(impl, {"runaway": {"max_rounds": 1}})["verdict_runs"]
        self.assertEqual(sorted(got), sorted(ids.values()))

    def test_a_round_after_a_human_accept_starts_at_the_after_run_floor(self):
        impl, old = self.round_(10)
        seen = self.run_row(self.tid, role="reviewer", start=11, handoff=FAIL)
        db.event(self.conn, self.tid, "request_review", {"after_run": seen["id"]},
                 run_pk=impl["id"], now=12)
        fresh = self.run_row(self.tid, role="validator", start=13, handoff=FAIL)["run_id"]
        got = self.trip(impl, {"runaway": {"max_rounds": 1}})["verdict_runs"]
        self.assertEqual(got, [fresh])

    def test_a_security_run_of_the_round_is_attached(self):
        impl, ids = self.round_(10, roles=("reviewer", "security"))
        event = self.trip(impl, {"runaway": {"max_rounds": 1}})
        self.assertEqual(sorted(event["verdict_runs"]), sorted(ids.values()))

    def test_a_role_with_no_run_in_the_round_adds_no_id_and_the_trip_applies(self):
        impl, ids = self.round_(10, roles=("reviewer",))
        event = self.trip(impl, {"runaway": {"max_rounds": 1}})
        self.assertEqual(event["verdict_runs"], [ids["reviewer"]])

    def test_an_event_with_no_verdict_runs_gives_the_same_evidence_as_before(self):
        incidents.from_events(self.conn, board="b", now=1)
        impl = self.run_row(self.tid, start=10)
        db.event(self.conn, self.tid, "runaway", {"trigger": "rounds", "detail": "d", "pass": True},
                 run_pk=impl["id"], run_id=impl["run_id"], now=5)
        spool = self.dir / "spool"
        (opened,) = set(incidents.from_events(self.conn, board="b", spool_root=spool, now=6))
        evidence = json.loads(incidents.get(self.conn, opened)["evidence"])
        self.assertEqual([e for e in evidence if e.startswith(str(spool))],
                         [str(spool / impl["run_id"])])
