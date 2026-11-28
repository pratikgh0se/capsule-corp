# mutation-check: targets-predate-range b957739c9 -- owner-word rename, no behaviour pinned ::
#   Offered.test_a_request_offers_the_two_choices_and_other_contradictions_do_not
"""The guide's ruling on a tester's `red_exempt` list (`capsule/blind/exempt.py`), driven through
`red.step` as in test_blind_red_tick: a listed test that passes on the base, under a statement no
ruling names, files one guide ruling request for the round and the round waits; the guide's
`red-exempt` or `not-exempt` is applied and the round is checked again. Z2 oracles: the card's row,
the request card, the events and the blind repo's `main`."""
import unittest

from capsule import bundles, db
from capsule.blind import brief, exempt, red
from capsule.tests.test_blind_red import exempting
from capsule.tests.test_blind_red_tick import PASSING, RedCase

PASSES = "passes-on-base"


class Request(RedCase):
    def listed(self, covered=None, source=PASSING, ids=None):
        ids = [f"{self.card}.test_greets"] if ids is None else ids
        self.make_round(source, mutate=exempting(self.card, ids), covered=covered)

    def requests(self):
        return self.conn.execute(
            "SELECT t.* FROM tasks t JOIN task_links l ON l.child_id=t.id WHERE l.parent_id=?"
            " AND t.request_needs='role:guide'", (self.card,)).fetchall()

    def contradictions(self):
        return self.events("contradiction")

    def rule(self, choice, reason="because"):
        """The guide's ruling on the filed request, applied as the tick applies it."""
        ev = self.conn.execute("SELECT id FROM task_events WHERE kind='contradiction'"
                               " AND task_id=? ORDER BY id", (self.card,)).fetchone()[0]
        self.conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, started_at, run_id) VALUES"
            " (?,?,?,?,?)", (self.card, "guide", "done", 3, "g" * 32))
        guide = self.conn.execute("SELECT * FROM task_runs WHERE profile='guide'").fetchone()
        exempt.applier(self.conn, ev, None)(self.conn, self.cfg, guide, {
            "recommended": choice, "context": reason, "question": "q"}, ev, 200)


