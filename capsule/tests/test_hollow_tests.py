"""D85: the warn-only hollow-test detector (capsule/hollow_tests.py) and the reviewer bundle
section that shows it (bundles.reviewer). Behaviour contract: a test that checks nothing is NAMED
on one advisory line; a real assertion is clean; the section is advisory and can never stop a
bundle being built.

The sample sources below are strings; the tautology and skip spellings are built from pieces so
the repo's own static test-honesty flags do not read them as this file's tests."""
import time
import unittest
from unittest import mock

from capsule import bundles, hollow_tests as ht

ASSERT_TRUE = "self.assert" + "True(True)"
SKIP_DECO = "@unittest." + "skip('later')"
HEAD = "import unittest\nclass T(unittest.TestCase):\n"


def _line(path, text):
    return ht.hollow_test_lines([path], lambda p: text)


class PythonTests(unittest.TestCase):
    def _first(self, body):
        return _line("tests/test_a.py", HEAD + body)[0]

    def test_a_test_with_no_assertion_is_warned(self):
        line = self._first("    def test_x(self):\n        y = 1 + 1\n")
        self.assertTrue(line.startswith("hollow-tests: advisory - 1 test(s)"), line)
        self.assertIn("tests/test_a.py::test_x` (no assertion)", line)

    def test_assert_true_only_is_warned(self):
        for stmt in ("assert True", ASSERT_TRUE):
            line = self._first(f"    def test_x(self):\n        {stmt}\n")
            self.assertIn("test_x` (constant assertions only)", line)

    def test_is_not_none_on_a_just_constructed_object_is_warned(self):
        for stmt in ("self.assertIsNotNone(o)", "assert o is not None"):
            line = self._first(f"    def test_x(self):\n        o = Thing()\n        {stmt}\n")
            self.assertIn("constant assertions only", line)

    def test_is_not_none_on_a_function_result_is_a_real_check(self):
        for stmt in ("self.assertIsNotNone(o)", "assert o is not None"):
            body = f"    def test_x(self):\n        o = load_thing()\n        {stmt}\n"
            self.assertEqual(self._first(body), "hollow-tests: ok", stmt)

    def test_pass_and_skip_bodies_are_warned(self):
        self.assertIn("test_x` (empty body)", self._first("    def test_x(self):\n        pass\n"))
        skip = self._first("    def test_x(self):\n        self.skipTest('later')\n")
        self.assertIn("test_x` (skipped)", skip)
        body = f"    {SKIP_DECO}\n    def test_x(self):\n        self.assertEqual(f(), 1)\n"
        self.assertIn("test_x` (skipped)", self._first(body))

    def test_a_snapshot_only_test_is_warned(self):
        out = _line("tests/test_a.py", "def test_x(snapshot):\n    snapshot.assert_match(f())\n")
        self.assertIn("test_x` (snapshot-only)", out[0])

    def test_real_assertions_are_clean(self):
        bodies = (
            "    def test_x(self):\n        self.assertEqual(f(), 2)\n",
            "    def test_x(self):\n        with self.assertRaises(ValueError):\n            f()\n",
            "    def test_x(self):\n        assert f() == 2\n",
            "    def test_x(self):\n        o = Thing()\n        self.assertIsNotNone(o)\n"
            "        self.assertEqual(o.n, 1)\n",
            "    def test_x(self):\n        with pytest.raises(ValueError):\n            f()\n",
        )
        for body in bodies:
            self.assertEqual(self._first(body), "hollow-tests: ok", body)

    def test_helpers_that_are_not_tests_are_ignored(self):
        self.assertEqual(self._first("    def helper(self):\n        pass\n"), "hollow-tests: ok")

    def test_a_call_to_a_same_file_asserting_helper_counts_as_an_assertion(self):
        src = ("def _expect_ok(x):\n    assert x == 1\n\n"
               "def check_two(x):\n    assert x == 2\n\n"
               "def test_a():\n    _expect_ok(f())\n\n"
               "class T:\n    def test_b(self):\n        check_two(f())\n"
               "    def test_c(self):\n        self._expect_ok(f())\n"
               "    def test_d(self):\n        unrelated(f())\n")
        line = _line("tests/test_a.py", src)[0]
        self.assertIn("1 test(s)", line)
        self.assertIn("::test_d` (no assertion)", line)

    def test_a_helper_without_a_real_assertion_does_not_launder_the_test(self):
        src = "def helper():\n    pass\n\ndef test_a():\n    helper()\n"
        self.assertIn("::test_a` (no assertion)", _line("tests/test_a.py", src)[0])

    def test_a_leading_underscore_assert_helper_from_another_file_counts(self):
        src = "def test_a():\n    self._assert_clean(f())\n"
        self.assertEqual(_line("tests/test_a.py", src), ["hollow-tests: ok"])

    def test_an_invalid_escape_is_not_a_warning_leak(self):
        import warnings
        src = "def test_a():\n    assert re.match('\\d', '1')\n"
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            self.assertEqual(_line("tests/test_a.py", src), ["hollow-tests: ok"])
        self.assertEqual([w for w in caught if "invalid escape" in str(w.message)], [])


