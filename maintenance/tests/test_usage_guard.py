# mutation-check: targets-predate-range b957739c9 -- rename only, no behaviour pinned ::
#   UsageWindowShape.test_usage_window_is_empty_without_a_limit_and_never_touches_legacy
import os, sys, time, unittest, unittest.mock, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from capsule import legacy_names  # noqa: E402
from capsule.tests import tmpclean  # noqa: E402
tmpclean.install()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["AGENT_OPS_NO_SEND"] = "1"  # tests must never send Telegram messages
import tempfile as _tf
os.environ.setdefault("EVENTS_SPOOL", _tf.mkdtemp(prefix="events-test-") + "/events.jsonl")  # tests never write the live spool
import usage_guard as ug


class PauseAt(unittest.TestCase):
    def test_default_with_no_env(self):
        with unittest.mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPS_PAUSE_AT", None)
            self.assertEqual(ug.pause_at(), 0.85)

    def test_override_raises_the_ceiling(self):
        with unittest.mock.patch.dict(os.environ, {"OPS_PAUSE_AT": "0.99"}):
            self.assertEqual(ug.pause_at(), 0.99)

    def test_invalid_or_out_of_range_falls_back_to_default(self):
        for bad in ("abc", "0", "1.5"):
            with unittest.mock.patch.dict(os.environ, {"OPS_PAUSE_AT": bad}):
                self.assertEqual(ug.pause_at(), 0.85, msg=f"OPS_PAUSE_AT={bad!r}")

    def test_resolved_at_call_time_not_cached(self):
        with unittest.mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPS_PAUSE_AT", None)
            self.assertEqual(ug.pause_at(), 0.85)
            os.environ["OPS_PAUSE_AT"] = "0.99"
            self.assertEqual(ug.pause_at(), 0.99)
            os.environ.pop("OPS_PAUSE_AT", None)
            self.assertEqual(ug.pause_at(), 0.85)


class UsageWindowShape(unittest.TestCase):
    def test_usage_window_return_is_still_a_plain_3_tuple_for_existing_callers(self):
        uw = ug.UsageWindow(0.4, 123.0, 12.0, [("Current week", 0.4, 456.0)])
        util, reset, week_pct = uw  # exactly what board_bridge.py does
        self.assertEqual((util, reset, week_pct), (0.4, 123.0, 12.0))
        self.assertEqual(uw.weekly, [("Current week", 0.4, 456.0)])

    def test_usage_window_is_empty_without_a_limit_and_never_touches_legacy(self):
        home = tempfile.mkdtemp()
        with unittest.mock.patch.dict(os.environ, {"CAPSULE_HOME": home}, clear=False):
            os.environ.pop(legacy_names.OLD_ENV_HOME, None)
            before = list(sys.path)
            uw = ug.usage_window()
            self.assertEqual(tuple(uw), (None, None, None))
            self.assertEqual((uw.weekly, uw.meter), ([], None))
            # the old import was never tried
            self.assertNotIn(legacy_names.OLD_ENV_HOME, os.environ)
            self.assertEqual(sys.path, before)

    def test_usage_window_reports_the_weekly_meter_as_a_percent(self):
        m = {"fraction": 0.42, "used": 42.0, "limit": 100.0, "reset_at": 99.0, "pause_at": 0.9}
        with unittest.mock.patch("capsule.usage_meter.weekly_usage", return_value=m):
            uw = ug.usage_window()
        self.assertEqual(tuple(uw), (None, None, 42.0))     # percent 0..100, 5h slots stay None
        self.assertEqual(uw.weekly, [("Current week", 0.42, 99.0)])

    def test_check_line_shows_week_percent_used_and_limit(self):
        m = {"fraction": 0.5, "used": 500.0, "limit": 1000.0, "reset_at": 99.0, "pause_at": 0.9}
        uw = ug.UsageWindow(None, None, 50.0, [("Current week", 0.5, 99.0)])
        uw.meter = m
        with unittest.mock.patch.object(ug, "usage_window", lambda: uw), \
             unittest.mock.patch("builtins.print") as mock_print:
            ug.run(check_only=True)
        self.assertIn("week=50.0 week_used=500 week_limit=1000", mock_print.call_args[0][0])


