# mutation-check: targets-predate-range ff625e94 -- fixture rename only, no behaviour pinned ::
#   Acceptance.test_a_line_outside_the_acceptance_section_is_ignored,
#   Acceptance.test_a_line_with_no_command_is_named,
#   Acceptance.test_a_research_spec_needs_none,
#   Acceptance.test_a_spec_with_an_acceptance_line_lints_clean,
#   Acceptance.test_a_spec_without_one_lints_unchanged,
#   BetKey.test_a_good_bet_key_lists_nothing, Lint.test_bad_frontmatter
import json, os, sys, tempfile, unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from capsule.tests import tmpclean  # noqa: E402
tmpclean.install()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("AGENT_OPS_NO_SEND", "1")
import spec_lint
import board_bridge as bb


def setUpModule():
    # "my-app" must be a known repo; the product reads the configured REPO_PATHS.
    patcher = mock.patch.dict(bb.REPO_PATHS, {"my-app": "/home/example/my-app"})
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


GOOD = """## For future agent
Sample timer card, row 1.

## Goal
Timer page.

## Acceptance criteria
- [ ] Start button shows 24:59 after one second
- [ ] GET /api/presets returns the three presets

## Affected files
- server.py

## Test plan
`python3 -m unittest discover tests`

## Open questions
none
"""
FM = {"repo": "my-app", "risk": "low"}


class Lint(unittest.TestCase):
    def test_good_spec_is_clean(self):
        self.assertEqual(spec_lint.lint(GOOD, FM), [])

    def test_missing_sections(self):
        probs = spec_lint.lint("## Goal\nx\n", FM)
        self.assertIn("missing section: Acceptance criteria", probs); self.assertIn("missing section: Test plan", probs)

    def test_untestable_criterion(self):
        probs = spec_lint.lint(GOOD.replace("GET /api/presets returns the three presets", "The app feels fast"), FM)
        self.assertEqual(probs, ['not testable: "The app feels fast"'])

    def test_test_plan_needs_a_command(self):
        probs = spec_lint.lint(GOOD.replace("`python3 -m unittest discover tests`", "run the tests"), FM)
        self.assertEqual(probs, ["Test plan has no command"])

    def test_open_decision_block(self):
        probs = spec_lint.lint(GOOD + "\n## Decisions needed\nDECISION: Which db?\nWhy: w\n1. a - b\n2. c - d\nRecommend: 1 - r\nLearn: l\n", FM)
        self.assertEqual(probs, ["unanswered DECISION: Which db?"])

    def test_bad_frontmatter(self):
        self.assertIn("unknown repo: nope", spec_lint.lint(GOOD, {"repo": "nope", "risk": "low"}))
        probs = spec_lint.lint(GOOD, {"repo": "my-app", "risk": "urgent"})
        self.assertIn("risk must be low/med/high", probs)

    def test_summary(self):
        self.assertEqual(spec_lint.summary([]), "ok")
        self.assertEqual(spec_lint.summary(["a", "b", "c"]), "3 problems: a; b")

    def test_stemmed_criterion_passes(self):
        # fix round 1: past-participle / 3rd-person forms of an OBSERVABLE
        # word (e.g. "created" for "creates") must count as testable too.
        probs = spec_lint.lint(GOOD.replace(
            "GET /api/presets returns the three presets",
            "Findings note created at Specs/one.md"), FM)
        self.assertEqual(probs, [])

    def test_research_kind_skips_testability_rule(self):
        fm = dict(FM, kind="research")
        body = GOOD.replace("GET /api/presets returns the three presets", "The app feels fast")
        self.assertEqual(spec_lint.lint(body, fm), [])

    def test_placeholder_section_is_reported(self):
        body = GOOD.replace("Timer page.", "<one sentence: what is true when this is done>")
        probs = spec_lint.lint(body, FM)
        self.assertIn("placeholder section: Goal", probs)
        self.assertNotIn("missing section: Goal", probs)

    def test_real_spec_still_passes(self):
        self.assertEqual(spec_lint.lint(GOOD, FM), [])

    def test_placeholder_checkbox_acceptance_criteria_is_reported(self):
        body = GOOD.replace(
            "- [ ] Start button shows 24:59 after one second\n"
            "- [ ] GET /api/presets returns the three presets",
            "- [ ] <verifiable outcome>")
        probs = spec_lint.lint(body, FM)
        self.assertIn("placeholder section: Acceptance criteria", probs)
        self.assertNotIn("missing section: Acceptance criteria", probs)

    def test_empty_section_is_still_missing_not_placeholder(self):
        body = GOOD.replace("Timer page.", "")
        probs = spec_lint.lint(body, FM)
        self.assertIn("missing section: Goal", probs)
        self.assertNotIn("placeholder section: Goal", probs)

    def test_partial_placeholder_is_not_reported(self):
        body = GOOD.replace(
            "Timer page.",
            "<one sentence: what is true when this is done>\nTimer page really works.")
        probs = spec_lint.lint(body, FM)
        self.assertEqual([p for p in probs if "Goal" in p], [])


