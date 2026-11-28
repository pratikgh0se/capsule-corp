#!/usr/bin/env python3
"""Zenkai Z8 measured rules on the lines a branch adds or changes (rule 4 lives in
size_check.py). The ast pass decides; ruff, where it has the same rule, is the second
oracle (Z2), and a hard finding from either one fails."""
import argparse
import ast
import collections
import json
import re
import subprocess
import sys

import size_baseline as bl
import size_check

LINE_LIMIT, DEPTH_LIMIT, TRY_LIMIT = 100, 3, 5
MIN_CALLERS = 2
EXEMPT = ("main",)
RUFF_RULE = {"E501": 7, "E702": 7, "PLR1702": 3, "S102": 5, "S307": 5, "E731": 5,
             "E722": 6, "S110": 6}
CLAUSE_RE = re.compile(r"\s*(else|finally)\s*:")
TRIES = (ast.Try, ast.TryStar)
NESTING = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, *TRIES, ast.Match)
FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)
SCOPES = (*FUNCS, ast.ClassDef)
DYNAMIC = {"eval": "eval", "exec": "exec", "__import__": "dynamic import",
           "import_module": "dynamic import"}


def _line_findings(text, tree, added):
    """Rule 7: an added line over LINE_LIMIT, or two simple statements joined with `;`."""
    out = [(n, 7, f"{len(s)} chars") for n, s in enumerate(text.splitlines(), 1)
           if n in added and len(s) > LINE_LIMIT]
    starts = collections.Counter(n.lineno for n in ast.walk(tree)
                                 if isinstance(n, ast.stmt) and not hasattr(n, "body"))
    return out + [(n, 7, "two statements on one line") for n, c in sorted(starts.items())
                  if n in added and c > 1]


def _clause(lines, block):
    """Line of the `else:`/`finally:` that opens `block`: the nearest code line above it."""
    n = block[0].lineno - 1
    if CLAUSE_RE.match(lines[n]):
        return n + 1
    while n > 0 and (not lines[n - 1].strip() or lines[n - 1].lstrip().startswith(("#", "@"))):
        n -= 1
    return n


def _blocks(node, lines):
    """(statements, adds a level, header line) per block a statement owns; an elif adds none."""
    if isinstance(node, ast.If) and len(node.orelse) == 1 and isinstance(node.orelse[0], ast.If):
        return [(node.body, 1, node.lineno), (node.orelse, 0, node.lineno)]
    if isinstance(node, TRIES):
        return [(node.body, 1, node.lineno),
                *((blk, 1, _clause(lines, blk)) for blk in (node.orelse, node.finalbody) if blk),
                *((h.body, 1, h.lineno) for h in node.handlers)]
    if isinstance(node, ast.Match):
        return [(case.body, 1, case.pattern.lineno) for case in node.cases]
    level = 1 if isinstance(node, NESTING) else 0
    orelse = getattr(node, "orelse", [])
    return [(getattr(node, "body", []), level, node.lineno),
            (orelse, level, _clause(lines, orelse) if orelse else 0)]


def _deep_findings(tree, added, lines):
    """Rule 3: an added statement more than DEPTH_LIMIT blocks deep inside its function,
    when the branch also added the header of a block above it (row 597: edits inside old
    deep code are not the branch's level; row 605: a new else/except/finally/case clause
    is such a header)."""
    out = []

    def visit(stmts, depth, made):
        for node in stmts:
            if depth > DEPTH_LIMIT and made and node.lineno in added:
                out.append((node.lineno, 3, f"nesting depth {depth}"))
            scope = isinstance(node, SCOPES)
            for block, level, header in _blocks(node, lines):
                visit(block, 0 if scope else depth + level,
                      not scope and (made or bool(level) and header in added))

    visit(tree.body, 0, False)
    return out


def _call_kind(node):
    func = node.func
    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
    if name in DYNAMIC and (isinstance(func, ast.Name) or name == "import_module"):
        return DYNAMIC[name]
    computed = len(node.args) > 1 and not isinstance(node.args[1], ast.Constant)
    return "computed attribute name" if name in ("getattr", "setattr") and computed else ""


def _assign_kind(node, imported, in_tests):
    if isinstance(node.value, ast.Lambda) and any(isinstance(t, ast.Name) for t in node.targets):
        return "lambda bound to a name"
    if in_tests:
        return ""
    patched = any(isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name)
                  and t.value.id in imported for t in node.targets)
    return "assignment to an imported module's attribute" if patched else ""


