"""The decision-log parser and sidecar (Capsule decision row 128, M070).
Stdlib only, fixture text only: the real log is never read by this suite."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import decision_log as dl

GOOD = """---
date: 2026-09-06
---

## For future agent

Preamble prose with a | pipe in it that must not parse as a row.

## Decisions

| # | Topic | Decision | Status |
|---|---|---|---|
| 1 | Decisions that must reach the owner | Architecture and structure | build now |
| 2 | Component names (the owner, 2026-09-09 evening) | Capsule Corp = the whole system | decided |
| 3 | Usage guard threshold | The pause at 85 % stays | decided (already the behaviour) |

## Sources

- Conversation 2026-09-06.
"""


class Parse(unittest.TestCase):
    def test_only_numbered_table_rows_are_rows(self):
        rows = dl.parse(GOOD)
        self.assertEqual([r["number"] for r in rows], [1, 2, 3])

    def test_the_header_and_separator_rows_are_not_rows(self):
        self.assertNotIn("#", [r["title"] for r in dl.parse(GOOD)])

    def test_the_title_loses_a_trailing_parenthetical_but_keeps_inner_ones(self):
        rows = {r["number"]: r for r in dl.parse(GOOD)}
        self.assertEqual(rows[2]["title"], "Component names")
        self.assertEqual(rows[1]["title"], "Decisions that must reach the owner")

    def test_the_date_is_parsed_where_present_and_null_where_not(self):
        rows = {r["number"]: r for r in dl.parse(GOOD)}
        self.assertEqual(rows[2]["date"], "2026-09-09")
        self.assertIsNone(rows[1]["date"])

    def test_status_and_text_are_verbatim(self):
        rows = {r["number"]: r for r in dl.parse(GOOD)}
        self.assertEqual(rows[3]["status"], "decided (already the behaviour)")
        self.assertEqual(rows[3]["text"], "The pause at 85 % stays")

    def test_rows_come_back_in_file_order_not_keyed_by_number(self):
        """Final review C2: parse() used to accumulate into a dict keyed by
        number, which collapsed duplicates before lint() could see them."""
        text = GOOD.replace("| 1 |", "| 9 |")
        self.assertEqual([r["number"] for r in dl.parse(text)], [9, 2, 3])

    def test_a_row_with_too_few_cells_is_skipped_not_crashed(self):
        rows = dl.parse(GOOD + "\n| 4 | only two cells |\n")
        self.assertEqual([r["number"] for r in rows], [1, 2, 3])

    def test_a_two_cell_row_keeps_its_topic_and_text_with_no_status(self):
        """Real rows 595-604 are `| n | date | text |`: no status cell (evals E1 F3)."""
        text = GOOD + "\n| 4 | 2026-10-18 | the owner (stamp): go |\n"
        rows = {r["number"]: r for r in dl.parse(text)}
        self.assertEqual((rows[4]["status"], rows[4]["text"], rows[4]["date"]),
                         (None, "the owner (stamp): go", "2026-10-18"))
        self.assertEqual(rows[4]["topic"], "2026-10-18")

    def test_the_raw_topic_cell_keeps_its_provenance_but_the_sidecar_is_unchanged(self):
        rows = {r["number"]: r for r in dl.parse(GOOD)}
        self.assertEqual(rows[2]["topic"], "Component names (the owner, 2026-09-09 evening)")
        self.assertNotIn("topic", json.loads(dl.render_jsonl(dl.parse(GOOD)).splitlines()[1]))

    def test_escaped_pipes_inside_a_cell_do_not_split_it(self):
        text = GOOD + "\n| 5 | Routing table \\|R1\\|R2\\|R3 | Values \\|A\\|B | shipped |\n"
        rows = {r["number"]: r for r in dl.parse(text)}
        self.assertEqual(rows[5]["title"], "Routing table |R1|R2|R3")
        self.assertEqual(rows[5]["text"], "Values |A|B")
        self.assertEqual(rows[5]["status"], "shipped")


DUPES = GOOD.replace(
    "| 3 | Usage guard threshold | The pause at 85 % stays | decided (already the behaviour) |",
    "| 3 | Usage guard threshold | The pause at 85 % stays | decided |\n"
    "| 3 | Reused by mistake | Second row with the same number | decided |")


class Duplicates(unittest.TestCase):
    """Final review C2, and spec §10's required fixture: a decision table with
    a REUSED row number, driven through the real parse() -> lint() /
    render_jsonl() path rather than through hand-built row lists."""

    def test_parse_keeps_both_rows_that_share_a_number(self):
        rows = dl.parse(DUPES)
        self.assertEqual([r["number"] for r in rows], [1, 2, 3, 3])

    def test_lint_fires_on_a_duplicate_that_came_through_parse(self):
        problems = [p for p in dl.lint(dl.parse(DUPES)) if not dl.is_info(p)]
        self.assertTrue(any("duplicate row number 3" in p for p in problems), problems)

    def test_lint_names_both_occurrences(self):
        problem = next(p for p in dl.lint(dl.parse(DUPES)) if "duplicate" in p)
        self.assertIn("Usage guard threshold", problem)
        self.assertIn("Reused by mistake", problem)

    def test_the_sidecar_keeps_the_first_occurrence(self):
        rows = [json.loads(l) for l in dl.render_jsonl(dl.parse(DUPES)).splitlines()]
        self.assertEqual([r["number"] for r in rows], [1, 2, 3])
        self.assertEqual(rows[-1]["title"], "Usage guard threshold")


class Sidecar(unittest.TestCase):
    def test_one_compact_json_object_per_line_in_key_order(self):
        out = dl.render_jsonl(dl.parse(GOOD))
        lines = out.splitlines()
        self.assertEqual(len(lines), 3)
        self.assertEqual(list(json.loads(lines[0])), ["number", "title", "date", "text", "status"])
        self.assertNotIn("\n", lines[0])

    def test_the_render_is_byte_stable_for_the_same_input(self):
        self.assertEqual(dl.render_jsonl(dl.parse(GOOD)), dl.render_jsonl(dl.parse(GOOD)))

    def test_write_sidecar_is_idempotent_and_does_not_touch_mtime_twice(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            (vault / "Decisions").mkdir()
            src = vault / "Decisions" / "log.md"
            src.write_text(GOOD, encoding="utf-8")
            self.assertTrue(dl.write_sidecar(vault, src))
            out = vault / "Decisions" / "decisions.jsonl"
            first = out.stat().st_mtime_ns
            self.assertFalse(dl.write_sidecar(vault, src))   # unchanged -> no write
            self.assertEqual(out.stat().st_mtime_ns, first)

    def test_a_malformed_row_does_not_empty_the_sidecar(self):
        rows = dl.parse(GOOD + "\n| | | |\n")
        self.assertEqual(len(rows), 3)


class Lint(unittest.TestCase):
    """Row 128, M069: numbers are append-only and never reused, and the lint
    reports duplicates and gaps. Row 119's own renumber note is the precedent."""

    def _rows(self, numbers):
        return [{"number": n, "title": f"t{n}", "date": None, "text": "x",
                 "status": "decided"} for n in numbers]

    def test_a_clean_run_is_clean(self):
        self.assertEqual(dl.lint(self._rows([1, 2, 3, 4])), [])

    def test_a_duplicate_number_is_a_problem_naming_the_number(self):
        rows = self._rows([1, 2, 2, 3])
        problems = dl.lint(rows)
        self.assertTrue(any("duplicate" in p and "2" in p for p in problems))

    def test_a_gap_is_reported_with_the_missing_numbers(self):
        problems = dl.lint(self._rows([1, 2, 5]))
        self.assertTrue(any("gap" in p and "3, 4" in p for p in problems))

    def test_a_gap_is_informational_only_never_a_health_issue(self):
        """Final review I10: the real log has a permanent hole from row 119's
        renumber. A warn that can never be cleared teaches the reader to
        scroll past the duplicate signal this check exists to carry."""
        gap = next(p for p in dl.lint(self._rows([1, 2, 5])) if "gap" in p)
        self.assertTrue(dl.is_info(gap), gap)
        self.assertEqual([p for p in dl.lint(self._rows([1, 2, 5]))
                          if not dl.is_info(p)], [])

    def test_duplicate_and_starts_at_1_stay_warns(self):
        for rows in (self._rows([1, 2, 2]), self._rows([3, 4, 5])):
            self.assertTrue([p for p in dl.lint(rows) if not dl.is_info(p)])

    def test_the_append_only_rule_is_stated_in_the_module(self):
        self.assertIn("append-only", dl.__doc__)

    def test_an_empty_table_is_a_problem_not_a_clean_run(self):
        self.assertTrue(dl.lint([]))

    def test_the_real_logs_known_gaps_are_reported_not_hidden(self):
        # The renumber of "row 23" to 119 left a real hole. The lint's job is
        # to SAY so, so a future renumber is a decision, not a discovery -
        # but as an `info:` line, not a permanent health warn (I10).
        problems = dl.lint(self._rows([1, 2, 24]))
        self.assertEqual(len(problems), 1)
        self.assertTrue(dl.is_info(problems[0]))


