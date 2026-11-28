"""Zenkai Z8 (docs/ZENKAI.md) measured rules in tests/simplicity_check.py: one must-fail and
one must-pass fixture per rule, each run through the real check() on a fresh fixture repo."""
import ast
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import simplicity_check as simp  # noqa: E402


def git(cwd, *args):
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert p.returncode == 0, f"git {args} failed: {p.stdout}{p.stderr}"
    return p.stdout


def commit_all(root, message):
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", message)
    return git(root, "rev-parse", "HEAD").strip()


def make_repo():
    root = Path(tempfile.mkdtemp())
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    return root


LONG = "x = 0\ny = '" + "a" * 120 + "'\n"
DEEP = ("x = 0\ndef f(v):\n    for a in v:\n        for b in a:\n            if b:\n"
        "                while b:\n                    b -= 1\nf(0)\nf(1)\n")
FLAT = ("x = 0\ndef f(v):\n    for a in v:\n        for b in a:\n            if b:\n"
        "                b -= 1\nf(0)\nf(1)\n")
LONG_TRY = "x = 0\ntry:\n" + "    x += 1\n" * 6
# (rule, must-fail head source, must-pass head source, ruff code that also flags it or "");
# base is always "x = 0\n". S110 flags only broad handlers, hence the Exception row.
CASES = [
    (7, LONG, "x = 0\ny = 'short'\n", "E501"),
    (7, "x = 0\na = 1; b = 2\n", "x = 0\na = 1\nb = 2\n", "E702"),
    (3, DEEP, FLAT, "PLR1702"),
    (5, "x = 0\ny = eval('1')\n", "x = 0\ny = int('1')\n", "S307"),
    (5, "x = 0\nexec('y = 1')\n", "x = 0\ny = 1\n", "S102"),
    (5, "x = 0\nimport importlib\nm = importlib.import_module('os')\n",
        "x = 0\nimport os\n", ""),
    (5, "x = 0\nname = 'a'\ny = getattr(object, name)\n",
        "x = 0\ny = getattr(object, 'a', 1)\n", ""),
    (5, "x = 0\nsq = lambda v: v * v\n",
        "x = 0\ndef sq(v):\n    return v * v\nsq(1)\nsq(2)\n", "E731"),
    (5, "x = 0\nimport os\nos.sep = '/'\n", "x = 0\nimport os\nsep = os.sep\n", ""),
    (5, "x = 0\nclass M(type):\n    pass\nclass A(metaclass=M):\n    pass\n",
        "x = 0\nclass A:\n    pass\nA()\nA()\n", ""),
    (6, "x = 0\ntry:\n    x = 1\nexcept:\n    x = 2\n",
        "x = 0\ntry:\n    x = 1\nexcept ValueError:\n    x = 2\n", "E722"),
    (6, "x = 0\ntry:\n    x = 1\nexcept ValueError:\n    pass\n",
        "x = 0\ntry:\n    x = 1\nexcept ValueError:\n    raise\n", ""),
    (6, LONG_TRY + "except ValueError:\n    x = 2\n",
        LONG_TRY + "except ValueError as e:\n    raise RuntimeError(x) from e\n", ""),
    (6, "x = 0\ntry:\n    x = 1\nexcept Exception:\n    pass\n",
        "x = 0\ntry:\n    x = 1\nexcept Exception:\n    raise\n", "S110"),
]


def run_case(head_src):
    root = make_repo()
    (root / "m.py").write_text("x = 0\n")
    base = commit_all(root, "base")
    (root / "m.py").write_text(head_src)
    head = commit_all(root, "head")
    return simp.check(str(root), base, head)


