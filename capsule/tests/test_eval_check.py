"""Card t_31ef5314 (Evals E2, row 617): every weekly eval metric gets a second oracle.
`capsule/eval_check.py` computes oracle A (`touches.evaluate`) and oracle B (the hand ledger for
`owner_acts`, the intervention events for `interventions`), writes one `eval_check` event per
metric and week, and opens one `contradiction` incident (role `eval`) when they disagree.
Oracle 1 is the real module on the E1 fixture boards; oracle 2 is `fixtures/evals/expected.json`
(hand numbers, the `eval_*` keys) and the ledger each test writes by hand. No test reads the real
vault: `vault` and `ledger` are always passed or point into a temp dir."""
import contextlib
import datetime
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from capsule import db, eval_check, human, incidents, intent, outcomes, paths, report, touches
from capsule.tests import test_evals_report as er
from capsule.tests import tmpclean
from capsule.tests.fixtures.evals import boards

DAY = 86400
HEADER = "| week | since | until | acts | landed | by | note |"


def local(y, m, d, hh=0, mm=0):
    return int(datetime.datetime(y, m, d, hh, mm).timestamp())


def week():
    """The fixture week: local Monday 1970-01-12 to the next Monday (T0 falls inside it)."""
    return local(1970, 1, 12), local(1970, 1, 19)


class Case(er.Base):
    def setUp(self):
        super().setUp()
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(tmpclean.sweep, self.dir)
        self.since, self.until = week()

    def board(self, kind, acts=None, by="cos", rows=None):
        """(Built, vault, ledger path, rw conn). `acts=None` writes no ledger row."""
        b, vault = self.built(kind)
        ledger = self.dir / f"ledger-{kind}.md"
        table = [HEADER, "|---|---|---|---|---|---|---|"]
        if acts is not None:
            table.append(f"| 1970-W03 | 1970-01-12 | 1970-01-19 | {acts} | 10 | {by} | hand |")
        ledger.write_text("\n".join(table + (rows or [])) + "\n", encoding="utf-8")
        conn = db.connect(paths.board_db(b.board, b.home))
        self.addCleanup(conn.close)
        return b, vault, ledger, conn

    def run_check(self, b, vault, ledger, conn, days=8, **kw):
        kw.setdefault("since", self.since)
        kw.setdefault("until", self.until)
        return eval_check.check(conn, b.home, b.board, now=self.until + days * DAY, vault=vault,
                                ledger=ledger, **kw)

    @staticmethod
    def events(conn):
        return [json.loads(r[0]) for r in conn.execute(
            "SELECT payload FROM task_events WHERE kind='eval_check' ORDER BY id")]

    @staticmethod
    def eval_incidents(conn):
        return conn.execute("SELECT * FROM incidents WHERE role='eval'").fetchall()


