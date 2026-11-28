"""D85 / D44 intent rows for the hollow-test check: three handlers judge the rows with their own
built-in expectations (ROWS) and a loader test that skips, via self.skipTest in the body, until
contracts/intent/hollow_tests.jsonl ships (then it lints the file and judges its rows too).
Detector behaviour itself is in test_hollow_tests.py."""
import unittest
from unittest import mock

from capsule import bundles, hollow_tests as ht, intent, paths


def _bundle_prompt(paths_in_diff, read):
    diff = "".join(f"diff --git a/{p} b/{p}\n+++ b/{p}\n" for p in paths_in_diff)
    with mock.patch("capsule.workspace.read_at", side_effect=lambda w, rev, p: read(p)):
        task = {"id": "t_1", "title": "T", "body": "b"}
        return bundles.reviewer(task, diff, workspace_dir="/clone")["prompt"]


def _read_failure(path):
    raise RuntimeError("git exploded")


def _judge_bundle_hollow(doc):
    prompt = _bundle_prompt([doc["changed_test"]], lambda p: doc["test_text"])
    return {"section": "## Hollow tests (advisory)" in prompt,
            "named": f"`{doc['changed_test']}::{doc['hollow_name']}`" in prompt,
            "diff_kept": "## Diff" in prompt and f"diff --git a/{doc['changed_test']}" in prompt}


def _judge_bundle_quiet(doc):
    prompts = [_bundle_prompt(["capsule/tests/test_x.py"], lambda p: doc["clean_test"]),
               _bundle_prompt([doc["non_test_file"]], lambda p: doc["clean_test"]),
               _bundle_prompt(["capsule/tests/test_x.py"], _read_failure)]
    return {"sections": sum("Hollow tests" in p for p in prompts),
            "bundles_built": sum("## Diff" in p for p in prompts)}


def _judge_swift_ts(doc):
    def flagged(path, text):
        return ht.hollow_test_lines([path], lambda p: text)[0].startswith("hollow-tests: advisory")
    swift, web = "Tests/ATests.swift", "web/a.test.ts"
    return {"hollow_named": (int(flagged(swift, doc["swift_hollow"]))
                             + int(flagged(web, doc["ts_hollow"]))),
            "clean_named": (sum(flagged(swift, t) for t in doc["swift_clean"])
                            + sum(flagged(web, t) for t in doc["ts_clean"]))}


JUDGES = {"bundle-hollow": _judge_bundle_hollow, "bundle-quiet": _judge_bundle_quiet,
          "swift-ts": _judge_swift_ts}

# D85 / D44 intent rows (source: owner). The same three live in contracts/intent/hollow_tests.jsonl
# once that file ships; until then these built-in copies are what the handlers are judged against.
ROWS = [
    {"id": "reviewer-bundle-names-a-changed-test-with-no-assertion",
     "given": {"document": {"scenario": "bundle-hollow", "changed_test": "capsule/tests/test_x.py",
                            "hollow_name": "test_x",
                            "test_text": "def test_x():\n    y = 1 + 1\n"}},
     "expect": {"section": True, "named": True, "diff_kept": True}, "source": "owner"},
    {"id": "clean-or-non-test-diff-or-read-failure-adds-no-section",
     "given": {"document": {"scenario": "bundle-quiet",
                            "clean_test": "def test_x():\n    assert f() == 1\n",
                            "non_test_file": "README.md"}},
     "expect": {"sections": 0, "bundles_built": 3}, "source": "owner"},
    {"id": "swift-and-ts-tests-without-assertions-are-named-others-not",
     "given": {"document": {
         "scenario": "swift-ts",
         "swift_hollow": "final class A: XCTestCase {\n  func testX() { let v = f() }\n}\n",
         "ts_hollow": "it(\"x\", () => { f(); });\n",
         "swift_clean": ["func testX() { XCTAssertEqual(f(), 1) }\n",
                         "func assertOk(_ v: Int) { XCTAssertEqual(v, 1) }\n"
                         "func testX() { assertOk(f()) }\n"],
         "ts_clean": ["it(\"x\", () => { expect(f()).toBe(1); });\n",
                      "function checkOk(v) { expect(v).toBe(1); }\n"
                      "it(\"x\", () => { checkOk(f()); });\n"]}},
     "expect": {"hollow_named": 2, "clean_named": 0}, "source": "owner"},
]


class IntentRows(unittest.TestCase):
    def _judge_rows(self, rows):
        for ex in rows:
            doc = ex["given"]["document"]
            got = JUDGES[doc["scenario"]](doc)
            self.assertEqual(intent._differs(ex["expect"], got, None), [], ex["id"])

    def test_the_built_in_rows_are_judged_by_the_real_check(self):
        self.assertEqual(len(ROWS), 3)
        self._judge_rows(ROWS)

    def test_a_wrong_expectation_is_caught_by_the_handlers(self):
        wrong = {"id": "x", "expect": {"sections": 1, "bundles_built": 3},
                 "given": ROWS[1]["given"]}
        with self.assertRaises(AssertionError):
            self._judge_rows([wrong])

    def test_the_shipped_rows_lint_and_are_judged_once_they_exist(self):
        path = paths.INTENT / "hollow_tests.jsonl"
        if not path.is_file():
            self.skipTest("contracts/intent/hollow_tests.jsonl is not shipped yet (D85)")
        self.assertIn("hollow_tests", intent.STANDALONE)
        self.assertEqual(intent.lint(path, None, "hollow_tests"), [])
        self.assertEqual(len(intent.load("hollow_tests")), 3)
        self._judge_rows(intent.load("hollow_tests"))


if __name__ == "__main__":
    unittest.main()
