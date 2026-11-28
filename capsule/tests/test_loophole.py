"""BLD-20 (row 602): `python3 -m capsule loophole <file.jsonl>`, the deterministic part of the
loophole hunt that runs before a contract or intent stamp. Every case writes proposed intent rows
to a temp dir, so nothing here touches contracts/intent/."""
import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from capsule import __main__ as cli
from capsule import loophole

HINT_CHECKS = ("noop-friendly-expect", "presence-only-expect")


def row(id_, given, expect, source="row 602"):
    return {"id": id_, "given": {"document": given}, "expect": expect, "source": source}


def gate(id_, expect, verdict="q"):
    return row(id_, {"check": "gate", "verdict": verdict}, expect)


GOOD_POS = gate("gate-merges-clean-diff", {"merged": True}, "pass")
GOOD_NEG = gate("gate-blocks-failing-diff", {"merged": False, "state": "ready"}, "fail")


def hunt_rows(rows):
    return loophole._hunt(list(enumerate(rows, 1)))


def checks(rows):
    return {f["check"] for f in hunt_rows(rows)}


class HuntRows(unittest.TestCase):
    def test_a_sound_pair_has_no_findings(self):
        self.assertEqual(hunt_rows([GOOD_POS, GOOD_NEG]), [])

    def test_duplicate_id_is_named_with_its_line(self):
        dup = gate("gate-merges-clean-diff", {"merged": True}, "x")
        found = hunt_rows([GOOD_POS, GOOD_NEG, dup])
        got = [(f["id"], f["line"]) for f in found if f["check"] == "duplicate-id"]
        self.assertEqual(got, [("gate-merges-clean-diff", 3)])

    def test_same_given_and_expect_under_two_ids_is_a_copy(self):
        copy = {**GOOD_NEG, "id": "gate-blocks-failing-diff-again"}
        self.assertIn("duplicate-content", checks([GOOD_POS, GOOD_NEG, copy]))

    def test_same_given_with_different_expect_conflicts(self):
        other = row("gate-blocks-other", GOOD_NEG["given"]["document"], {"merged": True})
        self.assertIn("conflicting-expect", checks([GOOD_POS, GOOD_NEG, other]))

    def test_expect_an_empty_document_satisfies_is_a_noop_pass(self):
        for expect in ({"findings": "<empty>"},
                       {"findings": {"<not>": "<non-empty>"}, "$outcome": "hold"}):
            rows = [GOOD_POS, GOOD_NEG, gate("noop-ok", expect)]
            self.assertIn("noop-satisfies-expect", checks(rows), expect)

    def test_expect_of_only_nothing_happened_values_is_noop_friendly(self):
        for expect in ({"merged": False}, {"exit": 0, "errors": [], "n": 0},
                       {"a": {"b": None, "c": ""}}):
            rows = [GOOD_POS, GOOD_NEG, gate("only-nothing", expect)]
            self.assertIn("noop-friendly-expect", checks(rows), expect)

    def test_expect_with_one_positive_fact_is_not_noop_friendly(self):
        rows = [GOOD_POS, GOOD_NEG, gate("one-fact", {"merged": False, "state": "ready"})]
        self.assertNotIn("noop-friendly-expect", checks(rows))

    def test_expect_that_only_asks_for_presence_cannot_tell_content(self):
        rows = [GOOD_POS, GOOD_NEG, gate("presence", {"report": "<any>", "text": "<non-empty>"})]
        self.assertIn("presence-only-expect", checks(rows))

    def test_expect_that_restates_given_cannot_tell_behaviour_from_absence(self):
        given = {"check": "gate", "verdict": "pass", "n": {"k": 1}}
        echo = row("echo", given, {"verdict": "pass", "n": {"k": 1}})
        self.assertIn("expect-restates-given", checks([GOOD_POS, GOOD_NEG, echo]))

    def test_expect_with_one_new_fact_beside_a_restated_one_is_fine(self):
        mixed = gate("mixed", {"verdict": "pass", "merged": True}, "pass")
        self.assertNotIn("expect-restates-given", checks([mixed, GOOD_NEG]))

    def test_file_with_no_negative_case_is_named(self):
        pos2 = gate("gate-merges-other", {"merged": True}, "ok")
        found = hunt_rows([GOOD_POS, pos2])
        named = [f for f in found if f["check"] == "no-negative-case"]
        self.assertEqual(len(named), 1)
        self.assertIsNone(named[0]["id"])

    def test_negative_shapes_each_count_as_a_negative_case(self):
        shapes = ({"exit": 2}, {"ok": False, "x": 1}, {"$outcome": "hold"},
                  {"y": {"<not>": "<any>"}, "z": 1}, {"y": "<empty>", "z": 1},
                  {"state": "blocked"}, {"allowed": {"/a": False, "/b": True}})
        for neg in shapes:
            self.assertNotIn("no-negative-case", checks([GOOD_POS, gate("anything", neg)]), neg)

    def test_negative_word_in_the_id_counts(self):
        rows = [GOOD_POS, gate("gate-never-merges-twice", {"merged": 1})]
        self.assertNotIn("no-negative-case", checks(rows))

    def test_malformed_rows_are_findings_not_crashes(self):
        bad = [{"id": "bad"}, "text", {"id": "bad2", "given": 1, "expect": []}]
        found = hunt_rows([GOOD_POS, GOOD_NEG, *bad])
        self.assertEqual(sum(f["check"] == "malformed-row" for f in found), 3)

    def test_severity_split_is_exactly_the_two_noisy_checks(self):
        every = {
            "malformed-row": [{"id": "bad"}],
            "duplicate-id": [GOOD_POS, GOOD_POS],
            "duplicate-content": [GOOD_NEG, {**GOOD_NEG, "id": "copy"}],
            "conflicting-expect": [GOOD_NEG, row("o", GOOD_NEG["given"]["document"], {"m": 1})],
            "noop-satisfies-expect": [gate("n1", {"f": "<empty>"})],
            "expect-restates-given": [gate("e1", {"verdict": "q"})],
            "no-negative-case": [GOOD_POS],
            "noop-friendly-expect": [gate("n2", {"merged": False})],
            "presence-only-expect": [gate("p1", {"r": "<any>"})],
        }
        for check, rows in every.items():
            sev = {f["severity"] for f in hunt_rows(rows) if f["check"] == check}
            self.assertEqual(sev, {"hint" if check in HINT_CHECKS else "finding"}, check)


