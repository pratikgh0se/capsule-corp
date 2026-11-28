#!/usr/bin/env python3
"""PreToolUse guard for the weekly sprint-plan job.

run.sh invokes the Planner as `claude -p ... --allowedTools "Read Write Grep
Glob Bash(... eligible*) Bash(... link-spec *) Bash(... plan-next) Bash(...
plan-next-remove *)"`. `Write` with no path constraint would let a hijacked
prompt (the board and linked notes are untrusted-ish input) write anywhere
the user can. This hook clamps every write to one location:

  * <vault>/Specs/**          - the spec notes the Planner is meant to create

Boards/Engineering.md is GENERATED (T8) - the Planner never writes it. Its
writes outside Specs/ are CLI verbs, never a file Write/Edit, so they need no
entry here: `board_bridge.py link-spec`, and, since row 201,
`intake.py plan-next-remove` (clears a specced `## Plan next` line in
Boards/Intake.md - see intake.py's PLAN_NEXT docstring). Anything else is
denied. Wired in via maintenance/hooks/sprint-plan.settings.json,
which run.sh passes with `--settings` for the sprint-plan job only, so this
never affects interactive Claude Code sessions.

Contract: Claude Code sends the hook a JSON object on stdin. Exit 0 = allow;
exit 2 = block (stderr is shown to the model). Any other exit = non-blocking
error, which we avoid - on a malformed payload we fail CLOSED (exit 2).
"""
import json
import os
import sys
from pathlib import Path

WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}


def _fail_closed(msg: str) -> "NoReturn":  # noqa: F821
    sys.stderr.write(f"sprint-plan-guard: {msg}\n")
    sys.exit(2)


def main() -> None:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        _fail_closed("could not parse hook payload as JSON - blocking write")

    tool = payload.get("tool_name") or payload.get("tool") or ""
    if tool not in WRITE_TOOLS:
        sys.exit(0)  # not a write - nothing to police

    tool_input = payload.get("tool_input") or payload.get("input") or {}
    fp = (tool_input.get("file_path") or tool_input.get("notebook_path")
          or tool_input.get("path") or "")
    if not fp:
        _fail_closed(f"{tool} call carried no file_path - blocking")

    vault = os.environ.get("OBSIDIAN_VAULT_PATH", "").strip()
    if not vault:
        _fail_closed("OBSIDIAN_VAULT_PATH not set in the job environment")
    vault_root = Path(vault).resolve()

    # Resolve without requiring the target to exist yet (new spec files).
    target = Path(fp)
    if not target.is_absolute():
        target = vault_root / target
    target = target.resolve()

    specs_dir = (vault_root / "Specs").resolve()

    allowed = target == specs_dir or specs_dir in target.parents
    if not allowed:
        _fail_closed(
            f"{tool} to {target} is outside the sprint-plan write allowlist "
            f"({specs_dir}/**). If the Planner needs to record something "
            "else, put it under Open questions in the spec."
        )
    sys.exit(0)


if __name__ == "__main__":
    main()
