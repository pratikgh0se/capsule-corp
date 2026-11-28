"""Card t_69a86dff, row 584 (2026-10-08): `capsule/cost.py` measures a card's cost host-side from
`task_runs` and adds one advisory line to the landing notice. Two oracles per fact (Z2): the
function's own figure against a hand-computed one (each docstring works the threshold out by
hand) and against `token_accountant`, which reads the same spend from transcripts."""
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path

from capsule import cost, db, event_kinds, intent, paths, ruling_sweep, token_accountant, workspace
from capsule.tests import tmpclean
from capsule.models import Result, claude
from capsule.tests.test_lookout import Board
from capsule.tests.test_tick import BRANCH, IMPL_DONE, TickCase
from capsule.tests.test_token_accountant import DAY, Tree, usage

SHOWN = "(2.5x median), no measured gain"


class BoardCase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(tmpclean.sweep, self.dir)
        db.create_board(self.dir / "kanban.db")
        self.conn = db.connect(self.dir / "kanban.db")
        self.addCleanup(self.conn.close)
        self.clock = 0

    def run_row(self, task_id, tin=0, out=0, cw=0, cr=0, start=0, end=None, role="implementer",
                handoff=None):
        cur = self.conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, tokens_in,"
            " tokens_out, tokens_cache_read, tokens_cache_write, run_id, handoff)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, role, "done", start, end, tin, out, cr, cw, f"r{self.conn.total_changes}",
             handoff))
        return self.conn.execute("SELECT * FROM task_runs WHERE id=?", (cur.lastrowid,)).fetchone()

    def card(self, *, status="done", fresh=None, completed=True):
        tid = db.new_task(self.conn, title="card", assignee="dev", branch="epic/x", status=status)
        if status == "done":
            self.clock += 1
            self.conn.execute("UPDATE tasks SET completed_at=? WHERE id=?", (self.clock, tid))
            if completed:
                db.event(self.conn, tid, "completed", {"gates": {}}, now=self.clock)
        if fresh is not None:
            self.run_row(tid, tin=fresh)
        return tid

    def landed(self, *costs):
        return [self.card(fresh=c) for c in costs]

    def ruling_cards(self, n):
        """n triage ruling cards closed through the real `close_card` (a bare `completed` event)."""
        for _ in range(n):
            ruling_sweep.close_card(self.conn, self.card(status="triage"), self.clock + 100)

    def impl(self, tid, report):
        return self.run_row(tid, handoff=json.dumps(report))

    def land(self, tid, report=None, span=None):
        impl = self.impl(tid, report or {})
        return cost.record(self.conn, tid, impl, span, now=99)

    def cost_event(self, tid):
        sql = "SELECT payload FROM task_events WHERE task_id=? AND kind='cost'"
        return json.loads(self.conn.execute(sql, (tid,)).fetchone()[0])

    def repo(self, added, deleted, extra=()):
        """A real git repo whose base..head adds `added` and deletes `deleted` lines of app.py,
        plus the `extra` {path: lines} files added in head; returns (path, base, head)."""
        top = Path(tempfile.mkdtemp(dir=self.dir))
        root = top / "ws"       # the factory's clone layout: the git dir sits beside the work tree
        root.mkdir()
        (top / workspace.GITDIRS).mkdir()

        def run(*a):
            return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a],
                                  cwd=root, check=True, capture_output=True, text=True)
        run("init", "-q", f"--separate-git-dir={top / workspace.GITDIRS / 'ws'}")
        (root / "app.py").write_text("".join(f"old {i}\n" for i in range(deleted)))
        run("add", "-A")
        run("commit", "-qm", "base")
        base = run("rev-parse", "HEAD").stdout.strip()
        (root / "app.py").write_text("".join(f"new {i}\n" for i in range(added)))
        for path, lines in dict(extra).items():
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            (root / path).write_text("x\n" * lines)
        run("add", "-A")
        run("commit", "-qm", "head")
        return root, base, run("rev-parse", "HEAD").stdout.strip()