class WeeklyBlock(unittest.TestCase):
    def test_blocks_at_and_over_the_threshold_only(self):
        hot = ug.weekly_block({"fraction": 0.9, "pause_at": 0.9})
        self.assertTrue(hot.startswith("weekly: 90%"))
        self.assertIsNone(ug.weekly_block({"fraction": 0.89, "pause_at": 0.9}))

    def test_no_reading_is_never_a_block(self):
        self.assertIsNone(ug.weekly_block(None))


class RunEmitsEvents(unittest.TestCase):
    def test_run_emits_usage_event(self):
        import events, tempfile
        spool = Path(tempfile.mkdtemp()) / "e.jsonl"; old = events.SPOOL; events.SPOOL = spool
        try:
            with unittest.mock.patch.object(ug, "usage_window", lambda: (0.42, time.time() + 100, 12.0)), \
                 unittest.mock.patch.object(ug, "USAGE_WINDOW_PATH", Path(tempfile.mkdtemp()) / "usage-window.json"):
                ug.run()
            rows = [r for r in events.read(spool) if r["kind"] == "usage"]
            self.assertEqual(rows[0]["payload"]["session_pct"], 42.0); self.assertEqual(rows[0]["payload"]["week_pct"], 12.0)
        finally:
            events.SPOOL = old

    def test_check_line_shows_the_plain_weekly_percent(self):
        now = time.time()
        uw = ug.UsageWindow(0.2, now + 7200, 96.04, [("Current week", 0.9604, now + 100)])
        with unittest.mock.patch.object(ug, "usage_window", lambda: uw), \
             unittest.mock.patch("builtins.print") as mock_print:
            ug.run(check_only=True)
        line = mock_print.call_args[0][0]
        self.assertIn("week=96.0 ", line)
        self.assertNotIn("[", line)
        self.assertNotIn("limited=", line)  # a reading, never a pause verdict


