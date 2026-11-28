"""Factory autonomy Task 1 (spec 2.2, 2.6): scope-neutral auto-amend decisions.
Oracle 1: the per-hunk rules. Oracle 2: autoamend.structure(), a separate walker, compared
before/after (Z2). Intent examples: contracts/intent/auto_amend.jsonl (Z1), run for real below.
The texts are the real sample-app plan Task 7 (main ea24f89 and its parent) and card t_de1466d6."""
import json
import unittest
from unittest import mock

from capsule import autoamend, intent, paths

CARD = ("Do ONLY this task. The branch is shared with the epic's other tasks, so a change the task does not"
        " ask for is a finding at the whole-branch review. Call kanban_request_review when the task is done:"
        " the quality gate runs per task, exactly as it does for a card.\n")
TASK7 = ("### Task 7: session complete (`/complete`) and the form-crossed variant\n\n"
         "- Files: create `static/complete.html`, `static/sr/complete.js`; `server.py` (`PAGES[\"/complete\"]`);"
         " `static/sr/timer.js`; `tests/ui/screens.py`; create `tests/ui/test_complete.py`; `README.md`;"
         " `CHANGELOG.md`. Card carries `needs_ui`.\n"
         "- Interfaces: transcription of `popover-finished.html` and `popover-transformed.html` into one body with"
         " `?form=1` selecting the variant.\n"
         "- [ ] Tests first, `tests/ui/test_complete.py` (`ui_guard`, `allow=()`): the headline is all caps.\n"
         "- [ ] Per row 335 (epic-level pixel contract): the `complete-*` rows stay `live=False` and"
         " `EXPECTED_SKIPS` stays 14; the golden-rows check is deferred.\n"
         "- [ ] Implement. `complete.js` reads `bean.minutes` from `/api/rules`; no threshold literal.\n\n"
         "```python\nassert skips == 14\n```\n")
DONE = "## Done when\n\nAll tests pass.\n\nThe screen renders in both themes.\n\n## Notes\n\nFree text here.\n"


def hunks(*triples):
    return autoamend.parse_hunks([{"target": t, "find": f, "replace": r} for t, f, r in triples])


def refused(text, *triples, **kw):
    return autoamend.check_scope(text, hunks(*triples), **kw)["refused"]


class ParseAndFingerprint(unittest.TestCase):
    def test_shape_errors_raise(self):
        for bad in ([], [{"target": "card", "find": "a"}], [{"target": "x", "find": "a", "replace": "b"}],
                    [{"target": "card", "find": "", "replace": "b"}],
                    [{"target": "card", "find": "a", "replace": "b", "extra": 1}],
                    [{"target": "card", "find": "a", "replace": "b"}] * 4):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                autoamend.parse_hunks(bad)

    def test_parse_hunks_refuses_find_equal_to_replace(self):
        with self.assertRaises(ValueError):
            autoamend.parse_hunks([{"target": "card", "find": "a", "replace": "a"}])

    def test_fingerprint_is_context_free_and_symmetric(self):
        fp = lambda t, f, r: autoamend.fingerprint(hunks((t, f, r))[0])
        self.assertEqual(fp("card", "a change the task", "a change this task"), fp("card", "the task", "this task"))
        self.assertEqual(fp("card", "the task", "this task"), fp("card", "this task", "the task"))    # flip-flop
        self.assertNotEqual(fp("card", "the task", "this task"), fp("spec_section", "the task", "this task"))
        self.assertEqual(len(fp("card", "a", "b")), 16)


