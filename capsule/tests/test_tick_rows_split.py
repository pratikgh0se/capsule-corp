import unittest


class TickRowsSplit(unittest.TestCase):
    def test_tick_reexports_the_moved_names(self):
        from capsule import tick, tick_rows
        self.assertIs(tick.decision_rows, tick_rows.decision_rows)
        self.assertIs(tick.ROW_TEXT_CAP, tick_rows.ROW_TEXT_CAP)
        self.assertIs(tick.ROW_TEXT_BUDGET, tick_rows.ROW_TEXT_BUDGET)
        self.assertIs(tick.ROW_TEXT_OVER_BUDGET, tick_rows.ROW_TEXT_OVER_BUDGET)
        self.assertIs(tick._row_columns, tick_rows._row_columns)
        self.assertIs(tick._truncate_row_text, tick_rows._truncate_row_text)
        self.assertIs(tick._cap_row_texts, tick_rows._cap_row_texts)
        self.assertIs(tick.spec_sections, tick_rows.spec_sections)
        self.assertIs(tick.EPIC_SECTION_MAX_LINES, tick_rows.EPIC_SECTION_MAX_LINES)
        self.assertIs(tick.epic_spec_section, tick_rows.epic_spec_section)

    def test_tick_rows_does_not_import_tick(self):
        from capsule import tick_rows
        with open(tick_rows.__file__) as fh:
            src = fh.read().replace("capsule.tick_rows", "")
        self.assertNotIn("capsule.tick ", src)


if __name__ == "__main__":
    unittest.main()
