# mutation-check: targets-predate-range b957739c9 -- fixture rename only, no behaviour pinned ::
#   Cli.test_a_board_still_in_the_old_schema_is_refused_cleanly,
#   FixtureNumbers.test_rows_before_the_window_are_not_acts,
#   Matcher.test_another_name_matches_its_own_rows_and_not_the_old_name,
#   Matcher.test_configured_alex_equals_the_old_literal_patterns,
#   Traps.test_a_migration_reset_file_is_not_an_act_to_dedupe_against,
#   Traps.test_a_row_number_repeated_in_the_table_is_one_act,
#   Traps.test_an_alex_row_is_found_in_the_last_topic_parenthetical_only,
#   Traps.test_every_real_actor_shape_is_an_act_and_each_is_found,
#   Traps.test_mentions_are_not_acts,
#   Traps.test_no_rows_and_no_spool_files_is_unmeasured_never_zero,
#   Traps.test_one_row_naming_many_cards_is_one_act,
#   Traps.test_row_without_a_card_id_is_an_act_for_all_in_but_not_landed_only,
#   Traps.test_rows_all_before_the_window_and_no_spool_is_unmeasured,
#   Traps.test_same_day_is_the_local_day_not_utc,
#   Traps.test_same_day_row_for_a_spool_act_counts_once_other_day_twice,
#   Traps.test_two_cell_rows_without_a_status_cell_are_read
"""E1 (t_269cc273): `report` carries Alex touches. Oracle 2 is fixtures/evals/expected.json (hand
numbers and hand labels); the report derives the same from rows, spool files and decision-row text.
The tests never call the fixture's own `reference()` counter."""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from capsule import db, human, paths, report, touches
from capsule.tests.fixtures.evals import boards
from capsule.tests.fixtures.board_ddl import board_ddl

REPO = Path(__file__).resolve().parents[2]
# expected.json keys the report must reproduce. reverts, landed_without_landed_word_names and
# double_recorded need the git log / cos-log / a hand label, which the report does not read.
COVERED = (
    "landed_cards", "landed_card_names", "owner_act_ids", "owner_act_rows", "owner_acts",
    "owner_touches_per_card", "owner_touches_per_landed_card_only", "interventions_total",
    "interventions_per_card", "migration_resets", "fresh_tokens_total", "fresh_tokens_max_card",
    "max_rework_rounds", "repeat_incidents",
)
LISTS = ("landed_card_names", "owner_act_ids", "owner_act_rows")
DAY_START = 11 * 86400 + 1  # 1970-01-12 00:00:01 UTC


def write_vault(root, rows):
    """A vault whose decision table holds `rows` ({number, text}); the real parser reads it."""
    note = Path(root) / "Decisions" / "Capsule Corp - System design decisions.md"
    note.parent.mkdir(parents=True)
    lines = ["| # | Topic | Decision | Status |", "|---|---|---|---|"]
    for r in rows:
        topic = r.get("topic", f"row {r['number']}")
        status = "" if r.get("bare") else " decided |"  # rows 595-604 have no status cell
        lines.append(f"| {r['number']} | {topic} | {r['text']} |{status}")
    note.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return Path(root)


def tmp(cleanup):
    d = Path(tempfile.mkdtemp())
    cleanup(shutil.rmtree, d, ignore_errors=True)
    return d


def trap_board(cleanup, n_cards, spool_card=None, spool_at=DAY_START + 5, tag=None):
    """A board `t` with `n_cards` landed cards and, optionally, one approve file on one of them."""
    home = tmp(cleanup)
    db.create_board(paths.board_db("t", home))
    conn = db.connect(paths.board_db("t", home))
    ids = []
    for i in range(n_cards):
        tid = db.new_task(conn, title=f"c{i}", now=i)
        db.event(conn, tid, "completed", {}, now=100 + i)
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
        ids.append(tid)
    conn.close()
    if spool_card is not None:
        human.write(home, "t", ids[spool_card], "approve", "ok", now=spool_at, tag=tag)
    return home, ids


def trap_eval(cleanup, home, rows):
    """Report numbers for decision rows given as (number, topic cell, text)."""
    table = [{"number": n, "topic": top, "text": t} for n, top, t in rows]
    return touches.evaluate(home, "t", vault=write_vault(tmp(cleanup), table))