class Agreement(Case):
    def test_1_good_board_agrees_writes_two_events_and_opens_nothing(self):
        b, vault, ledger, conn = self.board("good_board", acts=10)
        rows = {r["metric"]: r for r in self.run_check(b, vault, ledger, conn)}
        want = boards.expected()["good_board"]
        self.assertEqual(rows["owner_acts"]["a"], want["eval_owner_acts_a"][0])
        self.assertEqual(rows["owner_acts"]["b"], want["eval_owner_acts_b"][0])
        self.assertTrue(all(r["agree"] for r in rows.values()) and want["eval_agree"][0])
        self.assertEqual(len(self.events(conn)), 2)
        self.assertEqual(len(conn.execute("SELECT 1 FROM incidents WHERE role='eval'").fetchall()),
                         want["eval_incidents"][0])
        md = report.render_md(touches.evaluate(b.home, b.board, vault=vault))
        self.assertTrue(md.startswith("# Interventions report"))
        self.assertIn("owner_acts a=10 b=10 agree=yes", md)

    def test_cos_authored_interventions_count_on_neither_side(self):
        b, vault, ledger, conn = self.board("good_board", acts=10)
        at, tid = boards.T0 + 700, b.ids["g0"]
        p = human.write(b.home, b.board, tid, "approve", "cos ok", now=at, author="cos")
        p.rename(p.parent / "done" / p.name)
        db.event(conn, tid, "intervention", {
            "kind": "approve", "text": "cos ok", "at": at,
            "file": p.name, "src": p.name, "author": "cos"}, now=at)
        rows = {r["metric"]: r for r in self.run_check(b, vault, ledger, conn)}
        self.assertEqual(rows["interventions"]["a"], rows["interventions"]["b"])
        self.assertTrue(rows["interventions"]["agree"])
        self.assertFalse(conn.execute(
            "SELECT 1 FROM incidents WHERE root_key LIKE '%eval:interventions%'").fetchall())

    def test_event_shape_and_sentinel_task_id(self):
        b, vault, ledger, conn = self.board("good_board", acts=10)
        self.run_check(b, vault, ledger, conn)
        got = conn.execute(
            "SELECT DISTINCT task_id FROM task_events WHERE kind='eval_check'").fetchall()
        self.assertEqual([r[0] for r in got], ["eval"])
        self.assertEqual(tuple(self.events(conn)[0]),
                         ("metric", "a", "b", "tolerance", "agree", "window", "note"))
        self.assertEqual(self.events(conn)[0]["window"], [self.since, self.until])