class Preflight(unittest.TestCase):
    def setUp(self):
        self.q = Path(tempfile.mkdtemp(prefix="wq-")) / "window-queue.json"
        os.environ.pop("OPS_WINDOW_PREFLIGHT", None)

    def test_flag_defaults_off(self):
        self.assertFalse(ug.preflight_enabled())
        os.environ["OPS_WINDOW_PREFLIGHT"] = "1"
        self.addCleanup(os.environ.pop, "OPS_WINDOW_PREFLIGHT", None)
        self.assertTrue(ug.preflight_enabled())

    def test_headroom_is_measured_to_the_pause_not_to_one_hundred(self):
        est = ug.window_estimate(0.80, 1000.0, now=400.0)
        self.assertEqual(est["headroom_pct"], 5.0)   # 85 - 80, not 100 - 80
        self.assertEqual(est["reset_in_s"], 600.0)

    def test_truth_table(self):
        now = 0.0
        self.assertEqual(ug.preflight(0.10, 7200.0, now)[0], True)
        self.assertEqual(ug.preflight(0.80, 7200.0, now)[0], False)
        self.assertEqual(ug.preflight(0.10, 300.0, now)[0], False)
        self.assertEqual(ug.preflight(None, None, now)[0], True,
                         "a missing window signal never queues work")
        self.assertEqual(ug.preflight(0.10, None, now)[0], True)

    def test_floors_are_overridable(self):
        self.assertTrue(ug.preflight(0.80, 7200.0, 0.0, floor_pct=1.0)[0])
        self.assertFalse(ug.preflight(0.10, 7200.0, 0.0, min_s=99999)[0])

    def test_queue_is_silent_capped_and_never_raises(self):
        for i in range(ug.QUEUE_CAP + 7):
            ug.queue_card("capsule-corp", f"t_{i}", "headroom 2.0% under 10.0%",
                          now=float(i), path=self.q)
        rows = ug.read_queue(self.q)
        self.assertEqual(len(rows), ug.QUEUE_CAP)
        self.assertEqual(rows[-1]["task_id"], f"t_{ug.QUEUE_CAP + 6}")
        # A regular file used as a directory component makes the write fail
        # with ENOTDIR for every user, including root - root can create
        # missing directories (so "/nope/dir/q.json" would silently
        # succeed under the worker sandbox's root user), but no user can
        # mkdir through a plain file.
        with tempfile.TemporaryDirectory() as td:
            blocker = Path(td) / "blocker"
            blocker.write_text("not a directory")
            unwritable = blocker / "dir" / "q.json"
            self.assertEqual(ug.queue_card("b", "t", "why", path=unwritable), 0)

    def test_past_reset_at_is_go_when_headroom_is_sufficient(self):
        """Fix round 1, Minor 5: `if reset is None` not `if not reset` - a
        reset_at that has already passed is a real (nonzero) value, not a
        missing signal, and with headroom to spare it must still be a go."""
        ok, _ = ug.preflight(0.10, 100.0, now=500.0)  # reset was 400s ago
        self.assertTrue(ok)

    def test_missing_reset_at_is_go_when_headroom_is_sufficient(self):
        ok, _ = ug.preflight(0.10, None, now=0.0)
        self.assertTrue(ok)

    def test_refusal_reason_always_carries_headroom_percentage(self):
        """Fix round 1, Important 3: T6's report line parses the queue's
        `reason`, so a refusal - whichever floor tripped - always states the
        headroom percentage in the same shape."""
        ok, why = ug.preflight(0.10, 300.0, now=0.0)  # tripped by min_s
        self.assertFalse(ok)
        self.assertTrue(why.startswith("window: "))
        self.assertIn("% headroom", why)
        self.assertIn("resets in 300s", why)

    def test_refusal_reason_on_the_headroom_floor_also_carries_reset(self):
        ok, why = ug.preflight(0.80, 7200.0, now=0.0)  # tripped by floor_pct
        self.assertFalse(ok)
        self.assertIn("% headroom", why)
        self.assertIn("resets in 7200s", why)

    def test_refusal_reason_with_no_reset_signal_says_unknown(self):
        ok, why = ug.preflight(0.10, None, now=0.0, floor_pct=90.0)
        self.assertFalse(ok)
        self.assertIn("resets in unknown", why)

    def test_queue_card_is_idempotent_on_board_and_task_id(self):
        """Fix round 1, Critical 1: a repeat call for the same card replaces
        its row (last reason/ts wins) - one row per card, not one per tick."""
        ug.queue_card("capsule-corp", "t_1", "first reason", now=1.0, path=self.q)
        ug.queue_card("capsule-corp", "t_1", "second reason", now=2.0, path=self.q)
        rows = ug.read_queue(self.q)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reason"], "second reason")
        self.assertEqual(rows[0]["ts"], 2.0)

    def test_drain_queue_prunes_rows_from_an_earlier_window(self):
        """Fix round 1, Critical 2: a row queued before the current window's
        start (reset_at - WINDOW_LENGTH_S) is stale and drops; a fresh row
        (queued inside the current window) survives."""
        reset_at = 100000.0
        stale_ts = reset_at - ug.WINDOW_LENGTH_S - 1
        fresh_ts = reset_at - 10
        ug._write_queue([{"board": "b", "task_id": "stale", "reason": "x", "ts": stale_ts},
                         {"board": "b", "task_id": "fresh", "reason": "y", "ts": fresh_ts}], self.q)
        rows = ug.drain_queue(now=reset_at - 5, reset_at=reset_at, path=self.q)
        self.assertEqual([r["task_id"] for r in rows], ["fresh"])
        self.assertEqual([r["task_id"] for r in ug.read_queue(self.q)], ["fresh"])

    def test_drain_queue_with_no_reset_signal_drains_nothing(self):
        ug._write_queue([{"board": "b", "task_id": "old", "reason": "x", "ts": 0.0}], self.q)
        rows = ug.drain_queue(now=1000.0, reset_at=None, path=self.q)
        self.assertEqual(len(rows), 1)

    def test_queue_card_also_prunes_stale_rows_before_appending(self):
        reset_at = 100000.0
        stale_ts = reset_at - ug.WINDOW_LENGTH_S - 1
        ug._write_queue([{"board": "b", "task_id": "stale", "reason": "x", "ts": stale_ts}], self.q)
        ug.queue_card("b", "new", "reason", now=reset_at - 10, path=self.q, reset_at=reset_at)
        rows = ug.read_queue(self.q)
        self.assertEqual([r["task_id"] for r in rows], ["new"])


