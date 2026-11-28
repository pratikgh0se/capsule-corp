import re
import unittest
from pathlib import Path

from capsule import bundles

SIZE_CHECK = Path(__file__).resolve().parents[2] / "tests" / "size_check.py"


class SizeRuleNote(unittest.TestCase):
    def test_the_implementer_brief_states_the_baseline_only_shrinks_and_names_the_routes(self):
        prompt = bundles.implementer({"id": "t_0000aaaa", "title": "t", "body": "b"})["prompt"]
        flat = " ".join(prompt.split())
        for text in ("tests/size_baseline.json", "may only shrink", "new file", "same file",
                     "Nobody raises it", "does not override the gate",
                     "Offset the added lines", "in-scope work", "do not hold the card"):
            self.assertIn(text, flat)
        names = "NEW_FUNC_LIMIT, NEW_FILE_LIMIT, NEW_COMPLEXITY_LIMIT"
        found = re.search(rf"^{names} = (\d+), (\d+), (\d+)", SIZE_CHECK.read_text(), re.M)
        func, file, cx = found.groups()
        for text in (f"{file} lines", f"{func} lines", f"complexity {cx}"):
            self.assertIn(text, flat)


if __name__ == "__main__":
    unittest.main()