class MeasuredRules(unittest.TestCase):
    def test_each_rule_fails_its_bad_fixture_and_passes_its_good_one(self):
        for rule, bad, good, _ruff in CASES:
            with self.subTest(rule=rule, bad=bad[:40]):
                ok, report = run_case(bad)
                self.assertFalse(ok, report)
                self.assertIn(f"rule {rule}:", report)
                ok, report = run_case(good)
                self.assertTrue(ok, report)

    def test_untouched_old_violation_is_not_flagged(self):
        root = make_repo()
        (root / "m.py").write_text("y = '" + "a" * 120 + "'\n")
        base = commit_all(root, "base with a long line")
        (root / "m.py").write_text("y = '" + "a" * 120 + "'\nz = 1\n")
        head = commit_all(root, "add a short line")
        ok, report = simp.check(str(root), base, head)
        self.assertTrue(ok, report)

    def moved(self, extra):
        root = make_repo()
        (root / "old").mkdir()
        (root / "new").mkdir()
        (root / "old" / "m.py").write_text(DEEP + LONG)
        base = commit_all(root, "base with old-style code")
        git(root, "mv", "old/m.py", "new/m.py")
        if extra:
            (root / "new" / "m.py").write_text(DEEP + LONG + extra)
        return simp.check(str(root), base, commit_all(root, "move"))

    def test_a_pure_move_of_old_style_code_passes(self):
        ok, report = self.moved("")
        self.assertTrue(ok, report)

    def test_a_move_plus_a_new_bad_function_still_fails_on_that_function(self):
        ok, report = self.moved("def g(v):\n    return eval(v)\ng(1)\ng(2)\n")
        self.assertFalse(ok, report)
        self.assertIn("rule 5:", report)
        self.assertNotIn("rule 7:", report)

    def test_compound_one_liner_is_not_rule_7(self):
        ok, report = run_case("x = 0\nif x: y = 1\n")
        self.assertTrue(ok, report)

    def test_method_calls_named_exec_or_eval_are_not_rule_5(self):
        ok, report = run_case("x = 0\nrunner.exec('c', 'true', 5)\ny = x.eval()\n")
        self.assertTrue(ok, report)

    def test_elif_chain_is_not_nesting(self):
        src = ("x = 0\ndef f(v):\n    if v == 1:\n        return 1\n    elif v == 2:\n"
               "        return 2\n    elif v == 3:\n        for a in v:\n            if a:\n"
               "                return a\n    return 0\nf(0)\nf(1)\n")
        ok, report = run_case(src)
        self.assertTrue(ok, report)

    # Row 597: rule 3 flags a deep statement only when the branch added a header above it.
    OLD_DEEP = ("x = 0\ndef f(v):\n    for a in v:\n        with a:\n            for b in a:\n"
                "                if b:\n                    b -= 1\nf(0)\nf(1)\n")

    def test_one_line_edit_inside_old_deep_code_is_not_rule_3(self):
        head = self.OLD_DEEP.replace("b -= 1\n", "b -= 1\n                    b += 2\n", 1)
        ok, report = run_files({"m.py": self.OLD_DEEP}, {"m.py": head})
        self.assertTrue(ok, report)

    def test_added_header_above_a_deep_statement_is_rule_3(self):
        base = ("x = 0\ndef f(v):\n    for a in v:\n        for b in a:\n            if b:\n"
                "                b -= 1\nf(0)\nf(1)\n")
        head = base.replace("                b -= 1\n",
                            "                while b:\n                    b -= 1\n")
        ok, report = run_files({"m.py": base}, {"m.py": head})
        self.assertFalse(ok, report)
        self.assertIn("rule 3:", report)

    # Row 605: a new else/except/finally/case clause on an old header is a level the branch made.
    CLAUSES = {
        "else": ("if b:\n    b -= 1\n", "else:\n    # why\n    b += 1\n    b += 2\n"),
        "except": ("try:\n    b -= 1\nexcept ValueError:\n    b = 0\n",
                   "except TypeError:\n    b += 1\n    b += 2\n"),
        "finally": ("try:\n    b -= 1\nexcept ValueError:\n    b = 0\n",
                    "finally:\n    b += 1\n    b += 2\n"),
        "case": ("match b:\n    case 1:\n        b -= 1\n",
                 "    case 2:\n        b += 1\n        b += 2\n"),
    }

    def clause_src(self, kind, with_new):
        old, new = self.CLAUSES[kind]
        body = old + (new if with_new else "")
        pad = "    for a in v:\n        with a:\n            for b in a:\n"
        text = "".join("                " + ln + "\n" for ln in body.splitlines())
        return f"x = 0\ndef f(v):\n{pad}{text}f(0)\nf(1)\n"

    def test_a_new_clause_on_an_old_deep_header_is_rule_3(self):
        for kind in self.CLAUSES:
            with self.subTest(kind=kind):
                ok, report = run_files({"m.py": self.clause_src(kind, False)},
                                       {"m.py": self.clause_src(kind, True)})
                self.assertFalse(ok, report)
                self.assertIn("rule 3: nesting depth 4", report)

    def test_an_edit_inside_an_old_clause_is_not_rule_3(self):
        base = self.clause_src("else", True)
        head = base.replace("b += 2\n", "b += 2\n                b += 3\n")
        ok, report = run_files({"m.py": base}, {"m.py": head})
        self.assertTrue(ok, report)

    def test_a_statement_named_like_a_clause_is_not_its_header(self):
        for kind, name in (("else", "elsewhere"), ("finally", "finally_flag")):
            with self.subTest(kind=kind):
                base = self.clause_src(kind, True)
                pad = " " * 20
                head = base.replace(f"{pad}b += 1\n", f"{pad}{name} = 1\n{pad}b += 1\n", 1)
                self.assertNotEqual(base, head)
                ok, report = run_files({"m.py": base}, {"m.py": head})
                self.assertTrue(ok, report)

    def test_try_star_handler_counts_without_ruff(self):
        src = ("def f(v):\n    for a in v:\n        with a:\n            for b in a:\n"
               "                try:\n                    b -= 1\n"
               "                except* ValueError:\n                    b += 1\n")
        found = simp._deep_findings(ast.parse(src), {7, 8}, src.splitlines())
        self.assertEqual(found, [(8, 3, "nesting depth 4")])

    def test_an_added_with_counts_toward_depth(self):
        base = ("x = 0\ndef f(v):\n    for a in v:\n        for b in a:\n            if b:\n"
                "                b -= 1\nf(0)\nf(1)\n")
        head = base.replace("                b -= 1\n",
                            "                with b:\n                    b -= 1\n")
        ok, report = run_files({"m.py": base}, {"m.py": head})
        self.assertFalse(ok, report)
        self.assertIn("rule 3:", report)


