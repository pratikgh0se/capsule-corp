#!/usr/bin/env python3
# Capsule Corp rule (row 557): a SUBAGENT (hook input carries `agent_id`)
# edits only inside its own worktree, never the main checkout of capsule-corp; the
# main thread (no `agent_id`) is never blocked. PreToolUse hook for Edit/Write/NotebookEdit
# only (not Bash, not MCP write tools). `<repo>/.worktrees/` and `<repo>/scratchpad/` stay
# allowed at the repo root. Malformed/empty stdin, no path field, or an unresolvable path:
# allow (exit 0) with one stderr warning - never wedge a session.
import json
import os
import sys

DEFAULT_REPO = "~/CodeBase/capsule-corp"


def _repos():
    """CAPSULE_WORKTREE_REPOS (colon list) after `~` expansion. Entries that are not absolute,
    or that are the filesystem root, are dropped with a stderr warning; a missing directory
    is kept with a warning. No usable entry left: the default, so the guard is never off."""
    keep = []
    for entry in (os.environ.get("CAPSULE_WORKTREE_REPOS") or "").split(":"):
        path = os.path.expanduser(entry)
        if not entry:
            continue
        if not os.path.isabs(path) or os.path.realpath(path) == os.sep:
            print(f"worktree-only-edits: ignoring {entry!r} (need an absolute non-root path)",
                  file=sys.stderr)
            continue
        if not os.path.isdir(path):
            print(f"worktree-only-edits: configured repo {path!r} does not exist",
                  file=sys.stderr)
        keep.append(path)
    return tuple(keep) or (os.path.expanduser(DEFAULT_REPO),)


REPOS = _repos()
EXEMPT_DIRS = (".worktrees", "scratchpad")
FIRMLINK = "/system/volumes/data"


def normalize(path):
    """Casefolded, firmlink-stripped: two spellings of one on-disk file compare equal."""
    path = path.casefold()
    return path[len(FIRMLINK):] if path.startswith(FIRMLINK + "/") else path


def resolve(path, cwd):
    """Absolute, `..`-normalised, symlinks resolved for whatever prefix already exists on
    disk (a Write's target may not exist yet). A relative path resolves against the hook
    call's own `cwd`, not this process's."""
    path = os.path.expanduser(path)
    if not os.path.isabs(path):
        path = os.path.join(cwd, path)
    return os.path.realpath(path)


def target_path(tool_input):
    for key in ("file_path", "notebook_path"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def refused(resolved):
    nres = normalize(resolved)
    for repo in REPOS:
        repo_real = os.path.realpath(repo)
        nrepo = normalize(repo_real)
        if nres != nrepo and not nres.startswith(nrepo + os.sep):
            continue
        rel = nres[len(nrepo):].lstrip(os.sep)
        top = rel.split(os.sep, 1)[0] if rel else ""
        if top in EXEMPT_DIRS:
            return None
        return (f"Blocked by the Capsule Corp worktree-only-edits rule (row 557, "
                f"t_f47a8f51): {resolved!r} is in the main checkout of {repo!r}, outside "
                f".worktrees/. Work in a worktree (see common-rules.md).")
    return None


def main():
    try:
        payload = json.load(sys.stdin)
        tool_input = payload.get("tool_input")
        if not isinstance(tool_input, dict):
            raise ValueError("tool_input missing or not an object")
    except Exception as exc:
        print(f"worktree-only-edits: malformed hook input, allowing ({exc})", file=sys.stderr)
        return 0
    if not payload.get("agent_id"):
        return 0  # main thread: never blocked
    path = target_path(tool_input)
    if path is None:
        print("worktree-only-edits: no file_path/notebook_path in tool_input, allowing",
              file=sys.stderr)
        return 0
    try:
        resolved = resolve(path, payload.get("cwd") or os.getcwd())
    except (TypeError, ValueError) as exc:
        print(f"worktree-only-edits: malformed path, allowing ({exc})", file=sys.stderr)
        return 0
    reason = refused(resolved)
    if reason:
        print(reason, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
