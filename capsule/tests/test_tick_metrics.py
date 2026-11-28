"""Row 470's tick wiring: capsule/metrics.py's export step (6c) and the same-condition
unowned-idle check (6c continued), both inside a real `Tick` (capsule/tick.py), a real board and
the real `maintenance/events.py` spool (redirected to a temp file - never the live one).

`capsule_last_dispatch_timestamp` comes from capsule's own dispatch memory
(`Tick._record_dispatch`/`_last_dispatch_ts`, `guard_state` key `last_dispatch_at`), never from
the detector's cache (row 470 follow-up: the real `capsule/unblocked` branch's report carries no
timestamp of any kind - see `capsule/tests/test_metrics.py`'s `RealFixture` class)."""
import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from capsule import db, dispatch, incidents, metrics, unowned_idle
from capsule.tests.test_tick import FakeEvents, TickCase


class MetricsExport(TickCase):
    """The exporter, isolated from dispatch: the pre-created card is parked `blocked` so `once()`
    never dispatches anything this pass and the dispatch clock stays unset."""

    def setUp(self):
        super().setUp()
        self.conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (self.tid,))
        self.cache = Path(tempfile.mkdtemp()) / "unblocked.json"
        self._old_cache_env = os.environ.get("CAPSULE_UNBLOCKED_CACHE")
        os.environ["CAPSULE_UNBLOCKED_CACHE"] = str(self.cache)
        self.spool = Path(tempfile.mkdtemp()) / "events.jsonl"
        self._old_spool_env = os.environ.get("EVENTS_SPOOL")
        os.environ["EVENTS_SPOOL"] = str(self.spool)
        # maintenance/events.py fixes its module-level SPOOL from the environment at import
        # time; force a fresh import so it picks up THIS test's spool, not a path cached from
        # an earlier test in the same process (module caching, not a capsule/tick.py bug).
        patcher = mock.patch.dict(sys.modules)
        patcher.start()
        self.addCleanup(patcher.stop)
        sys.modules.pop("events", None)

    def tearDown(self):
        if self._old_cache_env is None:
            os.environ.pop("CAPSULE_UNBLOCKED_CACHE", None)
        else:
            os.environ["CAPSULE_UNBLOCKED_CACHE"] = self._old_cache_env
        if self._old_spool_env is None:
            os.environ.pop("EVENTS_SPOOL", None)
        else:
            os.environ["EVENTS_SPOOL"] = self._old_spool_env
        super().tearDown()

    def spooled(self):
        if not self.spool.exists():
            return []
        return [json.loads(l) for l in self.spool.read_text().splitlines() if l.strip()]

    def test_no_cache_and_no_dispatch_yet_is_recomputed_and_exports_the_detector_counts_only(self):
        # The tick's no-arg refresh (hotfix: detect() resolves $CAPSULE_HOME itself) fills the gap.
        counts = self.tick.once()
        self.assertEqual(counts["dispatched"], 0)
        self.assertEqual(counts["metrics_exported"], 1)
        self.assertEqual(counts["tick_errors"], 0)
        rows = [r for r in self.spooled() if r["kind"] == "capsule_unblocked"]
        self.assertEqual(set(rows[0]["payload"]),
                         {"capsule_unblocked_total", "capsule_unowned_total"})

    def test_a_full_cache_with_no_dispatch_yet_exports_the_two_detector_metrics(self):
        self.cache.write_text(json.dumps({"items": [], "unblocked": 2, "unowned": 1}))
        counts = self.tick.once()
        self.assertEqual(counts["metrics_exported"], 1)
        rows = [r for r in self.spooled() if r["kind"] == "capsule_unblocked"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["payload"], {"capsule_unblocked_total": 2, "capsule_unowned_total": 1})
        # row 472 (I9): the event row carries the board it came from, so a query can filter by it.
        self.assertEqual(rows[0]["board"], self.cfg["board"])

    def test_a_stale_cache_is_recomputed_by_the_tick_and_ships_the_fresh_count(self):
        # t_742be58f: the hook only writes at SessionStart; the tick must not depend on it.
        from unittest import mock
        self.cache.write_text(json.dumps({"items": [], "unblocked": 9, "unowned": 9}))
        os.utime(self.cache, (1, 1))
        with mock.patch("capsule.unblocked.detect", return_value=[]):
            counts = self.tick.once()
        self.assertEqual(counts["metrics_exported"], 1)
        rows = [r for r in self.spooled() if r["kind"] == "capsule_unblocked"]
        self.assertEqual(rows[0]["payload"],
                         {"capsule_unblocked_total": 0, "capsule_unowned_total": 0})

    def test_a_bad_cache_never_crashes_the_tick(self):
        self.cache.write_text("{{{not json")
        counts = self.tick.once()
        self.assertEqual(counts["metrics_exported"], 1)    # the corrupt file is recomputed
        self.assertEqual(counts["tick_errors"], 0)


