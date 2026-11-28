# mutation-check: targets-predate-range b957739c9 -- owner-word rename, no behaviour pinned ::
#   EvalFixtures.test_the_production_report_carries_the_pinned_owner_keys
"""Oracle 2 = the hand-stated numbers and labels in fixtures/evals/expected.json; oracle 1 =
`reference()`, a small counter over the raw rows (SQL, human/ and human/done/ files, decision-row
text). the owner's decision rows are hand labels in the JSON, never a field the counter reads."""

import json
import os
import re
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capsule import cost, db, human, paths, report, touches
from capsule.tests.fixtures.evals import boards

LANDED = (
    "SELECT id FROM tasks WHERE status='done' AND EXISTS"
    " (SELECT 1 FROM task_events WHERE task_id=tasks.id AND kind='completed')"
)
T_ID = re.compile(r"t_[0-9a-f]{8}")


def _sql(b, query):
    conn = sqlite3.connect(str(paths.board_db(b.board, b.home)))
    try:
        return [tuple(r) for r in conn.execute(query)]
    finally:
        conn.close()


def _spool_docs(b):
    """(name, doc) of every human file, pending and archived (the tick moves them to done/)."""
    hdir = b.home / "boards" / b.board / "human"
    files = list(hdir.glob("*.json")) + list((hdir / "done").glob("*.json"))
    return sorted((p.name, json.loads(p.read_text())) for p in files)


def _acts(b, want):
    """Q2b distinct acts {id: cards named}: every non-migration spool file, plus the decision rows
    hand-labelled as the owner's, minus a row that re-records a spool act on the same card."""
    spool = {f"spool:{n}": [d["task_id"]] for n, d in _spool_docs(b) if d.get("tag") is None}
    labels = want.get("owner_act_rows", [[]])[0]
    twin = {(x["row"]): b.ids[x["card"]] for x in want.get("double_recorded", [[]])[0]}
    acts = dict(spool)
    for r in b.decision_rows:
        again = r["number"] in twin and any(twin[r["number"]] in c for c in spool.values())
        if r["number"] in labels and not again:
            acts[f"row:{r['number']}"] = T_ID.findall(r["text"])
    return acts


def _runs(b):
    sql = (
        "SELECT task_id, SUM(tokens_in+tokens_out+tokens_cache_write)"
        " FROM task_runs GROUP BY task_id"
    )
    fresh = dict(_sql(b, sql) or [(None, 0)])
    q = "SELECT COUNT(*) FROM task_runs WHERE profile='implementer' GROUP BY task_id"
    return fresh, [n for (n,) in _sql(b, q)]


def reference(b, want):
    """Every pinned number, recomputed from raw rows."""
    names = {v: k for k, v in b.ids.items()}
    landed = [r[0] for r in _sql(b, LANDED)]
    acts = _acts(b, want)
    docs = [d for _, d in _spool_docs(b)]
    plain = sum(d.get("tag") is None for d in docs)
    fresh, impl = _runs(b)
    active = _sql(b, "SELECT COUNT(DISTINCT task_id) FROM task_runs")[0][0]
    unworded = [t for t in landed if not any(t in x and "LANDED" in x for x in b.cos_log)]
    on_landed = [a for a, cards in acts.items() if set(cards) & set(landed)]
    return {
        "landed_cards": len(landed),
        "landed_card_names": sorted(names[t] for t in landed),
        "landed_without_landed_word_names": sorted(names[t] for t in unworded),
        "owner_act_ids": sorted(acts),
        "owner_acts": len(acts),
        "owner_touches_per_card": len(acts) / len(landed) if landed else "insufficient",
        "owner_touches_per_landed_card_only": len(on_landed) / len(landed) if landed else None,
        "interventions_total": plain,
        "intervention_events": _sql(
            b,
            "SELECT COUNT(*) FROM task_events WHERE kind='intervention'"
            " AND json_extract(payload,'$.tag') IS NULL",
        )[0][0],
        "interventions_per_card": plain / active if active else 0.0,
        "migration_resets": len(docs) - plain,
        "fresh_tokens_total": sum(fresh.values()),
        "fresh_tokens_max_card": max(fresh.values()),
        "max_rework_rounds": max(impl, default=1) - 1,
        "repeat_incidents": _sql(b, "SELECT COUNT(*) FROM incidents WHERE kind='repeat'")[0][0],
        "reverts": sum(s.startswith("Revert") for s in b.git_subjects),
    }


def _pins(kind):
    return {k: v for k, v in boards.expected()[kind].items()}


