# mutation-check: targets-predate-range ff625e94 -- fixture rename only, no behaviour pinned ::
#   Names.test_branch_and_worktree
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import epic_lane  # noqa: E402

PLAN = """# Thing Implementation Plan

**Spec:** `docs/superpowers/specs/2026-09-10-thing-design.md`

## Global Constraints

- stdlib only.

## File map

| File | Task |
|---|---|
| `a.py` | T1 |

---

### Task 1: the parser

**Files:** `a.py`, `tests/test_a.py`.

- [ ] **Step 1: Failing test** for the parser.
- [ ] **Step 2: Implement** it.

---

### Task 2 (FORK): the second thing

**Files:** `b.py`.

- [ ] **Step 1:** write it.
"""


class Flag(unittest.TestCase):
    def test_off_by_default_and_on_only_for_one(self):
        self.assertFalse(epic_lane.enabled({}))
        self.assertFalse(epic_lane.enabled({"OPS_EPIC_LANE": "0"}))
        self.assertFalse(epic_lane.enabled({"OPS_EPIC_LANE": "true"}))
        self.assertTrue(epic_lane.enabled({"OPS_EPIC_LANE": "1"}))

    def test_no_argument_reads_the_process_environment(self):
        old = os.environ.pop("OPS_EPIC_LANE", None)
        try:
            self.assertFalse(epic_lane.enabled())
            os.environ["OPS_EPIC_LANE"] = "1"
            self.assertTrue(epic_lane.enabled())
        finally:
            os.environ.pop("OPS_EPIC_LANE", None)
            if old is not None:
                os.environ["OPS_EPIC_LANE"] = old


class Names(unittest.TestCase):
    def test_branch_and_worktree(self):
        self.assertEqual(epic_lane.branch("pipeline-shape"), "epic/pipeline-shape")
        import board_bridge as bb
        from unittest import mock
        fixture = {"capsule-corp": "/home/dev/capsule-corp"}
        with mock.patch.dict(bb.REPO_PATHS, fixture):
            arg = epic_lane.worktree_arg("capsule-corp", "pipeline-shape")
        self.assertTrue(arg.startswith("worktree:"))
        self.assertTrue(arg.endswith("/.worktrees/epic-pipeline-shape"))
        self.assertIn("/capsule-corp", arg)

    def test_worktree_refuses_an_unknown_repo(self):
        with self.assertRaises(SystemExit):
            epic_lane.worktree_arg("not-a-repo", "x")

    def test_keys_are_unique_per_role_and_reuse_the_sprint_namespace(self):
        keys = [epic_lane.task_key("s", 1), epic_lane.task_key("s", 2),
                epic_lane.review_key("s", 1), epic_lane.review_key("s", 2),
                epic_lane.fix_key("s", 1)]
        self.assertEqual(len(set(keys)), len(keys))
        self.assertTrue(all(k.startswith("sprint:s:") for k in keys))
        self.assertEqual(epic_lane.task_key("s", 3), "sprint:s:task3")

    def test_bad_slug_and_bad_number_refuse(self):
        for bad in ("", "Up", "a/b", "-x", "x" * 90):
            with self.assertRaises(ValueError, msg=bad):
                epic_lane.task_key(bad, 1)
        for n in (0, -1, epic_lane.MAX_TASKS + 1):
            with self.assertRaises(ValueError, msg=str(n)):
                epic_lane.task_key("s", n)


class PlanParser(unittest.TestCase):
    def test_parses_every_task_in_order_with_its_steps(self):
        tasks = epic_lane.parse_plan(PLAN)
        self.assertEqual([t.n for t in tasks], [1, 2])
        self.assertEqual(tasks[0].title, "the parser")
        self.assertEqual(tasks[1].title, "the second thing")   # the "(FORK):" marker is not the title
        self.assertEqual(len(tasks[0].steps), 2)
        self.assertIn("**Files:** `a.py`", tasks[0].body)
        self.assertNotIn("### Task 2", tasks[0].body)
        self.assertNotIn("## Global Constraints", tasks[0].body)

    def test_a_plan_with_no_task_headings_raises(self):
        with self.assertRaises(epic_lane.PlanError):
            epic_lane.parse_plan("# Plan\n\nsome prose only\n")

    def test_one_task_is_not_an_epic(self):
        with self.assertRaises(epic_lane.PlanError):
            epic_lane.parse_plan("### Task 1: only one\n\n- [ ] do it\n")

    def test_non_contiguous_numbers_raise(self):
        text = "### Task 1: a\n\n- [ ] x\n\n### Task 3: b\n\n- [ ] y\n"
        with self.assertRaises(epic_lane.PlanError):
            epic_lane.parse_plan(text)

    def test_too_many_tasks_raise(self):
        text = "".join(f"### Task {i}: t{i}\n\n- [ ] x\n\n" for i in range(1, epic_lane.MAX_TASKS + 2))
        with self.assertRaises(epic_lane.PlanError):
            epic_lane.parse_plan(text)

    def test_a_heading_inside_a_fenced_block_is_not_a_task(self):
        text = (
            "### Task 1: the real first task\n\n"
            "- [ ] **Step 1:** do the real thing.\n\n"
            "Example plan shape:\n\n"
            "```\n"
            "### Task 2: fake\n\n"
            "- [ ] step\n"
            "```\n\n"
            "### Task 2: the real second task\n\n"
            "- [ ] **Step 1:** do it.\n"
        )
        tasks = epic_lane.parse_plan(text)
        self.assertEqual([t.n for t in tasks], [1, 2])
        self.assertEqual(tasks[0].title, "the real first task")
        self.assertEqual(tasks[1].title, "the real second task")
        self.assertEqual(tasks[0].steps, ["- [ ] **Step 1:** do the real thing."])
        self.assertNotIn("- [ ] step", tasks[0].steps)


