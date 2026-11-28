"""RUNBOOK records the fork facts retrieval-eval depends on.

Z1 intent: the RUNBOOK section "Fork facts for retrieval-eval" names each pinned fact and marks it
with its read date; a missing heading, a missing fact or a fact without a date fails. Z2 oracle:
the fixed pinned list below against the real RUNBOOK section at HEAD; the fork is never read.
"""
import re
import unittest
import sys
from pathlib import Path

RUNBOOK = Path(__file__).resolve().parents[2] / "docs" / "RUNBOOK.md"
HEADING = "## Fork facts for retrieval-eval"
PINNED = ('"cases"', "top_hit", "INDEX_POLICY", "semantic_search.py", "_SKIP_DIRS", "vault_ops.py",
          ".smart-env", "architecture/skills/", ".excalidraw.md", "gold and rank only",
          "635.json", "637.json", "5527040", "`q`, `gold`, `rank`, `top_hit`, `title`, `chunk`",
          "0 < rank <= 5", "_LEXICAL_MIRROR_RE", "_archive", ".claude-runs", "copilot",
          "so it is indexed", "OBSIDIAN_EMBED_ALLOW", "re-read the fork read-only on the host")
_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))
from capsule.tests.public_tree import private_only  # noqa: E402


def section(text):
    """The text under HEADING up to the next level-2 heading; None when absent."""
    m = re.search(r"^" + re.escape(HEADING) + r"\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    return m.group(1) if m else None


class ForkFacts(unittest.TestCase):
    def test_section_is_found_and_bounded(self):
        self.assertEqual(section("## A\nx\n" + HEADING + "\nbody\n## B\nother\n"), "body\n")
        self.assertIsNone(section("## A\nx\n"))

    @private_only
    def test_the_runbook_states_the_eval_row_shape_and_the_index_rules(self):
        body = section(RUNBOOK.read_text(encoding="utf-8"))
        self.assertIsNotNone(body, HEADING + " is missing from docs/RUNBOOK.md")
        missing = [p for p in PINNED if p not in body]
        self.assertEqual(missing, [], f"fork facts missing from the section: {missing}")

    @private_only
    def test_every_fact_carries_its_read_date(self):
        body = section(RUNBOOK.read_text(encoding="utf-8")) or ""
        facts = [ln for ln in body.splitlines() if ln.startswith("- ")]
        self.assertGreaterEqual(len(facts), 10)
        undated = [ln[:40] for ln in facts if not re.search(r"\(read \d{4}-\d{2}-\d{2}\)", ln)]
        self.assertEqual(undated, [])


if __name__ == "__main__":
    unittest.main()
