"""Z1 intent examples for `capsule.replay` (contracts/intent/replay.jsonl, once the CoS ships it).
Each case has a handler that observes what the replay really does and a built-in expectation, so
the behaviour is held with or without the shipped rows; the loader test then checks every shipped
row against the same handlers and skips (inside the test) until the file exists."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capsule import db, intent, replay
from capsule.tests.test_replay import CFG2, OFF, history

OFF2 = dict(CFG2, redesign={})
BUILT_IN = {
    "replay_order": {"order_identical": True, "fidelity_complete": True, "matched": True},
    "forced": {"forced": 1, "matched": False, "verdict_says_forced": True,
               "verdict_different": True},
    "live_link": {"symlink_refused": True, "hard_link_refused": True,
                  "case_variant_refused": True},
}


class Cases(unittest.TestCase):
    def observe(self, case):
        handlers = {"replay_order": self.case_replay_order, "forced": self.case_forced,
                    "live_link": self.case_live_link}
        return handlers[case]()

    def case_replay_order(self):
        path, ids, order = history(self, CFG2)
        got = replay.replay(path, OFF2, n=20)
        k, total = got.fidelity
        return {"order_identical": got.recorded_order == got.replayed_order == order,
                "fidelity_complete": k == total, "matched": got.matched}

    def case_forced(self):
        path, ids, order = history(self, CFG2)
        conn = db.connect(path)
        first = conn.execute("SELECT task_id FROM task_events WHERE kind='claimed'"
                             " ORDER BY id").fetchone()
        conn.execute("DELETE FROM task_events WHERE task_id=? AND kind='created'",
                     (first["task_id"],))        # the rebuild cannot show the card as ready
        conn.close()
        got = replay.replay(path, OFF2, n=20)
        verdict = replay.report(got).splitlines()[-2]
        return {"forced": got.forced, "matched": got.matched,
                "verdict_says_forced": f"forced {got.forced};" in verdict,
                "verdict_different": "DIFFERENT" in verdict}

    def _refused(self, path):
        try:
            replay.snapshot(path)
        except ValueError:
            return True
        return False

    def case_live_link(self):
        home = Path(tempfile.mkdtemp()).resolve()
        live = home / "boards" / "x" / "kanban.db"
        live.parent.mkdir(parents=True)
        db.create_board(live)
        other = Path(tempfile.mkdtemp())
        (other / "link.db").symlink_to(live)
        os.link(live, other / "hard.db")
        variant = Path(str(live).replace("/boards/", "/BOARDS/"))
        with mock.patch.dict(os.environ, {"CAPSULE_HOME": str(home)}):
            return {"symlink_refused": self._refused(other / "link.db"),
                    "hard_link_refused": self._refused(other / "hard.db"),
                    # on a case-sensitive disk the variant is another path: nothing to refuse
                    "case_variant_refused": not variant.exists() or self._refused(variant)}

    def test_each_handler_holds_its_built_in_expectation(self):
        for case, want in BUILT_IN.items():
            self.assertEqual(self.observe(case), want, case)

    def test_the_shipped_rows_agree_with_the_handlers(self):
        rows = intent.load("replay")
        if not rows:
            self.skipTest("contracts/intent/replay.jsonl is not shipped yet")
        self.assertEqual(len(rows), 3)
        self.assertEqual({r["given"]["document"]["case"] for r in rows}, set(BUILT_IN))
        for ex in rows:
            seen = self.observe(ex["given"]["document"]["case"])
            self.assertEqual({k: ex["expect"][k] for k in seen}, seen, ex["id"])


if __name__ == "__main__":
    unittest.main()
