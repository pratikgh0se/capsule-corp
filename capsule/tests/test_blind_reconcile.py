"""R13: the unittest runner's comparison contract (see its docstring) for each ordinary test shape:
the rows, the summary line and the exit code must agree. Shares the runner scaffolding with
test_blind_harness."""
import unittest

from capsule.tests.test_blind_harness import RunnerTest


class Reconcile(RunnerTest):
    """One case per shape the two oracles must still agree on (the runner docstring's contract)."""

    def check(self, body, results, holders=0):
        self.write("test_x.py", body)
        proc, rows = self.runner()
        self.assertEqual(sorted(r["result"] for r in rows), sorted(results), proc.stdout)
        return proc, rows, self.contract(proc, rows, holders)

    def test_three_failing_subtests_are_one_row(self):
        _, _, (ran, status) = self.check("""
            import unittest

            class S(unittest.TestCase):
                def test_sub(self):
                    for i in range(3):
                        with self.subTest(i=i):
                            self.assertEqual(i, -1)
            """, ["fail"])
        self.assertIn("failures=3", status)

    def test_a_failure_plus_a_teardown_error_is_one_error_row(self):
        _, _, (ran, status) = self.check("""
            import unittest

            class T(unittest.TestCase):
                def tearDown(self):
                    raise RuntimeError("td")

                def test_x(self):
                    self.fail("x")
            """, ["error"])
        self.assertIn("failures=1", status)
        self.assertIn("errors=1", status)

    def test_a_setup_class_skip_is_one_holder_row_and_runs_no_test(self):
        _, _, (ran, status) = self.check("""
            import unittest

            class K(unittest.TestCase):
                @classmethod
                def setUpClass(cls):
                    raise unittest.SkipTest("nope")

                def test_x(self):
                    pass

                def test_y(self):
                    pass
            """, ["skipped"], holders=1)
        self.assertEqual(ran, 0)
        self.assertEqual(status, "OK (skipped=1)")

    def test_an_expected_failure_is_a_pass_row_and_the_run_is_ok(self):
        _, _, (ran, status) = self.check("""
            import unittest

            class E(unittest.TestCase):
                @unittest.expectedFailure
                def test_x(self):
                    self.fail("x")
            """, ["pass"])
        self.assertEqual(status, "OK (expected failures=1)")

    def test_an_unexpected_success_is_a_fail_row_and_the_run_fails(self):
        _, _, (ran, status) = self.check("""
            import unittest

            class E(unittest.TestCase):
                @unittest.expectedFailure
                def test_x(self):
                    pass
            """, ["fail"])
        self.assertIn("unexpected successes=1", status)

    def test_a_setup_class_error_and_an_import_error_each_get_a_row(self):
        self.write("test_bad.py", "import no_such_module_anywhere\n")
        _, _, (ran, status) = self.check("""
            import unittest

            class T(unittest.TestCase):
                @classmethod
                def setUpClass(cls):
                    raise RuntimeError("x")

                def test_never_runs(self):
                    pass
            """, ["error", "error"], holders=1)
        self.assertIn("errors=2", status)

    def test_two_subtest_skips_are_one_skipped_row_but_skipped_2_in_the_summary(self):
        _, _, (ran, status) = self.check("""
            import unittest

            class S(unittest.TestCase):
                def test_sub(self):
                    for i in range(2):
                        with self.subTest(i=i):
                            self.skipTest("later")
            """, ["skipped"])
        self.assertEqual(status, "OK (skipped=2)")

    def test_a_subtest_skip_plus_a_failure_is_a_fail_row_and_no_skipped_row(self):
        _, _, (ran, status) = self.check("""
            import unittest

            class S(unittest.TestCase):
                def test_sub(self):
                    with self.subTest("a"):
                        self.skipTest("later")
                    with self.subTest("b"):
                        self.fail("x")
            """, ["fail"])
        self.assertIn("skipped=1", status)

    def test_a_skipped_test_and_a_passing_one(self):
        self.check("""
            import unittest

            class T(unittest.TestCase):
                @unittest.skip("later")
                def test_a(self):
                    pass

                def test_b(self):
                    pass
            """, ["skipped", "pass"])


if __name__ == "__main__":
    unittest.main()