class TickBack(unittest.TestCase):
    def test_ticks_only_the_named_tasks_steps_and_is_idempotent(self):
        once = epic_lane.tick(PLAN, 1)
        self.assertIn("- [x] **Step 1: Failing test**", once)
        self.assertIn("- [x] **Step 2: Implement**", once)
        self.assertIn("- [ ] **Step 1:** write it.", once)      # task 2 untouched
        self.assertIn("| `a.py` | T1 |", once)                  # the table is not a step
        self.assertEqual(epic_lane.tick(once, 1), once)

    def test_ticking_an_absent_task_changes_nothing(self):
        self.assertEqual(epic_lane.tick(PLAN, 9), PLAN)


class EpicMeta(unittest.TestCase):
    BODY = ("<!-- epic-meta\nlane: epic\nslug: thing\ntask: 3\ntasks: 6\n"
            "design: docs/superpowers/specs/x-design.md\n"
            "plan: docs/superpowers/plans/x.md\n-->\n\nbody\n")

    def test_reads_the_block_and_recognises_the_card(self):
        self.assertTrue(epic_lane.is_epic_card(self.BODY))
        meta = epic_lane.epic_meta(self.BODY)
        self.assertEqual(meta["slug"], "thing")
        self.assertEqual(meta["task"], "3")
        self.assertEqual(meta["tasks"], "6")

    def test_a_card_lane_body_is_not_an_epic_card(self):
        self.assertFalse(epic_lane.is_epic_card("<!-- sprint-meta\nrisk: low\n-->\n\nbody"))
        self.assertFalse(epic_lane.is_epic_card(""))
        self.assertFalse(epic_lane.is_epic_card(None))
        self.assertEqual(epic_lane.epic_meta(""), {})

    def test_a_quoted_epic_meta_in_prose_is_not_a_card(self):
        """The marker must be a line of its own near the top, the
        board.HELPER_MARKER rule: a spec that QUOTES the block must not make a
        card an epic card."""
        self.assertFalse(epic_lane.is_epic_card("see the `<!-- epic-meta` block for the shape"))


class RenderMeta(unittest.TestCase):
    """Hotfix 9: `area`/`risk` are optional and, when given, land in the
    `<!-- epic-meta -->` block so `epic_lane.epic_meta` (and quality_gate's
    `area:\\s*(\\w+)` scan of the whole body) can read them off a review/fix
    card, which has no `<!-- sprint-meta -->` block of its own."""

    def test_area_and_risk_absent_by_default_byte_identical_block(self):
        body = epic_lane.render_meta("thing", 0, 3, "d.md", "p.md")
        self.assertNotIn("area:", body)
        self.assertNotIn("risk:", body)
        meta = epic_lane.epic_meta(body)
        self.assertNotIn("area", meta)
        self.assertNotIn("risk", meta)

    def test_area_and_risk_present_when_passed(self):
        body = epic_lane.render_meta("thing", 0, 3, "d.md", "p.md", area="infra", risk="low")
        meta = epic_lane.epic_meta(body)
        self.assertEqual(meta["area"], "infra")
        self.assertEqual(meta["risk"], "low")
        self.assertEqual(meta["lane"], "epic")
        self.assertEqual(meta["slug"], "thing")

    def test_empty_string_area_is_omitted_not_emitted_blank(self):
        """Fix wave 1 (Opus review #2): `board_bridge` passes
        `spec.get("area", "")`, so an epic spec with no `area` must render NO
        `area:` line at all - a blank `area: \\n` line would make
        `area:\\s*(\\w+)` skip past it and capture the NEXT key
        (`risk: <value>`) instead, so a card with no area would wrongly parse
        as `area == "<the risk value>"`."""
        body = epic_lane.render_meta("thing", 0, 3, "d.md", "p.md", area="", risk="low")
        self.assertNotIn("area:", body)
        meta = epic_lane.epic_meta(body)
        self.assertNotIn("area", meta)
        self.assertEqual(meta["risk"], "low")

    def test_empty_string_risk_is_also_omitted(self):
        body = epic_lane.render_meta("thing", 0, 3, "d.md", "p.md", area="infra", risk="")
        self.assertNotIn("risk:", body)
        meta = epic_lane.epic_meta(body)
        self.assertNotIn("risk", meta)
        self.assertEqual(meta["area"], "infra")


class ReviewModel(unittest.TestCase):
    def test_default_is_fable_and_the_override_is_bounded(self):
        self.assertEqual(epic_lane.review_model({}), "claude-fable-5-1")
        self.assertEqual(epic_lane.review_model({"OPS_EPIC_REVIEW_MODEL": "claude-opus-5"}),
                         "claude-opus-5")
        self.assertEqual(epic_lane.review_model({"OPS_EPIC_REVIEW_MODEL": "claude-haiku-4.5"}),
                         "claude-fable-5-1")   # a judging seat is never Haiku
        self.assertEqual(epic_lane.review_model({"OPS_EPIC_REVIEW_MODEL": ""}),
                         "claude-fable-5-1")


if __name__ == "__main__":
    unittest.main()
