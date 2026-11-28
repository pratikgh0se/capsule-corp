# mutation-check: targets-predate-range ff625e94 -- fixture rename only, no behaviour pinned ::
#   TestLeakLint.test_a_clean_run_opens_no_incident,
#   TestLeakLint.test_a_finding_that_shows_an_import_line_is_no_leak,
#   TestLeakLint.test_the_count_opens_an_incident_with_the_count_and_never_the_line,
#   TestLeakLint.test_the_incident_evidence_holds_the_count_text_beside_the_event_and_no_line
"""R32 (B4a) leak lint: a failing test's finding that holds a whole line of the card's own blind
tests is withheld, counted in the `blind_gated` event and opens one incident with the count.
Helpers and oracles as in test_blind_gate.py (the rows, the event, the rendered result)."""
import json

from capsule import incident_leak, incidents
from capsule.blind import run as blind_run
from capsule.tests.test_blind_gate import LEAKY, GateCase
from capsule.tests.test_blind_run import BOARD, TESTS, Container


class TestLeakLint(GateCase):
    def setUp(self):
        super().setUp()
        self.use(LEAKY, "l")

    def test_a_finding_holding_a_whole_line_of_the_tests_is_withheld_and_counted(self):
        rows = {r["cmd"]: r for r in self.go(Container(self.ws))}
        bad = rows[f"blind:{self.card}.test_fails"]
        self.assertTrue(bad["leaked"])
        for key in ("given", "expect", "observed"):
            self.assertIn("withheld", bad["test"][key])
        self.assertNotIn("secret line", json.dumps(bad))
        self.assertEqual(self.events()[0]["leaks"], 1)
        self.assertNotIn("secret line", json.dumps(self.result()))      # nor the evidence

    def test_the_count_opens_an_incident_with_the_count_and_never_the_line(self):
        self.go(Container(self.ws))
        incidents.from_events(self.conn, board=BOARD)
        (inc,) = [i for i in incidents.listing(self.conn) if i["kind"] == "review_infra"]
        self.assertEqual(inc["role"], f"blind_leak_lint:{self.card}")
        self.assertIn("1 finding", inc["root_key"].replace("#", "1"))
        self.assertNotIn("secret line", json.dumps(dict(inc)))

    def test_the_incident_evidence_holds_the_count_text_beside_the_event_and_no_line(self):
        self.go(Container(self.ws))
        incidents.from_events(self.conn, board=BOARD)
        (inc,) = [i for i in incidents.listing(self.conn) if i["kind"] == "review_infra"]
        evidence = json.loads(inc["evidence"])
        self.assertTrue(evidence[0].startswith("event:"))
        (text,) = [e for e in evidence if e.startswith("blind_leak_lint:")]
        self.assertIn("1 failing test(s) had a finding", text)
        self.assertNotIn("secret line", json.dumps(evidence))
        self.assertEqual(incident_leak.evidence({"kind": "blind_gated", "task_id": "t"}, {}), [])

    def test_a_clean_run_opens_no_incident(self):
        self.use(TESTS, "c")
        self.go(Container(self.ws))
        incidents.from_events(self.conn, board=BOARD)
        self.assertEqual(incidents.listing(self.conn), [])

    def test_a_finding_that_shows_an_import_line_is_no_leak(self):
        self.use(TESTS.replace('run("echo no")', 'run("echo import unittest")'), "i")
        rows = {r["cmd"]: r for r in self.go(Container(self.ws))}
        bad = rows[f"blind:{self.card}.test_fails"]
        self.assertIn("import unittest", bad["test"]["observed"])
        self.assertNotIn("leaked", bad)
        self.assertEqual(self.events()[0]["leaks"], 0)
        incidents.from_events(self.conn, board=BOARD)
        self.assertEqual(incidents.listing(self.conn), [])

    def test_lines_come_from_the_cards_own_folder_and_skip_real_imports_only(self):
        snap = self.root / "snap"
        (snap / "cards" / self.card).mkdir(parents=True)
        (snap / "harness").mkdir()
        (snap / "harness" / "h.py").write_text("shared harness code line\n")
        (snap / "cards" / self.card / "t.py").write_text(
            "import unittest\nfrom harness import run\nimport os, sys as s  # tools\n"
            "from a.b import (c as d, e)\n   the secret line of it   \nshort\n"
            "from the end of it all\nimport this line is prose\n")
        self.assertEqual(blind_run._lines(snap, self.card), {
            "the secret line of it", "from the end of it all", "import this line is prose"})

    def test_classify_wants_a_count(self):
        row = {"kind": "blind_gated", "task_id": "t_x"}
        self.assertIsNone(incident_leak.classify(row, {"leaks": 0}))
        self.assertIsNone(incident_leak.classify({"kind": "other", "task_id": "t_x"}, {"leaks": 3}))
        self.assertEqual(incident_leak.classify(row, {"leaks": 2})[:2], ("review_infra",
                                                                         "blind_leak_lint:t_x"))
