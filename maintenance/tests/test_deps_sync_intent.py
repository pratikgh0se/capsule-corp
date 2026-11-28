"""Z1: the three examples in contracts/intent/deps_sync.jsonl (row 217), replayed against
deps_sync.py. BUILTIN says what they must observe; the shipped-file test skips until the file
is shipped, then replays every row in it."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from capsule import intent  # noqa: E402
from deps_sync_fixture import Base  # noqa: E402


class IntentExamples(Base):
    """What each example must observe, and how a case is replayed."""

    BUILTIN = {
        "first_sync": {"exit": 0, "tail": "deps-sync: 0 ok, 2 got, 0 failed",
                       "fetched": 2, "events": 2},
        "second_run": {"exit": 0, "tail": "deps-sync: 2 ok, 0 got, 0 failed",
                       "fetched": 0, "events": 0},
        "malformed_allowlist_line": {
            "exit": 2, "stderr": "BAD repo-c/deps-allowlist.txt:2: pip:badpkg>=1.0.0",
            "fetched": 0},
    }

    def replay(self, case):
        """What the example observed: exit code, last stdout line, stderr, how many packages
        were fetched and how many sync events the last run wrote."""
        if case == "malformed_allowlist_line":
            self.add_repo("repo-c", "pip:fine==1.0\npip:badpkg>=1.0.0\n")
        if case == "second_run":
            self.run_main()
        before = len(self.sync_events())
        rc, out, err, f = self.run_main()
        return {"exit": rc, "tail": (out.splitlines() or [""])[-1], "stderr": err.strip(),
                "fetched": len(f.calls), "events": len(self.sync_events()) - before}

    def test_the_builtin_examples(self):
        for case, want in self.BUILTIN.items():
            self.setUp()
            got = self.replay(case)
            self.assertEqual({k: got[k] for k in want}, want, case)

    def test_the_shipped_rows_say_the_same(self):
        rows = intent.load("deps_sync")
        self.assertEqual(len(rows), len(self.BUILTIN))
        for ex in rows:
            self.setUp()
            case = ex["given"]["document"]["case"]
            want = {k: v for k, v in ex["expect"].items() if k != "note"}
            got = self.replay(case)
            self.assertEqual({k: got[k] for k in want}, want, ex["id"])


if __name__ == "__main__":
    unittest.main()
