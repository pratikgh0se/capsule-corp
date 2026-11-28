"""BLD-20 false positives (triage 2026-10-27): each case is a minimal fixture the loophole hunt
used to flag wrongly, plus the real weaknesses it must still flag."""
import unittest

from capsule import loophole


def row(id_, given, expect, inputs=None):
    g = {"document": given, **({"inputs": inputs} if inputs else {})}
    return {"id": id_, "given": g, "expect": expect, "source": "row 602"}


POS = row("gate-merges-clean-diff", {"check": "gate", "verdict": "q"}, {"merged": True})
NEG = row("gate-blocks-failing-diff", {"check": "gate", "verdict": "z"},
          {"merged": False, "state": "x"})


def checks(rows):
    return {f["check"] for f in loophole._hunt(list(enumerate(rows, 1)))}


def line_checks(rows):
    return [(f["line"], f["check"]) for f in loophole._hunt(list(enumerate(rows, 1)))]


class BanRows(unittest.TestCase):
    def test_a_ban_on_the_given_value_is_not_a_noop(self):
        for given, expect in (
                ({"amendment": "<non-empty>"}, {"amendment": "<empty>"}),
                ({"rows": [999]}, {"rows": "<empty>"}),
                ({"closure": "watch"}, {"closure": {"<not>": "watch"}})):
            ban = row("ban", {"schema": "x.v1", **given}, expect)
            self.assertNotIn("noop-satisfies-expect", checks([POS, NEG, ban]), expect)

    def test_a_ban_row_counts_as_the_negative_case(self):
        ban = row("ban", {"schema": "x.v1", "closure": "watch"}, {"closure": {"<not>": "watch"}})
        self.assertNotIn("no-negative-case", checks([ban]))

    def test_a_standalone_transform_row_still_gets_noop(self):
        # no `schema`: given is the input and expect the output, so this is not a ban
        park = row("park-clean", {"check": "park", "owner": "live-run"}, {"owner": "<empty>"})
        self.assertIn("noop-satisfies-expect", checks([POS, NEG, park]))

    def test_a_free_text_path_the_schema_walk_cannot_follow_fails_closed(self):
        ban = row("nested", {"schema": "postmortem.v1", "a": {"b": "x"}},
                  {"a": {"b": {"<not>": "x"}}})
        self.assertIn("literal-ban-on-free-text", checks([ban]))

    def test_a_do_nothing_expect_is_still_a_noop(self):
        noop = row("noop", {"check": "gate"}, {"findings": "<empty>"})
        self.assertIn("noop-satisfies-expect", checks([POS, NEG, noop]))

    def test_a_literal_ban_on_a_free_text_field_is_still_a_finding(self):
        ban = row("bare-review", {"schema": "postmortem.v1", "enforcer": "review"},
                  {"enforcer": {"<not>": "review"}})
        self.assertIn("literal-ban-on-free-text", checks([ban]))

    def test_a_literal_ban_on_an_enum_is_not(self):
        ban = row("low", {"schema": "postmortem.v1", "confidence": "low"},
                  {"confidence": {"<not>": "low"}})
        self.assertNotIn("literal-ban-on-free-text", checks([ban]))

    def test_a_ban_that_holds_only_for_an_empty_input_is_a_finding(self):
        ban = row("empty-bundle", {"schema": "x.v1", "rows": "<non-empty>"}, {"rows": "<empty>"},
                  inputs={"rows": "<empty>"})
        self.assertIn("ban-covers-one-input", checks([ban]))

    def test_a_ban_with_a_non_empty_input_is_not_one_input_only(self):
        ban = row("full-bundle", {"schema": "x.v1", "rows": "<non-empty>"}, {"rows": "<empty>"},
                  inputs={"rows": [417]})
        self.assertNotIn("ban-covers-one-input", checks([ban]))

    def test_an_empty_input_unrelated_to_the_banned_path_is_not_one_input_only(self):
        ban = row("other-input", {"schema": "x.v1", "rows": "<non-empty>"}, {"rows": "<empty>"},
                  inputs={"incident": "<empty>"})
        self.assertNotIn("ban-covers-one-input", checks([ban]))


class ContrastPairs(unittest.TestCase):
    def test_a_row_paired_with_one_asking_for_the_value_is_not_a_noop(self):
        has = row("gap-needs-proposal", {"check": "g", "coverage": "gap"},
                  {"proposals": "<non-empty>"})
        none = row("full-needs-none", {"check": "g", "coverage": "full"}, {"proposals": "<empty>"})
        self.assertNotIn("noop-satisfies-expect", checks([has, none]))
        self.assertIn("noop-satisfies-expect", checks([none, POS]))


