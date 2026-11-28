import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from capsule.tests import tmpclean  # noqa: E402
tmpclean.install()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["AGENT_OPS_NO_SEND"] = "1"  # tests must never send Telegram messages
import tempfile as _tf
os.environ.setdefault("EVENTS_SPOOL", _tf.mkdtemp(prefix="events-test-") + "/events.jsonl")  # tests never write the live spool
import ai_usage


def _claude_root(root, now):
    """A minimal ~/.claude: one in-window assistant turn."""
    d = Path(root) / "projects" / "-Users-owner"
    d.mkdir(parents=True)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(now - 600))
    (d / "s.jsonl").write_text(json.dumps({"type": "assistant", "timestamp": stamp,
        "message": {"model": "claude-sonnet-5", "usage": {
            "input_tokens": 100, "output_tokens": 20,
            "cache_read_input_tokens": 5, "cache_creation_input_tokens": 7}}}) + "\n")


class ClaudeCodeScan(unittest.TestCase):
    def setUp(self):
        self.now = time.time()
        self.root = Path(tempfile.mkdtemp())
        d = self.root / "projects" / "-Users-owner"
        d.mkdir(parents=True)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(self.now - 600))
        lines = [
            {"type": "assistant", "timestamp": stamp,
             "message": {"model": "claude-sonnet-5", "usage": {
                 "input_tokens": 10, "output_tokens": 3,
                 "cache_read_input_tokens": 40, "cache_creation_input_tokens": 50}}},
            {"type": "assistant", "timestamp": stamp,
             "message": {"model": "<synthetic>", "usage": {
                 "input_tokens": 7777, "output_tokens": 7777,
                 "cache_read_input_tokens": 7777, "cache_creation_input_tokens": 7777}}},
        ]
        (d / "session.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n")

    def test_sums_usage_and_skips_synthetic(self):
        rows = ai_usage.scan_claude_code(self.root, self.now)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["model"], "claude-sonnet-5")
        self.assertEqual(r["turns"], 1)
        self.assertEqual((r["input"], r["output"], r["cache_read"], r["cache_write"]),
                         (10, 3, 40, 50))

    def test_top_level_usage_shape_also_counted(self):
        d = self.root / "projects" / "-Users-other"
        d.mkdir(parents=True)
        (d / "s.jsonl").write_text(json.dumps({
            "model": "claude-opus-5",
            "usage": {"input_tokens": 1, "output_tokens": 2,
                      "cache_read_input_tokens": 3, "cache_creation_input_tokens": 4}}) + "\n")
        rows = {r["model"]: r for r in ai_usage.scan_claude_code(self.root, self.now)}
        self.assertEqual(rows["claude-opus-5"]["input"], 1)

    def _rec(self, mid, out, age_s=600, model="claude-sonnet-5", req="r1"):
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(self.now - age_s))
        return json.dumps({"type": "assistant", "timestamp": stamp, "requestId": req,
                           "message": {"id": mid, "model": model, "usage": {
                               "input_tokens": 1, "output_tokens": out,
                               "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}})

    def test_duplicate_message_counted_once_with_max_output(self):
        d = self.root / "projects" / "-dup"
        d.mkdir(parents=True)
        (d / "s.jsonl").write_text(self._rec("m1", 5) + "\n" + self._rec("m1", 9) + "\n")
        rows = {x["model"]: x for x in ai_usage.scan_claude_code(self.root, self.now)}
        r = rows["claude-sonnet-5"]
        self.assertEqual(r["turns"], 2)  # m1 once, plus the setUp turn
        self.assertEqual(r["output"], 9 + 3)

    def test_nested_subagent_transcript_counted(self):
        d = self.root / "projects" / "-p" / "sid" / "subagents"
        d.mkdir(parents=True)
        (d / "agent-1.jsonl").write_text(self._rec("sub1", 11, model="claude-haiku-4-5") + "\n")
        rows = {x["model"]: x for x in ai_usage.scan_claude_code(self.root, self.now)}
        self.assertEqual(rows["claude-haiku-4-5"]["output"], 11)

    def test_old_record_in_recently_modified_file_excluded(self):
        d = self.root / "projects" / "-old"
        d.mkdir(parents=True)
        old = self._rec("old1", 500, age_s=3 * 86400, model="claude-opus-5")
        (d / "s.jsonl").write_text(old + "\n")
        rows = {x["model"] for x in ai_usage.scan_claude_code(self.root, self.now)}
        self.assertNotIn("claude-opus-5", rows)

    def test_missing_root_does_not_raise(self):
        self.assertEqual(ai_usage.scan_claude_code(Path("/nonexistent-claude-root"), self.now), [])


class UsdEstimate(unittest.TestCase):
    """List-price estimate per row. Subscription users pay nothing extra; this is a proxy."""

    def test_known_model_uses_its_prices(self):
        row = {"model": "claude-opus-5", "input": 1_000_000, "output": 1_000_000,
               "cache_read": 0, "cache_write": 0}
        ai_usage.price_row(row)
        self.assertEqual(row["usd_estimate"], 30.0)      # 5 in + 25 out
        self.assertNotIn("price_assumed", row)

    def test_cache_read_is_a_tenth_and_cache_write_is_1_25x_of_input(self):
        row = {"model": "claude-sonnet-5", "input": 0, "output": 0,
               "cache_read": 1_000_000, "cache_write": 1_000_000}
        ai_usage.price_row(row)
        # sonnet-5 cache_read 0.2/M + explicit cache_write 2.5/M
        self.assertAlmostEqual(row["usd_estimate"], 2.7, places=6)

    def test_fable_and_haiku_prices(self):
        fable = {"model": "claude-fable-5-1", "input": 1_000_000, "output": 1_000_000,
                 "cache_read": 0, "cache_write": 0}
        haiku = {"model": "claude-haiku-4-5-20251001", "input": 1_000_000, "output": 1_000_000,
                 "cache_read": 0, "cache_write": 0}
        ai_usage.price_row(fable)
        ai_usage.price_row(haiku)
        self.assertEqual(fable["usd_estimate"], 60.0)     # 10 + 50
        self.assertEqual(haiku["usd_estimate"], 6.0)      # 1 + 5

    def test_unknown_model_falls_back_to_sonnet_and_flags_the_assumption(self):
        row = {"model": "some-new-model", "input": 1_000_000, "output": 1_000_000,
               "cache_read": 0, "cache_write": 0}
        ai_usage.price_row(row)
        self.assertEqual(row["usd_estimate"], 12.0)       # 2 + 10, sonnet
        self.assertTrue(row["price_assumed"])

    def test_missing_counters_are_treated_as_zero(self):
        row = {"model": "claude-sonnet-5"}
        ai_usage.price_row(row)
        self.assertEqual(row["usd_estimate"], 0.0)

    def test_sample_prices_every_row_and_changes_no_other_key(self):
        tmp = Path(tempfile.mkdtemp())
        now = time.time()
        _claude_root(tmp / "claude", now)
        spool = tmp / "events.jsonl"
        payload = ai_usage.sample(now=now, spool=spool, state_path=tmp / "s.json",
                                  claude_root=tmp / "claude")
        self.assertEqual(set(payload), {"window_h", "claude_code"})
        row = payload["claude_code"][0]
        self.assertEqual(set(row), {"model", "turns", "input", "output",
                                    "cache_read", "cache_write", "usd_estimate"})
        self.assertGreater(row["usd_estimate"], 0)


class HourlyStamp(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.state = self.tmp / "samplers.json"
        self.spool = self.tmp / "events.jsonl"
        self.now = time.time()

    def _sample(self, now, **kw):
        return ai_usage.sample(now=now, spool=self.spool, state_path=self.state,
                               claude_root=self.tmp / "noclaude", **kw)

    def test_second_run_within_the_hour_is_skipped(self):
        self.assertIsNotNone(self._sample(self.now))
        self.assertIsNone(self._sample(self.now + 60))
        self.assertEqual(len(self.spool.read_text().splitlines()), 1)
        self.assertIsNotNone(self._sample(self.now + 3601))
        self.assertEqual(len(self.spool.read_text().splitlines()), 2)

    def test_force_ignores_the_stamp(self):
        self._sample(self.now)
        self.assertIsNotNone(self._sample(self.now + 5, force=True))

    def test_event_row_shape(self):
        self._sample(self.now)
        row = json.loads(self.spool.read_text().splitlines()[0])
        self.assertEqual(row["kind"], "ai_usage")
        self.assertEqual(row["source"], "ai_usage.py")
        self.assertEqual(set(row["payload"]), {"window_h", "claude_code"})
        self.assertEqual(row["payload"]["window_h"], 24)


if __name__ == "__main__":
    unittest.main()
