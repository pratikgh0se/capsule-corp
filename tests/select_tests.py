"""select_tests: which tests a diff needs (decision D273).

Prints one test per line, NONE (no test needed), or FULL.

Usage: select_tests.py --base BASE --head HEAD [--repo DIR] [--max-files N]

Safety beats savings: narrowing happens only when EVERY changed file is understood, and the answer
is FULL (the whole suite) otherwise. Understood files are python files under capsule/,
maintenance/, tests/ and bin/, and markdown under docs/ that no test mentions by name
(it selects the tests that touch docs/ at all, e.g. the citation and link checks). A python
file maps to its own tests when it is a test; tests named for it (x.py -> test_x*); tests that
import it, directly or through other importers (static ast scan, relative imports included); and
the tests (shell suites too) that mention its path, file name or `-m` module name. Python tests
print as dotted unittest ids, shell suites as tests/test_*.sh paths.
FULL for everything else: sql, sh, json, yaml, templates, fixtures, contracts, migrations,
docs/ZENKAI.md, unknown types, shared helpers (FULL_PATHS), a module an `__init__.py` imports, a
python file nothing names or imports, an unreadable diff or tree, or more than --max-files files.
"""
import argparse
import ast
import re
import subprocess
import sys
from pathlib import Path

TEST_DIRS = ("capsule/tests", "maintenance/tests", "tests")
SOURCE_DIRS = ("capsule", "maintenance", "tests", "bin")
# Shared helpers, runner plumbing and data the tests read: a change here can break anything.
FULL_PATHS = re.compile(r"^(capsule/(paths|config|db|lander_select)\.py|.*/__init__\.py|"
                        r".*conftest.*|tests/lib_.*|bin/suite_hook/.*|bin/capsule-suite|"
                        r"tests/select_tests\.py|contracts/.*|templates/.*|.*/templates/.*|"
                        r".*/fixtures/.*|.*migrations?/.*|docs/ZENKAI\.md)$")
MAX_FILES = 25


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          check=True).stdout


def _is_test(p):
    return bool(re.match(r"^(capsule/tests|maintenance/tests|tests)/test_[^/]*\.(py|sh)$", p))


def _all_tests(repo):
    return sorted(str(f.relative_to(repo)) for d in TEST_DIRS
                  for f in (Path(repo) / d).glob("test_*") if f.suffix in (".py", ".sh"))


def _dotted(p):
    p = p[:-3].replace("/", ".")
    return p[:-9] if p.endswith(".__init__") else p


def _imports(path, rel):
    """Dotted module names a python file imports (`from a import b` yields a and a.b); relative
    imports resolve against the file's package. `rel` is the repo-relative path."""
    try:
        tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return None
    pkg = _dotted(rel).split(".")
    if not rel.endswith("__init__.py"):
        pkg = pkg[:-1]
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out.update(a.name for a in n.names)
        elif isinstance(n, ast.ImportFrom):
            base = pkg[:len(pkg) - n.level + 1] if n.level else []
            mod = ".".join(base + ([n.module] if n.module else []))
            if mod:
                out.add(mod)
                out.update(f"{mod}.{a.name}" for a in n.names)
    return out


def _names(p):
    """The import names a source file answers to: its dotted path, plus its bare stem for scripts
    that tests put on sys.path (maintenance/, bin/, tests/ helpers)."""
    names = {_dotted(p)}
    if not p.startswith("capsule/"):
        names.add(Path(p).stem)
    return names


def _importers(imp, p):
    """`p` and every file that imports it, transitively (a changed module taints its importers)."""
    names, seen, grew = set(_names(p)), {p}, True
    while grew:
        grew = False
        for f, imports in imp.items():
            if f not in seen and imports & names:
                seen.add(f)
                names |= _names(f)
                grew = True
    return seen


def _mentions(f):
    """Strings that name source file `f` in a test: its path, its file name, or `-m module`."""
    if Path(f).name == "__main__.py":
        return {f"-m {_dotted(f)[:-len('.__main__')]}"}
    return {f, Path(f).name, f"-m {_dotted(f)}"}


def _tests_for(p, tests, text, imp):
    """The tests a changed python file `p` needs, or None when it cannot be mapped (FULL)."""
    hit = {t for t in tests if Path(t).name.startswith(f"test_{Path(p).stem}")}
    affected = _importers(imp, p)
    if any(f.endswith("__init__.py") for f in affected):
        return None                          # a package loads it for every test
    hit |= {t for t in affected if _is_test(t)}
    hit |= {t for t in tests if any(m in text[t] for f in affected for m in _mentions(f))}
    return hit or None                       # nothing names or imports it: cannot map


def _select(repo, changed, max_files=MAX_FILES):
    """The sorted test list for `changed` (repo-relative paths), or None meaning FULL."""
    repo = Path(repo)
    if len(changed) > max_files or any(FULL_PATHS.match(p) for p in changed):
        return None
    tests = _all_tests(repo)
    text = {t: (repo / t).read_text(errors="ignore") for t in tests}
    sources = [str(f.relative_to(repo)) for d in SOURCE_DIRS
               for f in (repo / d).rglob("*.py") if "__pycache__" not in f.parts]
    imp = {f: _imports(repo / f, f) for f in sources}
    if any(v is None for v in imp.values()):
        return None
    chosen = set()
    for p in changed:
        if p.endswith(".md") and p.startswith("docs/"):
            if any(Path(p).name in t for t in text.values()):
                return None                  # a test reads this prose: cannot tell what it needs
            chosen |= {t for t in tests if "docs" in text[t]}    # tests that read every doc
            continue
        if not (p.endswith(".py") and p.split("/")[0] in SOURCE_DIRS):
            return None                      # a file type the selector does not understand
        if _is_test(p):
            if (repo / p).exists():
                chosen.add(p)
            continue
        hit = _tests_for(p, tests, text, imp)
        if hit is None:
            return None
        chosen |= hit
    return sorted(chosen)


def _to_ids(tests):
    """Python tests as dotted unittest ids, shell suites unchanged."""
    return [_dotted(t) if t.endswith(".py") else t for t in tests]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--head", required=True)
    ap.add_argument("--repo", default=".")
    ap.add_argument("--max-files", type=int, default=MAX_FILES)
    a = ap.parse_args(argv)
    try:
        changed = _git(a.repo, "diff", "--name-only", "--no-renames",
                       f"{a.base}...{a.head}").split("\n")
        got = _select(a.repo, [c for c in changed if c], a.max_files)
    except Exception:                         # cannot diff or read the tree: run everything
        got = None
    print("FULL" if got is None else "\n".join(_to_ids(got)) or "NONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