class Oracles(unittest.TestCase):
    def test_ruff_and_ast_both_record_a_long_line(self):
        ok, report = run_case(LONG)
        self.assertFalse(ok)
        self.assertIn("[ast, hard]", report)
        self.assertIn("[ruff, hard]", report)

    def test_the_ast_oracle_alone_catches_every_bad_fixture(self):
        for rule, bad, _good, _ruff in CASES:
            with self.subTest(rule=rule, bad=bad[:40]):
                _ok, report = run_case(bad)
                rows = [r for r in report.splitlines() if f"rule {rule}:" in r]
                self.assertTrue(any("[ast, hard]" in r for r in rows), report)

    def test_the_ruff_oracle_alone_catches_every_bad_fixture_it_covers(self):
        for rule, bad, _good, code in CASES:
            if not code:
                continue
            with self.subTest(rule=rule, code=code):
                _ok, report = run_case(bad)
                rows = [r for r in report.splitlines() if f"rule {rule}:" in r]
                self.assertTrue(any(f"{code} [ruff, hard]" in r for r in rows), report)

    def test_unparseable_file_fails_closed(self):
        ok, report = run_case("x = 0\ndef broken(:\n")
        self.assertFalse(ok)
        self.assertIn("m.py", report)

    def test_no_ruff_fails_closed(self):
        bare = Path(tempfile.mkdtemp())
        (bare / "git").symlink_to(shutil.which("git"))
        root = make_repo()
        (root / "m.py").write_text("x = 0\n")
        base = commit_all(root, "base")
        (root / "m.py").write_text("x = 0\ny = 1\n")
        head = commit_all(root, "head")
        argv = [sys.executable, str(Path(simp.__file__)), "--repo", str(root),
                "--base", base, "--head", head]
        env = {**os.environ, "PATH": str(bare)}
        p = subprocess.run(argv, env=env, capture_output=True, text=True)
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn("ruff", p.stdout)


def run_files(base_files, head_files):
    root = make_repo()
    for name in {**base_files, **head_files}:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
    for name, src in base_files.items():
        (root / name).write_text(src)
    base = commit_all(root, "base")
    for name, src in head_files.items():
        (root / name).write_text(src)
    head = commit_all(root, "head")
    return simp.check(str(root), base, head)


def run_move(old, new, src):
    root = make_repo()
    (root / old).parent.mkdir(parents=True, exist_ok=True)
    (root / new).parent.mkdir(parents=True, exist_ok=True)
    (root / old).write_text(src)
    base = commit_all(root, "base")
    git(root, "mv", old, new)
    return simp.check(str(root), base, commit_all(root, "head"))


