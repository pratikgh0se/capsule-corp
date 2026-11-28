#!/usr/bin/env python3
"""Size oracle for the merge gate (Zenkai Z7, row 509): a function over 60 lines, a file
over 800, or ruff C901 complexity over 15, in a changed .py file. Ruff (`ruff` on PATH,
else `uvx --offline ruff@RUFF_VERSION`) is a required second oracle (Zenkai Z2): any
non-zero rc is a hard fail with the real stderr, never a silent skip. Baseline entries
(tests/size_baseline.py, .json) key on path + ast-qualified name (mapped from ruff's
own line - its bare `` `name` `` alone can collide) + kind, storing the measured value:
no growing past it, a duplicate key or a deleted baseline fails closed; a gone violation
in a changed or deleted file is only an advisory line (row 636, the CoS prunes it) and keeps
rc 0, but a frozen entry only covers a violation that already existed at base (so a stale
entry cannot be reused by a later card); see size_baseline.pair() for base/head/main."""
import argparse
import ast
import re
import shutil
import subprocess
import sys

import size_baseline as bl

FUNC_LIMIT, FILE_LIMIT, COMPLEXITY_LIMIT = 60, 800, 15
NEW_FUNC_LIMIT, NEW_FILE_LIMIT, NEW_COMPLEXITY_LIMIT = 40, 400, 10
RUFF_VERSION = "0.17.4"
_RUFF_RE = re.compile(r"^(?P<path>[^:]+):(?P<line>\d+):\d+:\s+C901\s+`(?P<name>[^`]+)`.*\((?P<n>\d+)\s*>")


def _fn_map(tree):
    """{lineno: (qualified_name, length)} for every function def - shared by
    `ast_violations` and `ruff_violations`, so both key on the same qualified name."""
    out = {}
    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qname = prefix + child.name
                out[child.lineno] = (qname, (child.end_lineno or child.lineno) - child.lineno + 1)
                walk(child, qname + ".")
            else:
                walk(child, prefix + child.name + "." if isinstance(child, ast.ClassDef) else prefix)
    walk(tree, "")
    return out


def _old_names(base_text):
    """Qualified function names in `base_text`; empty (every name new) when the file did
    not exist at base or does not parse there (fail-closed)."""
    if base_text is None:
        return set()
    try:
        return {q for q, _ in _fn_map(ast.parse(base_text)).values()}
    except (SyntaxError, ValueError):
        return set()


def _limit(qname, old, keep, new):
    """Z8 rule 4: a name that existed at base (or no base at all) keeps `keep`; else `new`."""
    return keep if old is None or qname in old else new


def ast_violations(path, text, old=None, new_file=False):
    """[(path, line, kind, qualified_name, n)]; `old` names keep 60, other names get 40."""
    try:
        tree = ast.parse(text, filename=path)
    except (SyntaxError, ValueError):
        return []
    n = len(text.splitlines())
    file_limit = NEW_FILE_LIMIT if new_file else FILE_LIMIT
    out = [(path, 1, "file_length", "", n)] if n > file_limit else []
    out += [(path, lineno, "function_length", qname, length)
            for lineno, (qname, length) in _fn_map(tree).items()
            if length > _limit(qname, old, FUNC_LIMIT, NEW_FUNC_LIMIT)]
    return out


def ruff_cmd():
    """`ruff` on PATH, else the pinned `uvx --offline` ruff, else None."""
    if shutil.which("ruff"):
        return ["ruff"]
    return ["uvx", "--offline", f"ruff@{RUFF_VERSION}"] if shutil.which("uvx") else None


def _complexity_hits(stdout, fn_map, old):
    """Ruff C901 lines that exceed the limit for their name (Z8 rule 4)."""
    out = []
    for line in stdout.splitlines():
        m = _RUFF_RE.match(line.strip())
        if not m:
            continue
        qname = fn_map.get(int(m.group("line")), (m.group("name"), 0))[0]
        n = int(m.group("n"))
        if n > _limit(qname, old, COMPLEXITY_LIMIT, NEW_COMPLEXITY_LIMIT):
            out.append((m.group("path"), int(m.group("line")), "complexity", qname, n))
    return out


def ruff_violations(repo, sha, files, base=None):
    """(ok, [violations], err): C901 at `sha`, file by file via `--stdin-filename`; ok
    False on any non-zero rc, or on no ruff/uvx, with the real reason in `err`. Ruff runs
    at the new limit; a hit on a name that existed at `base` (or with no base) must also
    exceed the old one."""
    cmd = ruff_cmd()
    if cmd is None:
        return False, [], "neither ruff nor uvx is on PATH"
    out = []
    for p in files:
        text = bl.read_at(repo, sha, p)
        if text is None:
            continue
        old = _old_names(bl.read_at(repo, base, p)) if base else None  # None: no base given
        try:
            fn_map = _fn_map(ast.parse(text, filename=p))
        except (SyntaxError, ValueError):
            fn_map = {}
        try:
            r = subprocess.run(
                [*cmd, "check", "--select", "C901", "--output-format", "concise", "--stdin-filename", p,
                 "--exit-zero", "--ignore-noqa", "--isolated",
                 "--config", f"lint.mccabe.max-complexity={NEW_COMPLEXITY_LIMIT}", "-"],
                input=text, cwd=repo, capture_output=True, text=True, timeout=60)
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            return False, [], f"{cmd[0]} failed to run: {e}"
        if r.returncode != 0:
            return False, [], f"{' '.join(cmd)} exited {r.returncode}: {(r.stderr or r.stdout).strip()[:400]}"
        out += _complexity_hits(r.stdout, fn_map, old)
    return True, out, ""


