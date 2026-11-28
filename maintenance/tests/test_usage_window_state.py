"""Tests for usage_guard's on-disk usage-window snapshot (card t_a7979d00).

`usage_window()` reads the window's utilization and reset time but
persisted nothing, so no other process (e.g. a budget-priority reader) could
see the guard's last observation without re-reading the source. `persist_usage_window`
writes an atomic snapshot; `read_usage_window` is the paired reader. Two
oracles per check (Zenkai Z2): the write is checked both by re-reading the
raw file content (bytes/JSON on disk) and by the `read_usage_window` reader
- two independent code paths over the same file, exactly the pairing this
card's brief asks for.
"""
import json, os, sys, time, unittest, unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from capsule.tests import tmpclean  # noqa: E402
tmpclean.install()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["AGENT_OPS_NO_SEND"] = "1"  # tests must never send Telegram messages
import tempfile as _tf
os.environ.setdefault("EVENTS_SPOOL", _tf.mkdtemp(prefix="events-test-") + "/events.jsonl")  # tests never write the live spool
import usage_guard as ug


class PersistUsageWindow(unittest.TestCase):
    """Write shape: RED until `persist_usage_window` exists."""

    def setUp(self):
        self.tmp = _tf.mkdtemp(prefix="usage-window-test-")
        self.path = Path(self.tmp) / "usage-window.json"

    def test_write_shape(self):
        now = time.time()
        reset = now + 1234
        ug.persist_usage_window(0.42, reset, now=now, path=self.path)

        # Oracle 1: raw file content on disk.
        raw = json.loads(self.path.read_text())
        self.assertEqual(set(raw.keys()), {"utilization", "reset_at", "written_at", "source"})
        self.assertEqual(raw["utilization"], 0.42)
        self.assertEqual(raw["source"], "usage_guard")
        self.assertIsInstance(raw["reset_at"], str)
        self.assertIsInstance(raw["written_at"], str)
        # ISO 8601 parseable.
        from datetime import datetime
        datetime.fromisoformat(raw["reset_at"])
        datetime.fromisoformat(raw["written_at"])
        # Review round 1 F5-M1: pin the VALUES, not just their type/parseability -
        # a hand mutant that writes `_iso(now)` into the `reset_at` field (instead
        # of `_iso(reset)`) survived every test until this assertion. `reset` and
        # `now` are 1234s apart (see above), so the two fields must differ and
        # each must match its own source epoch, not the other's.
        self.assertAlmostEqual(datetime.fromisoformat(raw["reset_at"]).timestamp(), reset, places=3)
        self.assertAlmostEqual(datetime.fromisoformat(raw["written_at"]).timestamp(), now, places=3)

        # Oracle 2: the reader helper, a separate code path over the same file.
        doc = ug.read_usage_window(self.path, max_age_s=900)
        self.assertIsNotNone(doc)
        self.assertEqual(doc["utilization"], 0.42)
        self.assertAlmostEqual(datetime.fromisoformat(doc["reset_at"]).timestamp(), reset, places=3)
        self.assertAlmostEqual(datetime.fromisoformat(doc["written_at"]).timestamp(), now, places=3)
        self.assertEqual(doc["source"], "usage_guard")

    def test_utilization_out_of_range_rejected_by_caller_contract(self):
        # persist_usage_window trusts its caller for 0.0-1.0; the reader is
        # what refuses an out-of-range value that somehow made it to disk
        # (covered in the malformed tests below). Here we just confirm a
        # normal in-range write round-trips exactly.
        now = time.time()
        ug.persist_usage_window(1.0, now + 10, now=now, path=self.path)
        self.assertEqual(json.loads(self.path.read_text())["utilization"], 1.0)

    def test_no_window_signal_skips_persist(self):
        """util=None means no window signal at all - never write a snapshot
        that pretends to know something it doesn't (the shape promises
        0.0-1.0, not null), and never clobber a still-fresh prior reading."""
        ug.persist_usage_window(0.5, time.time() + 10, now=time.time(), path=self.path)
        before = self.path.read_text()
        ug.persist_usage_window(None, None, now=time.time(), path=self.path)
        self.assertEqual(self.path.read_text(), before)


