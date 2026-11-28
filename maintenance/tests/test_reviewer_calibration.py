"""R15a: the reviewer calibration runner. No model, network or docker call: a dry run validates the
cases, builds the reviewer bundle that would be sent, and writes a dated JSON; recorded verdicts are
scored into a catch rate and a false-flag rate.

Oracle 1: the runner's own output. Oracle 2: `_check_result` below re-reads the JSON with no runner
code and hand-computed rates. The seed cases under `calibration/` (a protected path) are tested in
`test_reviewer_calibration_seeds.py`; these tests use their own cases in a temp dir."""
import contextlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from capsule.tests import tmpclean  # noqa: E402
tmpclean.install()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ["AGENT_OPS_NO_SEND"] = "1"
import reviewer_calibration as rc  # noqa: E402

INTENT = Path(__file__).resolve().parents[2] / "contracts" / "intent" / "reviewer_calibration.jsonl"
DATE = "2026-10-28"
LINES = [f"+line {n}" for n in range(1, 13)]
PATCH = ("diff --git a/a.py b/a.py\nnew file mode 100644\n--- /dev/null\n+++ b/a.py\n"
         "@@ -0,0 +1,12 @@\n" + "\n".join(LINES) + "\n")
_STR = {"type": "string", "minLength": 1}
EDIT = ("diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,3 +1,4 @@\n"
        " l1\n l2\n+new\n l3\n")
SCHEMA = {"type": "object", "additionalProperties": False,
          "required": ["schema", "id", "kind", "origin", "task", "patch"],
          "properties": {"schema": {"const": "reviewer_case.v1"}, "id": _STR,
                         "kind": {"enum": ["planted", "clean"]}, "origin": _STR,
                         "task": {"type": "object", "required": ["id", "title", "body"]},
                         "patch": {"type": "string", "pattern": "^[A-Za-z0-9_.-]+\\.patch$"},
                         "bug": {"type": "object", "required": ["summary", "locations"]}}}


def _case(root, cid, kind="planted", line=10, patch=PATCH, base=None, **over):
    doc = {"schema": "reviewer_case.v1", "id": cid, "kind": kind, "origin": "test",
           "task": {"id": "t_5eed0001", "title": "t", "body": "do it"}, "patch": "c.patch"}
    if kind == "planted":
        doc["bug"] = {"summary": "bad", "locations": [{"file": "a.py", "line": line}]}
    doc.update(over)
    (root / cid).mkdir()
    (root / cid / "case.json").write_text(json.dumps(doc))
    (root / cid / "c.patch").write_text(patch)
    for name, text in (base or {}).items():
        target = root / cid / "base" / (name + rc.SUFFIX)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)


def _cases(*specs):
    root = Path(tempfile.mkdtemp(prefix="rc-cases-"))
    (root / rc.SCHEMA_FILE).write_text(json.dumps(SCHEMA))
    for cid, kind in specs:
        _case(root, cid, kind)
    return root


def _verdict(verdict, *where):
    return {"schema": "review_verdict.v1", "run_id": "0" * 32, "task_id": "t_5eed0001",
            "claim_lock": "x", "role": "reviewer", "verdict": verdict, "not_reviewed": [],
            "findings": [{"file": f, "line": n, "class": "slop", "text": "x"} for f, n in where]}


def _run(cases, verdicts=None, date=DATE):
    """(rc, stderr, parsed result or None) of one `main` run into a fresh out dir."""
    out = Path(tempfile.mkdtemp(prefix="rc-out-"))
    args = ["--cases", str(cases), "--out", str(out), "--date", date]
    if verdicts is not None:
        vfile = out / "v.json"
        vfile.write_text(json.dumps(verdicts))
        args += ["--verdicts", str(vfile)]
    err = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
        code = rc.main(args)
    files = sorted(out.glob("reviewer-calibration-*.json"))
    return code, err.getvalue(), (json.loads(files[0].read_text()) if files else None), files