class CacheStaleness(TickCase):
    """Row 472 follow-up (I1): a detector cache the hook has stopped refreshing must not be
    re-exported as fresh data forever, and the tick must say so exactly once per transition -
    never every pass it stays stale, never silently."""

    def setUp(self):
        super().setUp()
        self.conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (self.tid,))
        self.cache = Path(tempfile.mkdtemp()) / "unblocked.json"
        self._old_cache_env = os.environ.get("CAPSULE_UNBLOCKED_CACHE")
        os.environ["CAPSULE_UNBLOCKED_CACHE"] = str(self.cache)
        # _export_metrics is called directly in this class (not through Tick.once()), so it
        # needs the same self.cfg once() would have set first.
        self.tick.cfg = self.cfg
        # The tick refreshes before it judges, so a stale/missing/corrupt line means the refresh
        # fails: these tests run with a failing detector unless one swaps in a working one.
        patcher = mock.patch.object(metrics.unblocked, "detect", side_effect=RuntimeError("down"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        if self._old_cache_env is None:
            os.environ.pop("CAPSULE_UNBLOCKED_CACHE", None)
        else:
            os.environ["CAPSULE_UNBLOCKED_CACHE"] = self._old_cache_env
        super().tearDown()

    def test_a_working_refresh_never_flaps_across_the_max_age_boundary(self):
        """The flap: the check ran before the refresh, so every CACHE_MAX_AGE_SECONDS it logged
        stale, then fresh again. With a working detector the tick says nothing, pass after pass."""
        self.cache.write_text(json.dumps({"unblocked": 1, "unowned": 1}))
        now = 10_000_000
        os.utime(self.cache, (now, now))
        buf = io.StringIO()
        with mock.patch.object(metrics.unblocked, "detect", return_value={}), \
                mock.patch.object(metrics.unblocked, "render_json", return_value="{}"), \
                contextlib.redirect_stderr(buf):
            for step in range(0, 3 * metrics.CACHE_MAX_AGE_SECONDS, 30):
                self.tick._export_metrics(self.conn, now + step)
        self.assertEqual(buf.getvalue(), "")

    def test_a_cache_parent_that_is_a_file_prints_each_line_once_over_many_passes(self):
        """The cleanup after a failed refresh used to raise NotADirectoryError into the check's
        outer except, which printed on every pass."""
        blocker = Path(tempfile.mkdtemp()) / "blocker"
        blocker.write_text("x")
        os.environ["CAPSULE_UNBLOCKED_CACHE"] = str(blocker / "unblocked.json")
        metrics._refresh_failing = False
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            for step in range(5):
                self.tick._export_metrics(self.conn, 10_000_000 + step * 30)
        self.assertNotIn("check failed", buf.getvalue())
        self.assertEqual(buf.getvalue().count("unblocked cache refresh failed"), 1)
        self.assertEqual(buf.getvalue().count("unblocked cache missing"), 1)

    def test_going_stale_prints_one_line_and_later_passes_stay_silent(self):
        self.cache.write_text(json.dumps({"unblocked": 1, "unowned": 1}))
        now = 10_000_000
        stale_mtime = now - metrics.CACHE_MAX_AGE_SECONDS - 1
        os.utime(self.cache, (stale_mtime, stale_mtime))
        first = io.StringIO()
        with contextlib.redirect_stderr(first):
            self.tick._export_metrics(self.conn, now)
        self.assertIn("unblocked cache stale", first.getvalue())
        second = io.StringIO()
        with contextlib.redirect_stderr(second):
            self.tick._export_metrics(self.conn, now + 10)   # still stale, same transition
        self.assertNotIn("unblocked cache stale", second.getvalue())

    def test_recovering_from_stale_prints_its_own_transition_line(self):
        self.cache.write_text(json.dumps({"unblocked": 1, "unowned": 1}))
        now = 10_000_000
        stale_mtime = now - metrics.CACHE_MAX_AGE_SECONDS - 1
        os.utime(self.cache, (stale_mtime, stale_mtime))
        self.tick._export_metrics(self.conn, now)                 # goes stale
        os.utime(self.cache, (now, now))                           # the hook refreshed it
        recovered = io.StringIO()
        with contextlib.redirect_stderr(recovered):
            self.tick._export_metrics(self.conn, now + 1)
        self.assertIn("fresh again", recovered.getvalue())

    def test_a_fresh_cache_never_prints_anything(self):
        self.cache.write_text(json.dumps({"unblocked": 1, "unowned": 1}))
        now = 10_000_000
        os.utime(self.cache, (now, now))
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            self.tick._export_metrics(self.conn, now)
        self.assertEqual(buf.getvalue(), "")

    def test_a_missing_cache_prints_its_own_line_once(self):
        """Row 472 re-review (R2): a missing cache used to go unremarked entirely - only
        staleness ever got a note. Never created at all is its own distinct state
        (`metrics.cache_state` -> "missing"), not folded into "stale"."""
        now = 10_000_000
        first = io.StringIO()
        with contextlib.redirect_stderr(first):
            self.tick._export_metrics(self.conn, now)
        self.assertIn("unblocked cache missing", first.getvalue())
        second = io.StringIO()
        with contextlib.redirect_stderr(second):
            self.tick._export_metrics(self.conn, now + 10)   # still missing, same transition
        self.assertNotIn("unblocked cache missing", second.getvalue())

    def test_a_corrupt_cache_prints_its_own_line_once(self):
        """Row 472 re-review (R2): a cache that exists, is fresh enough, but fails to parse (a
        partial write, truncated JSON) used to be silently treated the same as "no data" with no
        note at all - `metrics.cache_state` names it "corrupt", distinct from "stale"/"missing"."""
        now = 10_000_000
        self.cache.write_text("{{{not valid json")
        os.utime(self.cache, (now, now))
        first = io.StringIO()
        with contextlib.redirect_stderr(first):
            self.tick._export_metrics(self.conn, now)
        self.assertIn("unblocked cache corrupt", first.getvalue())
        second = io.StringIO()
        with contextlib.redirect_stderr(second):
            self.tick._export_metrics(self.conn, now + 10)   # still corrupt, same transition
        self.assertNotIn("unblocked cache corrupt", second.getvalue())

    def test_missing_to_stale_to_corrupt_to_fresh_each_print_exactly_one_line(self):
        """Row 472 re-review (R2): the latch tracks the STATE, not a bare stale/not-stale bool -
        every distinct transition prints its own line, including transitions that never pass
        through "fresh" at all (missing -> stale, stale -> corrupt)."""
        now = 10_000_000

        def transition(now_):
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                self.tick._export_metrics(self.conn, now_)
            return buf.getvalue()

        # starts missing (no file at all)
        self.assertIn("unblocked cache missing", transition(now))
        # missing -> stale (a file appears, but already old)
        self.cache.write_text(json.dumps({"unblocked": 1, "unowned": 1}))
        os.utime(self.cache, (now - metrics.CACHE_MAX_AGE_SECONDS - 1, now - metrics.CACHE_MAX_AGE_SECONDS - 1))
        self.assertIn("unblocked cache stale", transition(now))
        # stale -> corrupt (the hook rewrites it, but with garbage)
        self.cache.write_text("{{{garbage")
        os.utime(self.cache, (now, now))
        self.assertIn("unblocked cache corrupt", transition(now))
        # corrupt -> fresh
        self.cache.write_text(json.dumps({"unblocked": 1, "unowned": 1}))
        os.utime(self.cache, (now, now))
        self.assertIn("fresh again", transition(now))


class DispatchClock(TickCase):
    """The other half: a real dispatch (the pre-created `ready` card, untouched from setUp)
    sets `guard_state`'s `last_dispatch_at`, which the SAME pass's exporter then reads - proving
    the 6 -> 6b ordering (`Tick.once`) actually wires the two together."""

    def setUp(self):
        super().setUp()
        self.cache = Path(tempfile.mkdtemp()) / "unblocked.json"
        self._old_cache_env = os.environ.get("CAPSULE_UNBLOCKED_CACHE")
        os.environ["CAPSULE_UNBLOCKED_CACHE"] = str(self.cache)
        self.spool = Path(tempfile.mkdtemp()) / "events.jsonl"
        self._old_spool_env = os.environ.get("EVENTS_SPOOL")
        os.environ["EVENTS_SPOOL"] = str(self.spool)
        patcher = mock.patch.dict(sys.modules)
        patcher.start()
        self.addCleanup(patcher.stop)
        sys.modules.pop("events", None)

    def tearDown(self):
        if self._old_cache_env is None:
            os.environ.pop("CAPSULE_UNBLOCKED_CACHE", None)
        else:
            os.environ["CAPSULE_UNBLOCKED_CACHE"] = self._old_cache_env
        if self._old_spool_env is None:
            os.environ.pop("EVENTS_SPOOL", None)
        else:
            os.environ["EVENTS_SPOOL"] = self._old_spool_env
        super().tearDown()

    def spooled(self):
        if not self.spool.exists():
            return []
        return [json.loads(l) for l in self.spool.read_text().splitlines() if l.strip()]

    def test_a_real_dispatch_sets_the_clock_and_is_exported_the_same_pass(self):
        now = 20_000_000
        counts = self.tick.once(now)
        self.assertEqual(counts["dispatched"], 1)
        self.assertEqual(counts["metrics_exported"], 1)
        rows = [r for r in self.spooled() if r["kind"] == "capsule_unblocked"]
        self.assertEqual(len(rows), 1)
        self.assertIn("capsule_last_dispatch_timestamp", rows[0]["payload"])
        # row 472 (I10): not just "some guard row exists" - the STORED timestamp equals this
        # pass's own clock, not merely non-None (a stray real-clock value would have passed the
        # old, weaker assertion too).
        self.assertEqual(rows[0]["payload"]["capsule_last_dispatch_timestamp"], now)
        self.assertEqual(self.tick._last_dispatch_ts(self.conn), now)

    def test_a_second_pass_with_nothing_new_to_dispatch_keeps_the_old_clock(self):
        self.tick.once(20_000_000)
        first = self.tick._last_dispatch_ts(self.conn)
        self.assertEqual(first, 20_000_000)
        # force a genuinely empty second pass (the base fixture's `alive` stub always reports
        # False, so an untouched task would be reaped and redispatched every pass, which is not
        # the "nothing new" case this test means to exercise - row 472 (I10) made this concrete
        # instead of assumed).
        self.conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (self.tid,))
        counts = self.tick.once(20_000_100)
        self.assertEqual(counts["dispatched"], 0)
        self.assertEqual(self.tick._last_dispatch_ts(self.conn), first)


class DispatchClockFallback(TickCase):
    """Row 472 ruling (I6): a board dispatched before this guard-state clock ever existed is not
    "no data" - `Tick._last_dispatch_ts` falls back to the newest `task_runs.started_at` this
    board already has until (and unless) `guard_state` gets its own memory. Only a board with
    NO run history at all has genuinely never dispatched anything."""

    def test_falls_back_to_the_newest_task_runs_started_at_when_guard_state_has_none(self):
        self.conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, started_at) VALUES (?,?,?,?)",
            (self.tid, "default", "done", 5_000_000))
        self.assertEqual(self.tick._last_dispatch_ts(self.conn), 5_000_000)

    def test_the_newest_run_wins_when_there_are_several(self):
        for started_at in (1_000_000, 5_000_000, 3_000_000):
            self.conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, started_at) VALUES (?,?,?,?)",
                (self.tid, "default", "done", started_at))
        self.assertEqual(self.tick._last_dispatch_ts(self.conn), 5_000_000)

    def test_guard_state_memory_wins_once_it_exists(self):
        self.conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, started_at) VALUES (?,?,?,?)",
            (self.tid, "default", "done", 5_000_000))
        db.guard_set(self.conn, metrics.LAST_DISPATCH_GUARD, {"ts": 9_000_000}, expect_version=0)
        self.assertEqual(self.tick._last_dispatch_ts(self.conn), 9_000_000)

    def test_none_when_task_runs_is_genuinely_empty(self):
        self.assertIsNone(self.tick._last_dispatch_ts(self.conn))