class Atomicity(unittest.TestCase):
    """No partial file survives a crash mid-write (Zenkai Z2: checked via the
    file's own bytes, a second, independent look from the reader's parse)."""

    def setUp(self):
        self.tmp = _tf.mkdtemp(prefix="usage-window-atomic-test-")
        self.path = Path(self.tmp) / "usage-window.json"

    def test_no_partial_file_after_crash_mid_write(self):
        now = time.time()
        ug.persist_usage_window(0.3, now + 100, now=now, path=self.path)
        good_bytes = self.path.read_bytes()

        import json as _json
        real_dump = _json.dump

        def boom(*a, **k):
            # Simulate a crash after some bytes are flushed to the temp file
            # but before the atomic rename: write partial garbage directly
            # to the fh, then blow up before os.replace runs.
            fh = a[1] if len(a) > 1 else k.get("fp")
            fh.write('{"utilization": 0.9,')
            fh.flush()
            raise OSError("simulated crash mid-write")

        with unittest.mock.patch.object(_json, "dump", side_effect=boom):
            with self.assertRaises(OSError):
                ug.persist_usage_window(0.9, now + 200, now=now, path=self.path)

        # Oracle 1: the target file's raw bytes are untouched (still the old,
        # complete, valid write) - never the partial garbage.
        self.assertEqual(self.path.read_bytes(), good_bytes)
        # Oracle 2: the reader still parses it cleanly.
        doc = ug.read_usage_window(self.path, max_age_s=900)
        self.assertEqual(doc["utilization"], 0.3)
        # No stray temp files left behind in the directory.
        leftovers = [p for p in self.path.parent.iterdir() if p != self.path]
        self.assertEqual(leftovers, [], f"leftover temp files: {leftovers}")


class ReadUsageWindow(unittest.TestCase):
    def setUp(self):
        self.tmp = _tf.mkdtemp(prefix="usage-window-read-test-")
        self.path = Path(self.tmp) / "usage-window.json"

    def test_missing_file_is_none(self):
        self.assertIsNone(ug.read_usage_window(self.path, max_age_s=900))

    def test_stale_is_none(self):
        now = time.time()
        ug.persist_usage_window(0.5, now + 10, now=now - 1000, path=self.path)
        self.assertIsNone(ug.read_usage_window(self.path, max_age_s=900, now=now))

    def test_fresh_is_not_none(self):
        now = time.time()
        ug.persist_usage_window(0.5, now + 10, now=now - 10, path=self.path)
        self.assertIsNotNone(ug.read_usage_window(self.path, max_age_s=900, now=now))

    def test_naive_written_at_is_treated_as_utc(self):
        """F5-M5 (review round 1): if the naive-string -> UTC fallback
        (`if wa.tzinfo is None: wa = wa.replace(tzinfo=timezone.utc)`) were
        dropped, `wa.timestamp()` on a naive `datetime` falls back to
        Python's default: interpret it as the PROCESS'S LOCAL time zone. Run
        with `TZ=Asia/Tokyo` (UTC+9, forced via `time.tzset()`) so a
        mis-handled naive value reads as ~9h older than it really is - far
        past `max_age_s=900` - while the correct UTC handling still sees it
        as 10s old. A TZ ahead of UTC makes the mutant fail in the
        `stale -> None` direction deterministically, regardless of the test
        machine's own configured zone."""
        import time as _time
        from datetime import datetime, timezone
        old_tz = os.environ.get("TZ")
        os.environ["TZ"] = "Asia/Tokyo"
        _time.tzset()
        try:
            now = time.time()
            naive = datetime.fromtimestamp(now - 10, tz=timezone.utc).replace(tzinfo=None).isoformat()
            doc = {"utilization": 0.4, "reset_at": None, "written_at": naive, "source": "usage_guard"}
            self.path.write_text(json.dumps(doc))
            result = ug.read_usage_window(self.path, max_age_s=900, now=now)
        finally:
            if old_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old_tz
            _time.tzset()
        self.assertIsNotNone(result, "a naive written_at must be read as UTC, not the process's local tz")

    def test_malformed_json_is_none(self):
        self.path.write_text("{not json")
        self.assertIsNone(ug.read_usage_window(self.path, max_age_s=900))

    def test_malformed_missing_key_is_none(self):
        self.path.write_text(json.dumps({"utilization": 0.5, "reset_at": None}))
        self.assertIsNone(ug.read_usage_window(self.path, max_age_s=900))

    def test_malformed_utilization_out_of_range_is_none(self):
        now = time.time()
        from datetime import datetime, timezone
        doc = {"utilization": 1.5, "reset_at": None,
               "written_at": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
               "source": "usage_guard"}
        self.path.write_text(json.dumps(doc))
        self.assertIsNone(ug.read_usage_window(self.path, max_age_s=900, now=now))

    def test_malformed_utilization_wrong_type_is_none(self):
        now = time.time()
        from datetime import datetime, timezone
        doc = {"utilization": "high", "reset_at": None,
               "written_at": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
               "source": "usage_guard"}
        self.path.write_text(json.dumps(doc))
        self.assertIsNone(ug.read_usage_window(self.path, max_age_s=900, now=now))

    def test_not_a_dict_is_none(self):
        self.path.write_text(json.dumps([1, 2, 3]))
        self.assertIsNone(ug.read_usage_window(self.path, max_age_s=900))