def q2b_example(cleanup, case):
    """The report numbers for one intent-example `given.document` (landed cards, rows)."""
    home, ids = trap_board(cleanup, case["landed"])
    for who in case.get("spool_authors", []):   # t_092732be: one approve file per author
        human.write(home, "t", ids[0], "approve", "ok", now=DAY_START + 5, author=who)
    return trap_eval(cleanup, home, [(n, top, t.format(*ids)) for n, top, t in case["rows"]])


class Base(unittest.TestCase):
    def setUp(self):
        """Pin a zone whose day is not UTC's: a row's date is the local day (E1 F5)."""
        old = os.environ.get("TZ")
        self.addCleanup(self.set_tz, old)
        self.set_tz("Asia/Kolkata")

    @staticmethod
    def set_tz(zone):
        if zone is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = zone
        time.tzset()

    def built(self, kind):
        b = boards.build(kind, tmp(self.addCleanup))
        return b, write_vault(tmp(self.addCleanup), b.decision_rows)

    def doc(self, kind, **kw):
        b, vault = self.built(kind)
        return b, touches.evaluate(b.home, b.board, vault=vault, **kw)


class FixtureNumbers(Base):
    def test_json_equals_expected_on_good_and_bad_board(self):
        for kind in ("good_board", "bad_board"):
            _, doc = self.doc(kind)
            want = boards.expected()[kind]
            keys = [k for k in COVERED if k in want]
            self.assertGreaterEqual(len(keys), 13, kind)
            for key in keys:
                value, reason = want[key]
                got = doc[key]
                if key in LISTS:
                    got, value = sorted(got), sorted(value)
                if isinstance(value, float):
                    self.assertAlmostEqual(got, value, places=3, msg=f"{kind}.{key}: {reason}")
                else:
                    self.assertEqual(got, value, f"{kind}.{key}: {reason}")

    def test_archiving_the_pending_files_changes_no_number(self):
        b, vault = self.built("bad_board")
        before = touches.evaluate(b.home, b.board, vault=vault)
        hdir = b.home / "boards" / b.board / "human"
        for p in hdir.glob("*.json"):
            p.rename(hdir / "done" / p.name)
        self.assertEqual(touches.evaluate(b.home, b.board, vault=vault), before)

    def test_known_gap_is_in_the_json(self):
        _, doc = self.doc("bad_board")
        self.assertIn("legacy file with no author field", " ".join(doc["caveats"]))
        self.assertIn("chat_touches_per_card", doc)

    def test_missing_decision_table_is_said_not_silent(self):
        b = boards.build("bad_board", tmp(self.addCleanup))
        doc = touches.evaluate(b.home, b.board, vault=tmp(self.addCleanup))
        self.assertEqual(doc["owner_acts"], 4)  # the four non-migration spool files only
        self.assertTrue(any("decision table" in c for c in doc["caveats"]))

    def test_window_drops_old_rows_and_unlanded_cards(self):
        b, vault = self.built("good_board")
        doc = touches.evaluate(b.home, b.board, vault=vault, since=boards.T0 + 505)
        self.assertEqual(doc["landed_cards"], 5)  # g5..g9 completed at T0+505..509
        self.assertEqual(doc["owner_touches_per_card"], 2.0)  # 4 spool + 6 rows over 5
        late = touches.evaluate(b.home, b.board, vault=vault, since=boards.T0 + 600)
        self.assertEqual(late["owner_touches_per_card"], "insufficient")
        self.assertEqual(late["owner_acts"], 10)  # the acts stay; only the divisor is gone

    def test_rows_before_the_window_are_not_acts(self):
        home = tmp(self.addCleanup)
        db.create_board(paths.board_db("w", home))
        conn = db.connect(paths.board_db("w", home))
        tid = db.new_task(conn, title="c", now=1)
        db.event(conn, tid, "completed", {}, now=1_100_000)
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
        conn.close()
        rows = [
            {"number": 1, "topic": "GO (Alex, 1970-01-12)", "text": f"GO {tid}"},
            {"number": 2, "topic": "GO (Alex, 1970-01-13)", "text": f"GO {tid}"},
        ]
        vault = write_vault(tmp(self.addCleanup), rows)
        doc = touches.evaluate(home, "w", vault=vault, since=1_037_000)
        self.assertEqual(doc["owner_act_rows"], [2])