class Disagreement(Case):
    def test_2_bad_board_opens_exactly_one_eval_incident_and_it_is_line_one(self):
        b, vault, ledger, conn = self.board("bad_board", acts=9)
        want = boards.expected()["bad_board"]
        rows = {r["metric"]: r for r in self.run_check(b, vault, ledger, conn)}
        self.assertEqual((rows["owner_acts"]["a"], rows["owner_acts"]["b"]),
                         (want["eval_owner_acts_a"][0], want["eval_owner_acts_b"][0]))
        self.assertFalse(rows["owner_acts"]["agree"])
        self.assertTrue(rows["interventions"]["agree"])   # migration-reset file and event both out
        found = self.eval_incidents(conn)
        self.assertEqual(len(found), want["eval_incidents"][0])
        self.assertEqual((found[0]["kind"], found[0]["role"], found[0]["task_id"]),
                         ("contradiction", "eval", None))
        self.assertIn("eval:owner_acts", found[0]["root_key"])
        md = report.render_md(touches.evaluate(b.home, b.board, vault=vault))
        self.assertTrue(md.splitlines()[0].startswith(f"EVAL INCIDENT {found[0]['id']}: "))
        self.assertIn("RUNBOOK \"Korin (incidents)\"", md.splitlines()[0])
        self.assertTrue(md.splitlines()[1].startswith("# Interventions report"))
        # idempotence: the same window again writes nothing and opens nothing
        self.assertEqual(self.run_check(b, vault, ledger, conn), [])
        self.assertEqual((len(self.events(conn)), len(self.eval_incidents(conn))), (2, 1))
        # another window the same day dedupes into the open incident (F4)
        self.run_check(b, vault, ledger, conn, since=self.since - 7 * DAY, until=self.since)
        self.assertEqual(len(self.eval_incidents(conn)), 1)

    def test_a_closed_eval_incident_is_not_listed_as_open(self):
        b, vault, ledger, conn = self.board("bad_board", acts=9)
        self.run_check(b, vault, ledger, conn)
        conn.execute("UPDATE incidents SET status='closed', closed_at=? WHERE role='eval'",
                     (self.until,))
        doc = touches.evaluate(b.home, b.board, vault=vault)
        self.assertEqual(doc["eval_incidents"], [])
        self.assertTrue(report.render_md(doc).startswith("# Interventions report"))
        self.assertFalse(doc["eval_checks"]["owner_acts"]["agree"])   # the record stays

    def test_a_superseded_eval_incident_is_not_listed_as_open(self):
        b, vault, ledger, conn = self.board("bad_board", acts=9)
        self.run_check(b, vault, ledger, conn)
        conn.execute("UPDATE incidents SET status='superseded' WHERE role='eval'")
        doc = touches.evaluate(b.home, b.board, vault=vault)
        self.assertEqual(doc["eval_incidents"], [])

    def test_3_one_deleted_intervention_event_breaks_only_the_interventions_check(self):
        b, vault, ledger, conn = self.board("bad_board", acts=7)
        conn.execute("DELETE FROM task_events WHERE kind='intervention' AND task_id=?",
                     (b.ids["b3"],))
        rows = {r["metric"]: r for r in self.run_check(b, vault, ledger, conn)}
        self.assertTrue(rows["owner_acts"]["agree"])
        got = rows["interventions"]
        self.assertEqual((got["a"], got["b"], got["agree"]), (4, 3, False))
        found = self.eval_incidents(conn)
        self.assertEqual(len(found), 1)
        self.assertIn("eval:interventions", found[0]["root_key"])

    def test_the_window_end_is_exclusive_on_side_b(self):
        b, vault, ledger, conn = self.board("good_board", acts=10)
        late = {"kind": "approve", "text": "late", "at": self.until}
        reset = {"kind": "approve", "text": "tag", "at": self.since + 9, "tag": "migration-reset"}
        db.event(conn, b.ids["g0"], "intervention", late, now=self.until)
        db.event(conn, b.ids["g0"], "intervention", reset, now=self.since + 9)
        rows = {r["metric"]: r for r in self.run_check(b, vault, ledger, conn)}
        self.assertEqual((rows["interventions"]["a"], rows["interventions"]["b"]), (4, 4))

    def test_6_the_tolerance_is_read_from_the_data_file(self):
        b, vault, ledger, conn = self.board("bad_board", acts=9)
        loose = self.dir / "tol.json"
        loose.write_text(json.dumps({"owner_acts": {"abs": 2, "rel": 0.1, "delay_days": 7},
                                     "interventions": {"abs": 0, "rel": 0.0, "delay_days": 1}}))
        rows = {r["metric"]: r for r in self.run_check(
            b, vault, ledger, conn, tolerances=eval_check.load_tolerances(loose))}
        self.assertTrue(rows["owner_acts"]["agree"])
        self.assertEqual(self.eval_incidents(conn), [])


class MissingOracle(Case):
    def test_4_a_missing_ledger_row_is_a_disagreement_only_after_the_grace(self):
        b, vault, ledger, conn = self.board("good_board")
        rows = {r["metric"]: r for r in self.run_check(b, vault, ledger, conn, days=8)}
        got = rows["owner_acts"]
        self.assertEqual((got["b"], got["agree"], got["note"]), (None, False, "oracle B missing"))
        self.assertEqual(len(self.eval_incidents(conn)), 1)

    def test_4_three_days_after_the_close_the_ledger_check_is_not_made(self):
        b, vault, ledger, conn = self.board("good_board")
        rows = self.run_check(b, vault, ledger, conn, days=3)
        self.assertEqual([r["metric"] for r in rows], ["interventions"])   # delay 1 d, not 7 d
        self.assertEqual(self.eval_incidents(conn), [])

    def test_5_a_malformed_row_is_missing_and_its_text_is_clipped(self):
        for bad in ("| 1970-W03 | 1970-01-12 | 1970-01-19 | 10 | 10 | cos |",
                    "| 1970-W03 | 1970-01-12 | 1970-01-19 | 10 | 10 | keeper | " + "x" * 200 + "|"):
            with self.subTest(bad=bad[:30]):
                b, vault, ledger, conn = self.board("good_board", rows=[bad])
                rows = {r["metric"]: r for r in self.run_check(b, vault, ledger, conn)}
                row = rows["owner_acts"]
                self.assertEqual((row["b"], row["agree"]), (None, False))
                self.assertTrue(row["note"].startswith("oracle B missing; ledger row malformed"))
                self.assertLessEqual(len(row["note"]), 40 + 22 + 80)   # prefixes + clipped row

    def test_a_malformed_row_for_another_week_leaves_the_missing_note_alone(self):
        other = "| 1970-W04 | 1970-01-19 | 1970-01-26 | 10 | 10 | keeper | x |"
        b, vault, ledger, conn = self.board("good_board", rows=[other])
        rows = {r["metric"]: r for r in self.run_check(b, vault, ledger, conn)}
        self.assertIn("oracle B missing", rows["owner_acts"]["note"])

    def test_the_c01_row_parses_like_any_other(self):
        ledger = self.dir / "c01.md"
        ledger.write_text(f"{HEADER}\n|---|---|---|---|---|---|---|\n"
                          "| C01 | 2026-09-29 | 2026-10-08 | 46 | 24 | cos | bar |\n",
                          encoding="utf-8")
        rows, bad = eval_check._read_ledger(ledger)
        self.assertEqual((rows, bad), ({(local(2026, 9, 29), local(2026, 10, 8)):
                                        {"acts": 46, "landed": 24, "by": "cos"}}, []))