class CardCost(BoardCase):
    def test_sums_every_run_fresh_tokens_and_wall_clock_and_reports_cache_reads_apart(self):
        """Hand sum: fresh = (10+5+3) + (20+0+7) + (1+1+0) = 47; cache reads 100+50+0 = 150;
        wall = 120 + 60 = 180s; the third run never ended (killed): tokens, no seconds."""
        tid = self.card()
        self.run_row(tid, 10, 5, 3, 100, start=1000, end=1120)
        self.run_row(tid, 20, 0, 7, 50, start=2000, end=2060, role="reviewer")
        self.run_row(tid, 1, 1, 0, 0, start=3000, end=None, role="validator")
        got = cost.card_cost(self.conn, tid)
        self.assertEqual(got, {"tokens": 47, "cache_read": 150, "wall_s": 180, "runs": 3})
        raw = self.conn.execute("SELECT SUM(tokens_in), SUM(tokens_out), SUM(tokens_cache_write),"
                                " SUM(tokens_cache_read) FROM task_runs WHERE task_id=?",
                                (tid,)).fetchone()
        self.assertEqual((sum(raw[:3]), raw[3]), (got["tokens"], got["cache_read"]))

    def test_runs_with_null_tokens_still_count_their_wall_clock(self):
        tid = self.card()
        self.conn.execute("INSERT INTO task_runs (task_id, profile, status, started_at, ended_at)"
                          " VALUES (?,?,?,?,?)", (tid, "implementer", "crashed", 10, 70))
        self.assertEqual(cost.card_cost(self.conn, tid),
                         {"tokens": 0, "cache_read": 0, "wall_s": 60, "runs": 1})

    def test_agrees_with_the_token_accountant_on_the_same_card(self):
        """Second oracle (row 584): one card, three runs, as task_runs rows and as transcripts.
        By hand: implementer 40+10+50, reviewer 10+10+30, validator 0+5+5 = 160 fresh;
        cache reads 7+0+3 = 10."""
        tid = self.card()
        self.run_row(tid, 40, 10, 50, 7)
        self.run_row(tid, 10, 10, 30, 0, role="reviewer")
        self.run_row(tid, 0, 5, 5, 3, role="validator")
        tree = Tree(self.dir / "transcripts")
        tree.agent("a1", description=f"implementer {tid}", prompt="x",
                   tokens=usage(40, 10, 50, cr=7))
        tree.agent("a2", description=f"reviewer {tid}", prompt="x", tokens=usage(10, 10, 30))
        tree.agent("a3", description=f"validator {tid}", prompt="x", tokens=usage(0, 5, 5, cr=3))
        rep = token_accountant.report(token_accountant.scan(tree.finish(), DAY))
        theirs = {c["card"]: c for c in rep["cards"]}[tid]
        ours = cost.card_cost(self.conn, tid)
        self.assertEqual((ours["tokens"], ours["cache_read"]), (160, 10))
        self.assertEqual((theirs["tokens"], theirs["cache_read"]), (160, 10))