class KeyOrder(unittest.TestCase):
    def test_rows_that_differ_only_in_key_order_are_not_conflicting(self):
        a = row("verdict-last", {"doc": {"x": 1, "verdict": "fail"}}, {"problems": 1})
        b = row("verdict-first", {"doc": {"verdict": "fail", "x": 1}}, {"problems": 0})
        self.assertNotIn("conflicting-expect", checks([a, b]))

    def test_same_given_in_the_same_order_still_conflicts(self):
        a = row("one", {"doc": {"x": 1}}, {"problems": 1})
        b = row("two", {"doc": {"x": 1}}, {"problems": 0})
        self.assertIn("conflicting-expect", checks([a, b]))


class NegativeCase(unittest.TestCase):
    def accepts(self, expect, given=None):
        lone = row("only-row", given or {"check": "c"}, expect)
        return "no-negative-case" not in checks([lone])

    def test_each_blind_spot_is_a_negative_case(self):
        self.assertTrue(self.accepts({"refused": True, "lines": 3}))
        self.assertTrue(self.accepts({"problems": 1}))
        self.assertTrue(self.accepts({"flagged": ["a"]}, {"check": "c", "paths": ["a", "b"]}))
        self.assertTrue(self.accepts({"items": {"<each>": {"removes": {"<not>": "none"}}}}))

    def test_a_non_zero_count_or_a_subset_under_another_key_is_not(self):
        self.assertFalse(self.accepts({"count": 3}))
        self.assertFalse(self.accepts({"paths": ["a"]}, {"check": "c", "paths": ["a", "b"]}))

    def test_the_positive_forms_are_not(self):
        self.assertFalse(self.accepts({"problems": 0}))
        self.assertFalse(self.accepts({"flagged": ["a", "b"]}, {"check": "c", "paths": ["a", "b"]}))


class RealPatternsStillFire(unittest.TestCase):
    def test_a_classifier_file_with_only_accepting_rows_is_still_no_negative_case(self):
        rows = [row(f"class-{n}", {"verb": "answer", "n": n}, {"class": "text", "key": "answer: x"})
                for n in range(3)]
        self.assertIn("no-negative-case", checks(rows))

    def test_the_same_given_and_expect_is_still_a_duplicate(self):
        a = row("clean-done", {"status": "done", "left": "<empty>"}, {"verdict": "review"})
        b = row("lock-note", {"status": "done", "left": "<empty>"}, {"verdict": "review"})
        self.assertIn("duplicate-content", checks([POS, NEG, a, b]))

    def test_a_ban_on_the_empty_bundle_is_still_flagged_as_covering_one_input(self):
        ban = row("rows-beyond-bundle", {"schema": "x.v1", "verdict": "drifted", "rows": [999]},
                  {"rows": {"<not>": [999]}}, inputs={"rows": "<empty>"})
        self.assertIn("ban-covers-one-input", checks([POS, NEG, ban]))


class LabelOnly(unittest.TestCase):
    def test_all_prose_expects_are_a_hint_not_a_finding(self):
        rows = [row(f"case-{n}", {"check": "heal", "n": n},
                    {"pass": "the logout path pauses once and leaves the guard alone"})
                for n in range(2)]
        found = loophole._hunt(list(enumerate(rows, 1)))
        self.assertEqual([(f["check"], f["severity"]) for f in found],
                         [("label-only-expect", "hint")])


class LabelOnlyIsNarrow(unittest.TestCase):
    def test_a_prose_renderer_is_still_no_negative_case(self):
        rows = [row(f"render-{n}", {"check": "render", "n": n},
                    {"text": "the rendered line reads like this sentence"}) for n in range(2)]
        self.assertIn("no-negative-case", checks(rows))


class NotAnIntentFile(unittest.TestCase):
    def test_situation_rows_are_one_hint_not_malformed_findings(self):
        rows = [{"id": f"r{n}", "situation": "s", "expect": "exit 0", "source": "spec"}
                for n in range(3)]
        found = loophole._hunt(list(enumerate(rows, 1)))
        self.assertEqual([(f["check"], f["severity"]) for f in found],
                         [("not-an-intent-file", "hint")])

    def test_a_malformed_row_in_a_real_intent_file_is_still_a_finding(self):
        self.assertIn("malformed-row", checks([POS, NEG, {"id": "bad"}]))
        self.assertIn("malformed-row", checks([{"id": "bad"}]))


if __name__ == "__main__":
    unittest.main()
