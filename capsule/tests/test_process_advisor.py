"""The weekly process advisor (row 394, Z4, Z5): packet, report check, card drafts, the run wrapper.
Fixture boards in temp dirs only; no model is called (the command is a fake script)."""
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

from capsule import contracts, db, intent, paths
from capsule.jsonschema import load_all
from capsule.tests.test_report import setup_board

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "maintenance"))
import process_advisor as pa  # noqa: E402

ROLES = ["guide", "implementer", "korin"]
INCIDENT = ("INSERT INTO incidents (id, board, kind, opened_at, evidence, root_key, fingerprint,"
            " status, role) VALUES (?, ?, ?, 1, '[]', 'k', ?, 'open', ?)")
PROP = {"kind": "prune", "target": "contracts/implementer.md", "evidence": "never cited in 30 runs",
        "change": "drop the third may-not line", "removes": "the third may-not line",
        "card_title": "Prune a line"}
FAKE_MODEL = """import json, sys
p = json.load(sys.stdin)
ranking = [{'contract': k, 'count': v} for k, v in p['contradictions_by_contract'].items()]
print(json.dumps({'schema': 'advisor_report.v1', 'run_id': 'a' * 32, 'task_id': 't_0000aaaa',
                  'claim_lock': 'x', 'summary': 'A quiet week here.',
                  'contradiction_ranking': ranking, 'override_review': [], 'proposals': [%s]}))
"""


def packet_with(overrides=(), contradictions=None):
    contradictions = contradictions or {"guide": 0, "implementer": 0, "korin": 0}
    return {"roles": ROLES, "overrides": list(overrides), "board": "b",
            "contradictions_by_contract": contradictions}


def good_report(**over):
    ranking = [{"contract": r, "count": 0} for r in ROLES]
    doc = {"schema": "advisor_report.v1", "run_id": "a" * 32, "task_id": "t_0000aaaa",
           "claim_lock": "x", "summary": "One quiet week.", "contradiction_ranking": ranking,
           "override_review": [], "proposals": []}
    doc.update(over)
    return doc


def fake_command(text):
    path = Path(tempfile.mkdtemp()) / "fake.py"
    path.write_text(text)
    return f"{sys.executable} {path}"


class Packet(unittest.TestCase):
    def test_it_counts_the_board_history(self):
        home, board, tids = setup_board()
        conn = db.connect(paths.board_db(board, home))
        rows = (("contradiction", "guide"), ("contradiction", "guide"), ("crash", "korin"))
        for n, (kind, role) in enumerate(rows):
            conn.execute(INCIDENT, (f"inc_{n:08x}", board, kind, f"fp{n}", role))
        conn.commit()
        conn.close()
        packet = pa.packet_for_board(home, board, now=1000)
        self.assertEqual(packet["totals"]["runs"], 7)
        self.assertEqual(packet["wasted_by_error_class"], {"invalid_output": 1, "unknown": 1})
        self.assertEqual(packet["incidents_by_kind"], {"contradiction": 2, "crash": 1})
        self.assertEqual(packet["contradictions_total"], 2)
        self.assertEqual(list(packet["contradictions_by_contract"].items())[0], ("guide", 2))
        self.assertEqual(set(packet["contradictions_by_contract"]), set(pa._contract_roles()))
        self.assertIn("advisor", packet["roles"])
        self.assertEqual(packet["rework"][0]["task_id"], tids[0])

    def test_a_board_with_no_incidents_is_all_zero(self):
        home, board, _ = setup_board()
        conn = db.connect(paths.board_db(board, home))
        conn.execute("DROP TABLE incidents")
        conn.commit()
        conn.close()
        packet = pa.packet_for_board(home, board, now=1000)
        self.assertEqual(packet["contradictions_total"], 0)
        self.assertEqual(packet["incidents_by_kind"], {})

    def test_review_findings_are_counted_by_class(self):
        docs = [{"findings": [{"class": "slop"}, {"class": "slop"}, {"class": "security"}]},
                {"findings": [{"class": "slop"}], "verdict": "fail"}, {"findings": []}]
        self.assertEqual(pa._findings_by_class(docs), {"slop": 3, "security": 1})

    def test_unparsable_verdict_files_are_skipped(self):
        d = Path(tempfile.mkdtemp())
        (d / "a.json").write_text('{"findings": [{"class": "slop"}]}')
        (d / "b.json").write_text("not json")
        (d / "c.json").write_text("[1]")
        self.assertEqual(len(pa._load_verdict_docs(d)), 1)


