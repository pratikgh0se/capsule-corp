# mutation-check: targets-predate-range b957739c9 -- rename only, no behaviour pinned ::
#   Landed.test_since_until_filter_inclusive_local_dates,
#   Landed.test_three_landing_counts_per_week_and_period_by_hand,
#   Periods.test_default_override_and_cutover, ReadOnlyAndOutput.test_no_forbidden_imports,
#   Reverts.test_reverts_per_week_linked_and_unlinked,
#   Tokens.test_tokens_per_landed_card_and_coverage,
#   WaitWork.test_blocked_time_stops_at_completion_not_the_later_close
"""meters.py (W323): every metric checked against hand-computed values on a fixture board.

Fixture (Asia/Bangkok, Mon 2026-10-05 = ISO W41; built here, never the real board):
  A  created Mon 10:00, runs 10:30-11:00 + 12:00-12:30 (work 3600s), review_deferred 11:00 ->
     review_started 11:20 (1200s), wait 1800 + 1200 = 3000s, factory-landed 12:30, in150/out15.
     Plus a close stub (no profile, zero length) that must not count.
  B  created Mon 10:00, run 10:45-11:15 (overlaps A's first run: 2 lanes), blocked 11:15 ->
     unblocked 11:45 (1800s), wait 2700 + 1800 = 4500s, work 1800s, landed Tue 23:30 (night).
  C  done Mon 12 Oct 10:00: its `completed` event sits on a stub run ("CLOSE: obsolete"), so
     it is not a factory landing: not landed, and unmeasured (no real run), not 0.
  K  done Tue 13 Oct 10:00, no run at all, `completed` event with run_id NULL and "CLOSE: landed
     9f9f9f9f (W300)": landed outside the factory.
  E  created Mon 12 Oct 22:00, one failed run Tue 02:00-02:40: work 2400s, wait 14400s, 03:00.
  D  ready (never landed), one run Tue 6 Oct 10:00 with ended_at NULL.
  F  created Sat 24 Oct 09:00, run 09:10-09:20, work 600s, wait 600s, landed 10:00.
  G  closed "Obsolete ... (landed x)" Wed 7 Oct: not landed; 3 spawn_failed runs at one second.
  H  closed "landed 1a54e1a4 ...", no completed event, Thu 8 Oct 10:00: landed outside the
     factory; one real run Wed 09:10-09:40 (work 1800s, no wait).
  R  a guide_ruling: card: excluded from every card metric.
  X  created Mon 26 Oct 09:00, run 09:10-09:20, blocked 09:20, completed 10:00, but CoS closed it
     Mon 2 Nov: wait 600 + 2400 = 3000s, never running on to the close.
"""

import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import meters  # noqa: E402
import pricing  # noqa: E402
from capsule import intent, lookout_ship  # noqa: E402
from capsule.tests.private_names import retired_only, retired_word  # noqa: E402

BKK = ZoneInfo("Asia/Bangkok")
NA = meters.NO_AUTHOR


def T(day, hh, mm=0, month=10):
    return int(datetime(2026, month, day, hh, mm, tzinfo=BKK).timestamp())