class Acceptance(unittest.TestCase):
    BODY = ("## For future agent\n\nTick sound card, row 1.\n\n"
            "## Goal\n\nMake the tick audible.\n\n"
            "## Acceptance criteria\n\n- [ ] the page plays tick.wav\n"
            "acceptance: python3 -m unittest tests.test_tick\n\n"
            "## Test plan\n\n`python3 -m unittest discover tests`\n")

    def test_a_spec_with_an_acceptance_line_lints_clean(self):
        probs = spec_lint.lint(self.BODY, {"repo": "my-app", "risk": "low", "kind": "code"})
        self.assertEqual([p for p in probs if "acceptance" in p], [])

    def test_a_spec_without_one_lints_unchanged(self):
        """Fix round 2: `summary()` goes to the phone in the Spec Ready message,
        so a spec that predates the rule must lint EXACTLY as it did before this
        branch - no `missing acceptance: line`. Requiring the block is C5's scope."""
        body = self.BODY.replace("acceptance: python3 -m unittest tests.test_tick\n", "")
        probs = spec_lint.lint(body, {"repo": "my-app", "risk": "low", "kind": "code"})
        # the pre-branch verdict on this fixture, unchanged: one untestable
        # criterion ("plays" is not an OBSERVABLE word) and nothing else.
        self.assertEqual(probs, ['not testable: "the page plays tick.wav"'])

    def test_a_research_spec_needs_none(self):
        body = self.BODY.replace("acceptance: python3 -m unittest tests.test_tick\n", "")
        probs = spec_lint.lint(body, {"repo": "my-app", "risk": "low", "kind": "research"})
        self.assertNotIn("missing acceptance: line", probs)

    def test_a_line_with_no_command_is_named(self):
        body = self.BODY.replace("python3 -m unittest tests.test_tick", "it should feel nice")
        probs = spec_lint.lint(body, {"repo": "my-app", "risk": "low", "kind": "code"})
        self.assertTrue(any(p.startswith("acceptance:") and "not a command" in p
                            for p in probs), probs)

    def test_a_line_outside_the_acceptance_section_is_ignored(self):
        """Review I5: the grammar (spec 7.5) scopes `acceptance:` to the
        `## Acceptance criteria` section. Prose elsewhere that happens to
        start with the word must not add a problem to the `Lint:` fact that
        rides the Spec Ready message to the phone - nor a gate advisory."""
        body = self.BODY + "acceptance: the reviewer agrees this is done\n"
        probs = spec_lint.lint(body, {"repo": "my-app", "risk": "low", "kind": "code"})
        self.assertEqual([p for p in probs if p.startswith("acceptance:")], [])

    def test_commands_are_extracted_in_order(self):
        body = self.BODY + "acceptance: ruff check .\n"
        self.assertEqual(spec_lint.acceptance_commands(body),
                         ["python3 -m unittest tests.test_tick", "ruff check ."])


class ForFutureAgent(unittest.TestCase):
    """Row 128, M076. The template had 0 occurrences and 20 of 22 Specs/ notes
    lacked the section, so the template was guaranteeing the defect: the
    planner is told to follow it EXACTLY (sprint-plan.md:49-50)."""

    FM = {"repo": "my-app", "risk": "low", "kind": "code"}

    def test_a_spec_without_the_preamble_is_listed(self):
        body = "## Goal\n\nx creates y\n\n## Acceptance criteria\n\n- [ ] it returns 200\n\n## Test plan\n\nrun it\n"
        self.assertTrue(any("For future agent" in p for p in spec_lint.lint(body, self.FM)))

    def test_a_spec_with_the_preamble_is_not_listed(self):
        body = ("## For future agent\n\nWhat this card is for.\n\n## Goal\n\nx creates y\n\n"
                "## Acceptance criteria\n\n- [ ] it returns 200\n\n## Test plan\n\nrun it\n")
        self.assertFalse(any("For future agent" in p for p in spec_lint.lint(body, self.FM)))

    def test_an_empty_preamble_heading_does_not_satisfy_it(self):
        body = ("## For future agent\n\n## Goal\n\nx creates y\n\n"
                "## Acceptance criteria\n\n- [ ] it returns 200\n\n## Test plan\n\nrun it\n")
        self.assertTrue(any("For future agent" in p for p in spec_lint.lint(body, self.FM)))

    def test_the_lint_still_never_blocks(self):
        self.assertIsInstance(spec_lint.lint("", self.FM), list)


class Template(unittest.TestCase):
    def test_the_vault_template_carries_the_section(self):
        import os
        env = os.environ.get("OBSIDIAN_VAULT_PATH")
        if not env:
            self.skipTest("OBSIDIAN_VAULT_PATH unset - the suite never reads the real vault")
        t = Path(env) / "Specs" / "_TEMPLATE.md"
        if not t.is_file():
            self.skipTest("template not present")
        self.assertIn("## For future agent", t.read_text(encoding="utf-8"))