class CheckReport(unittest.TestCase):
    def test_a_good_report_passes(self):
        self.assertEqual(pa.check_report(good_report(), packet_with()), [])

    def test_a_missing_contract_in_the_ranking_is_refused(self):
        doc = good_report(contradiction_ranking=[{"contract": "guide", "count": 0}])
        errors = pa.check_report(doc, packet_with())
        self.assertTrue(any("'korin'" in e and "Z5" in e for e in errors), errors)

    def test_a_ranking_out_of_order_or_wrong_count_is_refused(self):
        counts = {"guide": 1, "implementer": 3, "korin": 0}
        packet = packet_with(contradictions=counts)
        ranking = [{"contract": "guide", "count": 1}, {"contract": "implementer", "count": 3},
                   {"contract": "korin", "count": 0}]
        doc = good_report(contradiction_ranking=ranking)
        self.assertTrue(any("descending" in e for e in pa.check_report(doc, packet)))
        ranking[1]["count"] = 2
        self.assertTrue(any("packet says 3" in e for e in pa.check_report(doc, packet)))

    def test_an_override_without_a_review_is_refused(self):
        packet = packet_with(overrides=[{"id": "t_1:ruff", "state": "active", "check": "ruff"}])
        errors = pa.check_report(good_report(), packet)
        self.assertTrue(any("t_1:ruff" in e and "Z4" in e for e in errors))
        review = [{"override": "t_1:ruff", "verdict": "expire", "note": "no fix card in sight"}]
        self.assertEqual(pa.check_report(good_report(override_review=review), packet), [])

    def test_an_added_rule_must_name_what_it_removes(self):
        prop = dict(PROP, kind="add_rule", removes="none")
        errors = pa.check_report(good_report(proposals=[prop]), packet_with())
        self.assertTrue(any("removes" in e for e in errors), errors)
        prop["removes"] = "the old Y line in the same contract"
        self.assertEqual(pa.check_report(good_report(proposals=[prop]), packet_with()), [])

    def test_placeholder_removals_are_refused(self):
        texts = ("None.", "N/A.", "nothing at all", "nil", "TBD", "---", "none of it")
        for text in texts:
            prop = dict(PROP, kind="add_rule", removes=text)
            errors = pa.check_report(good_report(proposals=[prop]), packet_with())
            self.assertTrue(any("removes" in e for e in errors), text)

    def test_a_made_up_contract_in_the_ranking_is_refused(self):
        ranking = good_report()["contradiction_ranking"] + [{"contract": "ghost", "count": 0}]
        errors = pa.check_report(good_report(contradiction_ranking=ranking), packet_with())
        self.assertTrue(any("'ghost'" in e for e in errors), errors)

    def test_two_overrides_on_one_check_need_two_reviews(self):
        home, board, tids = setup_board()
        conn = db.connect(paths.board_db(board, home))
        for at in (100, 200):
            db.event(conn, tids[0], "override", {"check": "ruff", "reason": "r", "expires_at": 9999,
                                                 "fix_card": "t_1", "issuer": "p"}, now=at)
        conn.commit()
        conn.close()
        ids = [o["id"] for o in pa.packet_for_board(home, board, now=500)["overrides"]]
        self.assertEqual(len(set(ids)), 2, ids)
        review = [{"override": ids[0], "verdict": "keep", "note": "fine for now"}]
        packet = packet_with(overrides=[{"id": i} for i in ids])
        errors = pa.check_report(good_report(override_review=review), packet)
        self.assertEqual(len(errors), 1, errors)

    def test_roles_filter_scopes_the_ranking_after_a_model_swap(self):
        home, board, _ = setup_board()
        packet = pa.packet_for_board(home, board, now=500, roles=["guide"])
        self.assertEqual(packet["roles"], ["guide"])
        self.assertEqual(set(packet["contradictions_by_contract"]), {"guide"})

    def test_a_schema_violation_is_reported(self):
        doc = good_report()
        del doc["summary"]
        self.assertTrue(pa.check_report(doc, packet_with()))