class RenameGate(unittest.TestCase):
    def test_txt_renamed_to_py_with_a_bad_function_fails(self):
        ok, report = run_move("notes.txt", "m.py", DEEP)
        self.assertFalse(ok, report)

    def test_test_file_moved_into_production_fails_rule_2(self):
        ok, report = run_move("tests/test_m.py", "m.py", ONCE)
        self.assertFalse(ok, report)
        self.assertIn("rule 2: once", report)

    def test_prod_to_prod_py_move_still_passes(self):
        ok, report = run_move("m.py", "n.py", DEEP)
        self.assertTrue(ok, report)


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "simplicity"


def replay(fixture):
    """check() over a fresh repo built from a spec section 5 input excerpt: a `# source:`
    line, then `--- file: <path> ---` / base excerpt / `--- head ---` / head excerpt triples."""
    text = (FIXTURES / f"{fixture}.txt").read_text()
    files = {}
    for chunk in text.split("--- file: ")[1:]:
        name, rest = chunk.split(" ---\n", 1)
        base_src, _, head_src = rest.partition("--- head ---\n")
        files[name] = (base_src, head_src)
    return run_files({n: b for n, (b, _) in files.items() if b},
                     {n: h for n, (_, h) in files.items()})


ONCE = "def once():\n    return 1\n"
CALL_ONCE = "from m import once\nonce()\n"
EMPTY = {"m.py": "x = 0\n", "a.py": "y = 0\n"}


class Imports(unittest.TestCase):
    def test_third_party_import_fails_and_stdlib_and_repo_pass(self):
        ok, report = run_files({"m.py": "x = 0\n"}, {"m.py": "x = 0\nimport requests\n"})
        self.assertFalse(ok)
        self.assertIn("rule 1:", report)
        ok, report = run_files({"m.py": "x = 0\n", "helper.py": "y = 1\n"},
                               {"m.py": "x = 0\nimport json\nimport helper\n"})
        self.assertTrue(ok, report)

    def test_allowlisted_package_passes(self):
        base = {"m.py": "x = 0\n", "deps-allowlist.txt": "pip:requests==2.0.0\n"}
        ok, report = run_files(base, {"m.py": "x = 0\nimport requests\n"})
        self.assertTrue(ok, report)

    def test_hyphenated_pin_matches_its_underscore_module(self):
        base = {"m.py": "x = 0\n", "deps-allowlist.txt": "pip:Typing-Extensions==4.0.0\n"}
        ok, report = run_files(base, {"m.py": "x = 0\nimport typing_extensions\n"})
        self.assertTrue(ok, report)

    def test_npm_pin_and_comment_do_not_allow_an_import(self):
        pins = "# pip:requests==2.0.0\nnpm:requests@1.0.0\nnpm:requests==1.0.0\n"
        ok, report = run_files({"m.py": "x = 0\n", "deps-allowlist.txt": pins},
                               {"m.py": "x = 0\nimport requests\n"})
        self.assertFalse(ok)
        self.assertIn("rule 1: import requests", report)

    def test_from_import_dotted_and_package_forms(self):
        head = {"m.py": "x = 0\nfrom os import path\nimport os.path\nfrom pkg import mod\n"
                        "from . import sibling\n",
                "pkg/__init__.py": "", "pkg/mod.py": "y = 1\n"}
        ok, report = run_files({"m.py": "x = 0\n"}, head)
        self.assertTrue(ok, report)
        ok, report = run_files({"m.py": "x = 0\n"},
                               {"m.py": "x = 0\nfrom requests.auth import HTTPBasicAuth\n"})
        self.assertFalse(ok)
        self.assertIn("rule 1: import requests.auth", report)

    def test_a_module_inside_a_package_does_not_allow_its_bare_name(self):
        head = {"m.py": "x = 0\nimport docker\n", "pkg/__init__.py": "", "pkg/docker.py": "y = 1\n"}
        ok, report = run_files({"m.py": "x = 0\n"}, head)
        self.assertFalse(ok)
        self.assertIn("rule 1: import docker", report)

    def test_a_nested_package_does_not_allow_its_bare_name(self):
        head = {"m.py": "x = 0\nimport inner\n", "pkg/__init__.py": "",
                "pkg/inner/__init__.py": "", "pkg/inner/z.py": "y = 1\n"}
        self.assertFalse(run_files({"m.py": "x = 0\n"}, head)[0])

    def test_a_script_in_a_plain_directory_allows_its_bare_name(self):
        head = {"m.py": "x = 0\nimport helper\n", "tests/helper.py": "y = 1\n"}
        ok, report = run_files({"m.py": "x = 0\n"}, head)
        self.assertTrue(ok, report)

    def test_a_third_party_import_already_at_base_is_not_flagged(self):
        ok, report = run_files({"m.py": "import requests\n"},
                               {"m.py": "import requests\nx = 0\n"})
        self.assertTrue(ok, report)

    def test_import_inside_a_class_body_is_checked(self):
        ok, report = run_files({"m.py": "x = 0\n"},
                               {"m.py": "x = 0\nclass A:\n    import requests\n"})
        self.assertFalse(ok)
        self.assertIn("rule 1:", report)