class Advisory(BoardCase):
    def test_fires_only_above_twice_the_median_of_five_or_more_landed_cards(self):
        """Five landed cards of 100 fresh tokens: median 100, threshold 200. 200 is not more."""
        self.landed(100, 100, 100, 100, 100)
        at, over = self.card(status="review", fresh=200), self.card(status="review", fresh=250)
        self.assertEqual(self.land(at), "200 tokens / 0 min")
        self.assertEqual(self.land(over), "250 tokens / 0 min " + SHOWN)

    def test_silent_with_fewer_than_five_landed_cards(self):
        self.landed(100, 100, 100, 100)
        self.assertEqual(self.land(self.card(status="review", fresh=900)), "900 tokens / 0 min")

    def test_closed_ruling_cards_are_not_landed_cards(self):
        self.landed(100, 100, 100, 100)
        self.ruling_cards(3)                        # would make five landed cards if they counted
        self.assertEqual(cost.trailing_median(self.conn, "none"), (100, 4))

    def test_done_cards_with_no_completed_event_do_not_count_as_landed(self):
        self.landed(100, 100, 100, 100)
        self.card(completed=False), self.card(completed=False), self.card(completed=False)
        self.assertEqual(self.land(self.card(status="review", fresh=900)), "900 tokens / 0 min")

    def test_median_is_over_the_last_twenty_landed_cards_only(self):
        """25 old cards at 1000, then 20 at 100: the last 20 have median 100 (threshold 200);
        all 45 would have median 1000. 250 > 200 fires."""
        self.landed(*[1000] * 25)
        self.landed(*[100] * 20)
        self.assertIn(SHOWN, self.land(self.card(status="review", fresh=250)))

    def test_done_cards_that_never_landed_stay_out_of_the_median(self):
        """Five landed cards of 100 (threshold 200) beside six cheap done cards with a run but no
        `completed` event (Korin post-mortems): 150 is under 200 and stays quiet. Counting the
        post-mortems would give median 1 and fire."""
        self.landed(100, 100, 100, 100, 100)
        for _ in range(6):
            self.card(fresh=1, completed=False)
        self.assertEqual(self.land(self.card(status="review", fresh=150)), "150 tokens / 0 min")

    def test_median_is_the_middle_value_not_the_mean(self):
        """Landed 10, 10, 10, 10, 1000: median 10 (threshold 20), mean 208 (threshold 416).
        100 fires under the median and would not under the mean."""
        self.landed(10, 10, 10, 10, 1000)
        self.assertIn("(10.0x median)", self.land(self.card(status="review", fresh=100)))

    def test_the_card_itself_is_not_in_its_own_median(self):
        self.landed(100, 100, 100, 100)
        mine = self.card(status="done", fresh=900)      # the landing marks it done before recording
        self.assertEqual(self.land(mine), "900 tokens / 0 min")

    def test_wall_clock_is_reported_in_whole_minutes(self):
        tid = self.card(status="review")
        self.run_row(tid, 5, start=0, end=100)
        self.assertEqual(self.land(tid), "5 tokens / 2 min")

    def test_each_measured_gain_suppresses_it(self):
        self.landed(100, 100, 100, 100, 100)
        unknown = (self.repo(1, 1)[0], "0" * 40, "HEAD")
        cases = [("deletes as many as it adds", self.repo(10, 10), True),
                 ("adds one more than it deletes", self.repo(10, 9), False),
                 ("git could not count", unknown, False),
                 ("no span at all (a touched test is no gain, t_72237cea)", None, False)]
        for name, span, silent in cases:
            with self.subTest(name):
                got = self.land(self.card(status="review", fresh=250), {}, span)
                self.assertEqual(SHOWN not in got, silent, got)

    def test_the_reports_net_lines_is_never_trusted(self):
        """Report says it deleted as much as it added; git counted +10/-2: the advisory still fires,
        and the event names the report's figure as a mismatch beside git's."""
        self.landed(100, 100, 100, 100, 100)
        tid = self.card(status="review", fresh=250)
        self.assertIn(SHOWN, self.land(tid, {"net_lines": "+10/-10"}, self.repo(10, 2)))
        payload = self.cost_event(tid)
        self.assertEqual((payload["net"], payload["net_mismatch"]), ([10, 2], "+10/-10"))

    def test_a_report_that_agrees_with_git_is_no_mismatch_and_a_false_one_buys_no_gain(self):
        self.landed(100, 100, 100, 100, 100)
        tid = self.card(status="review", fresh=250)
        self.land(tid, {"net_lines": "+3/-1"}, self.repo(3, 1))
        payload = self.cost_event(tid)
        self.assertEqual(payload["net_mismatch"], "")
        other = self.card(status="review", fresh=250)
        self.assertNotIn(SHOWN, self.land(other, {}, self.repo(1, 5)))


    def test_closing_an_incident_is_a_measured_gain(self):
        self.landed(100, 100, 100, 100, 100)
        fix = self.card(status="review", fresh=250)
        self.conn.execute("INSERT INTO incidents (id, board, kind, opened_at, fingerprint,"
                          " fix_card) VALUES ('inc_1', 'b', 'crash', 1, 'fp', ?)", (fix,))
        self.assertNotIn("no measured gain", self.land(fix))

    def test_a_zero_median_never_fires(self):
        self.landed(0, 0, 0, 0, 0)
        self.assertEqual(self.land(self.card(status="review", fresh=5)), "5 tokens / 0 min")