class Filing(Request):
    def test_a_listed_passing_test_files_one_request_and_the_card_goes_to_the_guide(self):
        self.listed()
        self.step()
        card = self.card_row()
        self.assertEqual((card["status"], card["block_kind"]), ("triage", "guide_ruling"))
        ask, = self.requests()
        for want in ("statement 1", "the first thing works", "statement 2",
                     "the second thing works", f"{self.card}.test_greets", "blind-tester run"):
            self.assertIn(want, ask["body"])
        self.assertNotIn("hello", ask["body"])                      # never test source
        ev, = self.contradictions()
        self.assertEqual((ev["role"], ev["routed"], ev["ruling_card"]),
                         ("blind-tester", "guide", ask["id"]))
        self.assertEqual([e["id"] for e in ev["examples"]], ["statement 1", "statement 2"])
        self.assertEqual((self.events("blind_landed"), self.events("blind_red_checked")), ([], []))

    def test_a_round_files_at_most_one_request_across_passes_and_restarts(self):
        self.listed()
        self.step()
        execs = self.box.execs
        self.step()
        self.step()
        self.assertEqual((len(self.requests()), len(self.contradictions()), self.box.execs),
                         (1, 1, execs))
        self.conn.execute("UPDATE tasks SET status='ready', block_kind=NULL WHERE id=?",
                          (self.card,))                    # a person reset the card: no new ask
        self.step()
        self.assertEqual((len(self.requests()), len(self.contradictions())), (1, 1))
        self.assertEqual(self.events("blind_red_checked")[0]["verdict"], "bounced")

    def test_a_listed_test_under_a_ruled_statement_only_asks_about_the_unruled_one(self):
        self.listed()
        db.event(self.conn, self.card, "guide_ruling", {
            "role": "blind-tester", "choice": "red-exempt", "applied": True,
            "reason": "statement 1", "card_hash": self.hash}, now=1)
        self.step()
        ev, = self.contradictions()
        self.assertEqual([e["id"] for e in ev["examples"]], ["statement 2"])

    def test_unlisted_passing_tests_file_nothing(self):
        self.make_round(PASSING)
        self.step()
        self.assertEqual((self.requests(), self.contradictions(), self.card_row()["status"]),
                         ([], [], "ready"))
        self.assertEqual(self.events("blind_red_checked")[0]["outcomes"],
                         {f"{self.card}.test_greets": PASSES})

    def test_a_listed_test_whose_statement_is_not_in_the_spec_files_nothing(self):
        test = f"{self.card}.test_greets"
        self.listed(covered=[{"statement": 3, "tests": [test]}])
        self.step()
        self.assertEqual((self.requests(), self.contradictions()), ([], []))
        self.assertEqual(self.events("blind_red_checked")[0]["outcomes"], {test: PASSES})

    def test_a_listed_test_covering_no_statement_files_nothing(self):
        self.listed(covered=[{"statement": 1, "tests": []}], ids=[f"{self.card}.test_greets"])
        self.step()
        self.assertEqual((self.requests(), self.contradictions()), ([], []))

    def test_a_card_that_is_running_defers_the_round_until_it_can_be_moved(self):
        self.conn.execute("UPDATE tasks SET status='running' WHERE id=?", (self.card,))
        self.listed()
        self.step()
        self.step()
        self.assertEqual((self.requests(), self.contradictions(), self.card_row()["status"]),
                         ([], [], "running"))
        self.assertEqual((self.events("blind_red_checked"), self.box.execs), ([], 0))
        self.conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (self.card,))
        self.step()
        self.assertEqual(len(self.requests()), 1)
        self.assertEqual(self.card_row()["status"], "triage")
        self.assertEqual(self.events("blind_red_checked"), [])

    def test_a_card_that_moves_during_the_base_runs_is_not_bounced_nor_filed_and_is_deferred(self):
        from capsule.tests.test_blind_red_tick import RedBox
        self.listed()
        self.step(RedBox(after=lambda ws: self.conn.execute(
            "UPDATE tasks SET status='running' WHERE id=?", (self.card,))))
        self.assertEqual((self.requests(), self.contradictions(), self.events(
            "blind_red_checked")), ([], [], []))
        runs = self.box.execs
        self.step()
        self.assertEqual(self.box.execs, runs)              # deferred: no base run again
        self.conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (self.card,))
        self.step(RedBox())
        self.assertEqual((len(self.requests()), self.card_row()["status"]), (1, "triage"))

    def test_a_card_in_review_waits_for_its_live_reviewer_then_files(self):
        self.conn.execute("UPDATE tasks SET status='review' WHERE id=?", (self.card,))
        self.conn.execute("INSERT INTO task_runs (task_id, profile, status, started_at, run_id)"
                          " VALUES (?,?,?,?,?)", (self.card, "reviewer", "running", 1, "r" * 32))
        self.listed()
        self.step()
        self.assertEqual((self.requests(), self.card_row()["status"], self.events(
            "blind_red_checked")), ([], "review", []))
        self.conn.execute("UPDATE task_runs SET ended_at=5 WHERE profile='reviewer'")
        self.step()
        self.assertEqual((len(self.requests()), self.card_row()["status"]), (1, "triage"))

    def test_a_card_in_review_waits_for_its_live_security_run(self):
        self.conn.execute("UPDATE tasks SET status='review' WHERE id=?", (self.card,))
        self.conn.execute("INSERT INTO task_runs (task_id, profile, status, started_at, run_id)"
                          " VALUES (?,?,?,?,?)", (self.card, "security", "running", 1, "s" * 32))
        self.assertFalse(exempt.movable(self.conn, self.card_row()))
        self.conn.execute("UPDATE task_runs SET ended_at=5 WHERE profile='security'")
        self.assertTrue(exempt.movable(self.conn, self.card_row()))

    def test_off_files_nothing(self):
        self.listed()
        self.cfg["redesign"]["blind_tests"] = "off"
        self.step()
        self.assertEqual((self.requests(), self.contradictions(), self.box.execs), ([], [], 0))


