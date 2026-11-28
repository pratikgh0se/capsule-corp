"""Row 632 (host-exec fix, second half): the lander's gate scripts, the local modules they import,
the size baseline, deploy.sh and the unblocked hook run as the lander or as the owner. A worker that
could edit them could switch a check off or run code on the host; only the CoS or the owner may."""
import ast
import json
import unittest

from capsule import lander, paths, workspace

CHECK_PATHS = ["tests/mutation_check.py", "tests/size_check.py", "tests/simplicity_check.py",
               "tests/ruff_gate.py", "tests/portability_lint.py",
               "tests/size_baseline.py", "tests/size_baseline.json",
               "tests/discover_tmp.py", "tests/mutation_newsymbol.py",
               "tests/mutation_contractdata.py", "tests/mutation_info.py",
               "tests/mutation_alias.py", "tests/mutation_literals.py", "tests/mutation_env.py",
               "deploy.sh", "bin/capsule-unblocked-hook.sh"]


def configured_gate_scripts():
    """Every `tests/<name>.py` the lander runs: its built-in GATES and the example board's gates."""
    example = paths.REPO / "capsule" / "config.capsule-corp.example.json"
    gates = list(lander.GATES) + json.loads(example.read_text())["lander"]["gates"]
    return sorted({lander.GATE.match(g).group(1) for g in gates})


def imported_tests_files(script):
    """The `tests/*.py` files `script` imports at any depth (module or function level)."""
    names = []
    for node in ast.walk(ast.parse((paths.REPO / script).read_text())):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
    local = [f"tests/{n.split('.')[0]}.py" for n in names]
    return [p for p in local if (paths.REPO / p).is_file()]


def local_imports(script):
    """`imported_tests_files`, followed transitively."""
    seen, todo = set(), [script]
    while todo:
        for local in imported_tests_files(todo.pop()):
            if local not in seen:
                seen.add(local)
                todo.append(local)
    return sorted(seen)


class LanderChecksProtected(unittest.TestCase):
    def test_a_worker_write_to_each_check_path_is_refused(self):
        for path in CHECK_PATHS:
            self.assertEqual(workspace.touches_protected([path, "capsule/tick.py"]), [path], path)

    def test_every_configured_gate_script_is_protected(self):
        scripts = configured_gate_scripts()
        self.assertIn("tests/portability_lint.py", scripts)   # from the example board config
        self.assertIn("tests/mutation_check.py", scripts)     # from lander.GATES
        self.assertEqual(workspace.touches_protected(scripts), scripts)

    def test_every_local_import_of_every_gate_script_is_protected(self):
        for script in configured_gate_scripts():
            for module in local_imports(script):
                self.assertEqual(workspace.touches_protected([module]), [module],
                                 f"{script} imports {module}, which a worker could rewrite")

    def test_other_tests_files_and_look_alikes_stay_writable(self):
        neighbours = ["tests/test_deploy.sh", "tests/test_argparse_usage.py",
                      "tests/new_helper.py", "tests/portability_lint.py.bak",
                      "tests/mutation_check.py.bak", "tests/size_baseline.jsonl",
                      "deploy.sh.bak", "bin/capsule-unblocked-hook.sh.bak", "bin/capsule-own",
                      "capsule/tests/test_workspace.py", "capsule/json.py"]
        self.assertEqual(workspace.touches_protected(neighbours), [])

    def test_a_tests_entry_named_after_a_stdlib_module_is_refused(self):
        shadows = ["tests/argparse.py", "tests/json/__init__.py", "tests/re.cpython-313.pyc"]
        self.assertEqual(workspace.touches_protected(shadows + ["tests/new_helper.py"]), shadows)


if __name__ == "__main__":
    unittest.main()