class HostNet(BoardCase):
    """The host's own count of a card's production lines, read back from the `cost` event after a
    landing against a real temp git repo. Hand count: base app.py has 7 lines, head replaces them
    with 3 new lines, so git says +3/-7."""

    def counted(self, span):
        tid = self.card(status="review", fresh=1)
        self.land(tid, {}, span)
        return self.cost_event(tid)["net"]

    def test_counts_production_lines_git_reports(self):
        self.assertEqual(self.counted(self.repo(3, 7)), [3, 7])

    def test_tests_docs_intent_examples_and_notes_do_not_count(self):
        extra = {"capsule/tests/test_x.py": 40, "docs/a.txt": 9, "contracts/intent/x.jsonl": 5,
                 "NOTES.md": 6, "tests/helper.py": 8, "lib.py": 4}
        self.assertEqual(self.counted(self.repo(3, 7, extra)), [3 + 4, 7])

    def test_a_binary_file_adds_no_lines_and_is_not_a_crash(self):
        root, base, _ = self.repo(3, 7)
        (root / "blob.bin").write_bytes(bytes(range(256)) * 4)
        for args in (("add", "-A"), ("commit", "-qm", "blob")):
            subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                           cwd=root, check=True, capture_output=True)
        self.assertEqual(self.counted((root, base, "HEAD")), [3, 7])

    def test_an_unknown_revision_is_none_not_a_raise(self):
        root, base, _ = self.repo(1, 1)
        self.assertIsNone(self.counted((root, base, "0" * 40)))


class Recording(BoardCase):
    def test_writes_one_cost_event_inside_the_allowlist_with_nothing_on_stderr(self):
        tid = self.card(status="review", fresh=7)
        err = StringIO()
        with redirect_stderr(err):
            self.land(tid)
        rows = self.conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='cost'",
                                 (tid,)).fetchall()
        self.assertEqual(len(rows), 1)
        payload = json.loads(rows[0][0])
        self.assertEqual((payload["tokens"], payload["runs"], payload["line"]),
                         (7, 2, "cost: 7 tokens / 0 min"))
        self.assertIsNone(payload["net"])      # no span: git was not asked, so no count (not 0/0)
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(event_kinds.problems("cost", payload), [])

    def test_a_refused_event_never_fails_the_landing(self):
        """Break the event table; `record` tells stderr and still hands back a line."""
        tid = self.card(status="review", fresh=7)
        impl = self.impl(tid, {})
        self.conn.execute("DROP TABLE task_events")
        err = StringIO()
        with redirect_stderr(err):
            got = cost.record(self.conn, tid, impl, None, now=1)
        self.assertEqual(got, "not measured")
        self.assertIn("capsule cost", err.getvalue())

    def test_an_unreadable_handoff_is_told_and_writes_no_event_but_never_raises(self):
        self.landed(100, 100, 100, 100, 100)
        tid = self.card(status="review", fresh=250)
        impl = self.run_row(tid, handoff="{not json")
        err = StringIO()
        with redirect_stderr(err):
            self.assertEqual(cost.record(self.conn, tid, impl, None, now=1), "not measured")
        self.assertIn("capsule cost", err.getvalue())
        self.assertIsNone(
            self.conn.execute("SELECT 1 FROM task_events WHERE kind='cost'").fetchone())


class IntentExamples(BoardCase):
    def test_ships_one_to_three_standalone_examples_citing_the_row(self):
        self.assertIn("cost_line", intent.STANDALONE)
        examples = intent.load("cost_line")
        self.assertTrue(1 <= len(examples) <= 3)
        self.assertEqual(intent.lint(paths.INTENT / "cost_line.jsonl", None, "cost_line"), [])
        for ex in examples:
            self.assertEqual(ex["source"], "row 584, 2026-10-08")

    def test_every_example_is_judged_by_the_real_recorder(self):
        for ex in intent.load("cost_line"):
            with self.subTest(ex["id"]):
                doc = ex["given"]["document"]
                added, deleted = (int(n) for n in doc["net_lines"].replace("+", "").split("/-"))
                root, base, head = self.repo(added, deleted)
                self.landed(*doc["landed_costs"])
                tid = self.card(status="review", fresh=doc["card_tokens"])
                report = {"net_lines": doc.get("reported_net_lines", doc["net_lines"])}
                got = self.land(tid, report, (root, base, head))
                shown = {"advisory": "no measured gain" in got, "line": got}
                self.assertEqual(intent._differs(ex["expect"], shown, None), [], ex["id"])
                self.conn.execute("DELETE FROM task_runs")
                self.conn.execute("DELETE FROM tasks")


