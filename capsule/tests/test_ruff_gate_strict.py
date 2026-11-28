"""Zenkai Z1/Z2 for the strict tier of tests/ruff_gate.py (M096, D212): strict ruff rules block
only on lines a range adds or changes. Oracle 1 runs the gate through its command line on a
fixture repo; oracle 2 runs real ruff (same pin, select and ignores) on the whole file at base
and at head and diffs the finding sets keyed on (code, source-line text). Both must agree."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))


def _load(name):
    """tests/ is a package now (tmpclean __init__), so simplicity rule 1 no longer counts its
    scripts as bare repo modules; load by path, registered so ruff_gate's `import size_check`
    resolves to the same module."""
    spec = importlib.util.spec_from_file_location(name, ROOT / "tests" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


from capsule.tests.public_tree import needs_ruff  # noqa: E402

size_check = _load("size_check")
ruff_gate = _load("ruff_gate")

GATE = ROOT / "tests" / "ruff_gate.py"
OLD_E741 = "def old():\n    l = 1\n    return l\n"
BARE_EXCEPT = "def f():\n    try:\n        return 1\n    except:\n        pass\n"
CLEAN_FN = "\n\ndef clean(a):\n    return a + 1\n"
TEST_ASSERT = ("import unittest\n\n\nclass T(unittest.TestCase):\n    def test_a(self):\n"
               "        assert 1\n        self.assertEqual(1, 1)\n")
GIT_STATUS = 'import subprocess\n\n\ndef f():\n    subprocess.run(["git", "status"])\n'
MUTABLE = "def f(a=[]):\n    return a\n"


def git(cwd, *args):
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert p.returncode == 0, f"git {args} failed: {p.stdout}{p.stderr}"
    return p.stdout.strip()


def commit(root, files):
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "c")
    return git(root, "rev-parse", "HEAD")


def real_findings(source, path):
    """Whole-file strict findings from real ruff as {(code, source-line text)}."""
    r = subprocess.run([*size_check.ruff_cmd(), "check", "--isolated", "--select",
                        ruff_gate.STRICT_SELECT, "--ignore", ruff_gate.STRICT_IGNORE,
                        "--config", ruff_gate.STRICT_CONFIG, "--output-format", "json",
                        "--stdin-filename", path, "-"], input=source, capture_output=True,
                       text=True, timeout=60)
    assert r.returncode in (0, 1), r.stderr
    lines = source.splitlines()
    return {(d["code"], lines[d["location"]["row"] - 1]) for d in json.loads(r.stdout)}


@needs_ruff
class StrictTier(unittest.TestCase):
    def run_case(self, path, before, after, old_path=None):
        """Return (gate rc, gate output, oracle-2 rc) for base=`before`, head=`after`; with
        `old_path` the head renames that file to `path`."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            git(root, "init", "-q")
            git(root, "config", "user.email", "t@t")
            git(root, "config", "user.name", "t")
            base = commit(root, {"README": "x\n"} if before is None else
                          {old_path or path: before})
            if old_path:
                git(root, "mv", old_path, path)
            head = commit(root, {path: after})
            p = subprocess.run([sys.executable, str(GATE), "--base", base, "--head", head,
                                "--repo", str(root)], capture_output=True, text=True)
        new = real_findings(after, path) - (real_findings(before, path) if before else set())
        return p.returncode, p.stdout, 1 if new else 0

    def check(self, path, before, after, rc, needle=None, old_path=None):
        got, out, oracle = self.run_case(path, before, after, old_path)
        self.assertEqual(got, rc, out)
        self.assertEqual(oracle, rc, "second oracle (real ruff base/head diff) disagrees")
        if needle:
            self.assertIn(needle, out)

    def test_added_bare_except_fails_naming_line(self):
        self.check("a.py", OLD_E741, OLD_E741 + "\n\n" + BARE_EXCEPT, 1, "a.py:9 E722")

    def test_added_shell_true_fails(self):
        old = "import subprocess\n\n\n" + OLD_E741
        new = old + "\n\ndef g(x):\n    subprocess.run(x, shell=True)\n"
        self.check("a.py", old, new, 1, "a.py:10 S602")

    def test_old_finding_untouched_plus_clean_addition_passes(self):
        self.check("a.py", OLD_E741, OLD_E741 + CLEAN_FN, 0)

    def test_test_file_assert_and_unittest_style_pass(self):
        self.check("tests/test_a.py", None, TEST_ASSERT, 0)

    def test_list_form_subprocess_passes(self):
        self.check("a.py", None, GIT_STATUS, 0)

    def test_new_file_mutable_default_fails(self):
        self.check("a.py", None, MUTABLE, 1, "a.py:1 B006")

    def test_one_line_modification_is_a_changed_line(self):
        self.check("a.py", "def f(a=None):\n    return a\n", MUTABLE, 1, "a.py:1 B006")

    def test_pure_deletion_has_no_changed_lines(self):
        self.check("a.py", OLD_E741 + CLEAN_FN, OLD_E741, 0)

    def test_rename_of_file_with_old_finding_passes(self):
        self.check("b.py", OLD_E741, OLD_E741, 0, old_path="a.py")

    def test_noqa_suppresses(self):
        self.check("a.py", None, 'def f():\n    return eval("1")  # noqa: S307\n', 0)

    def test_nested_tests_helper_may_assert(self):
        self.check("capsule/tests/helpers.py", None, "def check(x):\n    assert x\n", 0)

    def test_git_failure_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            git(d, "init", "-q")
            p = subprocess.run([sys.executable, str(GATE), "--base", "nope", "--head", "nada",
                                "--repo", d], capture_output=True, text=True)
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn("git diff failed", p.stdout)

    def test_py_marked_nodiff_via_gitattributes_is_still_linted(self):
        with tempfile.TemporaryDirectory() as d:
            base, head = self.repo_range(
                Path(d), {"README": "x\n"},
                {".gitattributes": "*.py -diff\n", "m.py": "def f():\n    return missing\n"})
            p = self.gate(d, base, head)
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn("F821", p.stdout)

    def test_existing_py_marked_binary_gets_added_finding(self):
        with tempfile.TemporaryDirectory() as d:
            base, head = self.repo_range(
                Path(d), {".gitattributes": "m.py binary\n", "m.py": "def f():\n    return 1\n"},
                {"m.py": "def f():\n    return 1\n\n\ndef g():\n    return missing\n"})
            p = self.gate(d, base, head)
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn("F821", p.stdout)

    def repo_range(self, root, base_files, head_files):
        git(root, "init", "-q")
        git(root, "config", "user.email", "t@t")
        git(root, "config", "user.name", "t")
        return commit(root, base_files), commit(root, head_files)

    def gate(self, root, base, head, env=None):
        return subprocess.run([sys.executable, str(GATE), "--base", base, "--head", head,
                               "--repo", str(root)], capture_output=True, text=True, env=env)

    def test_added_header_lookalike_line_does_not_recredit_hunks(self):
        x = "def f():\n    return 1\n\n\n\ndef h():\n    return 2\n"
        spoof = 'DOC = """\n++ b/other.py\n"""\n'
        with tempfile.TemporaryDirectory() as d:
            base, head = self.repo_range(
                Path(d), {"x.py": x, "other.py": "def keep(a=[]):\n    return a\n"},
                {"x.py": spoof + x + "\n\ndef g(a=[]):\n    return a\n"})
            p = self.gate(d, base, head)
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn("x.py:", p.stdout)
        self.assertIn("B006", p.stdout)
        self.assertNotIn("other.py:", p.stdout)

    def test_quoted_path_is_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            base, head = self.repo_range(Path(d), {"README": "x\n"}, {'q"uote.py': "x = 1\n"})
            p = self.gate(d, base, head)
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn("unsupported", p.stdout)

    def test_syntax_error_is_left_to_tier_one(self):
        with tempfile.TemporaryDirectory() as d:
            base, head = self.repo_range(Path(d), {"README": "x\n"}, {"a.py": "def f(:\n"})
            p = self.gate(d, base, head)
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn("invalid-syntax", p.stdout)
        self.assertNotIn("did not parse", p.stdout)

    def run_fake_ruff(self, script):
        """The gate with a fake `ruff` first on PATH; `script` is its shell body."""
        with tempfile.TemporaryDirectory() as d:
            bindir = Path(d) / "bin"
            bindir.mkdir()
            fake = bindir / "ruff"
            fake.write_text("#!/bin/sh\n" + script)
            fake.chmod(0o755)
            (Path(d) / "r").mkdir()
            base, head = self.repo_range(Path(d) / "r", {"README": "x\n"}, {"a.py": "x = 1\n"})
            env = dict(os.environ, PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}")
            return self.gate(Path(d) / "r", base, head, env)

    def test_ruff_exit_two_fails_in_tier_one(self):
        p = self.run_fake_ruff("echo boom >&2\nexit 2\n")
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn("ruff exited 2", p.stdout)

    def test_ruff_exit_two_fails_in_strict_tier(self):
        p = self.run_fake_ruff('case "$*" in *E9,F63*) exit 0;; esac\nexit 2\n')
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn("ruff exited 2", p.stdout)

    def test_unparseable_strict_output_fails(self):
        p = self.run_fake_ruff('case "$*" in *E9,F63*) exit 0;; esac\necho garbage\nexit 1\n')
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn("did not parse", p.stdout)

    def test_undefined_name_is_printed_once(self):
        _, out, _ = self.run_case("a.py", None, "def f():\n    return nope\n")
        self.assertEqual(out.count("F821"), 1, out)

    def test_message_header_counts_findings(self):
        _, out, _ = self.run_case("a.py", None, MUTABLE)
        self.assertTrue(re.match(r"ruff_gate: 1 finding\(s\) on changed lines:\n", out), out)


if __name__ == "__main__":
    unittest.main()