def _check_result(test, result, ids, dry, catch, flag):
    """Second oracle: hand-written, reads only the JSON."""
    test.assertEqual(sorted(result), ["cases", "date", "dry_run", "schema", "summary"])
    test.assertEqual(result["schema"], "reviewer_calibration_result.v1")
    test.assertEqual((result["date"], result["dry_run"]), (DATE, dry))
    test.assertEqual([c["id"] for c in result["cases"]], ids)
    for c in result["cases"]:
        test.assertGreater(c["planned"]["diff_bytes"], 0)
        test.assertGreater(c["planned"]["prompt_bytes"], c["planned"]["diff_bytes"])
        test.assertEqual(len(c["planned"]["prompt_sha256"]), 64)
        if dry:
            test.assertIsNone(c["verdict"])
    test.assertEqual((result["summary"]["catch_rate"], result["summary"]["false_flag_rate"]),
                     (catch, flag))


class DryRun(unittest.TestCase):
    def test_a_dry_run_on_three_cases_writes_the_dated_json_and_scores_nothing(self):
        cases = _cases(("b-planted", "planted"), ("a-planted", "planted"), ("c-clean", "clean"))
        code, err, result, files = _run(cases)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual([f.name for f in files], [f"reviewer-calibration-{DATE}-dry-run.json"])
        _check_result(self, result, ["a-planted", "b-planted", "c-clean"], True, None, None)
        self.assertEqual(result["summary"]["cases"], 3)

    def test_a_dry_run_makes_no_network_call_and_starts_no_process(self):
        cases = _cases(("a-planted", "planted"))
        no_net = unittest.mock.patch.object(socket, "socket", side_effect=AssertionError("net"))
        no_proc = unittest.mock.patch.object(subprocess, "Popen", side_effect=AssertionError("p"))
        with no_net, no_proc:
            self.assertEqual(_run(cases)[0], 0)

    def test_the_planned_prompt_is_the_reviewer_bundle_for_that_case(self):
        from capsule import bundles
        cases = _cases(("a-planted", "planted"))
        bundle = bundles.reviewer({"id": "t_5eed0001", "title": "t", "body": "do it"}, PATCH)
        row = _run(cases)[2]["cases"][0]["planned"]
        self.assertEqual((row["prompt_bytes"], row["system_bytes"]),
                         (len(bundle["prompt"].encode()), len(bundle["system"].encode())))

    def test_a_rerun_the_same_day_replaces_the_file(self):
        cases = _cases(("a-planted", "planted"))
        out = Path(tempfile.mkdtemp(prefix="rc-out-"))
        for _ in range(2):
            args = ["--cases", str(cases), "--out", str(out), "--date", DATE]
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(rc.main(args), 0)
        self.assertEqual(len(list(out.iterdir())), 1)


class BadCases(unittest.TestCase):
    def _refused(self, cases, needle):
        code, err, result, _ = _run(cases)
        self.assertEqual((code, result), (1, None))
        self.assertIn(needle, err)

    def test_a_bug_line_the_patch_does_not_add_is_refused_and_nothing_is_written(self):
        cases = _cases()
        _case(cases, "a-planted", line=13)
        self._refused(cases, "a.py:13 is not a line the patch adds")

    def test_a_planted_case_without_a_bug_and_a_clean_case_with_one_are_refused(self):
        cases = _cases()
        _case(cases, "a-planted")
        doc = json.loads((cases / "a-planted" / "case.json").read_text())
        del doc["bug"]
        (cases / "a-planted" / "case.json").write_text(json.dumps(doc))
        self._refused(cases, "a planted case names its bug")
        _case(cases, "b-clean", kind="clean", bug={"summary": "x", "locations": []})
        self._refused(cases, "b-clean")

    def test_an_id_that_is_not_the_directory_name_is_refused(self):
        cases = _cases()
        _case(cases, "a-planted", id="other")
        self._refused(cases, "must equal the directory name")

    def test_a_patch_name_that_leaves_the_case_directory_is_refused(self):
        cases = _cases()
        _case(cases, "a-planted", patch="../c.patch")
        self._refused(cases, "a-planted")

    def test_a_missing_schema_or_a_missing_patch_is_refused(self):
        cases = _cases(("a-planted", "planted"))
        (cases / "a-planted" / "c.patch").unlink()
        self._refused(cases, "patch unreadable")
        self._refused(Path(tempfile.mkdtemp()), rc.SCHEMA_FILE)