class RunIntegration(unittest.TestCase):
    """run() persists a snapshot on every non-check-only pass (and therefore
    on every 60/70/80 layer transition too, since a transition is only ever
    observed during a pass)."""

    def setUp(self):
        self.tmp = _tf.mkdtemp(prefix="usage-window-run-test-")
        self.path = Path(self.tmp) / "usage-window.json"
        self.guard_tmp = _tf.mkdtemp(prefix="usage-guard-state-test-")
        os.environ["OPS_USAGE_GUARD_STATE"] = self.guard_tmp

    def tearDown(self):
        os.environ.pop("OPS_USAGE_GUARD_STATE", None)

    def test_run_persists_snapshot(self):
        now = time.time()
        with unittest.mock.patch.object(ug, "usage_window",
                                         return_value=ug.UsageWindow(0.6, now + 3600, None, [])), \
             unittest.mock.patch.object(ug, "USAGE_WINDOW_PATH", self.path):
            ug.run(check_only=False)
        doc = ug.read_usage_window(self.path, max_age_s=900)
        self.assertIsNotNone(doc)
        self.assertEqual(doc["utilization"], 0.6)

    def test_check_only_does_not_write(self):
        now = time.time()
        with unittest.mock.patch.object(ug, "usage_window",
                                         return_value=ug.UsageWindow(0.6, now + 3600, None, [])), \
             unittest.mock.patch.object(ug, "USAGE_WINDOW_PATH", self.path):
            ug.run(check_only=True)
        self.assertFalse(self.path.exists())


def _run_hot(util, snapshot_path, guard_path):
    """One `run()` at `util` with every path in a temp dir and `subprocess.run` mocked, so
    nothing here can reach the live state. Returns (exit code, the subprocess.run mock)."""
    reset = time.time() + 1800
    with unittest.mock.patch.object(ug, "usage_window",
                                     return_value=ug.UsageWindow(util, reset, None, [])), \
         unittest.mock.patch("subprocess.run") as spawned, \
         unittest.mock.patch.object(ug, "GUARD", guard_path), \
         unittest.mock.patch.object(ug, "USAGE_WINDOW_PATH", snapshot_path):
        return ug.run(check_only=False), spawned


def _unwritable_snapshot_path(tmp):
    """A snapshot path whose parent is a plain file, so `Path.mkdir` inside
    `persist_usage_window` raises `NotADirectoryError` (an `OSError`)."""
    blocker = tmp / "blocker"
    blocker.write_text("not a directory")
    return blocker / "nested" / "usage-window.json"


class RunSurvivesUnwritableSnapshot(unittest.TestCase):
    """`persist_usage_window` re-raises every write error, so `run()` must swallow one."""

    def test_unwritable_snapshot_path_does_not_raise_out_of_run(self):
        tmp = Path(_tf.mkdtemp(prefix="usage-window-unwritable-"))
        bad_path = _unwritable_snapshot_path(tmp)
        rc, spawned = _run_hot(0.99, bad_path, tmp / "usage-guard.json")
        self.assertEqual(rc, 0)
        spawned.assert_not_called()  # a hot window spawns nothing: no pause
        self.assertFalse((tmp / "usage-guard.json").exists())
        self.assertFalse(bad_path.exists())
        self.assertFalse(bad_path.parent.exists())  # mkdir itself never succeeded


class DefaultPathGuard(unittest.TestCase):
    """The real default `USAGE_WINDOW_PATH` must never be written under
    `AGENT_OPS_NO_SEND` unless a caller has patched it away from
    `_REAL_USAGE_WINDOW_PATH` first; reproduced hermetically (both point at
    the same fake tmp path) so this never has to touch the real live file."""

    def test_resolved_path_refuses_the_untouched_default_under_no_send(self):
        fake_default = Path(_tf.mkdtemp(prefix="usage-window-fakehome-")) / "usage-window.json"
        with unittest.mock.patch.object(ug, "USAGE_WINDOW_PATH", fake_default), \
             unittest.mock.patch.object(ug, "_REAL_USAGE_WINDOW_PATH", fake_default), \
             unittest.mock.patch.dict(os.environ, {"AGENT_OPS_NO_SEND": "1"}):
            ug._snapshot_usage_window(0.5, time.time() + 10, time.time())
        self.assertFalse(fake_default.exists())

    def test_a_fake_home_suite_run_never_touches_the_default_path(self):
        """A full `run()` pass with the untouched default must complete
        cleanly (the refusal is logged and swallowed, never fatal) and must
        never create the snapshot file."""
        fake_default = Path(_tf.mkdtemp(prefix="usage-window-fakehome-")) / "usage-window.json"
        reset = time.time() + 3600
        with unittest.mock.patch.object(ug, "USAGE_WINDOW_PATH", fake_default), \
             unittest.mock.patch.object(ug, "_REAL_USAGE_WINDOW_PATH", fake_default), \
             unittest.mock.patch.object(ug, "usage_window",
                                         return_value=ug.UsageWindow(0.99, reset, None, [])), \
             unittest.mock.patch("subprocess.run") as spawned, \
             unittest.mock.patch.dict(os.environ, {"AGENT_OPS_NO_SEND": "1"}):
            ug.run(check_only=False)
        spawned.assert_not_called()
        self.assertFalse(fake_default.exists())


