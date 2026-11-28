#!/usr/bin/env python3
"""PreToolUse write guard for unattended Claude jobs (generalises sprint-plan-guard.py).

Clamps Write/Edit/MultiEdit/NotebookEdit to the vault-relative paths listed in
the GUARD_ALLOW environment variable (colon-separated; a trailing `/**` allows
a subtree, otherwise it is one exact file). Anything else is denied. Fails
CLOSED on a malformed payload, and an empty or missing GUARD_ALLOW denies every
write.

run.sh ALWAYS renders hooks/write-guard.settings.json and passes it with
--settings (row 633), so interactive sessions never load this.
"""
import fnmatch
import json
import os
import sys
from pathlib import Path

WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}


def _deny(msg):
    sys.stderr.write(f"write-guard: {msg}\n")
    sys.exit(2)


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception as e:
        _deny(f"malformed hook payload: {e}")
    if payload.get("tool_name") not in WRITE_TOOLS:
        sys.exit(0)
    vault = os.environ.get("OBSIDIAN_VAULT_PATH", "").strip()
    allow = [a for a in os.environ.get("GUARD_ALLOW", "").split(":") if a]
    if not vault or not allow:
        _deny("OBSIDIAN_VAULT_PATH or GUARD_ALLOW not set in the job environment")
    tool_input = payload.get("tool_input") or {}
    target = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    if not target:
        _deny("no file_path in tool input")
    try:
        rel = Path(target).expanduser().resolve().relative_to(Path(vault).resolve())
    except ValueError:
        _deny(f"{target} is outside the vault")
    rel = rel.as_posix()
    for pat in allow:
        if pat.endswith("/**"):
            if rel.startswith(pat[:-3] + "/") or rel == pat[:-3]:
                sys.exit(0)
        elif rel == pat:
            sys.exit(0)
    _deny(f"{rel} is not in GUARD_ALLOW ({os.environ.get('GUARD_ALLOW')})")


if __name__ == "__main__":
    main()
