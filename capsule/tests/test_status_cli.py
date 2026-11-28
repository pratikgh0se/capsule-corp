"""`python3 -m capsule status`: the screen, `--json` and `--watch` over `statusview` (spec 4, 8).
Two oracles (Z2): a golden screen text for a hand-written snapshot, and a JSON shape check."""
import io
import json
import re
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stdout
from pathlib import Path

from capsule import __main__ as cli
from capsule import status_cli, statusview

SNAP = {
    "generated_at": 1_800_000_000,
    "boards": [{"board": "capsule-corp", "loop": "alive", "auto_landing": True},
               {"board": "other-app", "loop": "down", "auto_landing": False}],
    "running": [{"board": "capsule-corp", "number": 14, "name": "Retry on rate limit",
                 "role": "implementer", "role_label": "builder", "role_emoji": "🔨",
                 "model": "Sonnet 5.5", "minutes": 12, "quiet_minutes": None,
                 "ref": {"task": "t_0123abcd"}},
                {"board": "capsule-corp", "number": None, "name": "Slow one", "role": "reviewer",
                 "role_label": "reviewer", "role_emoji": "🔍", "model": "Opus 5.5", "minutes": 40,
                 "quiet_minutes": 25, "ref": {"task": "t_0123abce"}}],
    "needs_you": [{"board": "capsule-corp", "number": 27, "name": "Slack routing",
                   "ask": "Two reviewers disagree.", "since": 1_800_000_000 - 3 * 3600,
                   "ref": {"task": "t_0123abcf"}}],
    "blocked": [{"board": "capsule-corp", "number": None, "name": f"Card {i}",
                 "reason": "parked on purpose",
                 "ref": {"task": f"t_0123ab0{i}"}} for i in range(6)],
    "attention": {"open": [{"what": "The Grafana feed is not shipping, so alerts are blind."}],
                  "handled_count": 2},
    "outside_factory": {"seats": 3, "summary": "2 CoS keepers, 1 builder", "ages_min": [1, 2, 3]},
}

GOLDEN = """🟢 capsule-corp · auto-landing on  🔴 other-app (down) · auto-landing off
▶️ 2 running   🙋 1 need you   🧱 6 blocked   🔴 1 unowned problems

▶️ RUNNING (2)
  🔨 Retry on rate limit #14 · builder · Sonnet 5.5 · 12m
  🔍 Slow one · reviewer · Opus 5.5 · 40m (quiet 25m)

🙋 NEEDS YOU (1)
  🙋 Slack routing #27 · Two reviewers disagree. · 3h

🧱 BLOCKED (6)
  🧱 parked on purpose (6): Card 0, Card 1, Card 2, Card 3 +2 more

🔴 ATTENTION (1)
  🔴 The Grafana feed is not shipping, so alerts are blind.
  🔧 2 problems being handled

🤖 OUTSIDE FACTORY
  🤖 2 CoS keepers, 1 builder (seats 1m, 2m, 3m old)"""

ID_RE = re.compile(r"t_[0-9a-f]{8}|inc_|python3 -m|/Users/")
SHAPE = {"generated_at": int, "boards": list, "running": list, "needs_you": list, "blocked": list,
         "attention": dict, "outside_factory": dict}


def run_cli(*argv, home):
    out = io.StringIO()
    with redirect_stdout(out):
        code = cli.main(["status", "--capsule-home", str(home), *argv])
    return code, out.getvalue()


class Render(unittest.TestCase):
    def test_golden_screen(self):
        self.assertEqual(status_cli.render(SNAP), GOLDEN)

    def test_screen_has_no_ids(self):
        self.assertFalse(ID_RE.search(status_cli.render(SNAP)))

    def test_empty_snapshot_says_so_in_every_section(self):
        empty = dict(SNAP, running=[], needs_you=[], blocked=[],
                     attention={"open": [], "handled_count": 0},
                     outside_factory={"seats": 0, "summary": "", "ages_min": []})
        text = status_cli.render(empty)
        for line in ("nothing running", "nothing needs you", "nothing blocked", "nothing unowned",
                     "nobody working outside the factory"):
            self.assertIn(line, text)


class OutsideAges(unittest.TestCase):
    def test_single_seat_age_and_empty_ages(self):
        keeper = {"seats": 1, "summary": "1 CoS keeper", "ages_min": [39]}
        snap = dict(SNAP, outside_factory=keeper)
        self.assertIn("  🤖 1 CoS keeper (seat 39m old)", status_cli.render(snap))
        snap = dict(SNAP, outside_factory={"seats": 1, "summary": "1 builder", "ages_min": []})
        self.assertIn("  🤖 1 builder\n", status_cli.render(snap) + "\n")


class Cli(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())

    def test_print_once_exits_zero_with_every_board_down(self):
        code, text = run_cli(home=self.home)
        self.assertEqual(code, 0)
        self.assertIn("🔴 capsule-corp (down)", text)
        self.assertIn("🔴 ATTENTION", text)

    def test_json_has_the_spec_shape_and_no_refs(self):
        code, text = run_cli("--json", home=self.home)
        doc = json.loads(text)
        self.assertEqual(code, 0)
        for key, kind in SHAPE.items():
            self.assertIsInstance(doc[key], kind, key)
        self.assertEqual(set(doc["boards"][0]), {"board", "loop", "auto_landing"})
        self.assertEqual(set(doc["attention"]), {"open", "handled_count"})
        self.assertEqual(set(doc["outside_factory"]), {"seats", "summary", "ages_min"})
        self.assertNotIn('"ref"', text)

    def test_json_refs_keeps_ids(self):
        snap = json.loads(status_cli._to_json(SNAP, refs=True))
        self.assertEqual(snap["running"][0]["ref"], {"task": "t_0123abcd"})
        self.assertNotIn("ref", json.loads(status_cli._to_json(SNAP))["running"][0])

    def test_watch_with_json_and_refs_without_json_are_refused(self):
        for argv in (["--watch", "--json"], ["--refs"]):
            with self.assertRaises(SystemExit) as cm, redirect_stdout(io.StringIO()), \
                    unittest.mock.patch("sys.stderr", io.StringIO()):
                run_cli(*argv, home=self.home)
            self.assertEqual(cm.exception.code, 2)


class Watch(unittest.TestCase):
    def test_redraws_each_cycle_and_sleeps_between_at_least_three_seconds(self):
        out, naps, shown = io.StringIO(), [], iter(["one", "two", "three"])
        code = status_cli.watch(lambda: next(shown), 0.5, out, sleep=naps.append, cycles=3)
        self.assertEqual(code, 0)
        self.assertEqual(naps, [3.0, 3.0])
        self.assertEqual(out.getvalue().count("\033[H\033[2J"), 3)
        self.assertTrue(out.getvalue().endswith("three\n"))

    def test_ctrl_c_exits_zero(self):
        args = unittest.mock.Mock(json=False, refs=False, watch=True, interval=4, capsule_home=None)
        with unittest.mock.patch.object(status_cli, "watch", side_effect=KeyboardInterrupt):
            self.assertEqual(status_cli.run(args, None, io.StringIO()), 0)

    def test_cli_watch_draws_the_screen(self):
        home, out = Path(tempfile.mkdtemp()), io.StringIO()
        args = unittest.mock.Mock(json=False, refs=False, watch=True, interval=4,
                                  capsule_home=str(home))
        with unittest.mock.patch.object(status_cli, "watch", return_value=0) as w:
            self.assertEqual(status_cli.run(args, None, out), 0)
        show = w.call_args[0][0]
        self.assertIn("RUNNING", show())


if __name__ == "__main__":
    unittest.main()
