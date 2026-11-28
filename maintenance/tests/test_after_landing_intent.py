"""Z1 (row 643): the intent rows of contracts/intent/after_landing.jsonl, replayed against the
fake world of after_landing_fakes once the CoS has stamped the file (the storage_gc pattern)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from after_landing_fakes import Chain  # noqa: E402


class Replay(Chain):
    """One fresh fake world per case; `replay` returns the outcome fields the intent rows name."""

    def runTest(self):      # a TestCase needs one; the cases are driven by IntentRows
        pass

    def replay(self, case):
        if case == "restart":
            self.land("maintenance/vault_git.py")
            self.ticks.update(pomo=None, idle=None)         # alpha runs, nolist has no plist
            rc = self.run_main("--apply")[0]
            pulls = [a for a in self.argvs
                     if "pull" in a and a[:3] == ["git", "-C", str(self.harness)]]
            return {"exit": rc, "restarted_boards": len(self.restarts()),
                    "skipped_board_restarted": int("nolist" in self.restarts()),
                    "harness_pulled_by_script": len(pulls),
                    "harness_check_after_restart":
                        int(self.calls[-1] == ("deploy", ["--check"], self.harness)
                            and self.calls[-2][0] == "restart")}
        if case == "dirty":
            self.land()
            (self.checkout / "README").write_text("edited")
            before = self.heads()
            rc = self.run_main("--apply")[0]
            after = self.heads()
            return {"exit": rc, "checkout_moved": int(after[0] != before[0]),
                    "harness_moved": int(after[1] != before[1]),
                    "deploys_run": sum(c[0] == "deploy" for c in self.calls),
                    "restarts_run": len(self.restarts())}
        self.land()
        self.ticks.update(alpha=None, pomo=None)
        before = self.heads()
        rc, out, _ = self.run_main("--apply")
        return {"exit": rc, "restarts_run": len(self.restarts()),
                "harness_check_run": int(("deploy", ["--check"], self.harness) in self.calls),
                "harness_moved": int(self.heads()[1] != before[1]),
                "note_says_restart_not_start": int("`bin/capsule-tick.sh restart alpha`" in out
                                                   and "not start" in out)}


class IntentRows(unittest.TestCase):
    """The built-in cases run with or without the stamped file."""
    CASES = {"vault-git-change-restarts-tick-not-harness-pull": "restart",
             "tracked-local-change-refuses-before-anything-moves": "dirty",
             "tick-code-changed-but-no-tick-running": "none"}

    def got(self, case):
        world = Replay()
        world.setUp()
        self.addCleanup(world.doCleanups)
        return world.replay(case)

    def test_the_built_in_cases_hold_without_the_file(self):
        self.assertEqual(self.got("restart"),
                         {"exit": 0, "restarted_boards": 1, "skipped_board_restarted": 0,
                          "harness_pulled_by_script": 0, "harness_check_after_restart": 1})
        self.assertEqual(self.got("dirty"),
                         {"exit": 1, "checkout_moved": 0, "harness_moved": 0, "deploys_run": 0,
                          "restarts_run": 0})
        self.assertEqual(self.got("none"),
                         {"exit": 0, "restarts_run": 0, "harness_check_run": 0,
                          "harness_moved": 0, "note_says_restart_not_start": 1})

    def test_the_intent_rows_hold_when_the_file_exists(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from capsule import intent
        rows = intent.load("after_landing")
        self.assertEqual({r["id"] for r in rows}, set(self.CASES))
        for row in rows:
            got = self.got(self.CASES[row["id"]])
            for key, want in row["expect"].items():
                if isinstance(want, int):
                    self.assertEqual(got[key], want, row["id"])


if __name__ == "__main__":
    unittest.main()