def key(v):
    return f"{v[0]}:{v[3]}:{v[2]}"


def _ast_all(repo, sha, files, base):
    """ast violations for `files` at `sha`; a file absent at `base` is new (`base=None`: none)."""
    out = []
    for p in files:
        base_text = bl.read_at(repo, base, p) if base else None
        new_file = bool(base) and base_text is None
        old = _old_names(base_text) if base else None
        out += ast_violations(p, bl.read_at(repo, sha, p) or "", old, new_file)
    return out


def measure(repo, sha, files, base=None):
    """Violations at `sha`. With `base`, code absent at base gets the 40 / 400 / 10 limits;
    with `base=None` (baseline generation) only the old 60 / 800 / 15 apply."""
    out = _ast_all(repo, sha, files, base)
    ok, ruff_out, err = ruff_violations(repo, sha, files, base)
    if not ok:
        return False, [], err
    violations = out + ruff_out
    keys = [key(v) for v in violations]
    dup = next((k for k in keys if keys.count(k) > 1), None)
    if dup:
        return False, [], f"duplicate baseline key {dup!r} - two violations collapsed to one"
    return True, violations, ""


def generate_baseline(repo):
    files = bl.lines(["git", "ls-files", "*.py"], repo)
    ok, violations, err = measure(repo, "HEAD", files)
    if not ok:
        print(f"size_check: {err}"); return 1
    bl.write(repo, {key(v): v[4] for v in violations})
    print(f"wrote {len(violations)} baseline entries to {bl.BASELINE_PATH}"); return 0


def _changed(repo, base, head, kinds):
    """The .py files changed in base..head with git's `--diff-filter` letters `kinds`."""
    return bl.lines(["git", "diff", "--name-only", f"--diff-filter={kinds}", f"{base}..{head}",
                     "--", "*.py"], repo)


def check(repo, base, head):
    ok, base_bl, head_bl, base_existed, err = bl.pair(repo, base, head)
    if not ok:
        return False, f"size_check: {err}"
    grown = sorted(k for k, n in head_bl.items() if k not in base_bl or n > base_bl[k]) if base_existed else []
    files = _changed(repo, base, head, "ACMR")
    ok_m, violations, err = measure(repo, head, files, base)
    ok_b, at_base, err_b = measure(repo, base, files)   # old limits: what the baseline can hold
    if not (ok_m and ok_b):
        return False, f"size_check: {err or err_b}"
    current = {key(v): v[4] for v in violations}
    # row 636: against a baseline that existed at base, a frozen entry only covers a violation
    # that already existed at base, so a stale entry left by an earlier card cannot be reused by
    # a later one (it is new again). With no baseline at base (main's fallback) nothing predates it.
    base_has_baseline = bl.load_at(repo, base) is not bl.MISSING
    covered = {key(v) for v in at_base} if base_has_baseline else set(current)
    new = sorted(k for k, n in current.items()
                 if k not in head_bl or n > head_bl[k] or k not in covered)
    gone = set(files) | set(_changed(repo, base, head, "D"))
    stale = sorted(k for k in head_bl if k.split(":", 1)[0] in gone and k not in current)
    sections = [(grown, f"{bl.BASELINE_PATH} grew ({len(grown)} added/raised - may only shrink):", grown),
                (new, "new or worse size violations (not frozen at this value):",
                 [f"{k}={current[k]}" for k in new])]
    out = [line for cond, header, items in sections if cond for line in (header, *(f"  {i}" for i in items))]
    ok = not (grown or new)
    if ok:
        out = [f"size_check: ok ({len(files)} changed .py file(s), {len(head_bl)} frozen)"]
    # row 636: a stale entry (violation gone in a changed file) is advisory; the CoS prunes it
    advisory = [f"size_check: stale baseline entry, CoS prunes: {k}" for k in stale]
    return ok, "\n".join(out + advisory)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base"); ap.add_argument("--head"); ap.add_argument("--repo", default=".")
    ap.add_argument("--generate-baseline", action="store_true"); a = ap.parse_args(argv)
    if a.generate_baseline:
        return generate_baseline(a.repo)
    if not a.base or not a.head:
        ap.error("--base and --head are required unless --generate-baseline")
    ok, report = check(a.repo, a.base, a.head)
    print(report)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