class Scope(unittest.TestCase):
    def test_a_wording_change_on_a_prose_line_is_scope_neutral(self):
        out = autoamend.check_scope(CARD, hunks(("card", "a change the task does", "a change this task does")))
        self.assertTrue(out["ok"], out["refused"])
        self.assertEqual(out["lines_changed"], 2)

    def test_real_list_labels_are_structural(self):
        self.assertIn("structural", refused(TASK7, ("spec_section", "`tests/ui/screens.py`; ", "")))
        self.assertIn("structural", refused(TASK7, ("spec_section", "into one body with", "into two bodies with")))
        self.assertIn("structural", refused(TASK7, ("spec_section", "the headline is all caps",
                                                    "the headline is title case")))

    def test_any_numeric_change_is_refused(self):
        self.assertEqual(refused(TASK7, ("spec_section", "`EXPECTED_SKIPS` stays 14", "`EXPECTED_SKIPS` stays 15")),
                         ["numeric", "structure-drift"])

    def test_rewriting_an_existing_amended_line_is_refused_appending_a_new_one_is_applied(self):
        """D5: provenance is append-only - a hunk that finds and rewrites
        an EXISTING "Amended per row N (date)." line is refused, with both reasons, even when the
        new citation stays inside the verdict's own `rows` (never edit an existing one); a hunk that
        leaves every existing Amended line untouched and only appends a brand-new one is applied."""
        text = "Body.\n\nAmended per row 452 (2026-09-26).\n"
        self.assertEqual(refused(text, ("card", "Amended per row 452 (2026-09-26).",
                                        "Amended per row 452 (2026-09-27)."), rows=(452,)),
                         ["numeric", "structure-drift"])
        self.assertEqual(refused(text, ("card", "Body.\n", "Body.\nAmended per row 485 (2026-09-27).\n"),
                                  rows=(485,)), [])

    def test_only_a_row_citation_number_the_hunk_adds_from_the_verdicts_rows_is_exempt(self):
        """D5: a citation number is provenance only when it was already cited in `find` or is one of the
        verdict's `rows`; a whole "Amended per row N (date)" line only when every N is in `rows`."""
        self.assertEqual(refused(TASK7, ("spec_section", "Per row 335 (epic-level", "Per row 485 (epic-level"),
                                  rows=(485,)), [])
        self.assertEqual(refused("Follows rows 452 and 481.\n", ("card", "rows 452 and 481", "rows 452, 481 and 485"),
                                  rows=(485,)), [])
        amended = "Body.\n\nAmended per row 452 (2026-09-26).\n"
        # D5 (Task 1 review carry-over): growing an existing Amended line in place is a rewrite of
        # it, never exempt - provenance is append-only, see test_rewriting_an_existing_amended_line...
        grown = ("card", "Amended per row 452 (2026-09-26).", "Amended per row 452 and row 485 (2026-09-27).")
        self.assertEqual(refused(amended, grown, rows=(452, 485)), ["numeric", "structure-drift"])
        both = ["numeric", "structure-drift"]
        for text, triple, rows in [
                (amended, grown, (485,)),                                        # 452 is not a verdict row
                (TASK7, ("spec_section", "`EXPECTED_SKIPS` stays 14; the golden", "`EXPECTED_SKIPS` stays 15; the golden"),
                 (485,)),
                (amended, ("card", "Amended per row 452 (2026-09-26).",          # P1: text past the date
                           "Amended per row 452 (2026-09-26); the timer runs 30 minutes."), ()),
                ("Body.\n", ("card", "Body.\n", "Body.\nAmended per row 485: the break is 10 minutes.\n"), ()),  # P2
                ("The table keeps row 3 of the golden set.\n", ("card", "keeps row 3 of", "keeps row 4 of"), ()),
                (amended, ("card", "per row 452 (2026", "per row 999 (2026"), ()),
                ("Body.\n", ("card", "Body.\n", "Body.\nAmended per row 999.\n"), ()),
                ("The grid has row 3 and row 4.\nThe test keeps row 4 green.\n", ("card", "row 4 green", "row 3 green"), ()),
                ("Golden rows 3 and 4 exist. Only row 4 stays live.\n", ("card", "Only row 4", "Only row 3"), ()),
                ("Amended per row 485 (break 10).\n",                    # row 485 masters, "10" does not (P1)
                 ("card", "Amended per row 485 (break 10).", "Amended per row 485 (break 15)."), (485,))]:
            with self.subTest(replace=triple[2]):
                self.assertEqual(refused(text, triple, rows=rows), both)

    def test_spelled_out_and_ordinal_numbers_are_refused_too(self):
        self.assertIn("numeric", refused("The break runs twice today.\n", ("card", "runs twice today",
                                                                          "runs three times today")))
        self.assertEqual(refused("The second break is long.\n", ("card", "second break", "third break")),
                         ["numeric", "structure-drift"])

    def test_wider_number_words_are_refused_too(self):
        """NUMBER_WORDS covers tens (thirty), teens ordinals (eleventh) and scale words
        (thousand/million), on oracle 1's OWN list, never shared with oracle 2."""
        self.assertEqual(refused("The task takes thirty minutes.\n",
                                  ("card", "thirty minutes", "forty minutes")),
                         ["numeric", "structure-drift"])
        self.assertEqual(refused("This is the eleventh attempt.\n",
                                  ("card", "eleventh attempt", "twelfth attempt")),
                         ["numeric", "structure-drift"])
        self.assertEqual(refused("We ordered a thousand more.\n",
                                  ("card", "a thousand more", "a million more")),
                         ["numeric", "structure-drift"])

    def test_hyphenated_number_words_are_refused_by_both_oracles(self):
        """Oracle 2's number check uses a different method from oracle 1's word list: it splits
        on `-` too, so a hyphenated compound is caught even though the whole hyphenated word is
        in neither list."""
        self.assertEqual(refused("The break runs thirty-five minutes.\n",
                                  ("card", "thirty-five minutes", "forty-five minutes")),
                         ["numeric", "structure-drift"])
        self.assertEqual(refused("This is the twenty-first attempt.\n",
                                  ("card", "twenty-first attempt", "twenty-second attempt")),
                         ["numeric", "structure-drift"])

    def test_the_suffix_rule_does_not_flag_ordinary_words(self):
        """The "-ty"/"-teen"/"-th"/"-ieth" suffix rule requires the stem left over to be a real
        number root, so ordinary English words (with, path, both, quality, month, ...) are not
        treated as numbers and the edit applies clean."""
        self.assertEqual(refused("The task does for the epic.\n", ("card", "does for", "does with")), [])
        self.assertEqual(refused("Run the script now.\n", ("card", "the script", "the path")), [])
        self.assertEqual(refused("Only this task applies.\n", ("card", "Only this task", "Only both tasks")), [])

    def test_a_number_the_word_list_misses_is_still_caught_by_the_stem_and_suffix_rule(self):
        """The stem+suffix path catches a number word that is in neither exact word list:
        "millionth"/"billionth" are reached only by stripping "-th" down to a real root
        ("million"/"billion", both number words); oracle 1 has no such fallback."""
        out = autoamend.check_scope("This is the millionth request.\n",
                                     hunks(("card", "the millionth request", "the billionth request")))
        self.assertEqual(out["refused"], ["structure-drift"])
        # and oracle 2 alone, independent of oracle 1's own (unrelated) list, still catches
        # "fortieth"->"fiftieth" even with oracle 1's list emptied
        with mock.patch.object(autoamend, "NUMBER_WORDS", set()):
            out2 = autoamend.check_scope("This is the fortieth attempt.\n",
                                         hunks(("card", "the fortieth attempt", "the fiftieth attempt")))
        self.assertEqual(out2["refused"], ["structure-drift"])

    def test_a_citation_group_ends_at_a_sentence_boundary(self):
        """Oracle 2's citation scan: 'Also row 5. Row 7 too.' must not merge into one group
        `(5, 7)` across the full stop, or a swap for an already-cited number would read as a
        bare drop and pass. A `.`/`;` ends a group (a newline already does, since this runs per
        line)."""
        text = "Also row 5. Row 7 too.\n"
        self.assertEqual(refused(text, ("card", "row 5", "row 7")), ["numeric", "structure-drift"])

    def test_a_citation_number_may_never_be_swapped_for_another_even_if_both_are_cited_elsewhere(self):
        """D5: a hunk may keep, drop, or add-from-`rows` a citation number, but never change an
        existing one for a DIFFERENT number just because that number happens to be cited somewhere
        else in the text - refuse a row 3<->row 4 swap even with no `rows` restriction."""
        text = "The audit keeps row 3 of the golden set; row 4 is deferred.\n"
        self.assertEqual(refused(text, ("card", "row 3 of the golden set; row 4 is",
                                        "row 4 of the golden set; row 3 is")),
                         ["numeric", "structure-drift"])

    def test_citations_are_aligned_by_content_not_position(self):
        """Citations are aligned by content (`difflib.SequenceMatcher`), not by index, so a drop
        or add elsewhere in the text does not misalign the citations after it. Each of these is
        allowed by spec S:114 ("may keep or drop ... may add only a verdict row") and applies
        clean."""
        # drop the middle citation of three: the other two are untouched and stay aligned
        self.assertEqual(refused("Row 3 stays. Also row 5. Row 7 stays too.\n",
                                  ("card", " Also row 5.", "")), [])
        # drop the first citation of two: the second must not be forced to "become" the first
        self.assertEqual(refused("Applies to row 3. Applies to row 4.\n",
                                  ("card", "Applies to row 3. ", "")), [])
        # add a verdict row EARLY in the doc, ahead of an unrelated later citation
        out = autoamend.check_scope("Intro text. Row 335 stays.\n",
                                     hunks(("card", "Intro text.", "Intro text (row 485).")), rows=(485,))
        self.assertEqual(out["refused"], [])
        # the same add, inside one hunk, ahead of the hunk's own existing citation
        out2 = autoamend.check_scope(TASK7, hunks(("spec_section", "Per row 335 (epic-level",
                                                    "(row 485) Per row 335 (epic-level")), rows=(485,))
        self.assertEqual(out2["refused"], [])

    def test_too_long_mixed_targets_and_hunk_count_are_refused(self):
        long_replace = "Notes. " + "banana " * 100
        self.assertEqual(refused("Notes.\n", ("card", "Notes.", long_replace)), ["too-long"])

        text = "Line one is here.\nLine two follows after.\n"
        out = autoamend.check_scope(text, hunks(("card", "Line one is here.", "Line one is fixed."),
                                                  ("spec_section", "Line two follows after.",
                                                   "Line two follows later.")))
        self.assertIn("mixed-targets", out["refused"])

        # hunk-count can't be reached through parse_hunks (it already enforces 1-3); call
        # check_scope directly with a shape parse_hunks would refuse first.
        self.assertIn("hunk-count", autoamend.check_scope("text\n", [])["refused"])

    def test_both_oracles_agree_on_the_allowed_and_the_refused_row_citation(self):
        """Z2: oracle 1's masked token diff and oracle 2's `structure_drift` reach the same verdict
        without sharing code or computed data; both read only the verdict's `rows`."""
        find, replace = "Per row 335 (epic-level", "Per row 485 (epic-level"
        after = TASK7.replace(find, replace)
        m_removed, m_added = autoamend.token_change(*autoamend.provenance_masked(find, replace, (485,)))
        self.assertEqual(m_removed + m_added, [])                                       # oracle 1: exempt
        self.assertFalse(autoamend.structure_drift(TASK7, after, (485,)))                 # oracle 2: exempt
        m_removed, m_added = autoamend.token_change(*autoamend.provenance_masked(find, replace, ()))
        self.assertIn("485", m_added)                                                    # oracle 1: refuses
        self.assertTrue(autoamend.structure_drift(TASK7, after, ()))                      # oracle 2: refuses

    def test_oracle_2_alone_refuses_a_numeric_change_oracle_1s_masking_would_miss(self):
        """Disagreement fails closed (Z2): with oracle 1's masking broken to exempt every citation,
        oracle 2 still refuses the hunk as `structure-drift`."""
        text = "The table keeps row 3 of the golden set.\n"
        h = hunks(("card", "keeps row 3 of", "keeps row 4 of"))
        with mock.patch.object(autoamend, "provenance_masked",
                               lambda f, r, rows=(): tuple(autoamend._ROW_CITE.sub("ROWCITE", s) for s in (f, r))):
            out = autoamend.check_scope(text, h)
        self.assertEqual(out["refused"], ["structure-drift"])
        self.assertFalse(out["ok"])

    def test_a_heading_block_runs_to_the_next_heading_not_the_next_blank_line(self):
        self.assertIn("structural", refused(DONE, ("card", "renders in both themes", "renders in the light theme")))
        self.assertEqual(refused(DONE, ("card", "Free text here.", "Free prose here.")), [])

    def test_semantic_words_are_refused(self):
        self.assertIn("semantic", refused("The screen must animate on enter.\n",
                                          ("card", "must animate", "may animate")))

    def test_code_fence_inline_code_and_paths(self):
        self.assertIn("code-fence", refused(TASK7, ("spec_section", "assert skips ==", "assert count ==")))
        text = "Run `make test` over web/a.html daily.\n"
        self.assertIn("inline-code", refused(text, ("card", "`make test`", "`make check`")))
        self.assertIn("path-or-test", refused(text, ("card", "web/a.html", "web/b.html")))

    def test_not_unique_and_no_change(self):
        self.assertIn("not-unique", refused("one two one\n", ("card", "one", "three")))
        scope = autoamend.check_scope("one two one\n", [autoamend.Hunk("card", "two", "two")])
        self.assertIn("no-change", scope["refused"])

    def test_too_big(self):
        text = "".join(f"line {c}\n" for c in "abcdefghij")
        many = "".join(f"line {c}\n" for c in "abcd")
        self.assertIn("too-big", refused(text, ("card", many, many.replace("line", "item")), max_lines=6))

    def test_outside_section(self):
        start = TASK7.index("- [ ] Implement")
        self.assertIn("outside-section", refused(TASK7, ("spec_section", "no threshold literal",
                                                         "no threshold constant"), span=(0, start)))

    def test_structure_oracle_catches_what_the_hunk_rules_miss(self):
        """Oracle 2 (Z2): a hunk on a plain prose line that inserts a structural heading passes every
        per-hunk rule; only the before/after structure walk refuses it."""
        text = "Intro.\n\nThe screen keeps its timer.\n"
        self.assertEqual(refused(text, ("card", "keeps its timer.", "keeps its timer.\n## Tests\nlater")),
                         ["structure-drift"])