SCHEMA = """
CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
  created_at INTEGER NOT NULL, completed_at INTEGER);
CREATE TABLE task_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, profile TEXT,
  started_at INTEGER NOT NULL, ended_at INTEGER, outcome TEXT, model TEXT, tokens_in INTEGER,
  tokens_out INTEGER, tokens_cache_read INTEGER, tokens_cache_write INTEGER, error_class TEXT);
CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
  kind TEXT NOT NULL, payload TEXT, created_at INTEGER NOT NULL, run_id INTEGER);
"""
TASKS = [  # id, title, status, created, completed_at
    ("A", "a", "done", T(5, 10), T(5, 12, 30)), ("B", "b", "done", T(5, 10), T(6, 23, 30)),
    ("C", "c", "done", T(12, 9), T(12, 10)), ("E", "e", "done", T(12, 22), T(13, 3)),
    ("D", "d", "ready", T(5, 9), None), ("F", "f", "done", T(24, 9), T(24, 10)),
    ("K", "k", "done", T(13, 9), T(13, 10)), ("G", "g", "done", T(7, 9), T(7, 10)),
    ("H", "h", "done", T(7, 9), T(8, 10)),
    ("R", "guide_ruling: t_x reviewer run", "done", T(7, 9), T(7, 11)),
    ("X", "x", "done", T(26, 9), T(2, 12, 11)),
]
N = (None,) * 5  # no model / token columns
RUNS = [  # task, profile, start, end, outcome, model, in, out, cache_read, cache_write
    ("A", "dev", T(5, 10, 30), T(5, 11), "completed", "claude-opus-5", 100, 10, 1000, 5),
    ("A", "dev", T(5, 12), T(5, 12, 30), "completed", "m", 50, 5, 500, None),
    ("A", None, T(5, 13), T(5, 13), "completed", *N),  # close stub
    ("B", "dev", T(5, 10, 45), T(5, 11, 15), "completed", *N),
    ("E", "dev", T(13, 2), T(13, 2, 40), "failed", *N),
    ("D", "dev", T(6, 10), None, None, *N),
    ("F", "dev", T(24, 9, 10), T(24, 9, 20), "completed", *N),
    *[("G", "dev", T(7, 9, 30), T(7, 9, 30), "spawn_failed", *N)] * 3,
    ("H", "dev", T(7, 9, 10), T(7, 9, 40), "completed", *N),
    ("X", "dev", T(26, 9, 10), T(26, 9, 20), "completed", *N),
    ("C", None, T(12, 10), T(12, 10), "completed", *N),  # close stub, id 13
]
RUN_OF = {"A": 2, "B": 4, "E": 5, "F": 7, "X": 12, "C": 13}  # the run each `completed` names
CLOSE_H = {"reason": "landed 1a54e1a4 (W311), deployed"}
EVENTS = [  # task, kind, payload, created_at
    ("A", "request_review", None, T(5, 11)), ("A", "review_deferred", None, T(5, 11)),
    ("A", "review_started", None, T(5, 11, 20)), ("A", "completed", None, T(5, 12, 30)),
    ("A", "intervention", {"kind": "accept", "author": "human", "at": T(5, 14)}, T(5, 14)),
    ("B", "blocked", None, T(5, 11, 15)),
    ("B", "intervention", {"author": "cos", "at": T(5, 11, 45)}, T(5, 11, 45)),
    ("B", "unblocked", None, T(5, 11, 45)), ("B", "completed", None, T(6, 23, 30)),
    ("C", "intervention", {"kind": "close", "at": T(12, 10)}, T(12, 10)),
    ("C", "completed", {"summary": "CLOSE: obsolete"}, T(12, 10)),
    ("K", "completed", {"summary": "CLOSE: landed 9f9f9f9f (W300)"}, T(13, 10)),
    ("E", "completed", None, T(13, 3)),
    ("F", "completed", None, T(24, 10)),
    ("G", "closed", {"reason": "Obsolete: superseded by t_cc39bb0c (landed 17474e14)"}, T(7, 10)),
    ("H", "closed", CLOSE_H, T(8, 10)), ("R", "completed", None, T(7, 11)),
    ("X", "blocked", None, T(26, 9, 20)), ("X", "completed", None, T(26, 10)),
    ("X", "closed", CLOSE_H, T(2, 12, 11)),
]


def build_board(path):
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.executemany("INSERT INTO tasks VALUES (?,?,?,?,?)", TASKS)
    con.executemany(
        "INSERT INTO task_runs (task_id, profile, started_at, ended_at, outcome, model, tokens_in,"
        " tokens_out, tokens_cache_read, tokens_cache_write) VALUES (?,?,?,?,?,?,?,?,?,?)", RUNS)
    con.executemany(
        "INSERT INTO task_events (task_id, kind, payload, created_at, run_id) VALUES (?,?,?,?,?)",
        [(t, k, json.dumps(p) if p else None, ts, RUN_OF.get(t) if k == "completed" else None)
         for t, k, p, ts in EVENTS])
    con.commit()
    con.close()


def build_repo(path):
    who = ("AUTHOR", "COMMITTER")
    ident = {f"GIT_{w}_{k}": v for w in who for k, v in (("NAME", "t"), ("EMAIL", "t@t"))}
    env = {**os.environ, **ident}
    subprocess.run(["git", "init", "-q", path], check=True, env=env)
    for day, msg in [(6, 'Revert "W312 hermetic home"'), (6, "REVERT: something unlinked"),
                     (7, "unrelated change"), (24, "Reverts t_0123abcd landing")]:
        e = {**env, "GIT_AUTHOR_DATE": f"{T(day, 12)} +0700",
             "GIT_COMMITTER_DATE": f"{T(day, 12)} +0700"}
        subprocess.run(["git", "-C", path, "commit", "-q", "--allow-empty", "--no-gpg-sign",
                        "-m", msg], check=True, env=e)


class MetersCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = Path(cls.tmp.name) / "kanban.db"
        cls.repo = str(Path(cls.tmp.name) / "repo")
        build_board(cls.db)
        build_repo(cls.repo)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def rep(self, m=None, **kw):
        return meters._compute(self.db, self.repo, meters._Meters(**(m or {})), **kw)

    def week(self, rep, week, period="legacy-era"):
        return next(r for r in rep["weeks"] if r["week"] == week and r["period"] == period)

    def period(self, rep, name):
        return next(r for r in rep["periods"] if r["period"] == name)

    def sql(self, q):
        return sqlite3.connect(f"file:{self.db}?mode=ro", uri=True).execute(q).fetchone()[0]


class Landed(MetersCase):
    def test_three_landing_counts_per_week_and_period_by_hand(self):
        rep = self.rep()
        got = [(r["week"], r["period"], r["landed"], r["landed_outside"], r["not_landed"])
               for r in rep["weeks"]]
        self.assertEqual(got, [("2026-W41", "legacy-era", 2, 1, 1),   # A,B / H / G
                               ("2026-W42", "legacy-era", 1, 1, 1),   # E / K / C
                               ("2026-W43", "off-legacy-push", 1, 0, 0),
                               ("2026-W44", "off-legacy-push", 1, 0, 0)])
        self.assertEqual([self.period(rep, p)["landed"] for p in ("legacy-era", "off-legacy-push")],
                         [3, 2])

    def test_classes_sum_to_raw_sql_count_without_ruling_cards(self):
        rep = self.rep()
        raw = self.sql("SELECT count(*) FROM tasks WHERE status='done' AND completed_at IS NOT NULL"
                       " AND title NOT LIKE 'guide_ruling:%'")
        keys = ("landed", "landed_outside", "not_landed")
        total = sum(p[k] for p in rep["periods"] for k in keys)
        self.assertEqual((raw, total), (9, 9))

    def test_landing_class_from_real_run_and_close_reason(self):
        def ev(kind, run_id=None, **p):
            return [{"kind": kind, "payload": json.dumps(p), "created_at": 5, "run_id": run_id}]

        task, land = {"completed_at": 99}, meters._landing
        self.assertEqual(land(task, ev("completed", 7), {7}), ("landed", 5))
        self.assertEqual(land(task, ev("completed", 3, summary="CLOSE: obsolete"), {7}),
                         ("not_landed", 99))              # completed on a stub run
        self.assertEqual(land(task, ev("completed", None, summary="CLOSE: landed abc"), {7}),
                         ("landed_outside", 99))
        self.assertEqual(land(task, ev("completed", 3, summary="Already fixed"), {7}),
                         ("not_landed", 99))
        self.assertEqual(land(task, ev("closed", reason="landed abc"), set()),
                         ("landed_outside", 99))
        self.assertEqual(land(task, ev("closed", reason="Won't fix: landed nowhere"), set()),
                         ("not_landed", 99))
        self.assertEqual(land(task, [], set()), ("not_landed", 99))

    def test_since_until_filter_inclusive_local_dates(self):
        self.assertEqual(self.period(self.rep(since="2026-10-12"), "legacy-era")["landed"], 1)
        r = self.rep(until="2026-10-12")                  # E lands 13 Oct 03:00 local: out
        self.assertEqual(self.period(r, "legacy-era")["landed"], 2)       # A, B (C is a stub close)
        self.assertEqual([p["period"] for p in r["periods"]], ["legacy-era"])


class Concurrency(MetersCase):
    def test_inferred_concurrency_per_week_ignores_stubs_and_bursts(self):
        rep = self.rep()
        self.assertEqual(self.week(rep, "2026-W41")["max_concurrency_inferred"], 2)  # A.r1 + B
        self.assertEqual(self.week(rep, "2026-W42")["max_concurrency_inferred"], 1)
        self.assertIn("inferred", rep["meta"]["lanes"])

    def test_run_that_never_ran_is_not_a_lane(self):
        runs = [{"started_at": 100, "ended_at": 100}, {"started_at": 100, "ended_at": None}]
        self.assertEqual(meters._max_concurrency(runs, lambda ts: "k"), {})
        runs.append({"started_at": 100, "ended_at": 160})
        self.assertEqual(meters._max_concurrency(runs, lambda ts: "k"), {"k": 1})


