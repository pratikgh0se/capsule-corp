# mutation-check: targets-predate-range ff625e94 -- fixture rename only, no behaviour pinned ::
#   UnownedPairs.test_only_unblocked_and_unowned_items_count_and_are_sorted
"""`capsule.unblocked_stop` (row 558): the Stop hook blocks only when the unowned/unblocked
set changed since the last block it issued, or when 30 minutes passed with the set unchanged
(Z2: two oracles per case - the `should_block` return value, and the state file's content).
Every test passes its own `state_path` under a tempdir - never `~/.local/share/capsule`.
"""
import json
import tempfile
import unittest
from pathlib import Path

from capsule import paths, unblocked_stop

T0 = 1_700_000_000.0    # an arbitrary fixed "now" - the clock is always injected, never real


def report(items):
    """A minimal `capsule unblocked --json` document: just enough for `unowned_pairs`."""
    return {"items": items, "unblocked": sum(1 for i in items if i.get("state") == "unblocked"),
            "unowned": sum(1 for i in items if i.get("state") == "unblocked" and not i.get("owner"))}


def unowned(item, blocker="", **extra):
    return {"item": item, "source": "board", "state": "unblocked", "blocker": blocker, "owner": "", **extra}


class TmpState(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.state_path = self.dir / "unblocked-stop.json"


class UnownedPairs(unittest.TestCase):
    def test_only_unblocked_and_unowned_items_count_and_are_sorted(self):
        doc = report([
            unowned("z card"),
            {"item": "owned card", "state": "unblocked", "blocker": "", "owner": "tick"},
            {"item": "blocked card", "state": "blocked", "blocker": "waits: owner", "owner": ""},
            {"item": "notice card", "state": "notice", "blocker": "", "owner": ""},
            unowned("a card"),
        ])
        self.assertEqual(unblocked_stop.unowned_pairs(doc), [("a card", ""), ("z card", "")])

    def test_empty_or_non_dict_document_is_empty_pairs(self):
        self.assertEqual(unblocked_stop.unowned_pairs({"items": []}), [])
        self.assertEqual(unblocked_stop.unowned_pairs(None), [])


class ShouldBlock(TmpState):
    def test_empty_set_forgets_state_so_a_reappearance_blocks_again(self):
        # I1: an empty set removes any stale record (nothing to silence a later
        # reappearance, e.g. an owner's heartbeat lapsing), and the reappearance blocks.
        doc = report([unowned("card 1")])
        unblocked_stop.should_block(doc, state_path=self.state_path, now=T0)
        blocked0, pairs0 = unblocked_stop.should_block(report([]), state_path=self.state_path, now=T0 + 60)
        self.assertFalse(blocked0)
        self.assertEqual(pairs0, [])
        self.assertFalse(self.state_path.exists())
        blocked, pairs = unblocked_stop.should_block(doc, state_path=self.state_path, now=T0 + 120)
        self.assertTrue(blocked)
        self.assertEqual(pairs, [("card 1", "")])

    def test_missing_state_blocks_and_writes_new_state(self):
        doc = report([unowned("card 1")])
        blocked, pairs = unblocked_stop.should_block(doc, state_path=self.state_path, now=T0)
        self.assertTrue(blocked)
        self.assertEqual(pairs, [("card 1", "")])
        saved = json.loads(self.state_path.read_text())
        self.assertEqual(saved, {"pairs": [["card 1", ""]], "blocked_at": T0})

    def test_corrupt_or_incomplete_state_blocks_and_is_replaced(self):
        for bad in ('{not json', json.dumps({"pairs": [["card 1", ""]]})):    # no blocked_at
            self.state_path.write_text(bad)
            blocked, _ = unblocked_stop.should_block(
                report([unowned("card 1")]), state_path=self.state_path, now=T0)
            self.assertTrue(blocked)
            self.assertEqual(json.loads(self.state_path.read_text())["pairs"], [["card 1", ""]])

    def test_unchanged_set_within_30_minutes_does_not_block_and_state_is_untouched(self):
        doc = report([unowned("card 1")])
        unblocked_stop.should_block(doc, state_path=self.state_path, now=T0)    # first call: blocks
        before = self.state_path.read_text()
        blocked, pairs = unblocked_stop.should_block(doc, state_path=self.state_path, now=T0 + 60)
        self.assertFalse(blocked)
        self.assertEqual(pairs, [("card 1", "")])
        self.assertEqual(self.state_path.read_text(), before)    # a non-block never rewrites state

    def test_a_changed_set_blocks(self):
        # a new item, an item gone, and the same item's blocker text changed - the stored
        # set is (item, blocker) pairs, not item alone.
        cases = [
            ([unowned("card 1")], [unowned("card 1"), unowned("card 2")], [("card 1", ""), ("card 2", "")]),
            ([unowned("card 1"), unowned("card 2")], [unowned("card 1")], [("card 1", "")]),
            ([unowned("card 1", blocker="")], [unowned("card 1", blocker="waits: new")],
             [("card 1", "waits: new")]),
        ]
        for before, after, want in cases:
            state_path = Path(tempfile.mkdtemp()) / "unblocked-stop.json"
            unblocked_stop.should_block(report(before), state_path=state_path, now=T0)
            blocked, pairs = unblocked_stop.should_block(report(after), state_path=state_path, now=T0 + 60)
            self.assertTrue(blocked)
            self.assertEqual(pairs, want)

    def test_30_minute_renag_blocks_and_resets_the_clock(self):
        doc = report([unowned("card 1")])
        unblocked_stop.should_block(doc, state_path=self.state_path, now=T0)
        self.assertFalse(unblocked_stop.should_block(doc, state_path=self.state_path, now=T0 + 1799)[0])
        blocked, pairs = unblocked_stop.should_block(doc, state_path=self.state_path, now=T0 + 1800)
        self.assertTrue(blocked)
        self.assertEqual(pairs, [("card 1", "")])
        self.assertEqual(json.loads(self.state_path.read_text())["blocked_at"], T0 + 1800)
        # re-nag resets the clock: a third call needs another 30 minutes
        self.assertFalse(unblocked_stop.should_block(doc, state_path=self.state_path, now=T0 + 1860)[0])

    def test_future_blocked_at_never_reads_as_recent(self):
        # M4: a `blocked_at` ahead of `now` (clock moved back) must not mute the gate.
        self.state_path.write_text(json.dumps({"pairs": [["card 1", ""]], "blocked_at": T0 + 500}))
        blocked, _ = unblocked_stop.should_block(
            report([unowned("card 1")]), state_path=self.state_path, now=T0)
        self.assertTrue(blocked)

    def test_a_save_failure_still_blocks(self):
        # I2: state_path's parent is a regular file, so `mkdir` raises - the block must not
        # be swallowed; nagging again next turn is the safe direction.
        blocker_file = self.dir / "not-a-directory"
        blocker_file.write_text("x")
        blocked, pairs = unblocked_stop.should_block(
            report([unowned("card 1")]), state_path=blocker_file / "unblocked-stop.json", now=T0)
        self.assertTrue(blocked)
        self.assertEqual(pairs, [("card 1", "")])

    def test_a_rename_failure_leaves_no_tmp_file_and_still_blocks(self):
        # R2-M1: the final `tmp.replace(path)` can fail on its own (path exists as a
        # directory, so the write succeeds but the rename can't land) - the orphaned
        # `*.tmp` must not be left behind in CAPSULE_HOME, and the block itself still fires.
        self.state_path.mkdir()
        blocked, pairs = unblocked_stop.should_block(
            report([unowned("card 1")]), state_path=self.state_path, now=T0)
        self.assertTrue(blocked)
        self.assertEqual(pairs, [("card 1", "")])
        leftovers = list(self.dir.glob("*.tmp"))
        self.assertEqual(leftovers, [], f"orphan tmp file(s) left behind: {leftovers}")


class BusySeats(TmpState):
    """D155: seats are fresh implementer claims; `build/*` branch claims are the builder seats."""

    def claims(self, repo, claims):
        (self.dir / repo).mkdir(exist_ok=True)
        (self.dir / repo / "owners.json").write_text(json.dumps(
            {k: {"owner": o, "heartbeat": T0 - age} for k, (o, age) in claims.items()}))

    def test_counts_all_implementer_branches_and_build_branches_separately(self):
        self.claims("a", {"build/x": ("implementer", 60), "capsule/y": ("implementer", 60),
                          "t_abc123": ("implementer", 60), "build/k": ("merge-keeper", 60)})
        self.claims("b", {"build/z": ("implementer", 60)})
        self.assertEqual(unblocked_stop._busy_seats(self.dir, 3, now=T0), (3, 2))

    def test_stale_future_and_corrupt_claims_hold_no_seat(self):
        self.claims("a", {"build/old": ("implementer", 4 * 3600),
                          "build/future": ("implementer", -60)})
        (self.dir / "b").mkdir()
        (self.dir / "b" / "owners.json").write_text("{not json")
        self.assertEqual(unblocked_stop._busy_seats(self.dir, 3, now=T0), (0, 0))


class SeatsBlock(TmpState):
    def test_blocks_then_rate_limits_then_renags_in_its_own_state_file(self):
        self.assertTrue(unblocked_stop._seats_block(self.state_path, now=T0))
        self.assertFalse(unblocked_stop._seats_block(self.state_path, now=T0 + 60))
        self.assertTrue(unblocked_stop._seats_block(self.state_path, now=T0 + 1800))
        self.assertEqual(json.loads(self.state_path.read_text()), {"blocked_at": T0 + 1800})

    def test_corrupt_or_future_state_blocks(self):
        self.state_path.write_text("{not json")
        self.assertTrue(unblocked_stop._seats_block(self.state_path, now=T0))
        self.state_path.write_text(json.dumps({"blocked_at": T0 + 500}))
        self.assertTrue(unblocked_stop._seats_block(self.state_path, now=T0))


class IntentExamples(unittest.TestCase):
    """Row 454/558 (Z1 + Z2's second oracle): every `contracts/intent/unblocked_stop.jsonl`
    example runs through the real `should_block` oracle, not merely linted."""

    def test_every_example_matches_its_oracle(self):
        lines = (paths.INTENT / "unblocked_stop.jsonl").read_text().splitlines()
        self.assertTrue(lines)
        for line in lines:
            row = json.loads(line)
            doc = row["given"]["document"]
            state_path = Path(tempfile.mkdtemp()) / "unblocked-stop.json"
            state_path.write_text(json.dumps({"pairs": doc["prior_pairs"], "blocked_at": T0}))
            current = report([unowned(item, blocker=blocker) for item, blocker in doc["current_pairs"]])
            blocked, _ = unblocked_stop.should_block(
                current, state_path=state_path, now=T0 + doc["seconds_since_last_block"])
            self.assertEqual(blocked, row["expect"]["blocked"], row["id"])


if __name__ == "__main__":
    unittest.main()
