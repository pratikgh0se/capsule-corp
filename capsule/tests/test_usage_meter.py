"""Weekly usage meter (D234): token sums from a tmp transcript tree, never the real one."""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

os.environ["AGENT_OPS_NO_SEND"] = "1"
from capsule import usage_meter as um

NOW = datetime(2026, 11, 12, 10, 0, tzinfo=timezone.utc).timestamp()    # Thursday
WEEK = datetime(2026, 11, 7, 21, 0, tzinfo=timezone.utc)                # Sun 04:00 Bangkok
SECRET = "SENTINEL-do-not-leak-7731"
DAY = "2026-11-12T"


def rec(mid, ts, model="claude-sonnet-5-5", req="r1", i=0, o=0, cw=0, cr=0, text=SECRET):
    use = {"input_tokens": i, "output_tokens": o, "cache_creation_input_tokens": cw,
           "cache_read_input_tokens": cr}
    return json.dumps({"type": "assistant", "timestamp": ts, "requestId": req, "uuid": mid + "u",
                       "message": {"id": mid, "model": model, "usage": use,
                                   "content": [{"type": "text", "text": text}]}}) + "\n"


class Meter(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="usage-meter-"))
        self.root, self.state = self.tmp / "projects", self.tmp / "state"
        self.home = self.tmp / "home"
        (self.root / "p" / "sid" / "subagents").mkdir(parents=True)
        self.home.mkdir()
        um._warned.clear()
        self.cfg({"usage_weekly_limit": 10000})

    def cfg(self, extra):
        doc = {"usage_transcripts_root": str(self.root), **extra}
        (self.home / "config.json").write_text(json.dumps(doc))

    def write(self, name, *lines, mode="w"):
        with open(self.root / "p" / name, mode) as fh:
            fh.write("".join(lines))

    def read(self, now=NOW):
        err = io.StringIO()
        with redirect_stderr(err):
            got = um.weekly_usage(state_dir=self.state, now=now, home=self.home)
        return got, err.getvalue()

    def test_duplicate_message_counts_once_with_the_larger_output(self):
        self.write("a.jsonl", rec("m1", DAY + "09:00:00Z", o=4),
                   rec("m1", DAY + "09:00:01Z", o=200))
        self.assertEqual(self.read()[0]["used"], 1000.0)       # 200 output * 5

    def test_week_boundary_is_sunday_04_bangkok(self):
        self.write("a.jsonl", rec("old", "2026-11-07T20:59:59Z", i=100),
                   rec("new", "2026-11-07T21:00:00Z", i=7))
        got = self.read()[0]
        self.assertEqual(got["used"], 7.0)
        self.assertEqual(um._week_start(NOW, um.RESET), WEEK.timestamp())
        self.assertEqual(got["reset_at"], WEEK.timestamp() + 7 * 86400)

    def test_weights_by_token_kind_and_model_and_subagent_files(self):
        self.write("a.jsonl", rec("s", DAY + "09:00:00Z", i=1000, o=100, cr=10000))
        self.write("sid/subagents/b.jsonl",
                   rec("h", DAY + "09:00:00Z", "claude-haiku-5-5", req="r2",
                       i=1000, o=100, cr=10000))
        self.write("a.jsonl", rec("z", DAY + "09:00:00Z", "<synthetic>", i=99999), mode="a")
        self.assertEqual(self.read()[0]["used"], 2500.0 + 500.0)   # haiku is 0.2 of sonnet

    def test_fraction_and_pause_threshold_from_config(self):
        self.write("a.jsonl", rec("m", DAY + "09:00:00Z", i=9000))
        self.assertEqual(self.read()[0]["fraction"], 0.9)
        self.assertEqual(self.read()[0]["pause_at"], 0.90)
        self.cfg({"usage_weekly_limit": 10000, "usage_pause_at": 0.5})
        self.assertEqual(self.read()[0]["pause_at"], 0.5)

    def test_no_signal_fails_open_with_one_warning(self):
        (self.home / "config.json").write_text("{}")
        got, err = self.read()
        self.assertIsNone(got)
        self.assertEqual(err.count("usage_meter:"), 1)
        self.assertEqual(self.read()[1], "")                   # one warning per process
        (self.home / "config.json").write_text("not json")
        self.assertIsNone(self.read()[0])

    def test_garbage_lines_are_skipped(self):
        self.write("a.jsonl", "not json\n", '{"type": "assistant", "usage": 1}\n',
                   rec("m", DAY + "09:00:00Z", i=5))
        self.assertEqual(self.read()[0]["used"], 5.0)

    def test_warm_scan_reads_only_new_bytes_and_keeps_max_across_scans(self):
        self.write("a.jsonl", rec("m1", DAY + "09:59:00Z", o=4))
        self.read()
        self.write("a.jsonl", rec("m1", DAY + "09:59:01Z", o=10), rec("m2", DAY + "09:59:02Z", i=3),
                   mode="a")
        with mock.patch.object(um, "_add", wraps=um._add) as add:
            got, _ = self.read()
        self.assertEqual(add.call_count, 2)                    # only the appended lines
        self.assertEqual(got["used"], 10 * 5 + 3.0)            # m1 grew by 6 output, not recounted
        self.assertEqual(self.read()[0]["used"], 53.0)         # idempotent

    def test_recent_table_is_bounded_and_week_rollover_resets(self):
        self.write("a.jsonl", rec("old", DAY + "01:00:00Z", i=1),
                   rec("new", DAY + "09:59:00Z", i=1))
        self.read()
        state = json.loads((self.state / um.STATE_NAME).read_text())
        self.assertEqual(list(state["recent"]), ["new|r1"])    # older than RECENT_S pruned
        self.assertEqual(self.read(now=NOW + 7 * 86400)[0]["used"], 0.0)   # new week, zero

    def test_message_text_never_reaches_state_or_streams(self):
        self.write("a.jsonl", rec("m", DAY + "09:59:00Z", i=1))
        _, err = self.read()
        self.assertNotIn(SECRET, err)
        self.assertNotIn(SECRET, (self.state / um.STATE_NAME).read_text())

    def test_missing_transcripts_dir_has_no_signal_and_one_warning(self):
        self.cfg({"usage_weekly_limit": 10000, "usage_transcripts_root": str(self.tmp / "none")})
        got, err = self.read()
        self.assertIsNone(got)
        self.assertEqual(err.count("usage_meter:"), 1)

    def test_result_has_week_start_and_accepts_str_paths(self):
        self.write("a.jsonl", rec("m", DAY + "09:00:00Z", i=1))
        with redirect_stderr(io.StringIO()):
            got = um.weekly_usage(state_dir=str(self.state), now=NOW, home=str(self.home))
        self.assertEqual(got["week_start"], WEEK.timestamp())
        self.assertEqual(got["used"], 1.0)

    def test_torn_last_line_waits_until_it_is_complete(self):
        line = rec("m1", DAY + "09:00:00Z", o=10)
        self.write("a.jsonl", line + rec("m2", DAY + "09:00:01Z", o=1)[:30])
        self.assertEqual(self.read()[0]["used"], 50.0)
        self.write("a.jsonl", rec("m2", DAY + "09:00:01Z", o=1)[30:], mode="a")
        self.assertEqual(self.read()[0]["used"], 55.0)

    def test_truncated_or_replaced_file_forces_a_rescan(self):
        self.write("a.jsonl", rec("m1", DAY + "09:00:00Z", o=10), rec("m5", DAY + "09:00:01Z", o=1))
        self.read()
        self.write("a.jsonl", rec("m1", DAY + "09:00:00Z", o=10))        # shrank
        self.assertEqual(self.read()[0]["used"], 50.0)
        self.write("b.jsonl", rec("m3", DAY + "09:01:00Z", o=2))
        self.read()
        new = self.root / "p" / "b.tmp"
        new.write_text(rec("m3", DAY + "09:01:00Z", o=2) + rec("m4", DAY + "09:01:01Z", o=3))
        os.replace(new, self.root / "p" / "b.jsonl")                      # new inode, longer
        self.assertEqual(self.read()[0]["used"], 75.0)

    def test_wrong_shape_state_starts_fresh_instead_of_turning_the_meter_off(self):
        self.write("a.jsonl", rec("m1", DAY + "09:00:00Z", o=10))
        self.read()
        path = self.state / um.STATE_NAME
        for bad in ({"recent": []}, {"files": None}, {"sums": 5}, {"week_start": "x"}):
            state = json.loads(path.read_text())
            path.write_text(json.dumps({**state, **bad}))
            self.assertEqual(self.read()[0]["used"], 50.0, bad)
        state = json.loads(path.read_text())
        del state["files"]
        path.write_text(json.dumps(state))
        self.assertEqual(self.read()[0]["used"], 50.0)
        path.write_text("{trunc")
        self.assertEqual(self.read()[0]["used"], 50.0)

    def test_bad_inner_state_values_start_fresh(self):
        self.write("a.jsonl", rec("m1", DAY + "09:00:00Z", o=10))
        self.read()
        path = self.state / um.STATE_NAME
        good = json.loads(path.read_text())
        bads = ({"recent": {"k": 5}}, {"recent": {"k": [1.0, "m", 1, 2]}},
                {"sums": {"m": [1, 2]}}, {"sums": {"m": "x"}}, {"sums": {"m": [1, 2, 3, "a"]}},
                {"files": {"f": 3}}, {"files": {"f": {"inode": 1}}})
        for bad in bads:
            path.write_text(json.dumps({**good, **bad}))
            self.assertEqual(self.read()[0]["used"], 50.0, bad)

    def test_a_scan_that_raises_on_loaded_state_is_rebuilt_once(self):
        self.write("a.jsonl", rec("m1", DAY + "09:00:00Z", o=10))
        self.read()
        real = um._scan
        calls = []

        def flaky(root, state, start):
            calls.append(1)
            if len(calls) == 1:
                raise ValueError("odd state")
            return real(root, state, start)
        with mock.patch.object(um, "_scan", flaky):
            self.assertEqual(self.read()[0]["used"], 50.0)

    def test_models_merge_and_integer_pause_at(self):
        self.write("a.jsonl", rec("m", DAY + "09:00:00Z", "claude-fable-5-1", i=100))
        self.cfg({"usage_weekly_limit": 10000, "usage_weights": {"models": {"fable": 3}},
                  "usage_pause_at": 1})
        got = self.read()[0]
        self.assertEqual(got["used"], 300.0)
        self.assertEqual(got["pause_at"], 1.0)
        self.cfg({"usage_weekly_limit": 10000, "usage_weights": {"models": {"zzz": 9}}})
        self.assertEqual(self.read()[0]["used"], 200.0)       # fable keeps its default weight 2


if __name__ == "__main__":
    unittest.main()
