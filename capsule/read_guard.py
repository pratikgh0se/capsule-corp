"""PreToolUse hook for worker sessions: deny Read, Glob and Grep outside an allow-list.

Run as `python -I capsule/read_guard.py ROOT [ROOT ...]` with the hook JSON on stdin. The
first ROOT is the session cwd. Exit 0 = no objection. Exit 2 = deny, with one stderr line.
Deny-only and fail closed: any parse problem or error denies. Stdlib only, no capsule import:
`-I` keeps PYTHONPATH, the user site and the cwd off sys.path. Design: read-guard.md.
"""

import contextlib
import json
import os
import re
import sys

PATHS = {"Read": ("file_path", True), "Glob": ("path", False), "Grep": ("path", False)}
PATTERNS = {"Glob": ("pattern", True), "Grep": ("glob", False)}
CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _inside(path, roots):
    real = os.path.realpath(path)
    return any(real == r or real.startswith(r.rstrip(os.sep) + os.sep) for r in roots)


def _field(tool_input, name, required):
    """The string in tool_input[name], None if optional and absent; ValueError if unusable."""
    if name not in tool_input and not required:
        return None
    value = tool_input.get(name)
    # The CLI trims its paths; JS trim also drops U+FEFF, which str.strip keeps.
    if not isinstance(value, str) or not value or CONTROL.search(value) or value[0] == "~":
        raise ValueError(f"bad {name} {value!r}")
    if value.strip().strip("\ufeff") != value:
        raise ValueError(f"bad {name} {value!r}")
    return value


def reason(tool, tool_input, roots):
    """Why the call is denied, or None when it may go on. `roots` are the raw ROOT arguments."""
    if tool not in PATHS or not isinstance(tool_input, dict):
        return "unknown tool or bad tool_input"
    if not roots or not all(os.path.isabs(r) for r in roots):
        return "roots must be absolute paths"
    real_roots = [os.path.realpath(r) for r in roots]
    name, required = PATHS[tool]
    path = _field(tool_input, name, required)
    base = os.path.join(roots[0], path or "")
    if path is not None and not _inside(base, real_roots):
        return f"path outside the allow-list: {path!r}"
    if tool in PATTERNS:
        name, required = PATTERNS[tool]
        pattern = _field(tool_input, name, required)
        if pattern is None:
            return None
        # Every brace or comma alternative must be relative; only the leading one may be absolute.
        parts = re.split(r"[{,]", pattern)
        if any(p[:1] in ("/", "~") for p in parts[1:]):
            return f"absolute alternative in pattern {pattern!r}"
        if ".." in re.split(r"[/{},]", pattern):
            return f"'..' in pattern {pattern!r}"
        wild = re.search(r"[*?\[{]", pattern)
        literal = pattern[: wild.start()] if wild else pattern
        walk = os.path.join(base, literal[: literal.rfind("/") + 1])
        if not _inside(walk, real_roots):
            return f"pattern walks outside the allow-list: {pattern!r}"
    return None


def _deny(message):
    with contextlib.suppress(Exception):  # a failed write must never change the exit code
        sys.stderr.write(f"read guard: {message}\n")
    sys.exit(2)


def main():
    try:
        data = json.loads(sys.stdin.read())
        tool = data["tool_name"]
        why = reason(tool, data.get("tool_input"), sys.argv[1:])
        if why:
            _deny(f"{tool} denied: {why}")
    except SystemExit:
        raise
    except BaseException as exc:
        _deny(f"denied: {type(exc).__name__}: {str(exc)[:200]!r}")


if __name__ == "__main__":
    main()
