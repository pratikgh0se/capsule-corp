"""cited_rows() (row 454, 519; t_f63e221f): a card body's decision-row citations, however the
list is joined - comma, "and", "&", "or" - and a range's endpoints, without an amend-date
tail or an unrelated number being misread as another cited row.
"""
import unittest

from capsule import citations


class CitedRows(unittest.TestCase):
    def test_and_list_keeps_every_row(self):
        # t_f63e221f's own reported bug: the greedy digit/comma/space run consumed the space
        # before "and", so the optional trailing "and N" group never fired and 3 was dropped.
        self.assertEqual(citations.cited_rows("rows 1, 2 and 3"), [1, 2, 3])

    def test_oxford_comma_list_keeps_every_row(self):
        # Fix round 1, I1: a comma before "and"/"or"/"&" ("1, 2, and 3") is one separator, not
        # two - the previous either/or _SEP needed two separator matches in a row for it and
        # stopped one number early, the same drop-the-last-row bug in a different shape.
        self.assertEqual(citations.cited_rows("rows 1, 2, and 3"), [1, 2, 3])
        self.assertEqual(citations.cited_rows("rows 1, 2, or 3"), [1, 2, 3])
        self.assertEqual(citations.cited_rows("rows 1, 2, & 3"), [1, 2, 3])

    def test_real_card_shape_four_rows_and_the_last(self):
        # The card t_f63e221f's own title/done-when example ("rows 417, 418, 419 and 420" must
        # not drop the trailing 420), also exercised end-to-end in test_tick.py's
        # GuideRowTexts.test_i5_the_cards_own_row_outbudgets_an_epic_only_row_end_to_end.
        self.assertEqual(citations.cited_rows("rows 417, 418, 419 and 420"), [417, 418, 419, 420])

    def test_real_zenkai_citation(self):
        # docs/ZENKAI.md line 5: "rows 454 and 455".
        self.assertEqual(citations.cited_rows("rows 454 and 455"), [454, 455])

    def test_real_factory_autonomy_plan_citation(self):
        # docs/superpowers/plans/2026-09-27-factory-autonomy.md line 240: "rows 452, 481 and 485".
        self.assertEqual(citations.cited_rows("rows 452, 481 and 485"), [452, 481, 485])

    def test_ampersand_and_or_separators(self):
        self.assertEqual(citations.cited_rows("rows 1 & 2"), [1, 2])
        self.assertEqual(citations.cited_rows("rows 1 or 2"), [1, 2])

    def test_ignores_a_trailing_iso_date(self):
        # t_de1466d6 (pilot run 3): "row 452, 2026-09-27" cites row 452 once, plus an amend
        # timestamp right after it - not rows [452, 2026, 9, 27].
        self.assertEqual(citations.cited_rows("see row 452, 2026-09-27"), [452])

    def test_and_list_before_a_trailing_iso_date(self):
        self.assertEqual(citations.cited_rows("rows 452 and 481, 2026-09-27"), [452, 481])

    def test_range_endpoints_not_expanded(self):
        # Pre-existing contract (unchanged by this fix): a range's endpoints are captured as
        # individual digits, not an expanded run - and a trailing "and N" after it still counts.
        self.assertEqual(citations.cited_rows("rows 12-15 and 20"), [12, 15, 20])

    def test_no_citations(self):
        self.assertEqual(citations.cited_rows("no citations here"), [])

    def test_no_false_positive_on_a_non_numeric_word_after_and(self):
        self.assertEqual(citations.cited_rows("row 108, and n"), [108])

    def test_tick_re_exports_the_same_function(self):
        from capsule import tick
        self.assertIs(tick.cited_rows, citations.cited_rows)