class Cli(Base):
    def run_cli(self, module, home, board, vault, *flags):
        env = {**os.environ, "OBSIDIAN_VAULT_PATH": str(vault), "AGENT_OPS_NO_SEND": "1"}
        cmd = [sys.executable, "-m", *module, board, "--capsule-home", str(home), *flags]
        return subprocess.run(cmd, cwd=REPO, env=env, capture_output=True, text=True)

    def report(self, kind, *flags):
        b, vault = self.built(kind)
        return self.run_cli(["capsule", "report"], b.home, b.board, vault, *flags)

    def test_zero_landed_board_exits_zero_and_says_insufficient(self):
        run = self.report("empty_board", "--json")
        self.assertEqual(run.returncode, 0, run.stderr)
        want = boards.expected()["empty_board"]["owner_touches_per_card"][0]
        self.assertEqual(json.loads(run.stdout)["owner_touches_per_card"], want)
        self.assertIn("insufficient", self.report("empty_board", "--md").stdout)

    def test_bar_is_labelled_bar_and_is_the_c01_bar_of_row_616(self):
        run = self.report("good_board", "--json")
        doc = json.loads(run.stdout)
        self.assertEqual(doc["bar"]["interventions_per_card"], 2.4)
        self.assertEqual(doc["bar"]["owner_touches_per_card"], 1.92)   # 46 / 24, row 616
        self.assertEqual(doc["bar"]["owner_touches_per_card_status"], "row 616")
        self.assertNotIn("baseline", run.stdout.lower())
        self.assertIn("vs bar 1.92: -0.92", self.report("good_board", "--md").stdout)  # 1 - 1.92

    def test_a_board_still_in_the_old_schema_is_refused_cleanly(self):
        """The capsule-corp board before the C03 cutover: no crash, a refusal like M7's."""
        home = tmp(self.addCleanup)
        board_db = paths.board_db("oldschema", home)
        board_db.parent.mkdir(parents=True)
        conn = db.connect(board_db)
        conn.executescript(board_ddl())
        conn.execute("INSERT INTO task_runs (task_id, profile, status, outcome, started_at) VALUES"
                     " ('t_x', 'implementer', 'done', 'failed', 5)")
        conn.close()
        for module in (["capsule", "report"], ["capsule.touches"]):
            run = self.run_cli(module, home, "oldschema", tmp(self.addCleanup))
            self.assertEqual(run.returncode, 1, run.stderr)
            self.assertIn("old system's schema", run.stderr)
            self.assertNotIn("Traceback", run.stderr)
            self.assertEqual(run.stdout, "")

    def test_touches_module_prints_only_the_q2b_block(self):
        b, vault = self.built("bad_board")
        run = self.run_cli(["capsule.touches"], b.home, b.board, vault)
        self.assertEqual(run.returncode, 0, run.stderr)
        doc = json.loads(run.stdout)
        self.assertEqual(doc["owner_acts"], 7)
        self.assertAlmostEqual(doc["owner_touches_per_card"], 1.4)