class Weeks(unittest.TestCase):
    def test_8_a_week_closes_on_local_monday_midnight_in_every_zone(self):
        old = os.environ.get("TZ")
        self.addCleanup(er.Base.set_tz, old)
        for zone in ("UTC", "America/Los_Angeles", "Asia/Bangkok"):
            with self.subTest(zone=zone):
                er.Base.set_tz(zone)
                time.tzset()
                after = eval_check._closed_weeks(local(1970, 3, 2, 0, 30), 0, 0)[-1]
                before = eval_check._closed_weeks(local(1970, 3, 1, 23, 30), 0, 0)[-1]
                self.assertEqual(after, (local(1970, 2, 23), local(1970, 3, 2)))
                self.assertEqual(before, (local(1970, 2, 16), local(1970, 2, 23)))

    def test_the_delay_and_the_floor_are_applied(self):
        now = local(1970, 3, 2, 0, 30)
        self.assertEqual(eval_check._closed_weeks(now, 0, 7)[-1][1], local(1970, 2, 23))
        floor = local(1970, 2, 23)
        self.assertEqual(eval_check._closed_weeks(now, floor, 0), [(floor, local(1970, 3, 2))])


class TickPath(Case):
    def setUp(self):
        super().setUp()
        self.b, self.vault, self.ledger, self.conn = self.board("good_board", acts=10)
        (Path(self.vault) / "Knowledge").mkdir()
        (Path(self.vault) / "Knowledge" / eval_check.report.ledger_name()).write_text(
            self.ledger.read_text(encoding="utf-8"), encoding="utf-8")
        for p in (mock.patch.object(outcomes, "SINCE", self.since),
                  mock.patch.dict(os.environ, {"OBSIDIAN_VAULT_PATH": str(self.vault)})):
            p.start()
            self.addCleanup(p.stop)

    def call(self, days, counts):
        human_dir = self.b.home / "boards" / self.b.board / "human"
        cfg = {"board": self.b.board, "autonomy": {"touch_board": self.b.board}}
        outcomes.tick_pass(self.conn, cfg, {"main_checkout": self.dir}, counts,
                           spool_root=None, human_dir=human_dir, now=self.until + days * DAY)

    def test_7_the_check_runs_once_a_day(self):
        first = {"tick_errors": 0}
        self.call(7, first)
        self.assertEqual((first["eval_checks"], len(self.events(self.conn))), (3, 3))
        again = {"tick_errors": 0}
        self.call(7, again)
        self.assertEqual((again["eval_checks"], len(self.events(self.conn)), again["tick_errors"]),
                         (0, 3, 0))
        # the fixture board has token rows but no spool dirs: only the tokens check disagrees
        self.assertEqual([r["root_key"].split(":")[-1] for r in self.eval_incidents(self.conn)],
                         ["fresh_tokens"])

    def test_7_a_failing_check_is_counted_and_incidents_are_still_set(self):
        counts = {"tick_errors": 0}
        with mock.patch.object(eval_check, "tick_step", side_effect=RuntimeError("boom")), \
                mock.patch("sys.stderr"):
            self.call(7, counts)
        self.assertEqual(counts["tick_errors"], 1)
        self.assertIn("incidents", counts)

    def test_a_weekless_pass_before_the_floor_writes_nothing(self):
        counts = {"tick_errors": 0}
        self.call(-3, counts)
        self.assertEqual((counts["eval_checks"], self.events(self.conn)), (0, []))