class EvalFixtures(unittest.TestCase):
    def build(self, kind):
        home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        return boards.build(kind, home)

    def test_every_pinned_number_matches_the_reference_counter(self):
        for kind in ("good_board", "bad_board", "empty_board"):
            pins = _pins(kind)
            got = reference(self.build(kind), pins) if kind != "empty_board" else None
            for key, (value, reason) in pins.items():
                if got is None or key not in got:
                    continue
                if isinstance(value, list):
                    value = sorted(value)
                if isinstance(value, float):
                    self.assertAlmostEqual(got[key], value, places=3, msg=f"{kind}.{key}: {reason}")
                else:
                    self.assertEqual(got[key], value, f"{kind}.{key}: {reason}")
            if got:
                self.assertEqual(got["intervention_events"], got["interventions_total"], kind)

    def test_boards_have_the_stated_shape_and_every_pin_has_a_reason(self):
        for kind in ("good_board", "bad_board", "empty_board"):
            self.assertTrue(all(len(v) == 2 and v[1] for v in _pins(kind).values()), kind)
        self.assertEqual(len(self.build("good_board").ids), 10)
        self.assertEqual(len(self.build("bad_board").ids), 7)
        self.assertTrue(boards.expected()["_note"])

    def test_the_production_report_carries_the_pinned_owner_keys(self):
        b = self.build("empty_board")
        doc = touches.evaluate(b.home, b.board, vault="")
        pins = _pins("empty_board")
        self.assertEqual(doc["owner_touches_per_card"], pins["owner_touches_per_card"][0])
        self.assertNotIn(human.LEGACY_OWNER + "_touches_per_card", doc)

    def test_zero_landed_board_is_insufficient_not_zero(self):
        b = self.build("empty_board")
        got = reference(b, {})
        pins = _pins("empty_board")
        self.assertEqual(got["landed_cards"], pins["landed_cards"][0])
        self.assertEqual(got["owner_touches_per_card"], pins["owner_touches_per_card"][0])

    def test_traps_and_double_record_are_not_acts(self):
        b, pins = self.build("bad_board"), _pins("bad_board")
        ids = reference(b, pins)["owner_act_ids"]
        self.assertIn("row:707", ids)  # same card as a spool act, another day: a new act
        self.assertEqual(ids.count("row:701"), 1)  # one act naming two cards
        self.assertFalse({"row:702", "row:706", "row:704"} & set(ids))  # mentions; a double record
        self.assertNotIn(b.ids["b6"], [r[0] for r in _sql(b, LANDED)])  # done by hand, not landed

    def test_archiving_the_pending_files_changes_no_pin(self):
        """The tick moves every handled file to human/done/; the numbers must not depend on that."""
        for kind in ("good_board", "bad_board"):
            b, pins = self.build(kind), _pins(kind)
            before = reference(b, pins)
            hdir = b.home / "boards" / b.board / "human"
            (hdir / "done").mkdir(exist_ok=True)
            for p in hdir.glob("*.json"):
                p.rename(hdir / "done" / p.name)
            self.assertEqual(reference(b, pins), before, kind)

    def test_existing_report_agrees_on_interventions_and_rework(self):
        """Oracle 1b: report.py's shipped numbers match the pinned ones."""
        for kind in ("good_board", "bad_board"):
            b, want = self.build(kind), _pins(kind)
            doc = report.interventions(b.home, b.board)
            self.assertAlmostEqual(
                doc["interventions_per_card"], want["interventions_per_card"][0], places=3
            )
            self.assertEqual(
                sum(c["migration_resets"] for c in doc["cards"]), want["migration_resets"][0]
            )
            self.assertEqual(
                max(c["rework_rounds"] for c in doc["cards"]), want["max_rework_rounds"][0]
            )
            self.assertEqual(doc["repeat_incidents"], want["repeat_incidents"][0])
            conn = db.connect_ro(paths.board_db(b.board, b.home))
            total = sum(cost.card_cost(conn, t)["tokens"] for t in b.ids.values())
            conn.close()
            self.assertEqual(total, want["fresh_tokens_total"][0])

    def test_perturbing_one_row_moves_the_number(self):
        """The metric is not a constant: each edit below moves a pinned number."""
        pins = _pins("good_board")
        b = self.build("good_board")
        next(iter((b.home / "boards" / b.board / "human" / "done").glob("1.json"))).unlink()
        self.assertNotEqual(reference(b, pins)["owner_acts"], pins["owner_acts"][0])
        b = self.build("good_board")
        conn = db.connect(paths.board_db(b.board, b.home))
        conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (b.ids["g0"],))
        conn.execute(
            "DELETE FROM task_events WHERE kind='intervention' AND task_id=?", (b.ids["g1"],)
        )
        conn.close()
        got = reference(b, pins)
        self.assertNotEqual(got["landed_cards"], pins["landed_cards"][0])
        self.assertNotEqual(got["owner_touches_per_card"], pins["owner_touches_per_card"][0])
        self.assertNotEqual(got["intervention_events"], got["interventions_total"])
        bad, bpins = self.build("bad_board"), _pins("bad_board")
        bpins["owner_act_rows"][0].append(702)  # the mention becomes an act
        self.assertNotEqual(
            reference(bad, bpins)["owner_act_ids"], sorted(bpins["owner_act_ids"][0])
        )
        bpins["double_recorded"][0].clear()  # the double record is counted twice
        self.assertNotEqual(reference(bad, bpins)["owner_acts"], bpins["owner_acts"][0])

_OWNER_PATCH = None


def setUpModule():
    """The decision-row text below names Alex: the matcher reads the owner name from config."""
    global _OWNER_PATCH
    _OWNER_PATCH = mock.patch.dict(os.environ, {"CAPSULE_OWNER_NAME": "Alex"})
    _OWNER_PATCH.start()


def tearDownModule():
    _OWNER_PATCH.stop()

