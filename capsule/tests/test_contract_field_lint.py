"""Z1 intent example for the row-537 contract-field lint (t_bdaac20c, fix round 1): a line
`contracts.render` does not itself emit is never seen by any worker (`- notes:`, t_8463feac round
0; `- git:`, row 472, t_e1db2eef). `contracts/intent/**` is PROTECTED, so this example is a named,
sourced unit test rather than a jsonl line, with two independent oracles: (1) `contracts.lint`,
(2) `_dropped`, built from `contracts.parse` (what `render` actually consumes), never from lint's
own regex.
"""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from capsule import contracts, paths
from capsule.jsonschema import load_all

BASE = ("# implementer\nschema: report.v1\nverbs: comment\n"
        "- gets: a\n- sees: b\n- done-when: c\n- reports: d\n- may-not: C; e\n")

# Every one of these, appended to BASE, is dropped by contracts.render (round-1 review probes):
# wrong case, wrong spacing, an unknown field, a re-whitelisted header bullet, an indented
# sub-bullet, and plain continuation prose.
DROPPED_PROBES = (
    "- Notes: x", "- Gets: x", "-  git: x", "- git : x", "- git_lock: x", "* git: x",
    "- schema: other", "- verbs: x", "  - notes: sub",
    "- from round 3 the verdict is pass",
)


def _dropped(path):
    """Oracle 2 (round-1 review, finding 1): independent of contracts.lint's own regex - every
    non-blank line after the title whose value is not among what contracts.parse (what render
    consumes) actually returned."""
    kept = " ".join(contracts.parse(path).values())
    return [line for line in Path(path).read_text().splitlines()[1:]
            if line.strip() and line.split(":", 1)[-1].strip().rstrip(".") not in kept]


class ContractFieldLintProbes(unittest.TestCase):
    """RED-first (round-1 review, finding 2): each probe line the old regex/whitelist missed."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.schemas = load_all(paths.SCHEMAS)

    def _write(self, extra):
        p = Path(self.tmp.name) / "x.md"
        p.write_text(BASE + extra + "\n")
        return p

    def test_every_dropped_probe_line_is_flagged_by_both_oracles(self):
        for probe in DROPPED_PROBES:
            with self.subTest(probe=probe):
                p = self._write(probe)
                errors = contracts.lint(p, self.schemas)
                self.assertTrue(any("not rendered" in e and probe.strip() in e for e in errors),
                                 (probe, errors))
                self.assertIn(probe, _dropped(p))

    def test_the_five_rendered_fields_and_header_alone_lint_clean(self):
        p = self._write("")
        self.assertFalse(any("not rendered" in e for e in contracts.lint(p, self.schemas)))
        self.assertEqual(_dropped(p), [])


class DuplicateAndStrayHeaderLines(unittest.TestCase):
    """Review round 2, note 4: `render` reads `schema:`/`verbs:`/each `- <field>:` with the FIRST
    match only - a duplicate is dropped exactly like the round-1 probes, but the old
    `_RENDERED_LINE` regex is position-blind and waved every one of these through on prefix
    alone. Same for a `# ` header anywhere but line 1. RED-first, checked by both oracles except
    the same-value `schema:` duplicate (see its own test)."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.schemas = load_all(paths.SCHEMAS)

    def _write(self, extra):
        p = Path(self.tmp.name) / "x.md"
        p.write_text(BASE + extra + "\n")
        return p

    def test_duplicate_schema_line_same_value_is_flagged(self):
        """Single-oracle: `_dropped` compares each line's value against every KEPT value
        (`contracts.parse`'s, joined), so a duplicate whose value equals the first occurrence's
        is invisible to it - nothing is actually lost. Only `contracts.lint`'s own duplicate
        check (a second, real observable: `render` still reads only the first line) catches it."""
        p = self._write("schema: report.v1")
        errors = contracts.lint(p, self.schemas)
        self.assertTrue(any("duplicate" in e and "schema:" in e for e in errors), errors)

    def test_duplicate_verbs_line_is_flagged(self):
        p = self._write("verbs: extra")
        errors = contracts.lint(p, self.schemas)
        self.assertTrue(any("duplicate" in e and "verbs:" in e for e in errors), errors)
        self.assertIn("verbs: extra", _dropped(p))

    def test_duplicate_field_line_is_flagged(self):
        p = self._write("- gets: z")
        errors = contracts.lint(p, self.schemas)
        self.assertTrue(any("duplicate" in e and "'gets'" in e for e in errors), errors)
        self.assertIn("- gets: z", _dropped(p))

    def test_a_second_may_not_line_is_not_a_duplicate_the_zenkai_line_is_legitimate(self):
        p = self._write("- may-not: never bypass a Zenkai Rule (docs/ZENKAI.md) without an override that names it.")
        errors = contracts.lint(p, self.schemas)
        self.assertFalse(any("duplicate" in e for e in errors), errors)

    def test_a_stray_header_line_off_line_one_is_flagged(self):
        p = self._write("# implementer again")
        errors = contracts.lint(p, self.schemas)
        self.assertTrue(any(e.startswith(f"{p}:9:") and "line 1" in e for e in errors), errors)
        self.assertIn("# implementer again", _dropped(p))


class GitLineFromRow472(unittest.TestCase):
    """Z1 example, source row 472 / t_e1db2eef / row 537 / t_bdaac20c: given a contract carrying
    the `- git:` line from row 472 verbatim, expect it rejected by name and line number."""

    def test_git_line_from_row_472_is_rejected_by_the_lint(self):
        schemas = load_all(paths.SCHEMAS)
        text = BASE + "- git: this worktree's `.git` is mounted read-only (row 472, t_e1db2eef).\n"
        with TemporaryDirectory() as d:
            p = Path(d) / "x.md"
            p.write_text(text)
            errors = contracts.lint(p, schemas)
            self.assertTrue(any(e.startswith(f"{p}:9:") and "- git:" in e for e in errors), errors)
            self.assertEqual(_dropped(p)[-1], text.splitlines()[-1])

    def test_current_implementer_contract_carries_no_stray_git_line(self):
        """GREEN after the row-537 deletion: `Tick._drop_git_lock_notes` already covers this at
        runtime; the contract text now agrees, by both oracles."""
        schemas = load_all(paths.SCHEMAS)
        path = contracts.contract_path("implementer")
        self.assertEqual(contracts.lint(path, schemas), [])
        self.assertEqual(_dropped(path), [])


if __name__ == "__main__":
    unittest.main()