class Traps(Base):
    """Shapes from offh-c01-review.md that a keyword or fan-out parser gets wrong."""

    def board(self, n_cards, **kw):
        return trap_board(self.addCleanup, n_cards, **kw)

    def run_rows(self, home, rows):
        return trap_eval(self.addCleanup, home, rows)

    def test_mentions_are_not_acts(self):
        home, (a,) = self.board(1)
        d = "1970-01-12"
        doc = self.run_rows(home, [
            (1, f"stamp wait (chief of staff, {d})", f"{a} awaiting Alex"),
            (2, f"filing (chief of staff, {d})", f"Under Alex's delegation the CoS filed {a}"),
            (3, f"{d} (Alex's delegate)", f"This is for Alex: {a} needs Alex"),
            (4, f"{d} (awaiting Alex, {d})", f"Alex away 3-4 h; {a} waits"),
        ])
        self.assertEqual((doc["owner_acts"], doc["owner_touches_per_card"]), (0, 0.0))

    def test_every_real_actor_shape_is_an_act_and_each_is_found(self):
        """Shapes of the live table (rows 400, 550, 567, 596, joint rows)."""
        home, (a,) = self.board(1)
        d = "1970-01-12"
        shapes = [
            (1, f"Theme 15 (Alex, {d} 16:32)", "Serial within an epic"),
            (2, d, f"Alex (row-393 stamp, {a}): the untrack verb"),
            (3, d, "Alex: the disk floor drops"),
            (4, f"Z8 (Alex + Fable, {d})", "joint row"),
            (5, f"Z9 (Fable / Alex, {d})", "joint row, Alex second"),
            (6, f"Z10 (Alex ran it, {d})", "ran it"),
            (7, d, "Token efficiency (Alex approved in session after the research)"),
            (8, d, "Alex asked for a recap"),
            (9, f"Q (Alex chose A, {d})", "chosen"),
        ]
        doc = self.run_rows(home, shapes)
        self.assertEqual(doc["owner_act_rows"], list(range(1, 10)))

    def test_two_cell_rows_without_a_status_cell_are_read(self):
        """Rows 595-604 are `| n | date | text |`; parse used to drop them (F3)."""
        home, _ = self.board(1)
        rows = [{"number": 601, "topic": "1970-01-12", "bare": True,
                 "text": "Alex (AskUserQuestion): two layers"}]
        doc = touches.evaluate(home, "t", vault=write_vault(tmp(self.addCleanup), rows))
        self.assertEqual(doc["owner_act_rows"], [601])

    def test_one_row_naming_many_cards_is_one_act(self):
        home, ids = self.board(5)
        doc = self.run_rows(home, [(1, "GO (Alex, 1970-01-12)", f"GO {' '.join(ids)}")])
        self.assertEqual(doc["owner_acts"], 1)
        self.assertAlmostEqual(doc["owner_touches_per_card"], 0.2)

    def test_same_day_row_for_a_spool_act_counts_once_other_day_twice(self):
        home, ids = self.board(1, spool_card=0)
        text = f"Alex approved {ids[0]}"
        same = self.run_rows(home, [(1, "approval (Alex, 1970-01-12)", text)])
        self.assertEqual(same["owner_acts"], 1)
        other = self.run_rows(home, [(1, "approval (Alex, 1970-01-13)", text)])
        self.assertEqual(other["owner_acts"], 2)

    def test_same_day_is_the_local_day_not_utc(self):
        """19:00 UTC on the 12th is 00:30 on the 13th in Kolkata: a row dated the 13th is it."""
        home, ids = self.board(1, spool_card=0, spool_at=11 * 86400 + 19 * 3600)
        text = f"Alex approved {ids[0]}"
        local = self.run_rows(home, [(1, "approval (Alex, 1970-01-13)", text)])
        self.assertEqual(local["owner_acts"], 1)
        utc = self.run_rows(home, [(1, "approval (Alex, 1970-01-12)", text)])
        self.assertEqual(utc["owner_acts"], 2)

    def test_a_migration_reset_file_is_not_an_act_to_dedupe_against(self):
        home, ids = self.board(1, spool_card=0, tag="migration-reset")
        row = (1, "approval (Alex, 1970-01-12)", f"Alex approved {ids[0]}")
        doc = self.run_rows(home, [row])
        self.assertEqual((doc["owner_acts"], doc["migration_resets"]), (1, 1))

    def test_row_without_a_card_id_is_an_act_for_all_in_but_not_landed_only(self):
        home, _ = self.board(1)
        doc = self.run_rows(home, [(1, "rule X (Alex, 1970-01-12)", "Alex: allow rule X"),
                                   (2, "GO (Alex, 1970-01-12)", "GO t_ab")])
        self.assertEqual(doc["owner_acts"], 2)
        self.assertEqual(doc["owner_touches_per_landed_card_only"], 0.0)

    def test_runs_without_token_counts_cost_zero_not_a_crash(self):
        home, (a,) = self.board(1)
        conn = db.connect(paths.board_db("t", home))
        conn.execute("INSERT INTO task_runs (task_id, profile, status, started_at) VALUES"
                     " (?, 'implementer', 'done', 5)", (a,))
        conn.close()
        doc = self.run_rows(home, [])
        self.assertEqual((doc["fresh_tokens_total"], doc["fresh_tokens_max_card"]), (0, 0))

    def test_rows_all_before_the_window_and_no_spool_is_unmeasured(self):
        """Review r2 R1: only rows IN the window count as a data source."""
        home, (a, b) = self.board(2)
        old = [{"number": 1, "topic": "GO (Alex, 1969-12-31)", "text": f"GO {a}"},
               {"number": 2, "topic": "1969-12-30", "text": "awaiting Alex"}]
        vault = write_vault(tmp(self.addCleanup), old)
        doc = touches.evaluate(home, "t", vault=vault, since=100)
        self.assertEqual(doc["landed_cards"], 2)
        self.assertEqual(doc["owner_act_rows"], [])
        self.assertEqual(doc["owner_touches_per_card"], "unmeasured")
        wide = touches.evaluate(home, "t", vault=vault, since=0)  # no window: the same rows count
        self.assertEqual(wide["owner_touches_per_card"], 0.5)

    def test_spool_files_with_no_rows_give_a_number_not_unmeasured(self):
        """Review r2 R2: a spool file is a data source on its own."""
        home, _ = self.board(2, spool_card=0)
        doc = touches.evaluate(home, "t", vault=write_vault(tmp(self.addCleanup), []))
        self.assertEqual(doc["owner_touches_per_card"], 0.5)

    def test_no_rows_and_no_spool_files_is_unmeasured_never_zero(self):
        home, _ = self.board(2)
        empty = touches.evaluate(home, "t", vault=tmp(self.addCleanup))  # no table at all
        parsed_none = touches.evaluate(home, "t", vault=write_vault(tmp(self.addCleanup), []))
        for doc in (empty, parsed_none):
            self.assertEqual(doc["owner_touches_per_card"], "unmeasured")
            self.assertEqual(doc["owner_touches_per_landed_card_only"], "unmeasured")
            self.assertTrue(any("unmeasured" in c for c in doc["caveats"]))
        self.assertIn("no bar (unmeasured)", report.render_md(parsed_none))
        mention = (1, "note (chief of staff, 1970-01-12)", "awaiting Alex")
        self.assertEqual(self.run_rows(home, [mention])["owner_touches_per_card"], 0.0)

    def test_a_row_number_repeated_in_the_table_is_one_act(self):
        home, (a,) = self.board(1)
        row = (1, "GO (Alex, 1970-01-12)", f"GO {a}")
        self.assertEqual(self.run_rows(home, [row, row])["owner_acts"], 1)

    def test_an_alex_row_is_found_in_the_last_topic_parenthetical_only(self):
        home, (a,) = self.board(1)
        topic = "(Alex, 1970-01-12) quoted (chief of staff, 1970-01-12)"
        self.assertEqual(self.run_rows(home, [(1, topic, f"filed {a}")])["owner_acts"], 0)