INTENT_FILE = Path(__file__).resolve().parents[2] / "contracts" / "intent" / "usage_guard.jsonl"


class IntentExamples(unittest.TestCase):
    """Each `contracts/intent/usage_guard.jsonl` example is dispatched by its
    `id` to the real `usage_guard` code path it names, so a flipped
    expectation fails here instead of staying green under lint alone."""

    def _examples(self):
        return [json.loads(line) for line in INTENT_FILE.read_text().splitlines() if line.strip()]

    def _check_no_signal_keeps_prior_snapshot(self, doc, expect):
        self.assertIsNone(doc["utilization"])  # the given.document itself must be machine-readable
        path = Path(_tf.mkdtemp(prefix="usage-window-intent-")) / "usage-window.json"
        now = time.time()
        ug.persist_usage_window(0.5, now + 10, now=now, path=path)
        before = path.read_bytes()
        with unittest.mock.patch.object(ug, "usage_window",
                                        lambda: ug.UsageWindow(None, None, None, [])), \
             unittest.mock.patch.object(ug, "USAGE_WINDOW_PATH", path):
            ug.run(check_only=False)
        unchanged = path.read_bytes() == before
        self.assertEqual(unchanged, expect["prior_snapshot_unchanged"])
        self.assertEqual(not unchanged, expect["snapshot_written"])

    def _check_stale_snapshot_reads_as_no_signal(self, doc, expect):
        path = Path(_tf.mkdtemp(prefix="usage-window-intent-")) / "usage-window.json"
        now = time.time()
        ug.persist_usage_window(0.5, now + 10, now=now - doc["snapshot_age_s"], path=path)
        result = ug.read_usage_window(path, max_age_s=doc["max_age_s"], now=now)
        self.assertEqual(result is None, expect["reading"] == "none")

    def _check_unwritable_snapshot_does_not_raise(self, doc, expect):
        tmp = Path(_tf.mkdtemp(prefix="usage-window-intent-"))
        self.assertFalse(doc["snapshot_path_writable"])
        bad_path = _unwritable_snapshot_path(tmp)
        rc, spawned = _run_hot(doc["utilization"], bad_path, tmp / "usage-guard.json")
        self.assertEqual(rc, expect["run_exit"])
        self.assertEqual(bad_path.exists(), expect["snapshot_written"])
        spawned.assert_not_called()

    def _check_hot_window_never_pauses(self, doc, expect):
        tmp = Path(_tf.mkdtemp(prefix="usage-window-intent-"))
        guard = tmp / "usage-guard.json"
        _rc, spawned = _run_hot(doc["utilization"], tmp / "usage-window.json", guard)
        self.assertEqual(spawned.called, expect["subprocess_spawned"])
        self.assertEqual(guard.exists(), expect["guard_file_written"])  # second oracle
        paused = guard.exists() and bool(json.loads(guard.read_text()).get("paused_until"))
        self.assertEqual(paused, expect["paused"])

    ORACLES = {
        "no-signal-pass-keeps-prior-snapshot": _check_no_signal_keeps_prior_snapshot,
        "unwritable-snapshot-does-not-raise": _check_unwritable_snapshot_does_not_raise,
        "stale-snapshot-reads-as-no-signal": _check_stale_snapshot_reads_as_no_signal,
        "hot-window-never-pauses": _check_hot_window_never_pauses,
    }

    def test_every_example_matches_the_real_code(self):
        examples = self._examples()
        self.assertEqual({e["id"] for e in examples}, set(self.ORACLES))
        for ex in examples:
            oracle = self.ORACLES.get(ex["id"])
            if oracle is None:
                self.fail(f"no oracle wired for {ex['id']!r}")
            oracle(self, ex["given"]["document"], ex["expect"])


if __name__ == "__main__":
    unittest.main()
