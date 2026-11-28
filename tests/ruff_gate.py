#!/usr/bin/env python3
"""Pre-merge ruff gate (LAND-07, ledger row 171): the same `ruff check` selection as the
`tests` job in .github/workflows/verify.yml (keep SELECT equal to it), on the .py files a
range adds or changes, linted as they are at --head. A second, stricter tier (M096, D212:
STRICT_SELECT minus STRICT_IGNORE) blocks only on lines the range adds or changes; an old
finding on an untouched line never fails. `# noqa: CODE` is the per-line escape. `ruff` on
PATH, else the pinned `uvx --offline` ruff; neither is a hard failure with a message, never a
silent pass (Z4)."""
import argparse
import re
import subprocess
import sys

from size_check import ruff_cmd  # one ruff pin for every gate: size_check.RUFF_VERSION

SELECT = "E9,F63,F7,F82"
STRICT_SELECT = "E,F,B,S,PT,RUF"
STRICT_IGNORE = ("E501,PT009,PT027,S603,S607,E701,E702,RUF100,"
                 "RUF005,RUF059,RUF012,PT008,B905")
_TEST_GLOBS = ["**/tests/**", "**/test_*.py", "**/*_test.py", "**/conftest.py"]
STRICT_CONFIG = ("lint.per-file-ignores = {"
                 + ", ".join(f'"{g}" = ["S101","S105","S106","S108","E402"]' for g in _TEST_GLOBS)
                 + "}")
_COVERED = ("E9", "F63", "F7", "F82")  # already blocked whole-file by the first tier
_HUNK = re.compile(r"^@@ -\S+ \+(\d+)(?:,(\d+))? @@")
_FINDING = re.compile(r"^(?P<path>.+?):(?P<line>\d+):\d+: (?P<code>[A-Z]+\d+) (?P<msg>.*)$")


def _changed(repo, base, head):
    """One rename-aware diff: (error or None, {head path: set of added or changed lines})."""
    # --text --no-textconv: a .gitattributes `-diff`/`binary` .py still gets a `+++` line and
    # hunks (else it lints nothing). A mode-only change (same content) has no entry: accepted.
    d = subprocess.run(["git", "-c", "core.quotePath=false", "diff", "-U0", "-M", "--no-color",
                        "--text", "--no-textconv", "--no-ext-diff", "--src-prefix=a/",
                        "--dst-prefix=b/", "--diff-filter=ACMR", base, head, "--", "*.py"],
                       cwd=repo, capture_output=True, text=True)
    if d.returncode != 0:
        return f"ruff_gate: git diff failed: {d.stderr.strip()}", {}
    out, cur, in_header = {}, None, False
    for row in d.stdout.splitlines():
        if row.startswith("diff --git "):
            cur, in_header = None, True
        elif in_header and row.startswith(("+++ ", "rename to ")):
            raw = row[4:] if row.startswith("+++") else row[10:]
            if raw.startswith('"'):
                return f"ruff_gate: unsupported (quoted) path in diff: {row}", {}
            cur = (raw[2:] if row.startswith("+++") else raw).rstrip("\t")
            out.setdefault(cur, set())
        elif row.startswith("@@"):
            in_header = False
            if cur and (m := _HUNK.match(row)):
                start, n = int(m.group(1)), int(m.group(2) or 1)
                out[cur].update(range(start, start + n))
    return None, out


def _ruff_err(r):
    return f"ruff_gate: ruff exited {r.returncode}: {(r.stderr or r.stdout).strip()}"


def _strict(cmd, path, source, changed):
    """Strict-tier findings on changed lines: (error or None, [concise lines])."""
    r = subprocess.run([*cmd, "check", "--isolated", "--select", STRICT_SELECT, "--ignore",
                        STRICT_IGNORE, "--config", STRICT_CONFIG, "--output-format", "concise",
                        "--stdin-filename", path, "-"], input=source,
                       capture_output=True, text=True, timeout=60)
    if r.returncode not in (0, 1):
        return _ruff_err(r), []
    lines = r.stdout.splitlines()
    rows = [m for m in map(_FINDING.match, lines) if m]
    if r.returncode == 1 and not rows and not any(": invalid-syntax" in x for x in lines):
        return f"ruff_gate: ruff reported findings for {path} that did not parse: {r.stdout}", []
    return None, [f"{path}:{m['line']} {m['code']} {m['msg']}" for m in rows
                  if int(m["line"]) in changed and not m["code"].startswith(_COVERED)]


def check(repo, base, head):
    cmd = ruff_cmd()
    if cmd is None:
        return False, "ruff_gate: neither ruff nor uvx is on PATH (red, never skipped)"
    err, files = _changed(repo, base, head)
    if err:
        return False, err
    found, strict = [], []
    for path, changed in files.items():
        show = subprocess.run(["git", "show", f"{head}:{path}"], cwd=repo, capture_output=True,
                              text=True)
        if show.returncode != 0:
            return False, f"ruff_gate: git show {head}:{path} failed: {show.stderr.strip()}"
        r = subprocess.run([*cmd, "check", "--isolated", "--select", SELECT, "--output-format",
                            "concise", "--stdin-filename", path, "-"],
                           input=show.stdout, cwd=repo, capture_output=True, text=True, timeout=60)
        if r.returncode not in (0, 1):
            return False, _ruff_err(r)
        if r.returncode == 1:
            found.append(r.stdout.strip())
        err, hits = _strict(cmd, path, show.stdout, changed)
        if err:
            return False, err
        strict += hits
    report = []
    if found:
        report += ["ruff_gate: findings on changed files:", *found]
    if strict:
        report += [f"ruff_gate: {len(strict)} finding(s) on changed lines:", *strict]
    if report:
        return False, "\n".join(report)
    return True, f"ruff_gate: ok ({len(files)} changed .py file(s))"


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