class RunFilter(MetersCase):
    def test_stubs_and_spawn_failed_burst_are_not_runs(self):
        w41 = self.week(self.rep(), "2026-W41")
        # real: A.r1, A.r2, B, D (unended), H; dropped: A's close stub, G's 3 spawn_failed
        self.assertEqual((w41["runs"], w41["ended_at_null"], w41["ended_at_null_rate"]),
                         (5, 1, 0.2))
        self.assertEqual(w41["runs_by_daynight"], {"day_completed": 4, "day_other": 1})

    def test_filter_is_what_the_sql_says(self):
        self.assertEqual(self.sql("SELECT count(*) FROM task_runs"), 13)
        self.assertEqual(sum(p["runs"] for p in self.rep()["periods"]), 8)


class WaitWork(MetersCase):
    def test_wait_and_work_medians_by_hand(self):
        w41 = self.week(self.rep(), "2026-W41")
        self.assertEqual(w41["work_h_total"], 2.0)      # A 3600 + B 1800 + H 1800 (outside)
        self.assertEqual(w41["work_h_median"], 0.5)
        self.assertEqual(w41["wait_h_total"], 2.08)     # factory only: (3000 + 4500) / 3600
        self.assertEqual(w41["wait_h_median"], 1.04)    # median(3000, 4500) = 3750s
        w42 = self.week(self.rep(), "2026-W42")         # E only: 2400s work, 14400s wait
        self.assertEqual((w42["work_h_median"], w42["wait_h_median"]), (0.67, 4.0))
        self.assertEqual((w42["cards_unmeasured"], w41["cards_unmeasured"]), (2, 1))  # C, K ; G

    def test_blocked_time_stops_at_completion_not_the_later_close(self):
        w44 = self.week(self.rep(), "2026-W44", "off-legacy-push")
        self.assertEqual((w44["work_h_median"], w44["wait_h_median"]), (0.17, 0.83))  # 600s/3000s

    def test_work_total_matches_independent_sql_sum(self):
        sec = self.sql(
            "SELECT sum(ended_at - started_at) FROM task_runs WHERE profile IS NOT NULL AND task_id"
            " IN (SELECT id FROM tasks WHERE status='done' AND title NOT LIKE 'guide_ruling:%')")
        self.assertEqual(sec, 3600 + 1800 + 2400 + 600 + 600 + 1800)
        total = sum(r["work_h_total"] for r in self.rep()["periods"])
        self.assertAlmostEqual(total, sec / 3600, delta=0.02)

    def test_ruling_card_is_in_no_card_metric(self):
        rep = self.rep()
        self.assertEqual(sum(p["landed"] + p["landed_outside"] + p["not_landed"]
                             for p in rep["periods"]), 9)    # R would make it 10

    def test_card_times_blocked_and_deferred_intervals_union(self):
        task = {"created_at": 0}
        runs = [{"started_at": 100, "ended_at": 200}]
        evs = [{"kind": k, "created_at": ts} for k, ts in (
            ("review_deferred", 300), ("blocked", 350), ("review_started", 500),
            ("unblocked", 600))]
        # wait 100 + union([300,500],[350,600]) = 100 + 300; work 100
        self.assertEqual(meters._card_times(task, runs, evs, 1000), (100, 400))
        self.assertIsNone(meters._card_times(task, [], evs, 1000))


class Tokens(MetersCase):
    def test_tokens_per_landed_card_and_coverage(self):
        w41 = self.week(self.rep(), "2026-W41")
        self.assertEqual(w41["tokens"], dict(zip(meters.TOKEN_COLS, (150, 15, 1500, 5))))
        self.assertEqual(w41["cards_with_tokens"], 1)              # B has runs but no tokens
        self.assertEqual(w41["tokens_per_card"]["tokens_in"], 150)
        self.assertEqual(w41["token_coverage"], 0.4)               # 2 of 5 W41 runs
        self.assertEqual(self.period(self.rep(), "legacy-era")["token_coverage"], 0.333)  # 2 of 6


