# mutation-check: targets-predate-range b957739c9 -- names out of tests, no behaviour pinned ::
#   ShippedExamples.test_every_source_shape_lint_accepts_is_valid,
#   SourceType.test_hand_written_bad_sources_are_rejected_by_schema_and_by_lint
"""Row 569 item 6: the intent example line is typed by `contracts/schemas/rows/intent_example.v1.json`
(its `source` is no longer regex-only: `intent.SOURCE_RE` + `_valid_source` stay, the schema is the
declared type beside them), and `intent.lint` runs it on every example line.
Oracle 1: every shipped example of every `contracts/intent/*.jsonl` validates. Oracle 2: the schema's
`source` pattern is `intent.SOURCE_RE` itself modulo the two spellings the stdlib validator needs, and
hand-written bad lines are rejected both by the schema directly and through `intent.lint`. The one
thing the schema cannot say (a real calendar date) is pinned here as lint-only, so the layering is
explicit rather than accidental."""
import json
import tempfile
import unittest
from pathlib import Path

from capsule import intent, paths, row_schemas

GOOD = {"id": "an-example", "given": {"document": {"status": "done"}}, "expect": {"status": "done"},
        "source": "row 569"}


def lint_one(example):
    p = Path(tempfile.mkdtemp()) / "x.jsonl"
    p.write_text(json.dumps(example) + "\n")
    return intent.lint(p, None, "x")


class ShippedExamples(unittest.TestCase):
    def test_every_shipped_example_line_validates(self):
        seen = 0
        for path in sorted(paths.INTENT.glob("*.jsonl")):
            for n, line in enumerate(path.read_text().splitlines(), 1):
                if line.strip():
                    seen += 1
                    with self.subTest(at=f"{path.name}:{n}"):
                        self.assertEqual(row_schemas.problems("intent_example.v1", json.loads(line)), [])
        self.assertGreater(seen, 100)

    def test_every_source_shape_lint_accepts_is_valid(self):
        for src in ("row 454", "row 452, 2026-09-27", "recorded run 0f3a9c", "owner"):
            self.assertEqual(row_schemas.problems("intent_example.v1", dict(GOOD, source=src)), [], src)


class SourceType(unittest.TestCase):
    def test_the_schema_pattern_is_source_re_itself(self):
        pattern = row_schemas.load("intent_example.v1")["properties"]["source"]["pattern"]
        self.assertEqual(pattern, intent.SOURCE_RE.pattern.replace("(?P<date>", "(").replace("$", "\\Z"))

    def test_hand_written_bad_sources_are_rejected_by_schema_and_by_lint(self):
        for bad in ("a hunch", "row", "row 12345", "row 452, 2026/09/27", "row 452 2026-09-27", "row 452,2026-09-27",
                    "row 452, 26-09-27", "row 452, 2026-09-27\n", "recorded run x",
                    "Owner", "owner ", ""):
            with self.subTest(bad):
                self.assertTrue(row_schemas.problems("intent_example.v1", dict(GOOD, source=bad)), bad)
                self.assertTrue(lint_one(dict(GOOD, source=bad)), bad)

    def test_a_source_that_is_not_a_string_is_rejected(self):
        for bad in (None, 569, ["row 569"], {"row": 569}):
            with self.subTest(repr(bad)):
                self.assertTrue(row_schemas.problems("intent_example.v1", dict(GOOD, source=bad)))

    def test_an_impossible_calendar_date_is_the_one_thing_only_lint_catches(self):
        impossible = dict(GOOD, source="row 452, 0000-99-99")
        self.assertEqual(row_schemas.problems("intent_example.v1", impossible), [])    # shape only
        self.assertTrue(any("source must be" in e for e in lint_one(impossible)))      # calendar: _valid_source


class LineType(unittest.TestCase):
    def test_a_good_line_is_valid_and_lint_clean(self):
        self.assertEqual(row_schemas.problems("intent_example.v1", GOOD), [])
        self.assertEqual(lint_one(GOOD), [])

    def test_hand_written_bad_lines_are_rejected_by_the_schema(self):
        bad = {
            "id with capitals": dict(GOOD, id="Has Spaces"),
            "id with a trailing newline": dict(GOOD, id="x\n"),
            "given is a list": dict(GOOD, given=[]),
            "given without document": dict(GOOD, given={"inputs": {}}),
            "given with a third key": dict(GOOD, given={"document": {}, "run": {}}),
            "expect is a string": dict(GOOD, expect="done"),
            "extra key": dict(GOOD, why="x"),
            "missing source": {k: v for k, v in GOOD.items() if k != "source"},
        }
        for label, line in bad.items():
            with self.subTest(label):
                self.assertTrue(row_schemas.problems("intent_example.v1", line), label)

    def test_lint_reports_the_schema_violation_with_its_file_and_line(self):
        errors = lint_one(dict(GOOD, expect="done"))
        self.assertTrue(any(e.startswith("x.jsonl:1: intent_example.v1") for e in errors), errors)


class IntentExamples(unittest.TestCase):
    """Z1: `contracts/intent/intent_source.jsonl`, each example run through the real schema."""

    def test_every_example_matches_what_the_schema_does(self):
        examples = intent.load("intent_source")
        self.assertTrue(1 <= len(examples) <= 3)
        for ex in examples:
            row = ex["given"]["document"]["row"]
            self.assertEqual(row_schemas.problems("intent_example.v1", row) == [], ex["expect"]["valid"], ex["id"])


class SpecificViolations(unittest.TestCase):
    def test_the_message_names_the_field_and_the_rule(self):
        errs = row_schemas.problems("intent_example.v1", dict(GOOD, source="a hunch"))
        self.assertEqual(len(errs), 1)
        self.assertTrue(errs[0].startswith("#/source: does not match"), errs)
        self.assertEqual(row_schemas.problems("intent_example.v1", dict(GOOD, why="x")), ["#: unexpected field 'why'"])