class SwiftTests(unittest.TestCase):
    PATH = "Tests/AppTests/ATests.swift"

    def test_xctassert_is_clean_and_none_is_warned(self):
        clean = "final class A: XCTestCase {\n  func testX() { XCTAssertEqual(f(), 2) }\n}\n"
        bare = "final class A: XCTestCase {\n  func testX() { let v = f() }\n}\n"
        self.assertEqual(_line(self.PATH, clean), ["hollow-tests: ok"])
        self.assertIn("ATests.swift::testX` (no assertion)", _line(self.PATH, bare)[0])

    def test_constant_empty_skip_and_snapshot_swift_tests_are_warned(self):
        src = ("final class A: XCTestCase {\n"
               "  func testTrue() { XCTAssertTrue(true) }\n"
               "  func testNew() { let v = View(); XCTAssertNotNil(v) }\n"
               "  func testEmpty() {}\n"
               "  func testSkip() throws { throw XCTSkip(\"no\") }\n"
               "  func testSnap() { assertSnapshot(of: v, as: .image) }\n"
               "  func testReal() { XCTAssertEqual(1, f()) }\n"
               "  func helper() {}\n}\n")
        line = _line("Tests/ATests.swift", src)[0]
        for name, why in (("testTrue", "constant assertions only"),
                          ("testNew", "constant assertions only"),
                          ("testEmpty", "empty body"), ("testSkip", "skipped")):
            self.assertIn(f"::{name}` ({why})", line)
        self.assertIn("(+1 more)", line)     # four shown; the snapshot-only one is the fifth
        self.assertNotIn("testReal", line)
        self.assertNotIn("helper", line)

    def test_xctassertnotnil_on_a_function_result_is_a_real_check(self):
        src = "func testX() { let v = load(); XCTAssertNotNil(v) }\n"
        self.assertEqual(_line("Tests/ATests.swift", src), ["hollow-tests: ok"])

    def test_swift_testing_attribute_functions_are_read(self):
        src = ("@Test func good() { #expect(f() == 1) }\n@Test func bad() { let a = 1 }\n"
               "@Test(\"named\")\nfunc bad2() { let a = 1 }\n")
        line = _line("Tests/ATests.swift", src)[0]
        self.assertIn("::bad` (no assertion)", line)
        self.assertIn("::bad2` (no assertion)", line)
        self.assertNotIn("good", line)

    def test_a_call_to_a_same_file_asserting_helper_counts_as_an_assertion(self):
        src = ("func assertSorted(_ x: [Int]) { XCTAssertEqual(x, x.sorted()) }\n"
               "func testA() { assertSorted(f()) }\n"
               "func testB() { other(f()) }\n")
        line = _line("Tests/ATests.swift", src)[0]
        self.assertIn("1 test(s)", line)
        self.assertIn("::testB` (no assertion)", line)

    def test_braces_inside_a_triple_quoted_string_do_not_end_the_body(self):
        src = ('func testA() {\n  let s = """\n  } }\n  """\n  XCTAssertEqual(s.count, 3)\n}\n'
               'func testB() {\n  let s = """\n  { {\n  """\n}\n')
        line = _line("Tests/ATests.swift", src)[0]
        self.assertIn("1 test(s)", line)
        self.assertIn("::testB` (no assertion)", line)