class Callers(unittest.TestCase):
    def test_one_caller_fails_two_pass(self):
        ok, report = run_files(EMPTY, {"m.py": ONCE, "a.py": CALL_ONCE})
        self.assertFalse(ok)
        self.assertIn("rule 2: once", report)
        self.assertIn("[ast, hard]", report)
        twice = "from m import used\nused()\nused()\n"
        ok, report = run_files(EMPTY, {"m.py": ONCE.replace("once", "used"), "a.py": twice})
        self.assertTrue(ok, report)

    def test_no_caller_fails_and_a_public_class_is_hard(self):
        ok, report = run_files(EMPTY, {"m.py": "class Unused:\n    pass\n"})
        self.assertFalse(ok)
        self.assertIn("rule 2: Unused: 0 caller(s)", report)

    def test_test_callers_do_not_count(self):
        test_src = "from m import once\nonce()\nonce()\n"
        ok, report = run_files({"m.py": "x = 0\n"}, {"m.py": ONCE, "tests/test_m.py": test_src})
        self.assertFalse(ok, report)

    def test_one_caller_in_a_frozen_file_is_exempt(self):
        base = {"m.py": "x = 0\n", "big.py": "y = 0\n",
                "tests/size_baseline.json": '{"big.py::file_length": 900}\n'}
        ok, report = run_files(base, {"m.py": ONCE, "big.py": CALL_ONCE})
        self.assertTrue(ok, report)

    def test_one_caller_in_an_unfrozen_file_is_not_exempt(self):
        base = {"m.py": "x = 0\n", "big.py": "y = 0\n", "a.py": "z = 0\n",
                "tests/size_baseline.json": '{"big.py::file_length": 900}\n'}
        ok, report = run_files(base, {"m.py": ONCE, "a.py": CALL_ONCE})
        self.assertFalse(ok, report)

    def test_function_level_key_exempts_only_a_caller_inside_that_function(self):
        old = "def f():\n    return 1\n\n\ndef g():\n    return 2\n"
        base = {"m.py": "x = 0\n", "big.py": old,
                "tests/size_baseline.json": '{"big.py:f:function_length": 90}\n'}
        call = "from m import once\n    return once()"
        ok, report = run_files(base, {"m.py": ONCE, "big.py": old.replace("return 1", call)})
        self.assertTrue(ok, report)
        ok, report = run_files(base, {"m.py": ONCE, "big.py": old.replace("return 2", call)})
        self.assertFalse(ok, report)

    def test_a_caller_on_the_line_after_the_frozen_function_is_not_exempt(self):
        old = "def f():\n    return 1\ny = 0\n"
        base = {"m.py": "x = 0\n", "big.py": old,
                "tests/size_baseline.json": '{"big.py:f:function_length": 90}\n'}
        after = "def f():\n    return 1\ny = once()\n"
        ok, report = run_files(base, {"m.py": ONCE, "big.py": after})
        self.assertFalse(ok, report)
        last = "def f():\n    return once()\ny = 0\n"
        ok, report = run_files(base, {"m.py": ONCE, "big.py": last})
        self.assertTrue(ok, report)

    def test_file_level_key_exempts_a_caller_in_any_function_of_the_file(self):
        old = "def g():\n    return 2\n"
        base = {"m.py": "x = 0\n", "big.py": old,
                "tests/size_baseline.json": '{"big.py::file_length": 900}\n'}
        src = old.replace("return 2", "from m import once\n    return once()")
        ok, report = run_files(base, {"m.py": ONCE, "big.py": src})
        self.assertTrue(ok, report)

    def test_private_same_file_helper_is_info_not_hard(self):
        src = "def _helper():\n    return 1\n\n\ndef api():\n    return _helper()\n"
        ok, report = run_files(EMPTY, {"m.py": src, "a.py": "from m import api\napi()\napi()\n"})
        self.assertTrue(ok, report)
        self.assertIn("rule 2: _helper", report)
        self.assertIn("[ast, info]", report)

    def test_private_helper_with_no_caller_or_an_outside_caller_is_hard(self):
        ok, report = run_files(EMPTY, {"m.py": "def _dead():\n    return 1\n"})
        self.assertFalse(ok)
        self.assertIn("rule 2: _dead", report)
        ok, report = run_files(EMPTY, {"m.py": "def _once():\n    return 1\n",
                                       "a.py": "from m import _once\n_once()\n"})
        self.assertFalse(ok)
        self.assertIn("[ast, hard]", report)

    def test_a_private_helper_with_one_caller_in_another_file_names_that_caller_and_the_route(
            self):
        ok, report = run_files(EMPTY, {"m.py": "def _once():\n    return 1\n",
                                       "a.py": "from m import _once\n_once()\n"})
        self.assertFalse(ok)
        self.assertIn("[ast, hard]", report)
        self.assertIn("; its one caller is a.py:2, in another file: "
                      "move the def into that file or inline it", report)

    def test_recursion_and_the_def_itself_are_not_callers(self):
        src = "def walk(n):\n    return walk(n - 1)\n"
        ok, report = run_files(EMPTY, {"m.py": src, "a.py": "from m import walk\nwalk(1)\n"})
        self.assertFalse(ok)
        self.assertIn("rule 2: walk: 1 caller(s)", report)

    def test_a_def_already_at_base_is_not_checked(self):
        ok, report = run_files({"m.py": ONCE}, {"m.py": ONCE + "x = 0\n"})
        self.assertTrue(ok, report)

    def test_renamed_function_counts_as_new(self):
        # one caller at base too: the old name was never checked, the renamed one is
        base = {"m.py": ONCE.replace("once", "old"), "a.py": CALL_ONCE.replace("once", "old")}
        ok, report = run_files(base, {"m.py": ONCE.replace("once", "new"),
                                      "a.py": CALL_ONCE.replace("once", "new")})
        self.assertFalse(ok)
        self.assertIn("rule 2: new", report)

    def test_main_and_dunders_are_exempt(self):
        src = ("class A:\n    def __init__(self):\n        self.v = 1\ndef main():\n"
               "    return 0\nif __name__ == '__main__':\n    main()\nA()\nA()\n")
        ok, report = run_files({"m.py": "x = 0\n"}, {"m.py": src})
        self.assertNotIn("rule 2: main", report)
        self.assertNotIn("__init__", report)

    def test_override_with_no_caller_is_reported(self):
        src = "import threading\nclass W(threading.Thread):\n    def run(self):\n        return 1\n"
        ok, report = run_files(EMPTY, {"m.py": src, "a.py": "from m import W\nW()\nW()\n"})
        self.assertIn("rule 2: W.run", report)

    def test_method_called_twice_through_attributes_passes(self):
        src = "class K:\n    def go(self):\n        return 1\n"
        use = "from m import K\nK().go()\nK().go()\n"
        ok, report = run_files(EMPTY, {"m.py": src, "a.py": use})
        self.assertTrue(ok, report)

    def test_report_records_both_oracles(self):
        ok, report = run_files(EMPTY, {"m.py": ONCE, "a.py": CALL_ONCE})
        self.assertRegex(report, r"1 caller\(s\) \(ast\), \d+ mention\(s\) \(grep\)")

    def test_grep_counts_every_non_test_mention_and_skips_tests(self):
        head = {"m.py": ONCE, "a.py": CALL_ONCE,
                "tests/test_m.py": "from m import once\nonce()\nonce()\nonce()\n"}
        ok, report = run_files(EMPTY, head)
        self.assertIn("1 caller(s) (ast), 3 mention(s) (grep)", report)

    def test_public_function_with_one_same_file_caller_is_hard(self):
        src = "def helper():\n    return 1\n\n\ndef api():\n    return helper()\n"
        ok, report = run_files(EMPTY, {"m.py": src, "a.py": "from m import api\napi()\napi()\n"})
        self.assertFalse(ok)
        self.assertIn("rule 2: helper: 1 caller(s) (ast), 2 mention(s) (grep) [ast, hard]", report)

    def test_touching_an_existing_def_does_not_make_it_new(self):
        ok, report = run_files({"m.py": ONCE}, {"m.py": ONCE.replace("once()", "once(a=0)")})
        self.assertTrue(ok, report)

    def test_renaming_a_class_does_not_make_its_untouched_methods_new(self):
        old = "class A:\n    def go(self):\n        return 1\n"
        use = "from m import {0}\n{0}()\n{0}()\n"
        ok, report = run_files({"m.py": old, "a.py": use.format("A")},
                               {"m.py": old.replace("A", "B"), "a.py": use.format("B")})
        self.assertTrue(ok, report)

    def test_grep_counts_whole_words_only(self):
        head = {"m.py": ONCE, "a.py": CALL_ONCE + "once_more = 1\n"}
        ok, report = run_files(EMPTY, head)
        self.assertIn("1 caller(s) (ast), 3 mention(s) (grep)", report)

    def test_new_defs_in_a_test_file_are_not_rule_2(self):
        src = "import unittest\nclass T(unittest.TestCase):\n    def test_x(self):\n        pass\n"
        ok, report = run_files({"tests/test_m.py": "x = 0\n"}, {"tests/test_m.py": src})
        self.assertTrue(ok, report)
        self.assertNotIn("rule 2", report)

    def test_support_modules_under_capsule_and_maintenance_tests_are_exempt(self):
        for d in ("capsule/tests", "maintenance/tests"):
            with self.subTest(d=d):
                use = "from helper import make\nmake()\nmake()\n"
                head = {f"{d}/helper.py": "def make():\n    return 1\n", f"{d}/test_h.py": use}
                ok, report = run_files({f"{d}/helper.py": "x = 0\n"}, head)
                self.assertTrue(ok, report)
                self.assertNotIn("rule 2", report)

    def test_support_module_callers_do_not_count(self):
        head = {"m.py": ONCE, "capsule/tests/oracle.py": "from m import once\nonce()\nonce()\n"}
        ok, report = run_files({"m.py": "x = 0\n", "capsule/tests/oracle.py": "y = 0\n"}, head)
        self.assertFalse(ok)
        self.assertIn("rule 2: once: 0 caller(s)", report)

    def test_top_level_tests_non_test_files_stay_under_rule_2(self):
        head = {"tests/helper.py": "def make():\n    return 1\n"}
        ok, report = run_files({"tests/helper.py": "x = 0\n"}, head)
        self.assertFalse(ok)
        self.assertIn("rule 2: make", report)

    def test_a_changed_test_named_file_is_exempt_wherever_is_test_file_says(self):
        ok, report = run_files({"tests/test_a.py": "x = 0\n"},
                               {"tests/test_a.py": "def helper():\n    return 1\n"})
        self.assertTrue(ok, report)


    def test_grep_mentions_skip_support_modules_too(self):
        head = {"m.py": ONCE, "a.py": CALL_ONCE, "capsule/tests/oracle.py": "once = 1\n"}
        ok, report = run_files({"m.py": "x = 0\n", "a.py": "y = 0\n"}, head)
        self.assertIn("1 caller(s) (ast), 3 mention(s) (grep)", report)