def _clever_findings(tree, added, in_tests):
    """Rule 5: the constructs a cold reader cannot follow. Patching a module attribute is a
    test seam, so it is measured only outside the test trees (row 592)."""
    imported = {a.asname or a.name.split(".")[0] for n in ast.walk(tree)
                if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
    out = []
    for node in ast.walk(tree):
        kind = ""
        if isinstance(node, ast.Call):
            kind = _call_kind(node)
        elif isinstance(node, ast.Assign):
            kind = _assign_kind(node, imported, in_tests)
        elif isinstance(node, ast.ClassDef) and any(k.arg == "metaclass" for k in node.keywords):
            kind = "metaclass"
        if kind and node.lineno in added:
            out.append((node.lineno, 5, kind))
    return out


def _swallows(node):
    span = node.body[-1].end_lineno - node.body[0].lineno + 1
    reraises = any(isinstance(n, ast.Raise) for h in node.handlers for n in ast.walk(h))
    return span > TRY_LIMIT and not reraises


def _slop_findings(tree, added):
    """Rule 6: bare except, an except that only passes, a long try that never re-raises."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and node.lineno in added:
            if node.type is None:
                out.append((node.lineno, 6, "bare except"))
            if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                out.append((node.lineno, 6, "except that only passes"))
        if isinstance(node, ast.Try) and node.handlers and node.lineno in added:
            if _swallows(node):
                out.append((node.lineno, 6, f"try over {TRY_LIMIT} lines never re-raises"))
    return out


def _ruff_findings(repo, path, text, added):
    """The second oracle: ruff's own implementation of rules 3, 5, 6 and 7."""
    cmd = size_check.ruff_cmd()
    if cmd is None:
        raise RuntimeError("neither ruff nor uvx is on PATH")
    r = subprocess.run([*cmd, "check", "--preview", "--isolated", "--ignore-noqa", "--exit-zero",
                        "--select", ",".join(RUFF_RULE), "--line-length", str(LINE_LIMIT),
                        "--config", f"lint.pylint.max-nested-blocks={DEPTH_LIMIT}",
                        "--output-format", "json", "--stdin-filename", path, "-"],
                       input=text, cwd=repo, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"ruff exited {r.returncode}: {(r.stderr or r.stdout).strip()[:400]}")
    return [(d["location"]["row"], RUFF_RULE[d["code"]], d["code"])
            for d in json.loads(r.stdout) if d["location"]["row"] in added]


def _allowed_imports(repo, head):
    """Rule 1's allowlist: stdlib, every module the repo tracks, deps-allowlist.txt pip names."""
    paths = bl.lines(["git", "ls-tree", "-r", "--name-only", head], repo)
    pkgs = {p.rsplit("/", 1)[0] for p in paths if p.endswith("/__init__.py")}
    repo_mods = {p.rsplit("/", 1)[-1][:-3] for p in paths
                 if p.endswith(".py") and p.rpartition("/")[0] not in pkgs}
    repo_mods |= pkgs
    pins = (bl.read_at(repo, head, "deps-allowlist.txt") or "").splitlines()
    pinned = {p[4:].split("==")[0].replace("-", "_").lower() for p in pins if p.startswith("pip:")}
    return set(sys.stdlib_module_names) | repo_mods | pinned


def _import_findings(tree, added, allowed):
    """Rule 1: an added import of anything outside `allowed`."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            names = [node.module]
        else:
            continue
        out += [(node.lineno, 1, f"import {n}") for n in names
                if node.lineno in added and n.split(".")[0] not in allowed]
    return out


def _defs(tree):
    """{qualified name: (bare name, first line, last line)} for top-level defs and methods."""
    out = {}
    for node in tree.body:
        if isinstance(node, SCOPES):
            out[node.name] = (node.name, node.lineno, node.end_lineno)
        if isinstance(node, ast.ClassDef):
            out.update({f"{node.name}.{m.name}": (m.name, m.lineno, m.end_lineno)
                        for m in node.body if isinstance(m, FUNCS)})
    return out


def _rule2_test_file(path):
    """Test files for rule 2 and rule 5's module-patch check: bl.is_test_file, plus every .py
    under the two test-support trees."""
    return bl.is_test_side(path)


def _reference_sites(repo, head):
    """{name: [(path, line)]} for every Name and Attribute in non-test .py files at head."""
    sites = collections.defaultdict(list)
    for path in bl.lines(["git", "ls-tree", "-r", "--name-only", head], repo):
        if not path.endswith(".py") or _rule2_test_file(path):
            continue
        for node in ast.walk(ast.parse(bl.read_at(repo, head, path) or "", filename=path)):
            if isinstance(node, ast.Name):
                sites[node.id].append((path, node.lineno))
            elif isinstance(node, ast.Attribute):
                sites[node.attr].append((path, node.lineno))
    return sites


def _grep_mentions(repo, head, name):
    """Rule 2's second oracle: whole-word mentions in non-test .py files (git grep)."""
    rows = bl.lines(["git", "grep", "-n", "-w", "-e", name, head, "--", "*.py"], repo)
    return sum(1 for r in rows if not _rule2_test_file(r.split(":")[1]))


def _in_frozen(repo, head, frozen, site):
    """Row 514's shape: a `path::file_length` key freezes the whole file, a function key only
    the lines of that function (its qualified name, as size_check keys it)."""
    path, line = site
    keys = frozen.get(path, set())
    if "" in keys:
        return True
    fns = size_check._fn_map(ast.parse(bl.read_at(repo, head, path) or "")) if keys else {}
    return any(q in keys and start <= line < start + n for start, (q, n) in fns.items())


def _caller_findings(repo, base, head, path, tree, added, context):
    """Rule 2: (line, detail, severity) per new def with fewer than MIN_CALLERS callers.
    Test files are exempt: their cases and helpers are called by the runner, not by code."""
    if _rule2_test_file(path):
        return []
    base_text = bl.read_at(repo, base, path)
    old = _defs(ast.parse(base_text, filename=path)) if base_text else {}
    out = []
    for qname, (name, start, end) in _defs(tree).items():
        if qname in old or start not in added or name in EXEMPT or name.startswith("__"):
            continue
        callers = [(p, n) for p, n in context["sites"].get(name, [])
                   if not (p == path and start <= n <= end)]
        frozen_caller = (len(callers) == 1
                         and _in_frozen(repo, head, context["frozen"], callers[0]))
        if len(callers) >= MIN_CALLERS or frozen_caller:
            continue
        private_local = name.startswith("_") and len(callers) == 1 and callers[0][0] == path
        mentions = _grep_mentions(repo, head, name)
        detail = f"{qname}: {len(callers)} caller(s) (ast), {mentions} mention(s) (grep)"
        if name.startswith("_") and len(callers) == 1 and not private_local:
            detail += (f"; its one caller is {callers[0][0]}:{callers[0][1]}, in another file: "
                       "move the def into that file or inline it")
        out.append((start, detail, "info" if private_local else "hard"))
    return out


def _context_for(repo, head):
    """Everything rules 1 and 2 read beyond the changed file, built once per check."""
    baseline = bl.load_at(repo, head)
    frozen = collections.defaultdict(set)
    for key in baseline if isinstance(baseline, dict) else ():
        path, qname, _kind = key.split(":", 2)
        frozen[path].add(qname)
    return {"allowed": _allowed_imports(repo, head), "sites": _reference_sites(repo, head),
            "frozen": frozen}


def _file_findings(repo, base, head, path, context):
    text = bl.read_at(repo, head, path) or ""
    tree = ast.parse(text, filename=path)
    added = bl.added_line_numbers(repo, base, head, path)
    found = [(*f, "ast", "hard") for f in _deep_findings(tree, added, text.splitlines())]
    found += [(*f, "ast", "hard") for f in _slop_findings(tree, added)]
    found += [(*f, "ast", "hard") for f in _clever_findings(tree, added, _rule2_test_file(path))]
    found += [(*f, "ast", "hard") for f in _line_findings(text, tree, added)]
    found += [(*f, "ast", "hard") for f in _import_findings(tree, added, context["allowed"])]
    found += [(n, 2, d, "ast", sev)
              for n, d, sev in _caller_findings(repo, base, head, path, tree, added, context)]
    found += [(*f, "ruff", "hard") for f in _ruff_findings(repo, path, text, added)]
    return [(path, *f) for f in found]


def check(repo, base, head):
    files = bl.lines(["git", "diff", "-M", "--name-only", "--diff-filter=ACMR", f"{base}..{head}",
                      "--", "*.py"], repo)
    try:
        context = _context_for(repo, head)
        found = [f for p in files for f in _file_findings(repo, base, head, p, context)]
    except (SyntaxError, ValueError, RuntimeError, subprocess.TimeoutExpired) as e:
        return False, f"simplicity_check: {e}"
    hard = [f for f in found if f[-1] == "hard"]
    rows = [f"  {p}:{n}: rule {r}: {d} [{o}, {s}]" for p, n, r, d, o, s in sorted(found)]
    title = ("simplicity_check: Z8 violations on added lines:" if hard else
             f"simplicity_check: ok ({len(files)} changed .py file(s), {len(found)} info)")
    return not hard, "\n".join([title, *rows])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", required=True)
    ap.add_argument("--head", required=True)
    ap.add_argument("--repo", default=".")
    a = ap.parse_args(argv)
    ok, report = check(a.repo, a.base, a.head)
    print(report)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