class LaneField(unittest.TestCase):
    BODY = ("## For future agent\nwhy this exists\n\n## Goal\ng\n\n"
            "## Acceptance criteria\n- [ ] the page shows the count\n\n"
            "## Test plan\n`python3 -m unittest discover maintenance/tests`\n")

    def _fm(self, **kw):
        fm = {"repo": "capsule-corp", "risk": "med", "title": "t"}
        fm.update(kw)
        return fm

    def _plan(self, tasks=3):
        d = Path(tempfile.mkdtemp())
        (d / "docs" / "superpowers" / "plans").mkdir(parents=True)
        p = d / "docs" / "superpowers" / "plans" / "x.md"
        p.write_text("".join(f"### Task {i}: t{i}\n\n- [ ] do it\n\n" for i in range(1, tasks + 1)),
                     encoding="utf-8")
        return d, "docs/superpowers/plans/x.md"

    def test_no_lane_is_clean_and_means_card(self):
        self.assertEqual(spec_lint.lint(self.BODY, self._fm()), [])

    def test_a_bad_lane_is_listed_not_raised(self):
        probs = spec_lint.lint(self.BODY, self._fm(lane="sprint"))
        self.assertIn("lane must be one of ['card', 'epic']", probs)

    def test_epic_without_a_plan_is_listed(self):
        probs = spec_lint.lint(self.BODY, self._fm(lane="epic"))
        self.assertIn("lane: epic needs a plan: path", probs)

    def test_epic_with_a_missing_plan_file_is_listed(self):
        probs = spec_lint.lint(self.BODY, self._fm(lane="epic", plan="docs/superpowers/plans/nope.md"))
        self.assertTrue(any(p.startswith("plan not found:") for p in probs), probs)

    def test_epic_with_a_good_plan_is_clean(self):
        root, rel = self._plan()
        with mock.patch.dict(bb.REPO_PATHS, {"capsule-corp": str(root)}):
            self.assertEqual(spec_lint.lint(self.BODY, self._fm(lane="epic", plan=rel)), [])

    def test_epic_whose_plan_has_one_task_is_listed(self):
        root, rel = self._plan(tasks=1)
        with mock.patch.dict(bb.REPO_PATHS, {"capsule-corp": str(root)}):
            probs = spec_lint.lint(self.BODY, self._fm(lane="epic", plan=rel))
            self.assertTrue(any("plan has 1 tasks" in p for p in probs), probs)

    def test_a_plan_path_may_not_escape_the_repo(self):
        root, _ = self._plan()
        with mock.patch.dict(bb.REPO_PATHS, {"capsule-corp": str(root)}):
            probs = spec_lint.lint(self.BODY, self._fm(lane="epic", plan="../../etc/passwd"))
            self.assertTrue(any(p.startswith("plan path escapes the repo:") for p in probs), probs)

    def test_the_lint_still_never_blocks(self):
        """spec_lint.py:1-2 is the contract: it lists, the planner fixes."""
        probs = spec_lint.lint(self.BODY, self._fm(lane="epic"))
        self.assertTrue(probs)
        self.assertEqual(spec_lint.summary(probs)[:1], "1")   # a count, not an exception


class BetKey(unittest.TestCase):
    """Row 139: the `bet:` frontmatter key is OPTIONAL now, so an absent key
    must add nothing to the `Lint:` fact on the Spec Ready message - every
    spec written before this rule would otherwise gain a problem for free.
    spec_lint never blocks; a listed problem is what the planner fixes."""

    BODY = ("## For future agent\n\nWhy.\n\n## Goal\n\nG.\n\n"
            "## Acceptance criteria\n\n- [ ] the page renders\n\n"
            "## Test plan\n\n`python3 -m unittest discover maintenance/tests`\n")
    FM = {"title": "T", "repo": "my-app", "risk": "low"}

    def test_an_absent_bet_key_lists_nothing(self):
        probs = spec_lint.lint(self.BODY, dict(self.FM))
        self.assertEqual([p for p in probs if "bet" in p], [])

    def test_a_good_bet_key_lists_nothing(self):
        probs = spec_lint.lint(self.BODY, dict(self.FM, bet="my-app-menu-bar"))
        self.assertEqual([p for p in probs if "bet" in p], [])

    def test_a_malformed_bet_key_is_listed_once(self):
        probs = spec_lint.lint(self.BODY, dict(self.FM, bet="Not A Slug"))
        hits = [p for p in probs if "bet" in p]
        self.assertEqual(hits, ['bet: "Not A Slug" is not a slug'])

    def test_an_empty_bet_key_is_listed(self):
        """`bet:` with nothing after it is a typo, not an absent key."""
        probs = spec_lint.lint(self.BODY, dict(self.FM, bet=""))
        self.assertEqual([p for p in probs if "bet" in p],
                         ['bet: "" is not a slug'])

    def test_a_research_spec_is_checked_too(self):
        """The bet field is provenance, not a code-only rule."""
        probs = spec_lint.lint(self.BODY, dict(self.FM, kind="research",
                                               bet="Not A Slug"))
        self.assertIn('bet: "Not A Slug" is not a slug', probs)

    def test_bet_slug_is_the_one_reader(self):
        self.assertEqual(spec_lint.bet_slug({"bet": " x-y "}), "x-y")
        self.assertEqual(spec_lint.bet_slug({}), "")
        self.assertEqual(spec_lint.bet_slug({"bet": None}), "")