class FailClosed(unittest.TestCase):
    def test_an_unrelated_unparsable_file_at_head_fails_the_check(self):
        base = {"m.py": "x = 0\n", "other.py": "def broken(:\n"}
        ok, report = run_files(base, {"m.py": "x = 0\nz = 1\n"})
        self.assertFalse(ok)
        self.assertIn("other.py", report)


class TestPathModulePatch(unittest.TestCase):
    """Row 592: rule 5's module-attribute sub-check is not applied under the test trees."""
    PATCH = "import tempfile\ntempfile.tempdir = None\n"

    def test_module_attribute_patch_in_test_paths_passes_and_in_production_blocks(self):
        for path in ("capsule/tests/tmpclean.py", "maintenance/tests/seam.py",
                     "tests/test_seam.py"):
            with self.subTest(path=path):
                ok, report = run_files({path: "x = 0\n"}, {path: "x = 0\n" + self.PATCH})
                self.assertTrue(ok, report)
        ok, report = run_files({"capsule/seam.py": "x = 0\n"},
                               {"capsule/seam.py": "x = 0\n" + self.PATCH})
        self.assertFalse(ok, report)
        self.assertIn("assignment to an imported module's attribute", report)

    def test_other_rule_5_constructs_stay_measured_in_test_paths(self):
        path = "capsule/tests/tmpclean.py"
        for src, kind in (("sq = lambda v: v * v\n", "lambda bound to a name"),
                          ("y = eval('1')\n", "eval"),
                          ("import importlib\nm = importlib.import_module('os')\n",
                           "dynamic import"),
                          ("n = 'a'\ny = getattr(object, n)\n", "computed attribute name"),
                          ("class M(type):\n    pass\nclass A(metaclass=M):\n    pass\n",
                           "metaclass")):
            with self.subTest(src=src):
                ok, report = run_files({path: "x = 0\n"}, {path: "x = 0\n" + src})
                self.assertFalse(ok, report)
                self.assertIn(kind, report)