class OutcomeRowConvention(unittest.TestCase):
    """Row 139 / M251. The outcome row is a TABLE CONVENTION, not a schema
    change: `_ROW_RE` keys on `| <int> |` and `parse` takes the last cell as a
    free-form `status`, so `outcome` needs no parser work. This is the fence
    that keeps it true - a future status whitelist would break every outcome
    row silently, and the sidecar feeds worker cards."""

    ROW = ("| 141 | Outcome: sample-menu-bar (2026-11-01) | 0 external "
           "installers by day 30 -> kill; the pipeline test was still a win "
           "-> row 140 | outcome |")

    def test_an_outcome_row_parses_with_status_outcome(self):
        rows = dl.parse("| # | Topic | Ruling | Status |\n"
                                  "|---|---|---|---|\n" + self.ROW + "\n")
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["number"], 141)
        self.assertEqual(r["status"], "outcome")
        self.assertEqual(r["title"], "Outcome: sample-menu-bar")
        self.assertEqual(r["date"], "2026-11-01")
        self.assertIn("-> kill", r["text"])
        self.assertIn("row 140", r["text"])

    def test_lint_says_nothing_about_the_status_cell(self):
        rows = dl.parse("| 1 | A | x | decided |\n" + self.ROW + "\n")
        problems = [p for p in dl.lint(rows)
                    if not dl.is_info(p)]
        self.assertEqual(problems, [],
                         "lint must not object to an `outcome` status")

    def test_an_outcome_row_reaches_the_sidecar(self):
        rows = dl.parse(self.ROW + "\n")
        line = dl.render_jsonl(rows).strip()
        self.assertIn('"status":"outcome"', line)
        self.assertIn('"number":141', line)