class LedgerDue(Case):
    """inc_6e917366: the tick asks for the weekly ledger row before the check runs."""
    def setUp(self):
        super().setUp()
        self.b, self.vault, _, self.conn = self.board("good_board", acts=None)
        (Path(self.vault) / "Knowledge").mkdir()
        self.vault_ledger = Path(self.vault) / "Knowledge" / eval_check.report.ledger_name()
        self.put([])
        for p in (mock.patch.object(outcomes, "SINCE", self.since),
                  mock.patch.dict(os.environ, {"OBSIDIAN_VAULT_PATH": str(self.vault)})):
            p.start()
            self.addCleanup(p.stop)

    def put(self, rows):
        self.vault_ledger.write_text("\n".join([HEADER, "|---|---|---|---|---|---|---|"] + rows)
                                     + "\n", encoding="utf-8")

    def tick(self, days, **kw):
        cfg = {"board": self.b.board, "autonomy": {"touch_board": self.b.board}}
        return eval_check.tick_step(self.conn, cfg, self.b.home,
                                    self.until + days * DAY, since_floor=self.since, **kw)

    def due(self):
        return [json.loads(r[0]) for r in self.conn.execute(
            "SELECT payload FROM task_events WHERE kind='eval_ledger_due' AND task_id='eval'")]

    def lines(self):
        return [r[0] for r in self.conn.execute("SELECT body FROM outbox WHERE channel='file'")]

    def owner_incidents(self):
        return [r for r in self.eval_incidents(self.conn) if r["root_key"].endswith("owner_acts")]

    def test_a_closed_week_without_a_row_files_one_nudge_before_the_check(self):
        self.tick(2)
        self.assertEqual([e["window"] for e in self.due()], [[self.since, self.until]])
        (line,) = self.lines()
        for want in ("1970-01-12", "1970-01-19", eval_check.report.ledger_name()):
            self.assertIn(want, line)
        self.assertEqual(self.owner_incidents(), [])
        self.tick(3)
        self.assertEqual((len(self.due()), len(self.lines())), (1, 1))
        self.assertEqual([e["nudge"] for e in self.due()], [1])
        self.assertEqual(self.owner_incidents(), [])

    def ledger_md(self):
        return report.render_md(touches.evaluate(self.b.home, self.b.board, vault=self.vault))

    def test_a_row_still_missing_the_day_before_the_check_files_a_second_nudge_and_a_report_line(
            self):
        ledger = eval_check.report.ledger_name()
        want = f"LEDGER ROW DUE 1970-01-12 to 1970-01-19: write it in {ledger}"
        self.assertNotIn("LEDGER ROW DUE", self.ledger_md())
        self.tick(2)
        self.assertEqual(([e["nudge"] for e in self.due()], len(self.lines())), ([1], 1))
        self.assertEqual(self.ledger_md().count(want), 1)
        self.tick(5)
        self.assertEqual((len(self.due()), len(self.lines())), (1, 1))
        self.tick(6)
        self.assertEqual(([e["nudge"] for e in self.due()], len(self.lines())), ([1, 2], 2))
        for want_text in ("1970-01-12", "1970-01-19", eval_check.report.ledger_name(),
                          "the check runs tomorrow"):
            self.assertIn(want_text, self.lines()[1])
        self.assertNotIn("the check runs tomorrow", self.lines()[0])
        self.tick(6.5)
        self.assertEqual((len(self.due()), len(self.lines())), (2, 2))
        self.assertEqual(self.ledger_md().count(want), 1)

    def test_a_late_first_nudge_waits_a_day_before_the_second(self):
        self.tick(6)
        self.tick(6.2)
        self.assertEqual([e["nudge"] for e in self.due()], [1])
        self.tick(7)
        self.assertEqual([e["nudge"] for e in self.due()], [1, 2])

    def test_a_row_or_a_check_stops_the_next_nudge_and_the_report_line(self):
        self.tick(2)
        self.put(["| 1970-W03 | 1970-01-12 | 1970-01-19 | 10 | 10 | cos | hand |"])
        self.tick(6)
        self.assertEqual((len(self.due()), len(self.lines())), (1, 1))
        self.assertNotIn("LEDGER ROW DUE", self.ledger_md())
        self.put([])
        self.assertIn("LEDGER ROW DUE", self.ledger_md())
        self.run_check(self.b, self.vault, self.vault_ledger, self.conn, metrics=("owner_acts",))
        self.tick(6)
        self.assertEqual((len(self.due()), len(self.lines())), (1, 1))
        self.assertNotIn("LEDGER ROW DUE", self.ledger_md())

    def test_a_week_with_a_valid_row_gets_no_event(self):
        self.put(["| 1970-W03 | 1970-01-12 | 1970-01-19 | 10 | 10 | cos | hand |"])
        self.tick(2)
        self.assertEqual((self.due(), self.lines()), ([], []))

    def test_a_week_inside_the_due_days_gets_no_event(self):
        self.tick(0.5)
        self.assertEqual((self.due(), self.lines()), ([], []))

    def test_a_week_with_an_owner_acts_check_gets_no_event(self):
        self.run_check(self.b, self.vault, self.vault_ledger, self.conn, metrics=("owner_acts",))
        self.tick(2)
        self.assertEqual((self.due(), self.lines()), ([], []))

    def test_write_false_and_the_dry_run_cli_write_nothing(self):
        self.tick(2, write=False)
        args = [self.b.board, "--capsule-home", str(self.b.home), "--dry-run"]
        with mock.patch.object(outcomes, "SINCE", int(time.time()) - 60 * DAY), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(eval_check.main(args), 0)
        self.assertEqual((self.due(), self.lines()), ([], []))

    def test_a_malformed_row_counts_as_missing_and_the_line_carries_its_note(self):
        bad = "| 1970-W03 | 1970-01-12 | 1970-01-19 | ten | 10 | cos | hand |"
        self.put([bad])
        self.tick(2)
        (line,) = self.lines()
        note = self.due()[0]["note"]
        self.assertIn("ten", note)
        self.assertIn(note, line)