class Usd(MetersCase):
    # A fixed table, so a quarterly prices.json update cannot break the hand math.
    PRICES = {"claude-opus-5": {"in": 10, "out": 20, "cache_read": 30, "cache_write": None},
              pricing.FALLBACK_MODEL: {"in": 10000, "out": 20000, "cache_read": 30000,
                                       "cache_write": 40000}}

    def test_usd_by_hand_known_and_fallback_model(self):
        # A.r1 opus-5 (null cache_write = 1.25 x in = 12.5): 1000 + 200 + 30000 + 62.5 = 31262.5 e-6
        # A.r2 model "m" -> pricing.FALLBACK_MODEL: 500000 + 100000 + 15000000 = 15600000 e-6
        w41 = self.week(self.rep(prices=self.PRICES), "2026-W41")
        self.assertEqual((w41["usd"], w41["usd_per_card"]), (15.6313, 15.6313))
        self.assertEqual(w41["usd_assumed_runs"], 1)
        self.assertEqual(self.week(self.rep(prices=self.PRICES), "2026-W42")["usd"], 0)

    def test_report_labels_usd_as_notional(self):
        rep = self.rep()
        self.assertIn("notional", rep["meta"]["usd"])
        self.assertIn("notional", self.week(rep, "2026-W41")["usd_basis"])
        self.assertIn("USD is notional", meters._markdown(rep))


class Decisions(MetersCase):
    def test_interventions_by_author_and_latency(self):
        rep = self.rep()
        self.assertEqual(rep["interventions_by_day"], {"2026-10-05": {"human": 1, "cos": 1},
                                                       "2026-10-12": {NA: 1}})
        w41 = self.week(rep, "2026-W41")
        self.assertEqual(w41["decisions"], {"human": 1, "cos": 1, NA: 0})
        # A: request_review 11:00 -> 14:00 = 10800s; B: blocked 11:15 -> 11:45 = 1800s; C: none
        self.assertEqual((w41["decision_latency_n"], w41["decision_latency_h_median"]), (2, 1.75))
        self.assertEqual(self.week(rep, "2026-W42")["decision_latency_n"], 0)
        self.assertIn("not_recorded_before_2026-10-21", meters._markdown(rep))


class DayNight(MetersCase):
    def test_night_boundaries(self):
        m = meters._Meters()
        self.assertEqual([m.is_night(T(5, h)) for h in (23, 6, 7, 22)], [True, True, False, False])
        self.assertTrue(meters._Meters(night=(1, 5)).is_night(T(5, 3)))

    def test_landed_and_run_outcomes_split(self):
        rep = self.rep()
        w41, w42 = self.week(rep, "2026-W41"), self.week(rep, "2026-W42")
        self.assertEqual((w41["landed_day"], w41["landed_night"]), (1, 1))   # A day, B 23:30
        self.assertEqual((w42["landed_day"], w42["landed_night"]), (0, 1))   # E 03:00 only
        self.assertEqual(w42["runs_by_daynight"], {"night_failed": 1})


class Reverts(MetersCase):
    def test_reverts_per_week_linked_and_unlinked(self):
        rep = self.rep()
        w41 = self.week(rep, "2026-W41")
        self.assertEqual((w41["reverts_linked"], w41["reverts_unlinked"]), (1, 1))   # W312 / REVERT
        w43 = self.week(rep, "2026-W43", "off-legacy-push")
        self.assertEqual((w43["reverts_linked"], w43["reverts_unlinked"]), (1, 0))   # t_0123abcd


class Periods(MetersCase):
    def test_default_override_and_cutover(self):
        m = meters._Meters()
        self.assertEqual((m.period(T(22, 23, 59)), m.period(T(23, 0))),
                         ("legacy-era", "off-legacy-push"))
        self.assertEqual(m.period(T(1, 0, month=12)), "off-legacy-push")      # open-ended
        c = meters._Meters(cutover="2026-10-24")
        self.assertEqual((c.period(T(23, 12)), c.period(T(24, 0))), ("off-legacy-push", "baseline"))
        pushed = meters._Meters(push_start="2026-10-12")
        self.assertEqual(pushed.period(T(12, 1)), "off-legacy-push")
        r = self.rep(m={"cutover": "2026-10-24"})
        self.assertEqual(self.week(r, "2026-W43", "baseline")["landed"], 1)


