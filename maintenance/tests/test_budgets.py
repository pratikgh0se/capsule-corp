# mutation-check: targets-predate-range ff625e94 -- fixture rename only, no behaviour pinned ::
#   CapsuleCardSpend.test_a_legacy_board_alone_reports_zeros,
#   CapsuleCardSpend.test_a_legacy_era_board_is_ignored
import json, os, sqlite3, sys, tempfile, time, unittest, unittest.mock
from datetime import date as _date
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from capsule.tests import tmpclean  # noqa: E402
tmpclean.install()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["AGENT_OPS_NO_SEND"] = "1"          # tests never send Telegram
os.environ.setdefault("EVENTS_SPOOL", tempfile.mkdtemp(prefix="events-test-") + "/events.jsonl")
os.environ.setdefault("OBSIDIAN_VAULT_PATH", tempfile.mkdtemp(prefix="vault-test-"))
import budgets
from capsule import legacy_names as _LN  # noqa: E402


class Thresholds(unittest.TestCase):
    def setUp(self):
        # Saved and restored via addCleanup (review I7): clearing without
        # restoring leaks OPS_* between classes and makes the decision-hold
        # tests order-dependent.
        env = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(env)))
        for key in list(os.environ):
            if key.startswith("OPS_"):
                del os.environ[key]

    def test_defaults_are_row_126s_numbers(self):
        th = budgets.thresholds()
        self.assertEqual(th["cost_warn"], 1.80)
        self.assertEqual(th["cost_park"], 2.20)
        self.assertEqual(th["runs_warn"], 4)
        self.assertEqual(th["runs_park"], 6)
        self.assertEqual(th["files_route"], 5)
        self.assertEqual(th["files_park"], 10)
        self.assertEqual(th["decision_warn"], 8)
        self.assertEqual(th["decision_hold"], 10)
        self.assertEqual(th["decision_r3_max"], 2)
        self.assertEqual(budgets.REFIT_DATE, "2026-09-24")

    def test_ops_env_key_overrides_a_threshold(self):
        os.environ["OPS_CARD_COST_PARK"] = "3.50"
        self.assertEqual(budgets.thresholds()["cost_park"], 3.50)

    def test_a_malformed_override_falls_back_to_the_constant(self):
        os.environ["OPS_CARD_RUNS_PARK"] = "not-a-number"
        self.assertEqual(budgets.thresholds()["runs_park"], 6)

    def test_flags_default_off(self):
        self.assertFalse(budgets.enabled("card"))
        self.assertFalse(budgets.enabled("decision"))
        self.assertFalse(budgets.enabled("route"))
        os.environ["OPS_CARD_BUDGET"] = "1"
        self.assertTrue(budgets.enabled("card"))