class WebTests(unittest.TestCase):
    def test_it_with_expect_is_clean_and_empty_is_warned(self):
        clean = 'describe("x", () => {\n  it("adds", () => { expect(add(1, 1)).toBe(2); });\n});\n'
        empty = 'describe("x", () => {\n  it("adds", () => {});\n});\n'
        self.assertEqual(_line("web/add.test.ts", clean), ["hollow-tests: ok"])
        self.assertIn("web/add.test.ts::adds` (empty body)", _line("web/add.test.ts", empty)[0])

    def test_no_expect_constant_skip_and_snapshot_web_tests_are_warned(self):
        src = ('it("no expect", () => { const a = f(); });\n'
               'it("trivial", () => { expect(true).toBe(true); });\n'
               'it("new", () => { const w = new Widget(); expect(w).toBeDefined(); });\n'
               'it.skip("later", () => { expect(f()).toBe(1); });\n'
               'test("snap", async ({ page }) => {\n'
               '  expect(await page.content()).toMatchSnapshot();\n});\n'
               'it("real", () => expect(f()).toBe(1));\n')
        line = _line("a.spec.js", src)[0]
        self.assertIn("5 test(s)", line)
        for name, why in (("no expect", "no assertion"), ("trivial", "constant assertions only"),
                          ("new", "constant assertions only"), ("later", "skipped")):
            self.assertIn(f"::{name}` ({why})", line)
        self.assertNotIn("real", line)

    def test_defined_on_a_function_result_is_a_real_check(self):
        src = 'it("x", () => { const w = makeWidget(); expect(w).toBeDefined(); });\n'
        self.assertEqual(_line("a.test.js", src), ["hollow-tests: ok"])

    def test_a_title_that_says_expect_does_not_count_as_an_assertion(self):
        src = 'it("should expect(x) to be 1", function () { doIt(); });\n'
        self.assertIn("(no assertion)", _line("a.test.js", src)[0])

    def test_supertest_expect_and_regex_test_calls(self):
        src = 'it("api", () => { request(app).get("/").expect(200); });\n'
        src += 'const ok = /a/.test("a");\n'
        self.assertEqual(_line("a.test.js", src), ["hollow-tests: ok"])

    def test_expect_and_assert_families_count(self):
        src = ('it("a", () => { expectTypeOf(x).toBeString(); });\n'
               'it("b", () => { assertEquals(f(), 1); });\n'
               'it("c", () => { assert.strictEqual(f(), 1); });\n'
               'it("d", () => { expect.assertions(1); });\n')
        line = _line("a.test.ts", src)[0]
        self.assertIn("1 test(s)", line)
        self.assertIn("::d` (no assertion)", line)

    def test_a_call_to_a_same_file_asserting_helper_counts_as_an_assertion(self):
        src = ('function checkTotal(x) { expect(x).toBe(3); }\n'
               'const verify = (x) => { expect(x).toBe(4); };\n'
               'it("a", () => { checkTotal(f()); });\n'
               'it("b", () => { verify(f()); });\n'
               'it("c", () => { other(f()); });\n')
        line = _line("a.test.js", src)[0]
        self.assertIn("1 test(s)", line)
        self.assertIn("::c` (no assertion)", line)

    def test_braces_in_a_regex_literal_do_not_end_the_body(self):
        src = ('it("a", () => {\n  const re = /[{}]+\\{/;\n  expect(re.test(s)).toBe(true);\n});\n'
               'it("b", () => {\n  const re = /\\{\\{/;\n});\n')
        line = _line("a.test.js", src)[0]
        self.assertIn("1 test(s)", line)
        self.assertIn("::b` (no assertion)", line)

    def test_division_is_not_read_as_a_regex(self):
        src = 'it("a", () => { const r = a / b; expect(r).toBe(1); const q = c / d; });\n'
        self.assertEqual(_line("a.test.js", src), ["hollow-tests: ok"])