class Base(unittest.TestCase):
    def _edit_case(self, base):
        cases = _cases()
        _case(cases, "a-planted", line=3, patch=EDIT, base=base)
        return cases

    def test_a_base_the_patch_applies_to_is_accepted(self):
        cases = self._edit_case({"a.py": "l1\nl2\nl3\n"})
        self.assertEqual(_run(cases)[0], 0)

    def test_a_base_the_patch_does_not_apply_to_is_refused(self):
        for base in ({"a.py": "l1\nXX\nl3\n"}, {"a.py": "l1\n"}, {"other.py": "l1\n"}):
            with self.subTest(base):
                code, err, result, _ = _run(self._edit_case(base))
                self.assertEqual((code, result), (1, None))
                self.assertIn("the patch does not apply to base/a.py", err)

    def test_a_case_without_a_base_is_not_checked_for_applying(self):
        self.assertEqual(_run(self._edit_case(None))[0], 0)


class ApplyChecks(unittest.TestCase):
    """`_apply_problems` against hand-made patches, each one a way a patch could pass unchecked."""

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="rc-base-"))
        (self.base / "f.py.base").write_text("a\nb\n")

    def _problems(self, patch):
        return rc._apply_problems(self.base, "--- a/f.py\n+++ b/f.py\n" + patch
                                  if not patch.startswith("---") else patch)

    def test_a_patch_that_matches_the_base_has_no_problem(self):
        self.assertEqual(self._problems("@@ -1,2 +1,3 @@\n a\n b\n+c\n"), [])

    def test_an_empty_context_line_before_line_one_or_past_the_end_is_refused(self):
        for patch in ("@@ -0,1 +0,2 @@\n \n+c\n", "@@ -2,2 +2,3 @@\n b\n \n+c\n"):
            with self.subTest(patch):
                self.assertEqual(len(self._problems(patch)), 1)

    def test_an_added_only_hunk_into_a_file_with_no_base_copy_is_refused(self):
        patch = "--- a/g.py\n+++ b/g.py\n@@ -5,0 +6,1 @@\n+c\n"
        self.assertEqual(len(self._problems(patch)), 1)

    def test_a_header_pair_that_is_dev_null_on_both_sides_is_refused_not_a_crash(self):
        patch = "--- /dev/null\n+++ /dev/null\n@@ -0,0 +1,1 @@\n+c\n"
        self.assertEqual(len(self._problems(patch)), 1)

    def test_a_base_file_that_is_not_utf8_is_refused_not_a_crash(self):
        (self.base / "f.py.base").write_bytes(b"a\n\xff\xfe\n")
        self.assertEqual(len(self._problems("@@ -1,2 +1,3 @@\n a\n b\n+c\n")), 1)

    def test_the_base_copy_is_read_as_utf8_whatever_the_locale(self):
        (self.base / "f.py.base").write_text("\u00e9\nb\n", encoding="utf-8")
        real = Path.read_text
        seen = []

        def spy(path, *a, **kw):
            seen.append(kw.get("encoding"))
            return real(path, *a, **kw)
        with unittest.mock.patch.object(Path, "read_text", spy):
            self._problems("@@ -1,2 +1,3 @@\n \u00e9\n b\n+c\n")
        self.assertEqual(seen, ["utf-8"])

    def test_a_new_file_that_already_has_a_base_copy_is_refused(self):
        patch = "--- /dev/null\n+++ b/f.py\n@@ -0,0 +1,1 @@\n+c\n"
        self.assertEqual(len(self._problems(patch)), 1)

    def test_a_path_that_leaves_base_is_refused_absolute_or_through_a_symlink(self):
        outside = Path(tempfile.mkdtemp(prefix="rc-out-"))
        (outside / "x.py.base").write_text("z\n")
        (self.base / "s.py.base").symlink_to(outside / "x.py.base")
        for name in (f"{outside}/x.py", "a/s.py", "a/../../x.py"):
            patch = f"--- {name}\n+++ b/x.py\n@@ -1,1 +1,1 @@\n-z\n+y\n"
            with self.subTest(name):
                self.assertEqual(len(self._problems(patch)), 1)

    def test_a_removed_line_that_starts_with_two_dashes_is_not_read_as_a_header(self):
        (self.base / "f.py.base").write_text("a\n-- b\n")
        self.assertEqual(self._problems("@@ -1,2 +1,1 @@\n a\n--- b\n"), [])