class Capture(unittest.TestCase):
    def test_the_claude_adapter_keeps_cache_creation_tokens(self):
        obj = {"type": "result", "subtype": "success", "is_error": False, "result": "{}",
               "structured_output": {"ok": 1},
               "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 3,
                         "cache_creation_input_tokens": 7}}
        self.assertEqual(claude.parse(json.dumps(obj), "", 0).tokens,
                         {"in": 10, "out": 5, "cache_read": 3, "cache_write": 7})

    def test_an_envelope_without_usage_still_carries_all_four_keys_at_zero(self):
        self.assertEqual(claude.parse("not json", "", 1).tokens,
                         {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0})


class LookoutShips(Board):
    def test_run_snapshot_carries_cache_write_tokens_beside_cache_reads(self):
        """Row 584: the tick keeps cache-creation tokens; the run snapshot ships them, not null."""
        tid = db.new_task(self.conn, title="t", branch="epic/screens")
        with db.txn(self.conn):
            self.conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, started_at, run_id, runner,"
                " tokens_cache_read, tokens_cache_write) VALUES (?,?,?,?,?,?,?,?)",
                (tid, "implementer", "done", 1, "r_cw", "host", 11, 7))
        self.collect()
        run = [s["row"] for s in self.snaps("cap_runs") if s["row"]["run_id"] == "r_cw"][-1]
        self.assertEqual((run["tokens_cache_read"], run["tokens_cache_write"]), (11, 7))


class Landing(TickCase):
    """The tick path: four runs with known tokens land a card; the done notice and the `cost`
    event carry the host-measured figure. Hand sum per run 10+5+3 = 18, so 4 runs = 72; cache
    reads 4 * 100."""

    def setUp(self):
        super().setUp()
        self.cfg["slack"] = dict(self.cfg["slack"], enabled=True)
        self.write_cfg()
        self.sent = []
        self.tick = self.make_tick(
            slack_factory=lambda block: lambda row: self.sent.append(row["body"]))

    def fill(self, entry, run):
        doc = super().fill(entry, run)
        tokens = {"in": 10, "out": 5, "cache_read": 100, "cache_write": 3}
        return Result(doc, 0, tokens, 1, None, None, None, "")

    def land(self):
        self.script["implementer"] = [[dict(IMPL_DONE, net_lines="+5/-1")]]
        for _ in range(6):
            self.tick.once()
            if self.task()["status"] == "done":
                break
        self.assertEqual(self.task()["status"], "done")
        self.tick.once()
        # done is kept off Slack (visibility card 7): read the notice from its file row
        return [r[0].replace(" | ", "\n") for r in self.conn.execute(
            "SELECT body FROM outbox WHERE channel='file' AND body LIKE '✅%' ORDER BY id")]

    def test_the_run_rows_keep_cache_writes_and_the_notice_and_event_carry_the_cost(self):
        done = self.land()
        self.assertEqual([r["tokens_cache_write"] for r in self.runs()], [3, 3, 3, 3])
        self.assertEqual(len(done), 1)
        self.assertIn("\ncost: 72 tokens / 0 min\n", done[0] + "\n")
        payload = json.loads(self.conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='cost'",
            (self.tid,)).fetchone()[0])
        self.assertEqual((payload["tokens"], payload["cache_read"], payload["runs"]), (72, 400, 4))
        self.assertIsInstance(payload["net"], list)   # counted by git in the clone, not the report
        self.assertEqual(payload["net_mismatch"], "+5/-1" if payload["net"] != [5, 1] else "")

    def test_a_costly_card_with_no_gain_gets_the_advisory_in_the_notice(self):
        """Five landed cards of 10 tokens: median 10, threshold 20; this card costs 72 = 7.2x."""
        for i in range(5):
            tid = db.new_task(self.conn, title=f"old {i}", assignee="dev", branch=BRANCH,
                              status="done")
            self.conn.execute("UPDATE tasks SET completed_at=? WHERE id=?", (i + 1, tid))
            db.event(self.conn, tid, "completed", {"gates": {}}, now=i + 1)
            self.conn.execute("INSERT INTO task_runs (task_id, profile, status, started_at,"
                              " tokens_in) VALUES (?, 'implementer', 'done', 1, 10)", (tid,))
        done = self.land()
        self.assertIn("cost: 72 tokens / 0 min (7.2x median), no measured gain", done[0])


if __name__ == "__main__":
    unittest.main()
