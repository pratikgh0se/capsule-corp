"""M184 (D221): the message kinds are the nine Slack classes, and nothing else.
Oracle 1: every class literal the code emits is one of the nine (plus the two named aliases).
Oracle 2: the RUNBOOK kind table lists all 27 kinds, each on one of the nine classes."""
import ast
import re
import unittest
from pathlib import Path

from capsule.tests.public_tree import private_only
from capsule import slack

ROOT = Path(__file__).resolve().parents[2]
NINE = {"blocked", "done", "scheduled", "running", "incident", "paused", "needs_input",
        "ask", "page"}
ALIASES = set()


def _class_literal(node, name):
    """The literal class (arg 2) of a call to `name`, else None."""
    if not isinstance(node, ast.Call) or len(node.args) < 2:
        return None
    fn = node.func
    called = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
    arg = node.args[1]
    ok = called == name and isinstance(arg, ast.Constant) and isinstance(arg.value, str)
    return arg.value if ok else None


def _notify_literals():
    """(file, class) for each literal class passed to `notify` or `outbox_send`."""
    for folder, name in (("capsule", "notify"), ("maintenance", "outbox_send")):
        for path in sorted((ROOT / folder).glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            lits = (_class_literal(n, name) for n in ast.walk(tree))
            yield from ((path.name, c) for c in lits if c)


class KindMap(unittest.TestCase):
    def test_nine_classes_in_code(self):
        self.assertEqual(set(slack.CLASSES), NINE)

    def test_the_morning_digest_is_a_scheduled_line_not_a_tenth_class(self):
        body = "morning\nline two"
        self.assertEqual(slack.render("scheduled", body, digest=True),
                         f"{slack.SUMMARY_GLYPH} {body}")
        self.assertNotIn("summary", slack.CLASSES)

    def test_python_emitters_use_only_the_nine(self):
        seen = list(_notify_literals())
        self.assertTrue(seen)
        for name, cls in seen:
            with self.subTest(file=name, cls=cls):
                self.assertIn(cls, NINE | ALIASES)

    def test_shell_emitters_use_only_the_nine(self):
        found = []
        for path in sorted((ROOT / "maintenance").glob("*.sh")):
            text = path.read_text(encoding="utf-8")
            pat = r"\b(?:HEAL_CLASS|HEAL_V2_CLASS|MSG_CLASS|MSG_V2_CLASS)=([a-z_]+)"
            found += [(path.name, m) for m in re.findall(pat, text)]
        self.assertTrue(found)
        for name, cls in found:
            with self.subTest(file=name, cls=cls):
                self.assertIn(cls, NINE | ALIASES)

    @private_only
    def test_runbook_table_maps_all_27(self):
        text = (ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")
        head = text.index("### The 27 message kinds")
        rows = re.findall(r"^\| (\d+) \| .*? \| `(\w+)` \| (push|log) \|", text[head:], re.M)
        self.assertEqual([int(n) for n, _, _ in rows], list(range(1, 28)))
        for n, cls, _ in rows:
            with self.subTest(kind=n):
                self.assertIn(cls, NINE)


if __name__ == "__main__":
    unittest.main()