ACTION = r"(?:asked|stamped|chose|approved|ruled|ran)"
OLD = (re.compile(r"^Alex\s*[:(]"), re.compile(rf"\bAlex\s+{ACTION}\b"),
       re.compile(rf"^Alex(?:\s+{ACTION}\b.*)?$", re.IGNORECASE))
ROWS = [
    {"topic": "GO (Alex + Fable, 1970-01-12)", "text": "GO for t_00000001"},
    {"topic": "stamp (alex stamped, 1970-01-12)", "text": "x"},
    {"topic": "x", "text": "Alex (AskUserQuestion): t_00000002 may land"},
    {"topic": "x", "text": "then Alex approved the plan"},
    {"topic": "x", "text": "awaiting Alex"},
    {"topic": "wait (chief of staff)", "text": "Alexander asked nothing"},
    {"topic": "", "text": "Alex: stamps it"},
]


def old_is_actor(r):
    opens, acts, actor = OLD
    topic, text = r.get("topic") or "", r["text"]
    m = touches.LAST_PAREN.search(topic)
    named = m and any(actor.match(t) for t in touches.ACTOR_SPLIT.split(m.group(1)))
    return bool(named or opens.match(topic) or opens.match(text) or acts.search(text))


class Matcher(unittest.TestCase):
    def test_configured_alex_equals_the_old_literal_patterns(self):
        for r in ROWS:
            self.assertEqual(touches._is_actor_row(r, "Alex"), old_is_actor(r), r)
        self.assertTrue(any(old_is_actor(r) for r in ROWS))

    def test_another_name_matches_its_own_rows_and_not_the_old_name(self):
        row = {"topic": "x", "text": "Ada approved the plan"}
        self.assertTrue(touches._is_actor_row(row, "Ada"))
        self.assertFalse(touches._is_actor_row(row, "Alex"))

    def test_the_name_is_escaped(self):
        row = {"topic": "x", "text": "A.B approved it"}
        self.assertTrue(touches._is_actor_row(row, "A.B"))
        other = {"topic": "x", "text": "AxB approved it"}
        self.assertFalse(touches._is_actor_row(other, "A.B"))

    def test_default_name_is_read_from_config(self):
        with mock.patch.dict(os.environ, {"CAPSULE_OWNER_NAME": "Ada"}):
            self.assertTrue(touches._is_actor_row({"topic": "", "text": "Ada ran it"}))


_OWNER_PATCH = None


def setUpModule():
    """The decision-row text below names Alex: the matcher reads the owner name from config."""
    global _OWNER_PATCH
    _OWNER_PATCH = mock.patch.dict(os.environ, {"CAPSULE_OWNER_NAME": "Alex"})
    _OWNER_PATCH.start()


def tearDownModule():
    _OWNER_PATCH.stop()


if __name__ == "__main__":
    unittest.main()