class Rehearsal(Case):
    def test_dry_run_writes_no_event_and_opens_nothing(self):
        b, vault, ledger, conn = self.board("bad_board", acts=9)
        rows = self.run_check(b, vault, ledger, conn, write=False)
        self.assertEqual(len(rows), 2)
        self.assertEqual((self.events(conn), self.eval_incidents(conn)), ([], []))


class Cli(Case):
    def test_the_cli_dry_run_prints_rows_for_a_window_and_writes_nothing(self):
        b, vault, ledger, conn = self.board("bad_board", acts=9)
        out = io.StringIO()
        args = [b.board, "--capsule-home", str(b.home), "--ledger", str(ledger), "--dry-run",
                "--since", "1970-01-12", "--until", "1970-01-19"]
        with mock.patch.dict(os.environ, {"OBSIDIAN_VAULT_PATH": str(vault)}), \
                contextlib.redirect_stdout(out):
            self.assertEqual(eval_check.main(args), 0)
        rows = {r["metric"]: r for r in json.loads(out.getvalue())}
        self.assertEqual((rows["owner_acts"]["a"], rows["owner_acts"]["b"]), (7, 9))
        self.assertEqual((self.events(conn), self.eval_incidents(conn)), ([], []))

    def test_an_explicit_window_inside_the_delay_still_prints_both_rows_read_only(self):
        b, vault, ledger, conn = self.board("good_board", acts=10)
        until = int(time.time())
        out = io.StringIO()
        args = [b.board, "--capsule-home", str(b.home), "--ledger", str(ledger), "--dry-run",
                "--since", datetime.date.fromtimestamp(until - 7 * DAY).isoformat(),
                "--until", datetime.date.fromtimestamp(until).isoformat()]
        with mock.patch.dict(os.environ, {"OBSIDIAN_VAULT_PATH": str(vault)}), \
                contextlib.redirect_stdout(out):
            self.assertEqual(eval_check.main(args), 0)
        self.assertEqual(len(json.loads(out.getvalue())), 2)

    def test_the_cli_without_a_window_sweeps_closed_weeks_since_the_floor(self):
        b, vault, ledger, conn = self.board("good_board", acts=10)
        out = io.StringIO()
        args = [b.board, "--capsule-home", str(b.home), "--ledger", str(ledger), "--dry-run"]
        floor = int(time.time()) - 60 * DAY
        with mock.patch.object(outcomes, "SINCE", floor), \
                mock.patch.dict(os.environ, {"OBSIDIAN_VAULT_PATH": str(vault)}), \
                contextlib.redirect_stdout(out):
            self.assertEqual(eval_check.main(args), 0)
        windows = {tuple(r["window"]) for r in json.loads(out.getvalue())}
        self.assertTrue(windows and all(since >= floor for since, _ in windows))
        self.assertEqual(self.events(conn), [])


