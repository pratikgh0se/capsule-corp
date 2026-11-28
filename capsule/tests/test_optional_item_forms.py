import unittest

from capsule import blocks

BODY = "1. the screen\n4. (optional) fold modules\n"
TRUE_LINES = ("Item 4 not done", "Item 4 (optional Z8 module fold) not done",
              "Optional item 4 not done")
FALSE_LINES = ("Item 1 not done", "Item 4 skipped", "Item 4 deferred", "Step 4 not done",
               "the optional thing is not done",
               "Item 4 not done (the retry handler still needs a test)",
               "Item 4 not done, however tests/test_x.py still needs an assertion",
               "Item 4 (optional) not done", "Item 4 (optional ) not done",
               "Item 4 (optional, but the retry handler still needs a test) not done")
FALSE_BODIES = ("4. Optional fields must validate\n", "4. Not optional: fold modules\n",
                "4. fold the optional modules\n", "4. Optional : x\n", "4. Optional- x\n",
                "4. Optional-field validation\n",
                "4. required thing\n4. (optional) other thing\n")


class ClosedForms(unittest.TestCase):
    def test_only_the_named_lines_skip_an_optional_item(self):
        for line in TRUE_LINES:
            for text in (line, line + "."):
                with self.subTest(text=text):
                    self.assertTrue(blocks.is_optional_item_note(text, BODY))
        for line in FALSE_LINES:
            for text in (line, line + "."):
                with self.subTest(text=text):
                    self.assertFalse(blocks.is_optional_item_note(text, BODY))
        for body in FALSE_BODIES:
            with self.subTest(body=body):
                self.assertFalse(blocks.is_optional_item_note("Item 4 not done", body))
        self.assertFalse(blocks.is_optional_item_note("Item 4 not done", ""))