class VaultIsOptIn(unittest.TestCase):
    """D254: no vault is a clean no-op; the note glob comes from host.json. Temp dirs only."""

    def setUp(self):
        import json
        import os
        self.os, self.json = os, json
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.vault = Path(self.tmp.name) / "vault"
        (self.vault / "Decisions").mkdir(parents=True)
        self.env = "OBSIDIAN_VAULT_PATH"
        self._saved = {k: os.environ.get(k) for k in ("CAPSULE_HOME", self.env)}
        os.environ["CAPSULE_HOME"] = str(self.home)
        os.environ.pop(self.env, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                self.os.environ.pop(k, None)
            else:
                self.os.environ[k] = v
        self.tmp.cleanup()

    def cfg(self, vault_cfg):
        (self.home / "host.json").write_text(self.json.dumps({"vault": vault_cfg}))

    def test_no_vault_gives_none_and_a_clean_no_op(self):
        self.assertIsNone(dl.find_log(None))
        self.assertIsNone(dl.find_log(""))
        self.assertFalse(dl.write_sidecar(self.vault))     # a vault with no note: nothing written
        self.assertEqual(dl.main(["sidecar"]), 0)          # no vault configured: skipped, exit 0

    def test_default_glob_finds_a_decisions_note_and_host_json_changes_it(self):
        note = self.vault / "Decisions" / "My design decisions.md"
        note.write_text(GOOD)
        other = self.vault / "Decisions" / "Choices.md"
        other.write_text(GOOD)
        self.assertEqual(dl.find_log(self.vault), note)
        self.cfg({"decision_notes": "Decisions/Choices*.md"})
        self.assertEqual(dl.find_log(self.vault), other)

    def test_vault_from_host_json_and_from_env_both_drive_main(self):
        (self.vault / "Decisions" / "X decisions.md").write_text(GOOD)
        self.cfg({"path": str(self.vault)})
        self.assertEqual(dl.main(["sidecar"]), 0)
        sidecar = self.vault / "Decisions" / dl.SIDECAR_NAME
        self.assertTrue(sidecar.is_file())
        sidecar.unlink()
        self.cfg({})
        self.os.environ[self.env] = str(self.vault)
        self.assertEqual(dl.main(["sidecar"]), 0)
        self.assertTrue(sidecar.is_file())


class RenderJsonlKeyOrder(unittest.TestCase):
    """C4 L1 leftover: the emitted key order is `_KEYS`, in that order - part
    of the sidecar's byte contract, so a silent reorder rewrites every line of
    Decisions/decisions.jsonl."""

    def test_render_jsonl_key_order_is_pinned(self):
        row = {"number": 1, "title": "t", "date": "2026-09-06", "text": "x", "status": "decided"}
        line = dl.render_jsonl([row]).splitlines()[0]
        self.assertEqual(list(json.loads(line).keys()), list(dl._KEYS))
        self.assertEqual(dl._KEYS, ("number", "title", "date", "text", "status"))


if __name__ == "__main__":
    unittest.main()