class HuntFile(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def write(self, name, rows, raw=""):
        path = self.tmp / name
        path.write_text("".join(json.dumps(r) + "\n" for r in rows) + raw)
        return path

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(["loophole", *map(str, argv)])
        return rc, out.getvalue(), err.getvalue()

    def test_unparseable_line_is_a_finding_with_its_line(self):
        path = self.write("a.jsonl", [GOOD_POS, GOOD_NEG], raw="{not json\n")
        found = [f for f in loophole._hunt_file(path) if f["check"] == "malformed-row"]
        self.assertEqual([f["line"] for f in found], [3])

    def test_blank_lines_keep_line_numbers_honest(self):
        path = self.write("a.jsonl", [GOOD_POS], raw="\n" + json.dumps(GOOD_POS) + "\n")
        dup = [f for f in loophole._hunt_file(path) if f["check"] == "duplicate-id"]
        self.assertEqual([f["line"] for f in dup], [3])

    def test_cli_clean_file_exits_0(self):
        rc, out, _ = self.run_cli(self.write("ok.jsonl", [GOOD_POS, GOOD_NEG]))
        self.assertEqual(rc, 0)
        self.assertIn("no loopholes", out)

    def test_cli_findings_exit_1_and_name_file_line_id_check(self):
        path = self.write("bad.jsonl", [GOOD_POS, gate("noop", {"findings": "<empty>"})])
        rc, out, _ = self.run_cli(path)
        self.assertEqual(rc, 1)
        self.assertIn("bad.jsonl:2 noop noop-satisfies-expect", out)

    def test_hints_print_with_a_prefix_but_exit_0(self):
        rows = [GOOD_POS, GOOD_NEG, gate("only-nothing", {"merged": False}),
                gate("presence", {"report": "<any>"}, "r")]
        rc, out, _ = self.run_cli(self.write("hints.jsonl", rows))
        self.assertEqual(rc, 0)
        self.assertIn("hint: ", out)
        self.assertIn("noop-friendly-expect", out)
        self.assertIn("presence-only-expect", out)
        self.assertIn("no loopholes found", out)

    def test_a_finding_beside_hints_still_exits_1_and_is_not_prefixed(self):
        rows = [GOOD_POS, GOOD_NEG, gate("only-nothing", {"merged": False}),
                gate("noop", {"findings": "<empty>"}, "r")]
        rc, out, _ = self.run_cli(self.write("mixed.jsonl", rows))
        self.assertEqual(rc, 1)
        by_check = {line.removeprefix("hint: ").split(": ")[0].split(" ")[-1]: line
                    for line in out.splitlines()}
        self.assertTrue(by_check["noop-friendly-expect"].startswith("hint: "))
        self.assertFalse(by_check["noop-satisfies-expect"].startswith("hint: "))

    def test_cli_json_is_machine_readable(self):
        path = self.write("bad.jsonl", [GOOD_POS, GOOD_POS])
        rc, out, _ = self.run_cli(path, "--json")
        self.assertEqual(rc, 1)
        self.assertIn("duplicate-id", {f["check"] for f in json.loads(out)})

    def test_cli_missing_file_exits_2(self):
        rc, _, err = self.run_cli(self.tmp / "nope.jsonl")
        self.assertEqual(rc, 2)
        self.assertIn("nope.jsonl", err)

    def test_cli_several_files_exit_1_if_any_has_findings(self):
        good = self.write("ok.jsonl", [GOOD_POS, GOOD_NEG])
        bad = self.write("bad.jsonl", [GOOD_POS, GOOD_POS])
        self.assertEqual(self.run_cli(good, bad)[0], 1)

    def test_hunt_never_writes_to_the_file(self):
        path = self.write("ro.jsonl", [GOOD_POS, GOOD_POS])
        before = path.read_bytes()
        self.run_cli(path)
        self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