class Ruled(Request):
    def test_an_applied_red_exempt_marks_the_tests_ok_when_the_round_is_next_processed(self):
        self.listed()
        self.step()
        self.rule("red-exempt")
        card = self.card_row()
        self.assertEqual((card["status"], card["block_kind"]), ("ready", None))
        ruling, = self.events("guide_ruling")
        self.assertEqual((ruling["choice"], ruling["role"], ruling["applied"]),
                         ("red-exempt", "blind-tester", True))
        self.assertEqual(red._exempt_statements(self.conn, self.card), {1, 2})
        self.assertEqual(self.requests()[0]["status"], "done")
        self.step()
        landed, = self.events("blind_landed")
        self.assertEqual(landed["exempt"], [1, 2])
        self.assertEqual(self.card_row()["blind_commit"], self.main())
        self.assertEqual(len(self.requests()), 1)

    def test_an_applied_not_exempt_counts_them_as_passing_on_base_and_asks_nothing_more(self):
        self.listed()
        self.step()
        self.rule("not-exempt")
        self.assertEqual(self.card_row()["status"], "ready")
        self.step()
        got, = self.events("blind_red_checked")
        self.assertEqual((got["verdict"], got["outcomes"]),
                         ("bounced", {f"{self.card}.test_greets": PASSES}))
        self.assertEqual((len(self.requests()), len(self.contradictions()), self.events(
            "blind_landed")), (1, 1, []))
        self.assertEqual(red._exempt_statements(self.conn, self.card), set())

    def test_a_card_in_review_goes_back_to_review(self):
        self.conn.execute("UPDATE tasks SET status='review' WHERE id=?", (self.card,))
        self.listed()
        self.step()
        self.assertEqual(self.card_row()["status"], "triage")
        self.rule("not-exempt")
        self.assertEqual(self.card_row()["status"], "review")

    def test_a_ruling_text_naming_an_unasked_statement_exempts_only_the_asked_ones(self):
        test = f"{self.card}.test_greets"
        self.listed(covered=[{"statement": 1, "tests": [test]}, {"statement": 2, "tests": []}])
        self.step()
        self.rule("red-exempt", "also statement 2 and statement 5, whatever")
        ruling, = self.events("guide_ruling")
        self.assertIn("statement 5", ruling["text"])
        self.assertEqual(red._exempt_statements(self.conn, self.card), {1})
        self.step()
        self.assertEqual(self.events("blind_landed")[0]["exempt"], [1])

    def test_a_ruling_is_void_once_the_acceptance_text_changes(self):
        self.listed()
        self.step()
        self.rule("red-exempt")
        self.assertEqual(red._exempt_statements(self.conn, self.card), {1, 2})
        self.conn.execute("UPDATE tasks SET body=? WHERE id=?", (
            "Goal.\n\nAcceptance:\n- the second thing works\n- the first thing works\n",
            self.card))
        self.assertEqual(red._exempt_statements(self.conn, self.card), set())

    def test_a_ruling_with_no_card_hash_exempts_nothing(self):
        db.event(self.conn, self.card, "guide_ruling", {
            "role": "blind-tester", "choice": "red-exempt", "applied": True,
            "reason": "statement 1"}, now=1)
        self.assertEqual(red._exempt_statements(self.conn, self.card), set())
        db.event(self.conn, self.card, "guide_ruling", {
            "role": "blind-tester", "choice": "red-exempt", "applied": True,
            "reason": "statement 2", "card_hash": bundles.card_hash("x")}, now=2)
        self.assertEqual(red._exempt_statements(self.conn, self.card), set())

    def test_a_ruling_does_not_return_a_card_triaged_for_a_later_request(self):
        self.listed()
        self.step()
        db.event(self.conn, self.card, "contradiction", {
            "role": "reviewer", "examples": [{"id": "x", "source": "s", "differs": []}],
            "precedence": "p", "ruling_card": None, "routed": "guide"}, now=5)
        self.rule("red-exempt")
        ruling, = self.events("guide_ruling")
        self.assertFalse(ruling["applied"])
        self.assertEqual(self.card_row()["status"], "triage")
        self.assertEqual(red._exempt_statements(self.conn, self.card), set())

    def test_a_ruling_on_a_card_no_longer_waiting_is_recorded_unapplied(self):
        self.listed()
        self.step()
        self.conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (self.card,))
        self.rule("red-exempt")
        ruling, = self.events("guide_ruling")
        self.assertFalse(ruling["applied"])
        self.assertEqual(self.card_row()["status"], "blocked")
        self.assertEqual(red._exempt_statements(self.conn, self.card), set())


