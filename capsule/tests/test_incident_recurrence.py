"""Korin closure-gate runners (row 456): capsule/incident_recurrence.py. Temp git repos only."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from capsule import incident_recurrence as rec
from capsule import incidents


class RecurrenceTest(unittest.TestCase):
    """The real runner, on a temp git repo: branch check, exit code AND the Ran count."""

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.repo, True)
        (self.repo / "probe").mkdir()
        (self.repo / "probe" / "__init__.py").write_text("")
        (self.repo / "probe" / "test_probe.py").write_text(
            "import unittest\nclass P(unittest.TestCase):\n    def test_ok(self):\n        pass\n"
            "    def test_bad(self):\n        self.fail('boom')\n")
        for args in (["init", "-q", "-b", "main"], ["add", "."], ["commit", "-qm", "init"]):
            subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                            *args], cwd=self.repo, check=True, capture_output=True)

    def test_pass_fail_missing_and_wrong_branch(self):
        ok, _ = rec.run_recurrence_test("probe.test_probe.P.test_ok", self.repo, timeout=60)
        self.assertTrue(ok)
        ok, detail = rec.run_recurrence_test("probe.test_probe.P.test_bad", self.repo, timeout=60)
        self.assertFalse(ok)
        self.assertIn("boom", detail)
        ok, _ = rec.run_recurrence_test("probe.test_probe.P.test_nope", self.repo, timeout=60)
        self.assertFalse(ok)
        ok, detail = rec.run_recurrence_test("rm -rf /", self.repo, timeout=60)
        self.assertFalse(ok)
        subprocess.run(["git", "checkout", "-qb", "side"], cwd=self.repo, check=True)
        ok, detail = rec.run_recurrence_test("probe.test_probe.P.test_ok", self.repo, timeout=60)
        self.assertFalse(ok)
        self.assertIn("not main", detail)
        self.assertFalse((self.repo / "probe" / "__pycache__").exists())

    def _suite(self, name, body):
        (self.repo / "tests").mkdir(exist_ok=True)
        (self.repo / "tests" / name).write_text(body)

    def test_shell_suite_ids_need_exit_zero_a_pass_line_and_no_fail_line(self):
        good = ('check() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; fi; }\n'
                'check "one thing" true\n')
        self._suite("test_good.sh", good)
        self._suite("test_bad.sh", good + 'check "two thing" false\n')
        self._suite("test_silent.sh", "exit 0\n")
        self._suite("test_exit.sh", 'echo "PASS a"\nexit 3\n')
        self._suite("test_docker.sh", "#!/bin/bash\n# needs: docker\necho 'PASS a'\n")
        def run(test_id):
            return rec.run_recurrence_test(test_id, self.repo, timeout=60)
        self.assertTrue(run("tests/test_good.sh")[0])
        self.assertTrue(run("tests/test_good.sh::one thing")[0])
        self.assertFalse(run("tests/test_good.sh::absent label")[0])
        self.assertFalse(run("tests/test_bad.sh")[0])
        self.assertFalse(run("tests/test_silent.sh")[0])
        self.assertFalse(run("tests/test_exit.sh")[0])
        ok, why = run("tests/test_docker.sh")
        self.assertFalse(ok)
        self.assertIn("needs", why)
        ok, why = run("tests/test_missing.sh")
        self.assertFalse(ok)
        self.assertIn("not on main", why)
        self.assertFalse(run("tests/../x.sh")[0])

    def test_shell_id_regex_and_schema_pattern_agree(self):
        import re
        root = Path(rec.__file__).resolve().parent.parent
        schema = json.loads((root / "contracts" / "schemas" / "postmortem.v1.json").read_text())
        pat = re.compile(schema["properties"]["recurrence_test"]["pattern"])
        for good in ("tests/test_lib_compose.sh", "tests/test_lib_compose.sh::helper resolves",
                     "pkg.test_m.C.test_x"):
            self.assertTrue(rec.TEST_ID_RE.match(good), good)
            self.assertTrue(pat.match(good), good)
        for bad in ("tests/test_x.sh::", 'tests/test_x.sh::a"b', "tests/x.sh",
                    "tests/test_x.sh::a\nb", "../tests/test_x.sh", "tests/test_x.py", "rm -rf /"):
            self.assertFalse(rec.TEST_ID_RE.match(bad), bad)
            self.assertFalse(pat.match(bad), bad)


class RecurrenceIsNew(unittest.TestCase):
    """I4: the recurrence test must not exist on main when the incident opened, but must now."""

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.repo, True)
        (self.repo / "pkg" / "tests").mkdir(parents=True)
        for f in ("pkg/__init__.py", "pkg/tests/__init__.py"):
            (self.repo / f).write_text("")
        (self.repo / "pkg" / "tests" / "test_old.py").write_text(
            "import unittest\nclass Old(unittest.TestCase):\n    def test_there(self):\n"
            "        pass\n")
        self.git("init", "-q", "-b", "main")
        self.commit("init")
        self.open_sha = incidents.main_head(self.repo)

    def git(self, *args):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                        *args], cwd=self.repo, check=True, capture_output=True)

    def commit(self, msg):
        self.git("add", ".")
        self.git("commit", "-qm", msg)

    def test_an_old_test_or_a_missing_one_is_refused_a_new_one_passes(self):
        ok, why = rec.recurrence_test_is_new(
            "pkg.tests.test_old.Old.test_there", self.repo, self.open_sha)
        self.assertFalse(ok)
        self.assertIn("already on main", why)
        ok, why = rec.recurrence_test_is_new(
            "pkg.tests.test_new.New.test_guard", self.repo, self.open_sha)
        self.assertFalse(ok)
        self.assertIn("not on main", why)
        (self.repo / "pkg" / "tests" / "test_new.py").write_text(
            "import unittest\nclass New(unittest.TestCase):\n    def test_guard(self):\n"
            "        pass\n")
        self.commit("fix + recurrence test")
        ok, why = rec.recurrence_test_is_new(
            "pkg.tests.test_new.New.test_guard", self.repo, self.open_sha)
        self.assertTrue(ok, why)
        ok, why = rec.recurrence_test_is_new("pkg.tests.test_new.New.test_guard", self.repo, None)
        self.assertFalse(ok)

    def test_shell_label_is_new_only_if_the_literal_check_is_absent_at_open_and_present_now(self):
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "test_s.sh").write_text('check "old one" true\n')
        self.commit("suite")
        opened = incidents.main_head(self.repo)
        is_new = rec.recurrence_test_is_new
        ok, why = is_new("tests/test_s.sh::old one", self.repo, opened)
        self.assertFalse(ok)
        self.assertIn("already on main", why)
        ok, why = is_new("tests/test_s.sh::new one", self.repo, opened)
        self.assertFalse(ok)
        self.assertIn("not on main", why)
        both = 'check "old one" true\ncheck "new one" true\n'
        (self.repo / "tests" / "test_s.sh").write_text(both)
        (self.repo / "tests" / "test_t.sh").write_text('check "x" true\n')
        self.commit("fix")
        self.assertTrue(is_new("tests/test_s.sh::new one", self.repo, opened)[0])
        self.assertTrue(is_new("tests/test_t.sh", self.repo, opened)[0])
        ok, why = is_new("tests/test_s.sh", self.repo, opened)
        self.assertFalse(ok)
        self.assertFalse(is_new("tests/test_s.sh::new one", self.repo, None)[0])