class ReadOnlyAndOutput(MetersCase):
    def run_cli(self, *args, env=None):
        return subprocess.run([sys.executable, str(Path(meters.__file__)), *args], env=env,
                              capture_output=True, text=True, check=True).stdout

    def test_board_is_not_modified_and_ro_handle(self):
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        self.rep()
        self.assertEqual(before, hashlib.sha256(self.db.read_bytes()).hexdigest())
        self.assertFalse(Path(f"{self.db}-wal").exists())
        ro = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)
        with self.assertRaises(sqlite3.OperationalError):
            ro.execute("DELETE FROM tasks")

    def test_cli_json_markdown_and_out_dir(self):
        with tempfile.TemporaryDirectory() as out:
            args = ["--db", str(self.db), "--repo", self.repo]
            js = self.run_cli("--json", *args, "--out", out)
            self.assertEqual(json.loads(js)["periods"][0]["landed"], 3)
            files = sorted(p.name for p in Path(out).iterdir())
            self.assertEqual([f.split(".")[-1] for f in files], ["json", "md"])
            self.assertTrue(all(f.startswith("meters-") for f in files))
            self.assertIn("## Per ISO week", self.run_cli(*args))
            self.assertEqual(len(list(Path(out).iterdir())), 2)    # no --out, no new files

    def test_db_env_override(self):
        env = {**os.environ, meters.DB_ENV: str(self.db)}
        out = self.run_cli("--json", "--repo", self.repo, env=env)
        self.assertEqual(json.loads(out)["meta"]["db"], str(self.db))

    @retired_only
    def test_no_forbidden_imports(self):
        for mod in (meters, pricing):
            src = Path(mod.__file__).read_text().split('"""', 2)[2]
            for bad in ("legacy_gate", "ai_usage", "import budgets", "usage_guard",
                        f".{retired_word()}/",
                        ".claude/projects"):
                self.assertNotIn(bad, src, f"{mod.__name__}: {bad}")


def price_warning(model):
    """(event logged, stderr says so) when the shipper meets one run of `model`."""
    err, old = io.StringIO(), sys.stderr
    sys.stderr = err
    try:
        with mock.patch.object(lookout_ship.events, "log") as log:
            lookout_ship._warn_unpriced([("cap_runs", {"run_id": "r1", "model": model})])
    finally:
        sys.stderr = old
    return log.call_count == 1, "no price for model" in err.getvalue()


class IntentExamples(MetersCase):
    def test_every_example_holds_on_the_real_code(self):
        examples = intent.load("meters")
        self.assertEqual(len(examples), 5)                # an absent file would pass vacuously
        for ex in examples:
            with self.subTest(ex["id"]):
                doc, exp = ex["given"]["document"], ex["expect"]
                if doc["check"] == "night":
                    got = [meters._Meters().is_night(T(5, h)) for h in doc["hours"]]
                    self.assertEqual(got, exp["night"])
                elif doc["check"] == "stub_close":
                    stub = [{"kind": "completed", "run_id": 3, "created_at": 5,
                             "payload": '{"summary": "CLOSE: obsolete"}'}]
                    got = meters._landing({"completed_at": 9}, stub, {7})[0]
                    self.assertEqual(got, exp["class"])
                    self.assertIsNone(meters._card_times({"created_at": 0}, [], stub, 5))
                    self.assertEqual(self.week(self.rep(), "2026-W42")["not_landed"], 1)
                elif doc["check"] in ("price_known", "price_unknown"):
                    # Null cost = no row in the table the shipper upserts into ops-pg `prices`
                    # (v_run_cost left-joins it); the shipper's warning is the visible signal.
                    priced = doc["model"] in {p["model"] for p in lookout_ship._prices()
                                              if p["in"] is not None and p["out"] is not None}
                    warned, said = price_warning(doc["model"])
                    want = (exp["priced"], exp["warned"], exp["warned"])
                    self.assertEqual((priced, warned, said), want)
                    row = pricing.price_row({"model": doc["model"], "input": 1000})
                    self.assertEqual("price_assumed" in row, not exp["priced"])
                else:
                    run = {"started_at": 9, "ended_at": 9}
                    got = meters._max_concurrency([run], lambda t: 0).get(0, 0)
                    self.assertEqual(got, exp["max_concurrency"])


if __name__ == "__main__":
    unittest.main()
