"""The RUNBOOK's message-class heading and table match `slack.CLASSES` (message-classes)."""
import re
import unittest
from pathlib import Path

from capsule.tests.public_tree import private_only
from capsule import slack

RUNBOOK = Path(__file__).resolve().parents[2] / "docs" / "RUNBOOK.md"
WORDS = ("zero one two three four five six seven eight nine ten eleven twelve").split()


def _section():
    text = RUNBOOK.read_text(encoding="utf-8")
    m = re.search(r"^### The (\w+) message classes\n(.*?)(?=^#{1,3} )", text, re.S | re.M)
    assert m, "RUNBOOK has no 'The N message classes' heading"
    return m.group(1), m.group(2)


class RunbookMessageClasses(unittest.TestCase):
    @private_only
    def test_heading_count_is_the_class_count(self):
        word, _ = _section()
        self.assertEqual(WORDS.index(word), len(slack.CLASSES))

    @private_only
    def test_table_lists_exactly_the_classes_with_their_glyphs(self):
        _, body = _section()
        rows = re.findall(r"^\| (\S+) \| `(\w+)` \|", body, re.M)
        self.assertEqual(sorted(c for _, c in rows), sorted(slack.CLASSES))
        for glyph, cls in rows:
            self.assertEqual(glyph.strip("️"), slack.CLASSES[cls].strip("️"), cls)


if __name__ == "__main__":
    unittest.main()