class CapsuleCardSpend(unittest.TestCase):
    """Spend comes from the Capsule board's task_runs."""

    def make(self, rows):
        home = Path(tempfile.mkdtemp(prefix="budgets-capsule-"))
        bdir = home / ".local" / "share" / "capsule" / "boards" / "capsule-corp"
        bdir.mkdir(parents=True)
        con = sqlite3.connect(str(bdir / "kanban.db"))
        con.execute("CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, model TEXT,"
                    " tokens_in INTEGER, tokens_out INTEGER, tokens_cache_read INTEGER,"
                    " tokens_cache_write INTEGER)")
        con.executemany("INSERT INTO task_runs (task_id, model, tokens_in, tokens_out,"
                        " tokens_cache_read, tokens_cache_write) VALUES (?,?,?,?,?,?)", rows)
        con.commit()
        con.close()
        return home

    def test_runs_and_cost_come_from_the_capsule_board(self):
        home = self.make([("t_1", "claude-sonnet-5", 1_000_000, 100_000, 0, 0),
                          ("t_1", "claude-sonnet-5", 1_000_000, 0, 0, 0),
                          ("t_2", "claude-sonnet-5", 5_000_000, 0, 0, 0)])
        spend = budgets.card_spend("capsule-corp", "t_1", home=home)
        self.assertEqual((spend["runs"], spend["unjoined"]), (2, 0))
        self.assertAlmostEqual(spend["cost_usd"], 5.00, places=4)    # 2.00 + 1.00 + 2.00

    def test_a_missing_capsule_board_returns_zeros(self):
        home = Path(tempfile.mkdtemp(prefix="budgets-capsule-empty-"))
        self.assertEqual(budgets.card_spend("nope", "t_1", home=home),
                         {"runs": 0, "cost_usd": 0.0, "unjoined": 0})

    def _legacy_board(self, home, rows):
        bdir = home / f".{_LN.NAME}" / "kanban" / "boards" / "capsule-corp"
        bdir.mkdir(parents=True)
        con = sqlite3.connect(str(bdir / "kanban.db"))
        con.execute("CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT,"
                    " profile TEXT, metadata TEXT)")
        con.executemany("INSERT INTO task_runs (task_id, profile, metadata) VALUES (?,?,?)", rows)
        con.commit()
        con.close()

    def test_a_legacy_era_board_is_ignored(self):
        """The old usage reader is gone: a state file at its old path, even
        passed as boards_dir or with LEGACY_PRESENT=1, changes nothing."""
        home = self.make([("t_1", "claude-sonnet-5", 1_000_000, 0, 0, 0)])
        self._legacy_board(home, [("t_1", "dev", "{}")] * 3)
        with unittest.mock.patch.dict(os.environ, {"LEGACY_PRESENT": "1"}):
            for kwargs in ({}, {"boards_dir": home / f".{_LN.NAME}" / "kanban" / "boards"}):
                spend = budgets.card_spend("capsule-corp", "t_1", home=home, **kwargs)
                self.assertEqual((spend["runs"], spend["unjoined"]), (1, 0))
                self.assertAlmostEqual(spend["cost_usd"], 2.00, places=4)

    def test_a_legacy_board_alone_reports_zeros(self):
        home = Path(tempfile.mkdtemp(prefix="budgets-oldhome-only-"))
        self._legacy_board(home, [("t_1", "dev", "{}")] * 3)
        spend = budgets.card_spend("capsule-corp", "t_1", home=home)
        self.assertEqual(set(spend), {"runs", "cost_usd", "unjoined"})
        self.assertIs(type(spend["runs"]), int)
        self.assertIs(type(spend["unjoined"]), int)
        self.assertIs(type(spend["cost_usd"]), float)
        self.assertEqual((spend["runs"], spend["cost_usd"], spend["unjoined"]), (0, 0.0, 0))
        # Guard against an always-zero reader: the same legacy board plus one real
        # Capsule run for the same card must report that run.
        both = self.make([("t_1", "claude-sonnet-5", 1_000_000, 0, 0, 0)])
        self._legacy_board(both, [("t_1", "dev", "{}")] * 3)
        spend = budgets.card_spend("capsule-corp", "t_1", home=both)
        self.assertEqual(spend["runs"], 1)
        self.assertGreater(spend["cost_usd"], 0.0)


class CardVerdict(unittest.TestCase):
    def setUp(self):
        # Saved and restored via addCleanup (review I7): clearing without
        # restoring leaks OPS_* between classes and makes the decision-hold
        # tests order-dependent.
        env = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(env)))
        for key in list(os.environ):
            if key.startswith("OPS_"):
                del os.environ[key]

    def test_zeros_are_ok_so_a_brake_that_cannot_measure_never_parks(self):
        self.assertEqual(budgets.card_verdict({"runs": 0, "cost_usd": 0.0})[0], "ok")

    def test_the_four_boundaries(self):
        self.assertEqual(budgets.card_verdict({"runs": 3, "cost_usd": 1.79})[0], "ok")
        self.assertEqual(budgets.card_verdict({"runs": 4, "cost_usd": 0.10})[0], "warn")
        self.assertEqual(budgets.card_verdict({"runs": 1, "cost_usd": 1.80})[0], "warn")
        self.assertEqual(budgets.card_verdict({"runs": 6, "cost_usd": 0.10})[0], "park")
        self.assertEqual(budgets.card_verdict({"runs": 1, "cost_usd": 2.20})[0], "park")

    def test_the_park_reason_names_both_numbers(self):
        state, why = budgets.card_verdict({"runs": 7, "cost_usd": 2.31})
        self.assertEqual(state, "park")
        self.assertIn("$2.31", why)
        self.assertIn("7 runs", why)

    def test_park_reason_round_trips_through_is_budget_reason(self):
        reason = "NEEDS REPAIR: " + budgets.park_reason("cost $2.31 over $2.20 (7 runs)")
        self.assertTrue(budgets.is_budget_reason(reason))
        self.assertFalse(budgets.is_budget_reason("NEEDS REPAIR: Fix the tick sound"))
        self.assertFalse(budgets.is_budget_reason(""))


