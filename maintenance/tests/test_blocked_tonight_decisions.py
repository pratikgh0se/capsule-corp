# mutation-check: targets-predate-range ff625e94 -- fixture rename only, no behaviour pinned ::
#   DecisionItems.test_cards_become_decision_items_oldest_first,
#   DecisionItems.test_an_unopenable_board_raises_instead_of_reading_as_nothing_waiting,
#   BlockedTonightLine.test_one_line_names_the_oldest_decision_and_counts_the_rest
"""The evening blocked-tonight line lists decisions only, read from the Capsule board's own
NEEDS YOU list (capsule.statusview), never from the old notify-seen ledger."""
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from capsule.tests import tmpclean  # noqa: E402
tmpclean.install()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("AGENT_OPS_NO_SEND", "1")

import brief  # noqa: E402

# main() reads the real clock; a whole second keeps `NOW - 49 h` from flooring to 48 h
NOW = float(int(time.time()))


def card(board, number, name, hours, task="t_x"):
    return {"board": board, "number": number, "name": name, "ask": "A question is waiting.",
            "since": NOW - hours * 3600, "ref": {"task": task}}


SIGN_OFF = {"board": "capsule-corp", "number": None, "name": "settings", "since": NOW - 99 * 3600,
            "ask": "A change is waiting for your sign-off.", "ref": {"file": "x.json"}}


def snap(needs, problems=()):
    return {"needs_you": needs, "attention": {"open": [{"what": w} for w in problems]}}


class DecisionItems(unittest.TestCase):
    def items(self, needs, problems=()):
        now = brief._dt.datetime.fromtimestamp(NOW).astimezone()
        return brief._decision_items(now=now, snapshot=lambda: snap(needs, problems))

    def test_cards_become_decision_items_oldest_first(self):
        got = self.items([card("my-app", 153, "Sync", 5),
                          card("capsule-corp", 371, "Guard", 49)])
        self.assertEqual([(i["kind"], i["what"], i["title"], i["age_h"]) for i in got],
                         [("Decision", "capsule-corp #371", "Guard", 49),
                          ("Decision", "my-app #153", "Sync", 5)])

    def test_a_sign_off_file_is_not_a_decision(self):
        self.assertEqual(self.items([SIGN_OFF]), [])

    def test_a_card_without_a_number_is_named_by_its_task(self):
        got = self.items([card("capsule-corp", None, "Guard", 1, task="t_ab12")])
        self.assertEqual(got[0]["what"], "capsule-corp/t_ab12")

    def test_a_broken_snapshot_raises(self):
        def boom():
            raise RuntimeError("board unreadable")
        with self.assertRaises(RuntimeError):
            brief._decision_items(snapshot=boom)

    def test_an_unopenable_board_raises_instead_of_reading_as_nothing_waiting(self):
        with self.assertRaisesRegex(RuntimeError, "my-app: the board cannot be opened"):
            self.items([card("capsule-corp", 371, "Guard", 49)],
                       ["my-app: the board cannot be opened."])

    def test_other_attention_problems_do_not_stop_the_line(self):
        got = self.items([card("capsule-corp", 371, "Guard", 49)],
                         ["capsule-corp: the loop is not running."])
        self.assertEqual(len(got), 1)


class BlockedTonightLine(unittest.TestCase):
    def run_main(self, needs, seen=None):
        state = Path(tempfile.mkdtemp(prefix="bt-"))
        (state / "jobs").mkdir()
        (state / "events.jsonl").write_text("")
        if seen is not None:
            (state / "notify-seen.json").write_text(json.dumps(seen))
        out = io.StringIO()
        with redirect_stdout(out), \
                patch("capsule.statusview.snapshot", return_value=snap(needs)):
            rc = brief.main(["--blocked-tonight", "--state-dir", str(state)])
        self.assertEqual(rc, 0)
        return out.getvalue()

    def test_stale_ledger_records_never_reach_the_line(self):
        seen = {"t_old:blocked:abc": "2026-09-01", "t_old:pr": "2026-09-01"}
        self.assertEqual(self.run_main([], seen), "")

    def test_one_line_names_the_oldest_decision_and_counts_the_rest(self):
        out = self.run_main([card("capsule-corp", 371, "Guard", 49),
                             card("my-app", 153, "Sync", 5)])
        self.assertEqual(
            out, "Blocked tonight: Decision - capsule-corp #371 - Guard (open 49 h) (+1 more)\n")

    def test_exactly_one_decision_has_no_more_suffix(self):
        out = self.run_main([card("capsule-corp", 371, "Guard", 49)])
        self.assertEqual(out, "Blocked tonight: Decision - capsule-corp #371 - Guard (open 49 h)\n")


class AsLaunchdRuns(unittest.TestCase):
    """launchd puts only maintenance/ on sys.path: the script runs alone, no PYTHONPATH."""

    def run_script(self, home):
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env.update(OPS_BRIEF_V2="1", AGENT_OPS_NO_SEND="1", CAPSULE_HOME=str(home),
                   AGENT_OPS_STATE_DIR=tempfile.mkdtemp(prefix="bt-state-"))
        return subprocess.run(
            [sys.executable, str(Path(brief.__file__)), "--blocked-tonight"],
            cwd=tempfile.gettempdir(), env=env, capture_output=True, text=True, timeout=60)

    def seeded_home(self):
        """A home whose boards open; capsule-corp holds one needs_input card, 49 h old."""
        from capsule import db
        home = Path(tempfile.mkdtemp(prefix="bt-home-"))
        for name in ("capsule-corp", "my-app"):
            path = home / "boards" / name / "kanban.db"
            path.parent.mkdir(parents=True)
            db.create_board(path)
            conn = db.connect(path)
            if name == "capsule-corp":
                tid = db.new_task(conn, title="Guard the secret", assignee="dev-backend",
                                  status="blocked", now=int(time.time()) - 49 * 3600)
                conn.execute("UPDATE tasks SET block_kind='needs_input' WHERE id=?", (tid,))
                conn.commit()
            conn.close()
        return home

    def test_the_script_alone_composes_the_line_for_a_seeded_decision(self):
        run = self.run_script(self.seeded_home())
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertRegex(run.stdout, r"^Blocked tonight: Decision - capsule-corp "
                                     r".*Guard the secret \(open 49 h\)\n$")

    def test_the_script_alone_fails_when_a_board_cannot_be_opened(self):
        run = self.run_script(Path(tempfile.mkdtemp(prefix="bt-home-")))
        self.assertNotEqual(run.returncode, 0)
        self.assertEqual(run.stdout, "")
        self.assertIn("the board cannot be opened", run.stderr)


class QuietLine(unittest.TestCase):
    def test_the_close_names_the_07_30_end_of_quiet_hours(self):
        self.assertEqual(brief.QUIET_LINE, "Quiet until 07:30 unless something pages.")


if __name__ == "__main__":
    unittest.main()