class JsxTests(unittest.TestCase):
    def test_jsx_tags_are_not_regex_literals(self):
        src = ('it("a", () => {\n  render(<><Do /><Runner /></>, root);\n'
               '  expect(root.textContent).toBe("x");\n});\n')
        self.assertEqual(_line("a.test.tsx", src), ["hollow-tests: ok"])
        src = 'it("b", () => { render(<div>{x}</div>); expect(y).toBe(1); });\n'
        self.assertEqual(_line("a.test.tsx", src), ["hollow-tests: ok"])

    def test_a_hollow_test_after_jsx_is_still_found(self):
        src = ('it("a", () => { render(<A />); });\n'
               'it("b", () => { render(<div>{x}</div>); expect(y).toBe(1); });\n')
        line = _line("a.test.tsx", src)[0]
        self.assertIn("1 test(s)", line)
        self.assertIn("::a` (no assertion)", line)


class Reporting(unittest.TestCase):
    def test_no_test_file_changed_adds_no_line(self):
        self.assertEqual(ht.hollow_test_lines(["server.py", "README.md"], lambda p: "x"), [])

    def test_more_than_four_findings_are_counted(self):
        src = "".join(f"def test_{i}():\n    pass\n" for i in range(6))
        line = _line("tests/test_a.py", src)[0]
        self.assertIn("6 test(s)", line)
        self.assertIn("(+2 more)", line)

    def test_an_unparseable_or_unreadable_file_is_skipped_not_raised(self):
        self.assertEqual(_line("tests/test_a.py", "def test_(:\n"), ["hollow-tests: ok"])

        def boom(p):
            raise OSError("gone")
        self.assertEqual(ht.hollow_test_lines(["tests/test_a.py"], boom), ["hollow-tests: ok"])

    def test_a_file_over_256_kb_is_skipped(self):
        big = "def test_a():\n    pass\n" + "# pad\n" * (ht.MAX_BYTES // 6 + 1)
        self.assertEqual(_line("tests/test_a.py", big), ["hollow-tests: ok"])


class Performance(unittest.TestCase):
    """H1: no input shape makes a scan slow (a Swift regex once ran past 300 s)."""

    def _fast(self, path, text):
        start = time.perf_counter()
        ht._SCANNERS[ht._kind(path)](text)      # the scanner itself, past the size cap
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 1.0, f"{path}: {elapsed:.2f}s")

    def test_a_100_kb_string_literal(self):
        self._fast("Tests/ATests.swift", 'func testX() { let s = "' + "a" * 100_000 + '" }\n')
        self._fast("a.test.js", 'it("x", () => { const s = "' + "a" * 100_000 + '"; });\n')

    def test_thirty_double_spaced_attributes(self):
        self._fast("Tests/ATests.swift", "@Foo(x)  \n\n  " * 30 + "func testX() {}\n")
        self._fast("Tests/ATests.swift", "@Foo  " * 30 + "func testX() {}\n")
        self._fast("Tests/ATests.swift", "@Foo(x)\n" * 30_000 + "func testX() {}\n")

    def test_twenty_thousand_brace_less_func_lines(self):
        self._fast("Tests/ATests.swift", "func testF() -> Int\n" * 20_000)

    def test_five_thousand_unclosed_bodies(self):
        self._fast("Tests/ATests.swift", "func testX() {\n" * 5_000)
        self._fast("a.test.js", 'it("x", () => {\n' * 5_000)
        self._fast("a.test.js", 'it("x", function () { if (a) {\n' * 5_000)


    def test_long_lines_with_unclosed_brackets_and_comments(self):
        self._fast("a.test.js", "x = a / b[" * 8_000 + "\n")
        self._fast("a.test.js", "/* x\n" * 8_000)
        self._fast("a.test.js", "/*" * 20_000)
        self._fast("Tests/ATests.swift", "/*" * 20_000)