class StateDir(unittest.TestCase):
    """2026-09-11 incident: `unittest discover` on the host wrote 44 real
    `brief-batch.json` rows for a fake card `capsule-corp/t_00000003` because
    `defer()`'s default fell through to the real `maintenance/state` with no
    guard - unlike `verdicts.verdict_dir()`. `budgets.state_dir()` now
    carries the identical override-or-refuse rule; these tests are
    `test_verdicts.Store`'s pattern, one module over."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        for key, value in (("AGENT_OPS_NO_SEND", "1"), ("OPS_BUDGET_STATE", self.tmp.name)):
            self.addCleanup(self._restore, key, os.environ.get(key))
            os.environ[key] = value
        # BATCH/TURNS are lazily resolved (None until a caller or a test
        # overrides them) - a stray patch from another test class leaking
        # past its own cleanup would silently defeat state_dir()'s guard.
        self.addCleanup(self._restore_attr, "BATCH", budgets.BATCH)
        self.addCleanup(self._restore_attr, "TURNS", budgets.TURNS)
        budgets.BATCH = None
        budgets.TURNS = None

    @staticmethod
    def _restore(key, old):
        if old is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = old

    @staticmethod
    def _restore_attr(name, old):
        setattr(budgets, name, old)

    def test_refuses_the_default_dir_under_test(self):
        del os.environ["OPS_BUDGET_STATE"]
        with self.assertRaises(RuntimeError):
            budgets.state_dir()

    def test_defer_with_no_override_raises_under_test(self):
        del os.environ["OPS_BUDGET_STATE"]
        with self.assertRaises(RuntimeError):
            budgets.defer("budget", "x", board="b", task_id="t")

    def test_defer_with_OPS_BUDGET_STATE_writes_there(self):
        n = budgets.defer("budget", "parked - x", board="b", task_id="t")
        self.assertEqual(n, 1)
        written = Path(self.tmp.name) / budgets.BATCH_NAME
        self.assertTrue(written.exists())
        rows = json.loads(written.read_text())
        self.assertEqual(rows[0]["text"], "parked - x")

    def test_an_explicit_path_still_wins_over_the_override(self):
        explicit = Path(self.tmp.name) / "elsewhere.json"
        budgets.defer("budget", "y", board="b", task_id="t", path=explicit)
        self.assertTrue(explicit.exists())
        self.assertFalse((Path(self.tmp.name) / budgets.BATCH_NAME).exists())

    def test_production_path_resolution_is_unchanged_without_the_env(self):
        """No AGENT_OPS_NO_SEND, no OPS_BUDGET_STATE -> byte-identical to the
        pre-fix constant: `maintenance/state/brief-batch.json` /
        `maintenance/state/diff-turns.json`."""
        del os.environ["AGENT_OPS_NO_SEND"]
        del os.environ["OPS_BUDGET_STATE"]
        try:
            self.assertEqual(budgets.state_dir(), budgets.STATE)
            self.assertEqual(budgets._batch_path(), budgets.STATE / "brief-batch.json")
            self.assertEqual(budgets._turns_path(), budgets.STATE / "diff-turns.json")
        finally:
            os.environ["AGENT_OPS_NO_SEND"] = "1"
            os.environ["OPS_BUDGET_STATE"] = self.tmp.name


class Batch(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp(prefix="batch-")) / "brief-batch.json"

    def test_defer_appends_and_brief_lines_render(self):
        budgets.defer("budget", "parked - cost $2.31 over $2.20 (7 runs)",
                      board="sample-app", task_id="t_1", path=self.path, now=1000.0)
        budgets.defer("reclassify", "split · epic (after C6) · drop",
                      board="sample-app", task_id="t_1", path=self.path, now=1001.0)
        lines = budgets.brief_lines(now=1002.0, path=self.path)
        self.assertEqual(lines, [
            "budget: sample-app/t_1 - parked - cost $2.31 over $2.20 (7 runs)",
            "reclassify: sample-app/t_1 - split · epic (after C6) · drop"])

    def test_a_queued_brainstorm_row_is_left_to_the_waiting_block(self):
        budgets.defer("waiting", "Brainstorm - Plan X", board="b", task_id="a" * 16,
                      path=self.path, now=1000.0)
        budgets.defer("waiting", "Brainstorm - not an id", board="b", task_id="t_1",
                      path=self.path, now=1000.0)
        self.assertEqual(budgets.brief_lines(now=1001.0, path=self.path),
                         ["waiting: b/t_1 - Brainstorm - not an id"])

    def test_an_entry_older_than_24h_is_not_printed_but_is_kept(self):
        budgets.defer("budget", "warn - $1.92 / 5 runs", board="b", task_id="t",
                      path=self.path, now=1000.0)
        self.assertEqual(budgets.brief_lines(now=1000.0 + 25 * 3600, path=self.path), [])
        self.assertEqual(len(json.loads(self.path.read_text())), 1)

    def test_an_entry_older_than_7_days_is_pruned(self):
        budgets.defer("budget", "old", board="b", task_id="t", path=self.path, now=1000.0)
        self.assertEqual(budgets.brief_lines(now=1000.0 + 8 * 86400, path=self.path), [])
        self.assertEqual(json.loads(self.path.read_text()), [])

    def test_the_cap_keeps_the_newest(self):
        for i in range(budgets.BATCH_CAP + 5):
            budgets.defer("budget", f"line {i}", board="b", task_id=f"t{i}",
                          path=self.path, now=1000.0 + i)
        rows = json.loads(self.path.read_text())
        self.assertEqual(len(rows), budgets.BATCH_CAP)
        self.assertEqual(rows[-1]["text"], f"line {budgets.BATCH_CAP + 4}")

    def test_an_unwritable_path_never_raises(self):
        self.assertEqual(budgets.defer("budget", "x", path="/proc/nope/batch.json"), 0)
        self.assertEqual(budgets.brief_lines(path="/proc/nope/batch.json"), [])

    def test_a_line_with_no_card_omits_the_dash(self):
        budgets.defer("decisions", "9/10 today", path=self.path, now=1000.0)
        self.assertEqual(budgets.brief_lines(now=1000.0, path=self.path),
                         ["decisions: 9/10 today"])


class Turns(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp(prefix="turns-")) / "diff-turns.json"

    def test_the_fingerprint_is_stable_and_order_independent(self):
        a = budgets.diff_fingerprint(["b.py", "a.py"], {"a.py": [(1, "x")]}, {})
        b = budgets.diff_fingerprint(["a.py", "b.py"], {"a.py": [(1, "x")]}, {})
        self.assertEqual(a, b)

    def test_a_changed_line_count_changes_the_fingerprint(self):
        a = budgets.diff_fingerprint(["a.py"], {"a.py": [(1, "x")]}, {})
        b = budgets.diff_fingerprint(["a.py"], {"a.py": [(1, "x"), (2, "y")]}, {})
        self.assertNotEqual(a, b)

    def test_same_counts_but_different_content_is_not_an_empty_turn(self):
        """Review I3: rewriting the same line differently every round is real
        work - a counts-only fingerprint parked the card for it."""
        a = budgets.diff_fingerprint(["a.py"], {"a.py": [(1, "assert x == 1")]},
                                     {"a.py": [(1, "assert x == 0")]})
        b = budgets.diff_fingerprint(["a.py"], {"a.py": [(1, "assert x == 2")]},
                                     {"a.py": [(1, "assert x == 0")]})
        self.assertNotEqual(a, b)
        self.assertEqual(a, budgets.diff_fingerprint(
            ["a.py"], {"a.py": [(1, "assert x == 1")]}, {"a.py": [(1, "assert x == 0")]}))

    def test_a_run_older_than_a_day_is_forgotten(self):
        """Review I4: a stale run from a previous cut of the card must not
        still be counting against it."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({
            "b/old": {"fp": "aaa", "n": 2, "ts": time.time() - budgets.TURNS_TTL_S - 60},
            "b/new": {"fp": "aaa", "n": 2, "ts": time.time()}}), encoding="utf-8")
        self.assertEqual(budgets.record_turn("b", "old", "aaa", path=self.path), 1)
        self.assertEqual(budgets.record_turn("b", "new", "aaa", path=self.path), 3)

    def test_two_empty_diff_turns_take_three_identical_rounds(self):
        fp = budgets.diff_fingerprint(["a.py"], {}, {})
        self.assertEqual(budgets.record_turn("b", "t", fp, path=self.path), 1)
        self.assertEqual(budgets.record_turn("b", "t", fp, path=self.path), 2)
        n = budgets.record_turn("b", "t", fp, path=self.path)
        self.assertEqual(n, 3)
        self.assertGreaterEqual(n - 1, budgets.EMPTY_DIFF_TURNS)

    def test_a_moving_diff_resets_the_run(self):
        budgets.record_turn("b", "t", "aaa", path=self.path)
        budgets.record_turn("b", "t", "aaa", path=self.path)
        self.assertEqual(budgets.record_turn("b", "t", "bbb", path=self.path), 1)

    def test_cards_do_not_share_state_and_clear_removes_one(self):
        budgets.record_turn("b", "t1", "aaa", path=self.path)
        budgets.record_turn("b", "t2", "aaa", path=self.path)
        budgets.clear_turns("b", "t1", path=self.path)
        state = json.loads(self.path.read_text())
        self.assertNotIn("b/t1", state)
        self.assertIn("b/t2", state)

    def test_an_unwritable_path_never_raises(self):
        self.assertEqual(budgets.record_turn("b", "t", "aaa", path="/proc/nope/t.json"), 1)
        budgets.clear_turns("b", "t", path="/proc/nope/t.json")


