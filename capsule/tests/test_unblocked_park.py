# mutation-check: targets-predate-range b957739c9 -- names out of tests, no behaviour pinned ::
#   Reproduce.test_old_real_branch_with_no_marker_or_name_is_flagged_not_unowned
"""Park-or-delete flagging for stale unclassified branches (row 470/473, t_59a6f5af), fix
round 1: folded into `unblocked.render_table`'s existing unclassified section (I2 (a) - no
separate module, one detector run, `unblocked.py`/`test_unblocked.py` stay at their frozen
size caps). A `state == "unclassified"` item (row 470 fix round 1, M5) at least 30 days old
gets `, park-or-delete decision due` appended to its line; younger ones do not, and neither
ever counts as UNOWNED. Age is the branch tip's own commit date (`branch_items`'s
`committerdate:unix`) - a rebase resets it; unclassified branches carry no queue marker, so
the landing queue's keeper never rebases them for other reasons either.

Z2: two oracles - `render_table`'s own output text (this fix) AND
`unblocked.summarize`/`unblocked_stop.unowned_pairs` (the unowned count / Stop hook output),
sharing no code path with `render_table`.
"""
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from capsule import paths, unblocked, unblocked_stop

DAY = 86400
DUE = "park-or-delete decision due"


def _item(age_s, state="unclassified", item="capsule-corp:someones-scratch (unclassified)"):
    return unblocked.Item(item, "branch", state, age_s=age_s)


class RenderTableFlag(unittest.TestCase):
    def test_fresh_unclassified_branch_is_not_flagged(self):
        out = unblocked.render_table([_item(5 * DAY)])
        self.assertNotIn(DUE, out)

    def test_one_second_short_of_30_days_is_not_flagged(self):
        """Lower edge: kills a 29d/6d threshold."""
        self.assertNotIn(DUE, unblocked.render_table([_item(30 * DAY - 1)]))

    def test_branch_at_or_past_30_days_is_flagged(self):
        """`>=`, not `>` - a branch is never "almost" due."""
        for age in (30 * DAY, 31 * DAY):
            out = unblocked.render_table([_item(age)])
            self.assertIn(DUE, out, age)

    def test_an_unknown_age_is_unknown_never_old(self):
        """W96 follow-up (decision, t_ae574709): a missing age (`age_s=None`) counts as
        UNKNOWN, not old. It prints `(?)` so a reader sees the gap, and never claims a
        park-or-delete decision is due on no evidence. `branch_items` cannot produce one today
        (an unparseable tip date falls back to `now`), so this pins the rule for any future
        adapter - and kills an `(age or 10**9)` style flip to "treat unknown as ancient"."""
        out = unblocked.render_table([_item(None)])
        self.assertIn("(unclassified) (?)", out)
        self.assertNotIn(DUE, out)
        self.assertEqual(unblocked.summarize([_item(None)]), (0, 0))

    def test_old_but_not_unclassified_is_never_flagged(self):
        """Only the unclassified heading gets this text - an old `blocked`/`unblocked` item
        (a real, classified branch or card) is someone else's heading entirely."""
        out = unblocked.render_table([_item(60 * DAY, state="unblocked")])
        self.assertNotIn(DUE, out)

    def test_flagging_never_moves_the_unowned_count(self):
        """Oracle 2: whatever gets flagged, `summarize`/`unblocked_stop.unowned_pairs` must
        still read it as never counted - a decision being due isn't the same as unowned."""
        items = [_item(90 * DAY)]
        self.assertEqual(unblocked.summarize(items), (0, 0))
        self.assertEqual(unblocked_stop.unowned_pairs(json.loads(unblocked.render_json(items))), [])


def _git(repo, *args, env=None):
    e = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
             GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t", **(env or {}))
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=e)


class Reproduce(unittest.TestCase):
    """A real 40-day-old unclassified branch, straight from `unblocked.branch_items`."""

    def test_old_real_branch_with_no_marker_or_name_is_flagged_not_unowned(self):
        capsule_home = Path(tempfile.mkdtemp())    # M1: never the live $CAPSULE_HOME
        repo = Path(tempfile.mkdtemp())
        _git(repo, "init", "-q", "-b", "main")
        (repo / "f.txt").write_text("1")
        _git(repo, "add", "f.txt")
        _git(repo, "commit", "-q", "-m", "init")
        _git(repo, "checkout", "-q", "-b", "owners-scratch-idea")
        (repo / "f.txt").write_text("2")
        old_date = f"{int(time.time()) - 40 * DAY} +0000"
        _git(repo, "commit", "-q", "-am", "change",
             env={"GIT_AUTHOR_DATE": old_date, "GIT_COMMITTER_DATE": old_date})
        items = unblocked.branch_items(repo, review_notes=None, now=time.time(),
                                        run_status_by_commit={},
                                        capsule_home=capsule_home)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item.state, "unclassified")             # oracle 1: state/owner
        self.assertFalse(item.unowned())
        self.assertEqual(unblocked.summarize(items), (0, 0))
        out = unblocked.render_table(items)                       # oracle 2: the fix itself
        self.assertIn(f"{item.item} (40d), {DUE}", out)


class IntentExamples(unittest.TestCase):
    """Z1: every `contracts/intent/unblocked_park.jsonl` example through the real
    `render_table` oracle its own document names."""

    def test_every_example_matches_its_oracle(self):
        lines = (paths.INTENT / "unblocked_park.jsonl").read_text().splitlines()
        self.assertTrue(lines)
        self.assertIn("unclassified-branch-with-unknown-age-is-never-flagged",
                      [json.loads(line)["id"] for line in lines])    # W96 follow-up example is shipped
        for line in lines:
            row = json.loads(line)
            doc = row["given"]["document"]
            age = doc["age_days"]    # `<empty>` = an unknown age (W96 follow-up decision)
            item = _item(None if age == "<empty>" else age * DAY, state=doc["state"])
            flagged = DUE in unblocked.render_table([item])
            self.assertEqual(flagged, row["expect"]["stale"], row["id"])
            self.assertEqual(item.unowned(), row["expect"]["unowned"], row["id"])


if __name__ == "__main__":
    unittest.main()