class Outputs(unittest.TestCase):
    def test_cards_are_drafts_gated_on_owner(self):
        cards = pa._proposal_cards(good_report(proposals=[PROP]))
        self.assertEqual(len(cards), 1)
        self.assertEqual((cards[0]["title"], cards[0]["status"]), ("Prune a line", "blocked"))
        self.assertIn("Gate: the owner approves before this card runs (row 393).", cards[0]["body"])

    def test_the_note_carries_the_ranking_table(self):
        note = pa._render_note(good_report(proposals=[PROP]), packet_with())
        self.assertIn("| guide | 0 |", note)
        self.assertIn("[prune] Prune a line", note)


class Run(unittest.TestCase):
    def test_no_command_writes_the_packet_and_stops(self):
        home, board, _ = setup_board()
        out = Path(tempfile.mkdtemp())
        with self.assertRaises(SystemExit) as ctx:
            pa.run(home, board, out, env={})
        self.assertIn("PROCESS_ADVISOR_CMD is unset", str(ctx.exception))
        self.assertTrue((out / "advisor-packet.json").is_file())
        self.assertFalse((out / "advisor-note.md").exists())

    def test_a_valid_report_is_written_with_card_drafts(self):
        home, board, _ = setup_board()
        out = Path(tempfile.mkdtemp())
        prop = json.dumps(PROP)
        env = {"PROCESS_ADVISOR_CMD": fake_command(FAKE_MODEL % prop)}
        note, cards = pa.run(home, board, out, env=env)
        self.assertTrue(note.read_text().startswith("# Process advisor report"))
        self.assertEqual(json.loads(cards.read_text())[0]["status"], "blocked")

    def test_a_bad_report_is_refused_and_nothing_is_written(self):
        home, board, _ = setup_board()
        out = Path(tempfile.mkdtemp())
        with self.assertRaises(SystemExit) as ctx:
            pa.run(home, board, out, env={"PROCESS_ADVISOR_CMD": fake_command("print('{}')")})
        self.assertIn("report refused", str(ctx.exception))
        self.assertFalse((out / "advisor-note.md").exists())

    def test_a_failing_command_is_reported(self):
        home, board, _ = setup_board()
        env = {"PROCESS_ADVISOR_CMD": fake_command("import sys; sys.exit(3)")}
        with self.assertRaises(SystemExit) as ctx:
            pa.run(home, board, Path(tempfile.mkdtemp()), env=env)
        self.assertIn("exited 3", str(ctx.exception))


class Contract(unittest.TestCase):
    def test_the_contract_and_its_pins_hold(self):
        path = contracts.contract_path("advisor")
        self.assertEqual(contracts.lint(path, load_all(paths.SCHEMAS)), [])
        self.assertEqual(contracts.schema_for("advisor"), "advisor_report.v1")
        self.assertIn("never bypass a Zenkai Rule (docs/ZENKAI.md)", path.read_text())
        self.assertEqual(intent.lint_all(), [])
        for folder, name in ((paths.SCHEMAS, "advisor_report.v1.json"),
                             (paths.INTENT, "advisor.jsonl")):
            lines = (folder / "SHA256SUMS").read_text().split("\n")
            sums = dict(reversed(line.split("  ")) for line in lines if line)
            self.assertEqual(hashlib.sha256((folder / name).read_bytes()).hexdigest(), sums[name])