class Decisions(unittest.TestCase):
    def setUp(self):
        # Saved and restored via addCleanup (review I7): clearing without
        # restoring leaks OPS_* between classes and makes the decision-hold
        # tests order-dependent.
        env = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(env)))
        for key in list(os.environ):
            if key.startswith("OPS_"):
                del os.environ[key]
        os.environ.pop("OPS_DECISION_OVERRIDE", None)
        self.vault = Path(tempfile.mkdtemp(prefix="vault-"))
        (self.vault / "Decisions" / "sample-app").mkdir(parents=True)

    def note(self, name, cls="R1"):
        (self.vault / "Decisions" / "sample-app" / name).write_text(
            f"---\nclass: {cls}\n---\n\n## Decision\n\nq?\n", encoding="utf-8")

    def log(self, body):
        p = self.vault / "Decisions" / "log.md"
        p.write_text(body, encoding="utf-8")
        return p

    def test_it_counts_todays_cards_and_todays_log_rows(self):
        self.note("2026-09-10-should-we-split.md")
        self.note("2026-09-10-which-model.md", cls="R3")
        self.note("2026-09-09-yesterday.md")
        log = self.log("| # | Topic | Decision |\n|---|---|---|\n"
                       "| 1 | A (owner, 2026-09-10 07:50) | yes |\n"
                       "| 2 | B (owner, 2026-09-09 21:05) | no |\n"
                       "| 3 | C with no date at all | maybe |\n")
        counts = budgets.decision_count(vault=self.vault, today=_date(2026, 9, 10),
                                        log_path=log)
        self.assertEqual(counts["cards"], 2)
        self.assertEqual(counts["log"], 1)
        self.assertEqual(counts["r3"], 1)
        self.assertEqual(counts["total"], 3)

    def test_an_undated_log_row_is_uncounted_the_documented_floor(self):
        log = self.log("| 1 | C with no date at all | maybe |\n")
        counts = budgets.decision_count(vault=self.vault, today=_date(2026, 9, 10),
                                        log_path=log)
        self.assertEqual(counts["log"], 0)   # the count is a FLOOR, spec section 7.6

    def test_a_missing_vault_or_log_is_zero_not_an_error(self):
        counts = budgets.decision_count(vault=Path("/proc/nope"),
                                        today=_date(2026, 9, 10),
                                        log_path=Path("/proc/nope/log.md"))
        self.assertEqual(counts, {"cards": 0, "log": 0, "r3": 0, "total": 0})

    def test_the_verdict_ladder(self):
        self.assertEqual(budgets.decision_verdict({"total": 7, "r3": 0}, cls="R1")[0], "ok")
        self.assertEqual(budgets.decision_verdict({"total": 8, "r3": 0}, cls="R1")[0], "warn")
        self.assertEqual(budgets.decision_verdict({"total": 10, "r3": 0}, cls="R1")[0], "hold")

    def test_a_third_r3_holds_even_under_the_daily_line(self):
        state, why = budgets.decision_verdict({"total": 3, "r3": 2}, cls="R3")
        self.assertEqual(state, "hold")
        self.assertIn("R3", why)
        self.assertEqual(budgets.decision_verdict({"total": 3, "r3": 2}, cls="R1")[0], "ok")

    def test_owner_override_clears_the_hold(self):
        os.environ["OPS_DECISION_OVERRIDE"] = "1"
        self.assertEqual(budgets.decision_verdict({"total": 99, "r3": 9}, cls="R3")[0], "ok")

    def test_the_quiet_window_is_04_to_07_local(self):
        import time as _t
        mk = lambda h: _t.mktime((2026, 9, 10, h, 30, 0, 0, 0, -1))
        self.assertFalse(budgets.in_quiet_hours(mk(3)))
        self.assertTrue(budgets.in_quiet_hours(mk(4)))
        self.assertTrue(budgets.in_quiet_hours(mk(6)))
        self.assertFalse(budgets.in_quiet_hours(mk(7)))


if __name__ == "__main__":
    unittest.main()