class HeartbeatPauseRead(TickCase):
    def test_a_pause_guard_read_failure_keeps_the_counts(self):
        # t_1a0523e8: the pause read has its own try; only `paused` goes unknown
        events = FakeEvents()
        tick = self.make_tick(events=events)
        self.conn.execute("UPDATE tasks SET status='running' WHERE id=?", (self.tid,))
        with mock.patch.object(dispatch, "any_pause", side_effect=RuntimeError("guard")):
            tick._heartbeat(self.conn, self.cfg, time.time())
        payload = events.logged[-1]["payload"]
        self.assertEqual((payload["running"], payload["blocked"]), (1, 0))
        self.assertIsNone(payload["paused"])


class UnownedIdleAlert(TickCase):
    """t_742be58f: the tick's Korin check mirrors the `unowned-work-idle` rule, which now pages on
    detector silence only: the tick is alive (this code is running) and the detector cache has not
    been fresh for `metrics.COUNT_MISSING_SECONDS`. A backlog (any count) is never an episode."""

    def setUp(self):
        super().setUp()
        self.cache = Path(tempfile.mkdtemp()) / "unblocked.json"
        self._old_cache_env = os.environ.get("CAPSULE_UNBLOCKED_CACHE")
        os.environ["CAPSULE_UNBLOCKED_CACHE"] = str(self.cache)
        self.cfg["korin"]["enabled"] = True
        self.write_cfg()

    def tearDown(self):
        if self._old_cache_env is None:
            os.environ.pop("CAPSULE_UNBLOCKED_CACHE", None)
        else:
            os.environ["CAPSULE_UNBLOCKED_CACHE"] = self._old_cache_env
        super().tearDown()

    def present(self, now, unowned=38):
        """A fresh cache with a count, written at `now` (the cache's mtime is set explicitly so the
        fake small epoch values here do not make "fresh" true or false by accident)."""
        self.cache.write_text(json.dumps({"items": [], "unowned": unowned}))
        os.utime(self.cache, (now, now))

    def missing(self):
        self.cache.unlink(missing_ok=True)

    def firing_guard(self):
        state, _ = db.guard_get(self.conn, incidents.ALERT_GUARD_PREFIX + metrics.ALERT_NAME)
        return state

    def check(self, t):
        unowned_idle.check(self.conn, self.cfg, t)

    def test_a_short_gap_never_opens_an_incident(self):
        now = 10_000_000
        self.missing()
        self.check(now)
        self.check(now + metrics.COUNT_MISSING_SECONDS - 1)
        self.assertIsNone(self.firing_guard())
        self.assertEqual(incidents.listing(self.conn), [])

    def test_a_big_backlog_with_a_fresh_count_never_fires(self):
        now = 10_000_000
        for minute in range(0, 181, 5):
            self.present(now + minute * 60, unowned=38)
            self.check(now + minute * 60)
        self.assertIsNone(self.firing_guard())
        self.assertEqual(incidents.listing(self.conn), [])

    def test_two_short_gaps_within_an_hour_record_no_firing(self):
        now = 10_000_000
        self.missing()
        self.check(now)
        self.check(now + 10)
        self.present(now + 20)
        self.check(now + 20)
        self.missing()
        self.check(now + 1800)
        self.check(now + 1810)
        self.present(now + 1820)
        self.check(now + 1820)
        self.assertIsNone(self.firing_guard())

    def test_one_sustained_gap_fires_exactly_once(self):
        now = 10_000_000
        self.missing()
        for minute in range(0, 181, 5):
            self.check(now + minute * 60)
            self.assertEqual(incidents.listing(self.conn), [])    # one episode is a blip
        self.assertEqual(self.firing_guard()["count"], 1)

    def test_a_pause_never_fires_or_opens_an_incident(self):
        # t_86fc8bec: pause:board and pause:account:* are deliberate stops (intent example
        # unowned-work-idle-quiet-when-budget-paused)
        now = 10_000_000
        for key in ("pause:board", "pause:account:default"):
            db.guard_set(self.conn, key, {"until": now + 10 * 86400}, now=now)
            self.missing()
            for minute in range(0, 181, 5):
                self.check(now + minute * 60)
            self.assertIsNone(self.firing_guard(), key)
            self.assertEqual(incidents.listing(self.conn), [], key)
            self.conn.execute("DELETE FROM guard_state WHERE key=?", (key,))

    def test_a_board_with_deadman_off_never_fires(self):
        # t_1a0523e8: the rule's `watch` clause silences deadman-off boards; the tick matches it
        now = 10_000_000
        self.cfg["deadman"] = False
        self.missing()
        for minute in range(0, 181, 5):
            self.check(now + minute * 60)
        self.assertIsNone(self.firing_guard())
        self.assertEqual(incidents.listing(self.conn), [])

    def test_deadman_off_is_silent_whatever_the_board_name(self):
        now = 10_000_000
        self.cfg["deadman"], self.cfg["board"] = False, "sample-app"
        self.missing()
        self.check(now)
        self.check(now + metrics.COUNT_MISSING_SECONDS)
        self.assertIsNone(self.firing_guard())

    def test_an_expired_pause_does_not_silence_the_check(self):
        now = 10_000_000
        db.guard_set(self.conn, "pause:board", {"until": now - 1}, now=now)
        self.missing()
        self.check(now)
        self.check(now + metrics.COUNT_MISSING_SECONDS)
        self.assertEqual(self.firing_guard()["count"], 1)

    def two_episodes(self, now):
        self.missing()
        self.check(now)
        self.check(now + metrics.COUNT_MISSING_SECONDS)             # episode 1 fires (remembers)
        self.present(now + 3600)
        self.check(now + 3600)                                      # ...and resolves
        self.missing()
        self.check(now + 7200)
        self.check(now + 7200 + metrics.COUNT_MISSING_SECONDS)      # episode 2 fires (2nd, <24h)

    def test_two_episodes_within_24h_open_one_incident(self):
        self.two_episodes(10_000_000)
        rows = incidents.listing(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "silent_failure")
        self.assertIn("unowned_work_idle", rows[0]["root_key"])

    def test_incident_evidence_names_the_cache_state_and_the_last_run(self):
        now = 10_000_000
        self.conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, started_at, run_id) VALUES (?,?,?,?,?)",
            (self.tid, "default", "done", now - 5000, "r" * 32))
        self.two_episodes(now)
        rows = incidents.listing(self.conn)
        self.assertEqual(json.loads(rows[0]["evidence"])[0], "cache:missing")
        self.assertEqual(rows[0]["run_id"], "r" * 32)

    def test_never_raises_on_a_missing_cache(self):
        self.missing()
        self.check(10_000_000)
        self.assertEqual(incidents.listing(self.conn), [])


if __name__ == "__main__":
    unittest.main()