class Dates(unittest.TestCase):
    def test_a_date_that_is_not_a_calendar_day_is_a_clean_error(self):
        for bad in ("2026-13-01", "tomorrow", ""):
            with self.subTest(bad):
                code, err, result, _ = _run(_cases(("a-planted", "planted")), date=bad)
                self.assertEqual((code, result), (1, None))
                self.assertIn("is not a YYYY-MM-DD date", err)


class Scoring(unittest.TestCase):
    CASES = (("a-planted", "planted"), ("b-planted", "planted"), ("c-clean", "clean"),
             ("d-clean", "clean"))

    def test_catch_and_false_flag_rates_are_the_hand_computed_shares(self):
        verdicts = {"a-planted": _verdict("fail", ("a.py", 10)),      # caught
                    "b-planted": _verdict("fail", ("other.py", 10)),  # wrong file: missed
                    "c-clean": _verdict("fail", ("a.py", 1)),         # false flag
                    "d-clean": _verdict("pass")}                      # ok
        code, _, result, files = _run(_cases(*self.CASES), verdicts)
        self.assertEqual(code, 0)
        self.assertEqual(files[0].name, f"reviewer-calibration-{DATE}.json")
        _check_result(self, result, sorted(c for c, _ in self.CASES), False, 0.5, 0.5)
        outcomes = [c["outcome"] for c in result["cases"]]
        self.assertEqual(outcomes, ["caught", "missed", "false_flag", "ok"])

    def test_the_line_tolerance_edge_and_a_pass_verdict_with_a_finding(self):
        near, far = rc.LINE_TOLERANCE, rc.LINE_TOLERANCE + 1
        verdicts = {"a-planted": _verdict("fail", ("a.py", 10 + near)),
                    "b-planted": _verdict("fail", ("a.py", 10 - far)),
                    "c-clean": _verdict("pass", ("a.py", 1))}         # a finding on a pass: flagged
        result = _run(_cases(*self.CASES[:3]), verdicts)[2]
        outcomes = [c["outcome"] for c in result["cases"]]
        self.assertEqual(outcomes, ["caught", "missed", "false_flag"])

    def test_a_planted_pass_with_the_right_finding_is_missed_and_unscored_cases_are_skipped(self):
        verdicts = {"a-planted": _verdict("pass", ("a.py", 10))}
        result = _run(_cases(*self.CASES), verdicts)[2]
        _check_result(self, result, sorted(c for c, _ in self.CASES), False, 0.0, None)
        summary = result["summary"]
        self.assertEqual((summary["planted_scored"], summary["clean_scored"]), (1, 0))

    def test_every_scored_row_lists_the_findings_that_decided_it(self):
        verdicts = {"a-planted": _verdict("fail", ("a.py", 12), ("a.py", 1)),   # 12 matches, 1 not
                    "b-planted": _verdict("pass", ("a.py", 10)),
                    "c-clean": _verdict("fail", ("a.py", 1), ("b.py", 2)),
                    "d-clean": _verdict("pass")}
        rows = _run(_cases(*self.CASES), verdicts)[2]["cases"]
        self.assertEqual([[(f["file"], f["line"]) for f in r["matched"]] for r in rows],
                         [[("a.py", 12)], [("a.py", 10)], [("a.py", 1), ("b.py", 2)], []])
        self.assertEqual(set(rows[2]["matched"][0]), {"file", "line", "class", "text"})
        self.assertIsNone(_run(_cases(*self.CASES))[2]["cases"][0]["matched"])

    def test_paths_are_compared_without_a_leading_dot_slash_a_or_b(self):
        verdicts = {n: _verdict("fail", (path, 10)) for n, path in
                    (("a-planted", "./a.py"), ("b-planted", "b/a.py"), ("c-planted", "a/a.py"),
                     ("d-planted", "xa.py"))}
        cases = _cases(*[(n, "planted") for n in verdicts])
        outcomes = [c["outcome"] for c in _run(cases, verdicts)[2]["cases"]]
        self.assertEqual(outcomes, ["caught", "caught", "caught", "missed"])

    def test_a_file_level_finding_at_line_zero_never_catches(self):
        cases = _cases()
        _case(cases, "a-planted", line=1)
        (cases / rc.SCHEMA_FILE).write_text(json.dumps(SCHEMA))
        result = _run(cases, {"a-planted": _verdict("fail", ("a.py", 0))})[2]
        self.assertEqual(result["cases"][0]["outcome"], "missed")

    def test_a_finding_in_an_unlisted_file_does_not_catch_even_on_the_right_line(self):
        result = _run(_cases(("a-planted", "planted")),
                      {"a-planted": _verdict("fail", ("tests/test_a.py", 10))})[2]
        self.assertEqual(result["cases"][0]["outcome"], "missed")

    def test_verdicts_that_match_no_case_or_the_wrong_task_or_role_are_refused(self):
        cases = _cases(("a-planted", "planted"))
        wrong_task, wrong_role = _verdict("fail", ("a.py", 10)), _verdict("fail", ("a.py", 10))
        wrong_task["task_id"] = "t_00000000"
        wrong_role["role"] = "security"
        for verdicts, needle in (({"nope": _verdict("pass")}, "nope: no such case"),
                                 ({"a-planted": wrong_task}, "for task t_5eed0001"),
                                 ({"a-planted": wrong_role}, "must be a reviewer verdict"),
                                 ([_verdict("pass")], "must be a JSON object")):
            with self.subTest(needle):
                code, err, result, _ = _run(cases, verdicts)
                self.assertEqual((code, result), (1, None))
                self.assertIn(needle, err)

    def test_a_verdict_that_breaks_review_verdict_v1_is_refused(self):
        bad = _verdict("fail")
        bad["verdict"] = "maybe"
        code, err, result, _ = _run(_cases(("a-planted", "planted")), {"a-planted": bad})
        self.assertEqual((code, result), (1, None))
        self.assertIn("a-planted", err)


def _replay(example):
    """One intent example through the real runner; returns what `expect` should say."""
    given = example["given"]["document"]
    if given["check"] == "planted-verdict":
        cases = _cases(("a-planted", "planted"))
        verdict = _verdict(given["verdict"], (given["finding_file"], given["finding_line"]))
        row = _run(cases, {"a-planted": verdict})[2]["cases"][0]
        return {"outcome": row["outcome"]}
    cases = _cases(("a-clean", "clean"))
    result = _run(cases, {"a-clean": _verdict("fail", ("a.py", 1))})[2]
    return {"outcome": result["cases"][0]["outcome"],
            "false_flag_rate": result["summary"]["false_flag_rate"]}


class IntentRows(unittest.TestCase):
    def test_every_intent_example_replays(self):
        """Z1: `contracts/intent/reviewer_calibration.jsonl`, each example through the runner."""
        examples = [json.loads(ln) for ln in INTENT.read_text().splitlines() if ln.strip()]
        self.assertEqual(len(examples), 3)
        for ex in examples:
            self.assertEqual(_replay(ex), ex["expect"], ex["id"])


if __name__ == "__main__":
    unittest.main()
