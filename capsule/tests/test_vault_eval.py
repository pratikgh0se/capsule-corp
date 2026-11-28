"""vault_eval (MEM-13 V5): a temp board DB, a fixture vault and canned search results. Never the
live board, the real vault or Ollama."""
import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capsule import vault_eval as ve

CARDS = [  # (id, status, title, body)
    ("t1", "done", "Zenkai", "Follows row 454 and rows 455, 456."),
    ("t2", "done", "Spec link", "See [[Alpha Note]] for context."),
    ("t3", "archived", "Path", "Read Knowledge/Beta Note.md first. Also Knowledge/Gone.md."),
    ("t4", "done", "No gold", "Nothing cited here."),
    ("t5", "running", "Open", "row 9"),
]


class VaultEval(unittest.TestCase):
    def setUp(self):
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "v" / "Knowledge").mkdir(parents=True)
        (self.tmp / "v" / "Alpha Note.md").write_text("alpha")
        (self.tmp / "v" / "Knowledge" / "Beta Note.md").write_text("beta")
        self.db = self.tmp / "kanban.db"
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE tasks (id TEXT, status TEXT, title TEXT, body TEXT)")
        conn.executemany("INSERT INTO tasks VALUES (?,?,?,?)", CARDS)
        conn.commit()
        conn.close()
        self.vault = self.tmp / "v"

    def build(self):
        out = self.tmp / "cases.jsonl"
        self.assertEqual(ve.main(["build", "--board-db", str(self.db), "--vault",
                                  str(self.vault), "--out", str(out)]), 0)
        return [json.loads(x) for x in out.read_text().splitlines()], out

    def test_build_five_card_fixture(self):
        cases, _ = self.build()
        gold = {c["id"]: c["gold"] for c in cases}
        self.assertEqual(gold, {"card:t1": ["row 454", "row 455", "row 456"],
                                "card:t2": ["Alpha Note.md"],
                                "card:t3": ["Knowledge/Beta Note.md"]})
        self.assertTrue(cases[0]["q"].startswith("Zenkai\nFollows"))

    def test_verdicts_fold_in_and_survive_rebuild(self):
        _, out = self.build()
        vf = self.tmp / "verdicts.jsonl"
        lines = [{"verdict": "kept", "q": "q1", "path": "Alpha Note.md"},
                 {"verdict": "dropped", "q": "q2", "path": "Knowledge/Beta Note.md"},
                 {"verdict": "missing", "q": "q3", "path": "Nope.md"}]
        vf.write_text("".join(json.dumps(x) + "\n" for x in lines))
        vault = str(self.vault)
        ve.main(["build", "--from-verdicts", str(vf), "--vault", vault, "--out", str(out)])
        ve.main(["build", "--board-db", str(self.db), "--vault", vault, "--out", str(out)])
        got = [json.loads(x) for x in out.read_text().splitlines()]
        v = [c for c in got if c["source"] == "verdict"]
        self.assertEqual([(bool(c["gold"]), bool(c["negative"])) for c in v],
                         [(True, False), (False, True)])
        self.assertEqual(len(got), 5)

    def score(self, results, **kw):
        cases = [{"id": "a", "gold": ["x"]}, {"id": "b", "gold": ["y"]}]
        return ve.score(cases, results, self.tmp / "out", **{"min_cases": 0, **kw})

    def test_too_few_cases_exit_3_and_json(self):
        self.assertEqual(self.score({"a": ["x"], "b": ["y"]}, min_cases=40), 3)
        doc = json.loads(next((self.tmp / "out").glob("result-*.json")).read_text())
        self.assertTrue(doc["too_few_cases"])
        self.assertFalse(doc["two_oracles"])

    def test_nightly_needs_external(self):
        self.assertEqual(self.score({"a": ["x"], "b": ["y"]}, need_external=True), 2)
        self.assertEqual(self.score({"a": ["x"], "b": ["y"]}, need_external=True, external=1.0), 0)
        self.assertEqual(ve._external("not json"), None)
        self.assertEqual(ve._external('{"summary": {"recall_at": {"5": 0.8}}}'), 0.8)

    def test_external_file_forms_and_rate(self):
        self.assertEqual(ve._external('{"recall_at_5": 0.4}'), 0.4)
        cases = self.tmp / "c.jsonl"
        cases.write_text(json.dumps({"id": "a", "gold": ["x"]}) + "\n")
        res = self.tmp / "r.json"
        res.write_text('{"a": ["x"]}')
        for body, rc in (('{"recall_at_5": 0.4}', 2), ('{"junk": 1}', 2),
                         ('{"recall_at_5": 1}', 0)):
            ext = self.tmp / "e.json"
            ext.write_text(body)
            argv = ["score", "--cases", str(cases), "--results", str(res), "--external",
                    str(ext), "--min-cases", "0", "--out-dir", str(self.tmp / "o")]
            self.assertEqual(ve.main(argv), rc)
        self.assertEqual(ve.counter([], {})["stale_hit_at_3"], 0.0)
        m = ve.counter([{"id": "a", "gold": ["x"]}], {"a": [{"path": "x", "stale": True}]})
        self.assertEqual((m["stale_hits"], m["stale_hit_at_3"]), (1, 1.0))

    def test_setup_failures_exit_3(self):
        miss = str(self.tmp / "none.jsonl")
        self.assertEqual(ve.main(["score", "--cases", miss, "--results", miss]), 3)
        with mock.patch.dict("os.environ", clear=True):
            self.assertEqual(ve.main(["query", "x"]), 3)

    def test_wikilink_forms_resolve(self):
        (self.vault / "Specs").mkdir()
        (self.vault / "Specs" / "Alpha note.md").write_text("a")
        notes, stems = ve._notes(self.vault)
        body = "[[Specs/Alpha note]] [[Specs/Alpha note|alias]] [[Specs/Alpha note#H]] [[Nope]]"
        self.assertEqual(ve._body_paths(body, notes, stems), (["Specs/Alpha note.md"], 1))
        self.assertEqual(ve._body_paths("[[Alpha Note]]", notes, stems)[0], ["Alpha Note.md"])

    def test_counter_and_pass(self):
        m = ve.counter([{"id": "a", "gold": ["x"]}, {"id": "b", "gold": ["y"]}],
                       {"a": ["x"], "b": ["q", "y"]})
        self.assertEqual((m["recall_at_5"], m["mrr"], m["bad_hit_at_3"]), (1.0, 0.75, None))
        self.assertEqual(self.score({"a": ["x"], "b": ["y"]}), 0)
        self.assertEqual(len(list((self.tmp / "out").glob("result-*.json"))), 1)

    def test_threshold_miss_exits_1_and_names_metric(self):
        self.assertEqual(self.score({"a": ["x"], "b": []}), 1)
        doc = json.loads(next((self.tmp / "out").glob("result-*.json")).read_text())
        self.assertEqual(doc["failed"], ["recall_at_5"])

    def test_one_stale_hit_fails(self):
        res = {"a": [{"path": "x"}], "b": [{"path": "z", "stale": True}, {"path": "y"}]}
        self.assertEqual(self.score(res), 1)

    def test_counters_must_agree(self):
        self.assertEqual(self.score({"a": ["x"], "b": ["y"]}, external=0.5), 2)
        self.assertEqual(self.score({"a": ["x"], "b": ["y"]}, external=1.0), 0)

    def test_bad_hit_needs_ten_negatives(self):
        cases = [{"id": f"n{i}", "gold": [], "negative": ["bad"]} for i in range(10)]
        self.assertEqual(ve.counter(cases, {c["id"]: ["bad"] for c in cases})["bad_hit_at_3"], 1.0)
        self.assertIsNone(ve.counter(cases[:9], {})["bad_hit_at_3"])


if __name__ == "__main__":
    unittest.main()