class History(unittest.TestCase):
    def test_repeat_and_one_per_card(self):
        h = hunks(("card", "a change the task does", "a change this task does"))
        fp = autoamend.fingerprint(h[0])
        self.assertTrue(autoamend.decide(CARD, h)["applied"])
        self.assertIn("repeat", autoamend.decide(CARD, h, applied_fps=[fp])["refused"])
        self.assertIn("card-cap", autoamend.decide(CARD, h, card_count=1)["refused"])

    def test_recommended_option(self):
        doc = {"recommended": "b", "options": [{"id": "a", "label": "x"}, {"id": "b", "label": "y", "amendment": []}]}
        self.assertEqual(autoamend.recommended_option(doc)["id"], "b")
        self.assertIsNone(autoamend.recommended_option({"recommended": "z", "options": []}))


class IntentExamples(unittest.TestCase):
    """Z1: every line of contracts/intent/auto_amend.jsonl run through `decide`, the entry point the
    tick calls, and judged by `intent.matches` against its `expect`."""

    def test_every_example_matches_its_oracle(self):
        lines = [json.loads(x) for x in (paths.INTENT / "auto_amend.jsonl").read_text().splitlines() if x.strip()]
        self.assertGreaterEqual(len(lines), 3)          # Task 2 adds the two D5 examples (per-round cap)
        ids = {ex["id"] for ex in lines}
        self.assertLessEqual({"row-citation-change-is-applied", "skip-count-change-is-refused"}, ids)
        for ex in lines:
            doc = ex["given"]["document"]
            hs = autoamend.parse_hunks(doc["amendment"])
            fps = [autoamend.fingerprint(h) for h in hs] if doc["already_applied"] else []
            out = autoamend.decide(doc["target_text"], hs, applied_fps=fps, rows=doc.get("rows") or ())
            self.assertTrue(intent.matches(ex["expect"], {"applied": out["applied"], "refused": out["refused"]}),
                            (ex["id"], out["refused"]))


if __name__ == "__main__":
    unittest.main()
