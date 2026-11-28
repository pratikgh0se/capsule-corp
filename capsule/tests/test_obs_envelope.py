"""Schema test for the factory envelope (OBS-09 slice 1): tick, worker and Lookout sample lines."""
import unittest
from datetime import datetime, timezone

from capsule import obs_envelope as oe

RES = {"service.name": "capsule-corp", "service.version": "abc1234",
       "service.instance.id": "mac-1:42", "deployment.environment.name": "local"}
RUN = "6f1c2d3e-0000-4000-8000-123456789abc"
NOW = datetime(2026, 10, 23, 9, 30, 0, 123000, tzinfo=timezone.utc)
SAMPLES = {
    "tick": dict(event_name="claimed", body="claimed t_ab12cd34", board="capsule-corp",
                 task_id="t_ab12cd34", run_id=RUN),
    "worker": dict(event_name="request_review", body="implementer done", board="capsule-corp",
                   task_id="t_ab12cd34", run_id=RUN, step="implementing"),
    "lookout": dict(event_name="incident_opened", body="opened inc_1", severity="WARN",
                    board="capsule-corp", task_id="t_ab12cd34", run_id=RUN, step="ship"),
}


def line(name, **over):
    kw = dict(SAMPLES[name], now=NOW, **over)
    return oe.envelope(kw.pop("event_name"), kw.pop("body"), RES, **kw)


class Envelope(unittest.TestCase):
    def test_sample_lines_pass_schema(self):
        for name in SAMPLES:
            with self.subTest(name):
                self.assertEqual(oe.problems(line(name)), [])

    def test_exact_shape(self):
        got = line("worker")
        self.assertEqual(got["ts"], "2026-10-23T09:30:00.123Z")
        self.assertEqual(got["trace_id"], oe.trace_id(RUN))
        self.assertEqual(len(got["trace_id"]), 32)
        self.assertRegex(got["span_id"], "^[0-9a-f]{16}$")
        self.assertNotEqual(got["span_id"], line("worker", step="other")["span_id"])
        for k in oe.REQUIRED + oe.CORRELATION:
            self.assertIn(k, got)

    def test_trace_id_stable_and_distinct(self):
        self.assertEqual(oe.trace_id(RUN), oe.trace_id(RUN))
        self.assertNotEqual(oe.trace_id(RUN), oe.trace_id(RUN + "x"))

    def test_body_is_one_scrubbed_line(self):
        got = oe.envelope("claimed", "a\nb  token=sk-ant-api03-" + "x" * 40, RES, now=NOW)
        self.assertNotIn("\n", got["body"])
        self.assertNotIn("sk-ant", got["body"])

    def test_schema_rejects_each_breach(self):
        good = line("tick")
        bad = [({"event.name": "free text"}, "event.name"),
               ({"severity_text": "FATAL"}, "severity"),
               ({"ts": "yesterday"}, "ts"), ({"trace_id": "0" * 32}, "hash"),
               ({"span_id": "zz"}, "span_id"), ({"body": "a\nb"}, "one line"),
               ({"capsule.task_id": 5}, "string")]
        for over, word in bad:
            with self.subTest(word):
                self.assertTrue(any(word in p for p in oe.problems({**good, **over})))
        self.assertTrue(any("host.name" in p for p in oe.problems(
            {k: v for k, v in good.items() if k != "host.name"})))


if __name__ == "__main__":
    unittest.main()