class Offered(Request):
    def test_a_request_offers_the_two_choices_and_other_contradictions_do_not(self):
        self.listed()
        self.step()
        ev = self.conn.execute("SELECT id FROM task_events WHERE kind='contradiction'"
                               ).fetchone()[0]
        db.event(self.conn, self.card, "contradiction", {
            "role": "blind-tester", "examples": [{"id": "x", "source": "s", "differs": []}],
            "precedence": "p", "ruling_card": None, "routed": "guide"}, now=5)
        other = self.conn.execute("SELECT max(id) FROM task_events").fetchone()[0]
        default = ("document-wrong", "example-wrong", "ask-owner")
        self.assertEqual(exempt.offered(self.conn, ev, default), ("red-exempt", "not-exempt"))
        self.assertEqual(exempt.offered(self.conn, other, default), default)
        self.assertEqual(exempt.offered(self.conn, None, default), default)

    def test_the_guide_sees_the_statements_and_the_two_choices_never_the_document(self):
        self.listed()
        self.step()
        ev = self.contradictions()[0]
        ruling = {"role": ev["role"], "examples": ev["examples"], "precedence": ev["precedence"],
                  "document": {"covered": "SECRET-DOCUMENT"}, "contradiction": 1}
        out = bundles.guide_ruling({"id": self.card, "title": "card", "body": "b"}, ruling)
        for want in ("statement 1", "the first thing works", f"{self.card}.test_greets",
                     "`red-exempt`", "`not-exempt`", "exactly these two"):
            self.assertIn(want, out["prompt"])
        self.assertNotIn("SECRET-DOCUMENT", out["prompt"])
        self.assertNotIn("`example-wrong`", out["prompt"])


class Packet(Request):
    def packet(self, ev_id):
        from types import SimpleNamespace

        from capsule import tick
        ev = self.conn.execute("SELECT * FROM task_events WHERE id=?", (ev_id,)).fetchone()
        stub = SimpleNamespace(_run=lambda conn, pk: conn.execute(
            "SELECT * FROM task_runs WHERE id=?", (pk,)).fetchone(), intent_dir=None)
        return tick.Tick._ruling_packet(stub, self.conn, ev)

    def test_the_guide_gets_no_tester_report_for_a_request_but_one_for_other_contradictions(self):
        self.listed()
        self.step()
        ask = self.conn.execute("SELECT id FROM task_events WHERE kind='contradiction'"
                                ).fetchone()[0]
        got = self.packet(ask)
        self.assertIsNone(got["document"])
        self.assertEqual([e["id"] for e in got["examples"]], ["statement 1", "statement 2"])
        db.event(self.conn, self.card, "contradiction", {
            "role": "blind-tester", "examples": [{"id": "x", "source": "s", "differs": []}],
            "precedence": "p", "ruling_card": None, "routed": "guide"},
            run_pk=1, now=5)
        other = self.conn.execute("SELECT max(id) FROM task_events").fetchone()[0]
        self.assertIn("covered", self.packet(other)["document"])


class Hold(Request):
    """red.step runs after the review step in a pass: a verdict must not come first."""

    def reviewed(self):
        from types import SimpleNamespace

        from capsule import tick
        seen = []
        stub = SimpleNamespace(
            _guarded=lambda conn, cfg, task_id, now, counts, work: work(),
            _review_one=lambda conn, cfg, task, now, counts: seen.append(task["id"]))
        tick.Tick._review(stub, self.conn, self.cfg, 100, {})
        return seen

    def test_a_card_with_an_unprocessed_round_gets_no_verdict_until_it_is_processed(self):
        self.conn.execute("UPDATE tasks SET status='review' WHERE id=?", (self.card,))
        self.make_round()
        self.assertEqual(self.reviewed(), [])
        self.step()
        self.assertEqual(self.reviewed(), [self.card])

    def test_a_round_deferred_through_running_is_filed_before_the_card_is_judged(self):
        self.conn.execute("UPDATE tasks SET status='running' WHERE id=?", (self.card,))
        self.listed()
        self.step()
        self.conn.execute("UPDATE tasks SET status='review' WHERE id=?", (self.card,))
        self.assertEqual(self.reviewed(), [])
        self.step()
        self.assertEqual((len(self.requests()), self.reviewed()), (1, []))

    def test_with_blind_tests_off_the_review_step_is_unchanged(self):
        self.conn.execute("UPDATE tasks SET status='review' WHERE id=?", (self.card,))
        self.make_round()
        self.cfg["redesign"]["blind_tests"] = "off"
        self.assertEqual(self.reviewed(), [self.card])


class Brief(unittest.TestCase):
    def test_the_testers_brief_says_when_a_test_may_be_listed_as_red_exempt(self):
        card = {"id": "t_0a1b2c3d", "title": "t", "acceptance": ["one"], "card_hash": "h"}
        text = brief.render_blind_tester_brief(card, "/nonexistent")["prompt"]
        self.assertIn("`HEADER.json` `red_exempt`", text)
        self.assertIn("already has before the card", text)
        self.assertIn("that statement covered by the test", text)


if __name__ == "__main__":
    unittest.main()