class IntentExamples(Case):
    def test_ships_standalone_examples_that_lint(self):
        self.assertIn("eval_check", intent.STANDALONE)
        self.assertEqual(len(intent.load("eval_check")), 6)
        self.assertEqual(intent.lint(paths.INTENT / "eval_check.jsonl", None, "eval_check"), [])

    def test_every_example_is_judged_by_the_real_check(self):
        for ex in intent.load("eval_check"):
            doc = ex["given"]["document"]
            if "tokens" in doc:    # judged in test_eval_tokens.Intent
                continue
            kind = next(k for k, v in boards.expected().items()
                        if k != "_note" and v.get("owner_acts", [None])[0] == doc["a"])
            b, vault, ledger, conn = self.board(kind, acts=doc["ledger"])
            rows = []
            for _ in range(doc.get("runs", 1)):
                rows = self.run_check(b, vault, ledger, conn, days=doc["days_since_close"]) or rows
            acts = next(r for r in rows if r["metric"] == "owner_acts")
            got = {"agree": acts["agree"], "incidents": len(self.eval_incidents(conn))}
            self.assertEqual(intent._differs(ex["expect"], got, None), [], ex["id"])
            conn.close()



class OldNames(unittest.TestCase):
    def test_old_tolerance_and_metric_names_still_read(self):
        self.assertEqual(eval_check.LEGACY_METRICS, {human.LEGACY_OWNER + "_acts": "owner_acts"})
        old = Path(tempfile.mkdtemp()) / "tol.json"
        old.write_text(json.dumps({human.LEGACY_OWNER + "_acts": {"abs": 1},
                                   "interventions": {"abs": 1}}))
        self.assertEqual(sorted(eval_check.load_tolerances(old)), ["interventions", "owner_acts"])


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
