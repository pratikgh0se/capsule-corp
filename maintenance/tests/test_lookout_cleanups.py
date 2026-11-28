# mutation-check: targets-predate-range ff625e94 -- fixture rename only, no behaviour pinned ::
#   OwnedSkipDedupe.test_one_skip_event_per_board_per_local_day
"""Task 9 (The Lookout): the four small cleanups of spec 9.2."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from capsule.tests import tmpclean  # noqa: E402
tmpclean.install()

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "maintenance"))
os.environ.setdefault("EVENTS_SPOOL", tempfile.mkdtemp(prefix="events-test-") + "/events.jsonl")
import board_fence  # noqa: E402
import events  # noqa: E402


class OwnedSkipDedupe(unittest.TestCase):
    def test_one_skip_event_per_board_per_local_day(self):
        d = Path(tempfile.mkdtemp()) / "my-app"
        d.mkdir()
        (d / ".capsule-owned").write_text("")
        stamp = d.parent / "skip.json"
        before = len(events.read(events.SPOOL))
        for _ in range(3):
            self.assertTrue(board_fence.capsule_owned_skip(d, today="2026-09-27", stamp=stamp))
        board_fence.capsule_owned_skip(d, today="2026-09-28", stamp=stamp)
        rows = [r for r in events.read(events.SPOOL)[before:] if r["payload"].get("event") == "capsule_owned_skip"]
        self.assertEqual(len(rows), 2)


class UsagePayload(unittest.TestCase):
    def test_usage_event_no_longer_reports_runs_today(self):
        src = (REPO / "maintenance" / "usage_guard.py").read_text()
        call = src.split('events.log("usage"', 1)[1].split("})", 1)[0]
        self.assertNotIn("runs_today", call)
        self.assertNotIn("ceiling", call)
        self.assertIn("session_pct", call)


if __name__ == "__main__":
    unittest.main()