class ReviewerBundle(unittest.TestCase):
    TASK = {"id": "t_1", "title": "T", "body": "b"}
    DIFF = ("diff --git a/capsule/x.py b/capsule/x.py\n+++ b/capsule/x.py\n"
            "diff --git a/capsule/tests/test_x.py b/capsule/tests/test_x.py\n+++ b\n")
    HOLLOW = "def test_x():\n    pass\n"

    def _prompt(self, text, ws="/clone", diff=None, **kw):
        with mock.patch("capsule.workspace.read_at", side_effect=lambda w, rev, p: text(p)):
            return bundles.reviewer(self.TASK, diff or self.DIFF, workspace_dir=ws, **kw)["prompt"]

    def test_a_hollow_changed_test_gets_an_advisory_section_next_to_the_diff(self):
        prompt = self._prompt(lambda p: self.HOLLOW)
        self.assertIn("## Hollow tests (advisory)", prompt)
        self.assertIn("capsule/tests/test_x.py::test_x` (empty body)", prompt)
        self.assertLess(prompt.index("## Hollow tests (advisory)"), prompt.index("## Diff"))

    def test_the_section_sits_after_host_only_checks(self):
        prompt = self._prompt(lambda p: self.HOLLOW, host_only=["no docker"])
        self.assertLess(prompt.index("## Host-only checks"), prompt.index("## Hollow tests"))

    def test_the_text_is_read_from_the_card_commit_in_the_clone(self):
        with mock.patch("capsule.workspace.read_at", return_value=self.HOLLOW) as read_at:
            bundles.reviewer(self.TASK, self.DIFF, workspace_dir="/clone")
        read_at.assert_called_with("/clone", "HEAD", "capsule/tests/test_x.py")

    def test_a_clean_test_adds_no_section(self):
        prompt = self._prompt(lambda p: "def test_x():\n    assert f() == 1\n")
        self.assertNotIn("Hollow tests", prompt)

    def test_no_clone_means_no_section_and_no_read(self):
        with mock.patch("capsule.workspace.read_at") as read_at:
            prompt = bundles.reviewer(self.TASK, self.DIFF)["prompt"]
            read_at.assert_not_called()
        self.assertNotIn("Hollow tests", prompt)

    def test_any_error_emits_nothing_and_the_bundle_is_still_built(self):
        def boom(p):
            raise RuntimeError("git exploded")
        self.assertNotIn("Hollow tests", self._prompt(boom))
        with mock.patch("capsule.hollow_tests.hollow_test_lines", side_effect=ValueError("x")):
            out = bundles.reviewer(self.TASK, self.DIFF, workspace_dir="/clone")
        self.assertIn("## Diff", out["prompt"])
        self.assertNotIn("Hollow tests", out["prompt"])

    def test_a_non_ascii_or_spaced_changed_path_is_found(self):
        diff = ('diff --git "a/capsule/tests/test_caf\\303\\251.py" '
                '"b/capsule/tests/test_caf\\303\\251.py"\n+++ b\n'
                "diff --git a/capsule/tests/test a b.py b/capsule/tests/test a b.py\n+++ b\n")
        self.assertEqual(bundles._changed_files(diff),
                         ["capsule/tests/test_caf\u00e9.py", "capsule/tests/test a b.py"])
        seen = []
        self._prompt(lambda p: seen.append(p) or self.HOLLOW, diff=diff)
        # the spaced path is no test file, so only the first is read
        self.assertEqual(seen, ["capsule/tests/test_caf\u00e9.py"])

    def test_changed_files_keeps_its_plain_behaviour(self):
        self.assertEqual(bundles._changed_files(self.DIFF),
                         ["capsule/x.py", "capsule/tests/test_x.py"])


if __name__ == "__main__":
    unittest.main()