class Intent(unittest.TestCase):
    """Z1/Z2: each shipped example is judged by the real check on a fixture repo."""

    def test_examples_ship_and_the_real_check_agrees_with_each(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from capsule import intent, paths
        self.assertIn("simplicity_check", intent.STANDALONE)
        examples = intent.load("simplicity_check")
        self.assertGreaterEqual(len(examples), 1)
        spec_cases = {"line-length-dodge-blocks", "semicolon-join-blocks",
                      "frozen-caller-extraction-passes-rule-2",
                      "one-line-edit-inside-old-deep-code-is-not-rule-3",
                      "else-branch-on-old-deep-if-is-rule-3"}
        self.assertLessEqual(spec_cases, {ex["id"] for ex in examples})
        self.assertEqual(intent.lint(paths.INTENT / "simplicity_check.jsonl", None,
                                     "simplicity_check"), [])
        for ex in examples:
            with self.subTest(ex["id"]):
                doc = ex["given"]["document"]
                if "fixture" in doc:
                    ok, report = replay(doc["fixture"])
                else:
                    ok, report = run_files(doc["base"], doc["head"])
                self.assertEqual(ok, ex["expect"]["ok"], report)
                if "fixture" in doc and not ok:
                    code = {"line-length-dodge-blocks": "E501",
                            "semicolon-join-blocks": "E702"}[ex["id"]]
                    self.assertIn(f"rule 7: {code} [ruff, hard]", report)


class SpecCaseReplays(unittest.TestCase):
    def test_duplicate_private_helper_is_an_info_finding(self):
        """Q2 B (row 571): the gate prints it for checklist line 2 but does not fail."""
        ok, report = replay("dup_helper")
        self.assertTrue(ok, report)
        self.assertIn("rule 2: _added_top_level_names", report)
        self.assertIn("[ast, info]", report)

    def test_clone_redact_docstring_is_judgement_only(self):
        """Spec section 5: checklist line 1, not a measured rule."""
        ok, report = replay("clone_redact")
        self.assertTrue(ok, report)
        self.assertNotIn("hard", report)

    def test_the_dodges_fail_rule_7_under_both_oracles(self):
        for fixture, code in (("line_dodge", "E501"), ("semicolon_join", "E702")):
            with self.subTest(fixture=fixture):
                ok, report = replay(fixture)
                self.assertFalse(ok, report)
                self.assertIn(f"rule 7: {code} [ruff, hard]", report)
                self.assertIn("[ast, hard]", report)


class MergeGate(unittest.TestCase):
    """Row 559, Task 7: mutation_check.main folds simplicity_check into its exit code."""

    def gate(self, head_src):
        import contextlib
        import io
        import mutation_check as mc
        root = make_repo()
        (root / "tests").mkdir()
        (root / "tests" / "size_baseline.json").write_text("{}")
        (root / "prod.py").write_text("x = 0\n")
        base = commit_all(root, "base")
        (root / "prod.py").write_text(head_src)
        head = commit_all(root, "head")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = mc.main(["--repo", str(root), "--base", base, "--head", head])
        return rc, out.getvalue()

    def test_a_long_added_line_fails_the_gate(self):
        rc, out = self.gate(LONG)
        self.assertEqual(rc, 1, out)
        self.assertIn("rule 7:", out)

    def test_a_clean_added_line_passes_the_gate(self):
        rc, out = self.gate("x = 0\ny = 'short'\n")
        self.assertEqual(rc, 0, out)
        self.assertIn("simplicity_check: ok", out)
