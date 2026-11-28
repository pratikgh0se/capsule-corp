"""tests/select_tests.py and run_shell_suites.sh --only: which tests a diff needs (D273).

Each case builds a tiny repo layout in a temp dir and asserts the selection, so a regression in
the mapping (a missed importer, a silent no-test pass) fails here; FULL is the safe fallback.
Zenkai Z1: contracts/intent/select_tests.jsonl is replayed below (class Intent)."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

try:
    from tests import select_tests as st
except ImportError:    # `discover -s tests`: maintenance/tests took the name `tests`
    sys.modules.pop("tests", None)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from tests import select_tests as st

ROOT = Path(__file__).resolve().parents[1]
INTENT = ROOT / "contracts" / "intent" / "select_tests.jsonl"


def layout(files):
    tmp = tempfile.TemporaryDirectory()
    for name, text in files.items():
        p = Path(tmp.name) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return tmp


BASE = {
    "capsule/foo.py": "X = 1\n",
    "capsule/bar.py": "Y = 1\n",
    "capsule/chain_a.py": "Z = 1\n",
    "capsule/chain_b.py": "from capsule import chain_a\n",
    "capsule/lonely.py": "W = 1\n",
    "capsule/tests/test_foo_extra.py": "import unittest\n",
    "capsule/tests/test_importer.py": "from capsule import bar\n",
    "capsule/tests/test_chain.py": "import capsule.chain_b\n",
    "maintenance/tool.py": "T = 1\n",
    "maintenance/tests/test_tool_use.py": "import tool\n",
    "maintenance/run_x.sh": "echo x\n",
    "tests/test_wrapper.sh": "bash maintenance/run_x.sh\n",
    "tests/test_other.sh": "echo other\n",
    "capsule/tests/__init__.py": "from . import tmpclean\n",
    "capsule/tests/tmpclean.py": "T = 1\n",
    "capsule/rel_a.py": "X = 1\n",
    "capsule/rel_b.py": "from .rel_a import X\n",
    "capsule/tests/test_rel.py": "import capsule.rel_b\n",
    "capsule/modrun.py": "M = 1\n",
    "tests/test_cli_wrap.sh": "python3 -m capsule.modrun\n",
    "tests/test_docread.sh": "cat docs/READ.md\n",
    "docs/READ.md": "read by a test\n",
    "docs/NOTES.md": "read by nobody\n",
    "maintenance/data.sql": "select 1;\n",
    "maintenance/templates/keeper_brief.md": "brief\n",
    "docs/ZENKAI.md": "rules\n",
}


class Select(unittest.TestCase):
    def pick(self, *changed, **kw):
        with layout(BASE) as d:
            return st._select(d, list(changed), **kw)

    def test_same_name_test(self):
        self.assertEqual(self.pick("capsule/foo.py"), ["capsule/tests/test_foo_extra.py"])

    def test_importing_test(self):
        self.assertEqual(self.pick("capsule/bar.py"), ["capsule/tests/test_importer.py"])

    def test_importer_of_an_importer(self):
        self.assertEqual(self.pick("capsule/chain_a.py"), ["capsule/tests/test_chain.py"])

    def test_script_imported_by_bare_name(self):
        self.assertEqual(self.pick("maintenance/tool.py"), ["maintenance/tests/test_tool_use.py"])

    def test_changed_test_runs_itself(self):
        self.assertEqual(self.pick("capsule/tests/test_importer.py"),
                         ["capsule/tests/test_importer.py"])

    def test_shell_script_is_full(self):
        self.assertIsNone(self.pick("maintenance/run_x.sh"))

    def test_changed_shell_suite_is_full(self):
        self.assertIsNone(self.pick("tests/test_other.sh"))

    def test_relative_import_is_followed(self):
        self.assertEqual(self.pick("capsule/rel_a.py"), ["capsule/tests/test_rel.py"])

    def test_module_imported_by_an_init_is_full(self):
        self.assertIsNone(self.pick("capsule/tests/tmpclean.py"))

    def test_python_change_selects_the_suite_that_runs_the_module(self):
        self.assertEqual(self.pick("capsule/modrun.py"), ["tests/test_cli_wrap.sh"])

    def test_sql_template_and_unknown_types_are_full(self):
        for f in ("maintenance/data.sql", "maintenance/templates/keeper_brief.md",
                  "maintenance/data.toml", "tests/run_shell_suites.sh", "docs/ZENKAI.md",
                  "capsule/lander_select.py"):
            self.assertIsNone(self.pick(f), f)

    def test_doc_a_test_mentions_is_full_and_one_nobody_mentions_is_not(self):
        self.assertIsNone(self.pick("docs/READ.md"))
        self.assertEqual(self.pick("docs/NOTES.md"), ["tests/test_docread.sh"])

    def test_union_and_prose_ignored(self):
        got = self.pick("capsule/foo.py", "capsule/bar.py", "docs/NOTES.md")
        self.assertEqual(got, ["capsule/tests/test_foo_extra.py", "capsule/tests/test_importer.py",
                               "tests/test_docread.sh"])

    def test_prose_only_selects_the_tests_that_read_docs(self):
        self.assertEqual(self.pick("docs/NOTES.md"), ["tests/test_docread.sh"])

    def test_shared_helper_is_full(self):
        self.assertIsNone(self.pick("capsule/paths.py"))

    def test_contracts_and_init_are_full(self):
        self.assertIsNone(self.pick("contracts/intent/x.jsonl"))
        self.assertIsNone(self.pick("capsule/tests/__init__.py"))

    def test_unmapped_file_is_full(self):
        self.assertIsNone(self.pick("capsule/lonely.py"))
        self.assertIsNone(self.pick("deploy.cfg"))

    def test_too_many_files_is_full(self):
        self.assertIsNone(self.pick("capsule/foo.py", "capsule/bar.py", max_files=1))

    def test_ids(self):
        self.assertEqual(st._to_ids(["capsule/tests/test_a.py", "tests/test_b.sh"]),
                         ["capsule.tests.test_a", "tests/test_b.sh"])


class Cli(unittest.TestCase):
    def run_cli(self, files, change):
        with layout(files) as d:
            def git(*a):
                subprocess.run(["git", "-C", d, "-c", "user.name=t", "-c", "user.email=t@t.invalid",
                                *a], check=True, capture_output=True)
            git("init", "-q")
            git("add", "-A")
            git("commit", "-qm", "base")
            base = subprocess.run(["git", "-C", d, "rev-parse", "HEAD"], capture_output=True,
                                  text=True).stdout.strip()
            for name, text in change.items():
                (Path(d) / name).write_text(text)
            git("add", "-A")
            git("commit", "-qm", "change")
            out = subprocess.run([sys.executable, str(ROOT / "tests/select_tests.py"), "--repo", d,
                                  "--base", base, "--head", "HEAD"], capture_output=True, text=True)
            self.assertEqual(out.returncode, 0, out.stderr)
            return out.stdout.split()

    def test_diff_selects_the_test(self):
        got = self.run_cli(BASE, {"capsule/bar.py": "Y = 2\n"})
        self.assertEqual(got, ["capsule.tests.test_importer"])

    def test_diff_with_helper_prints_full(self):
        got = self.run_cli(BASE, {"capsule/paths.py": "P = 1\n"})
        self.assertEqual(got, ["FULL"])

    def test_bad_base_prints_full(self):
        with layout(BASE) as d:
            subprocess.run(["git", "-C", d, "init", "-q"], check=True)
            out = subprocess.run([sys.executable, str(ROOT / "tests/select_tests.py"), "--repo", d,
                                  "--base", "nope", "--head", "nope"], capture_output=True,
                                 text=True)
        self.assertEqual(out.stdout.split(), ["FULL"])


class Intent(unittest.TestCase):
    """Z1: every row of contracts/intent/select_tests.jsonl, through the real selector."""

    def test_rows(self):
        rows = [json.loads(line) for line in INTENT.read_text().splitlines() if line.strip()]
        self.assertEqual(len(rows), 3)   # Z1: one to three examples
        for row in rows:
            doc, want = row["given"]["document"], row["expect"]["selected"]
            with layout(BASE) as d:
                got = st._select(d, doc["changed"])
            got = "FULL" if got is None else st._to_ids(got) or "NONE"
            self.assertEqual(got, want, row["id"])


class ShellOnly(unittest.TestCase):
    def test_only_runs_the_named_suites(self):
        with layout({"a/test_one.sh": "echo one\n", "a/test_two.sh": "exit 1\n"}) as d:
            one = f"{d}/a/test_one.sh"
            run = subprocess.run(["bash", str(ROOT / "tests/run_shell_suites.sh"), "--dir",
                                  f"{d}/a", "--only", one], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stdout)
        self.assertIn("PASS", run.stdout)
        self.assertNotIn("test_two", run.stdout)


if __name__ == "__main__":
    unittest.main()
