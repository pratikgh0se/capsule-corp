#!/usr/bin/env python3
"""Golden transcripts for role SOULs: the fixture format and the shape check."""
import json
from pathlib import Path

import contracts as ct

VERSION = 1

FINISHING_CALLS = {
    "dev": ("kanban_request_review", ("reviewer", "summary", "metadata")),
    "dev-backend": ("kanban_request_review", ("reviewer", "summary", "metadata")),
    "dev-frontend": ("kanban_request_review", ("reviewer", "summary", "metadata")),
    "infra": ("kanban_request_review", ("reviewer", "summary", "metadata")),
    "reviewer": (("kanban_complete", "kanban_request_changes"), ()),
}
# A block ends a run too, on every escalation path but "happy".
TERMINAL = ("kanban_block",)
BANNED_TOOLS = ("delegate_task",)
BLOCK_KINDS = ("needs_input", "capability", "dependency")


def load(path):
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    if d.get("v") != VERSION:
        raise ValueError(f"{path}: unknown transcript version {d.get('v')!r}")
    return d


def _finishers(role):
    want, _ = FINISHING_CALLS.get(role, ((), ()))
    return (want,) if isinstance(want, str) else tuple(want)


def _all_finishers():
    """Every finishing-shaped tool across every role, plus TERMINAL."""
    out = set(TERMINAL)
    for want, _ in FINISHING_CALLS.values():
        out.update((want,) if isinstance(want, str) else want)
    return tuple(sorted(out))


ALL_FINISHERS = _all_finishers()


def check(t):
    """Return a list of violation strings. Empty means the shape is right."""
    out = []
    role, calls = t.get("role"), t.get("calls") or []
    if not calls:
        return ["transcript has no calls"]
    if calls[0].get("tool") != "kanban_show":
        out.append("the run does not open with kanban_show")
    for c in calls:
        if c.get("tool") in BANNED_TOOLS:
            out.append(f"calls {c['tool']}: the board is the only channel")
    finishers = _finishers(role)
    ends = [c for c in calls if c.get("tool") in ALL_FINISHERS]
    if len(ends) != 1:
        out.append(f"expected exactly one finishing call, found {len(ends)}")
    elif ends[0] is not calls[-1]:
        out.append("the finishing call is not the last call")
    if ends:
        end = ends[0]
        tool, args = end.get("tool"), end.get("args") or {}
        if tool in finishers:
            _, required = FINISHING_CALLS[role]
            for k in required:
                if k not in args:
                    out.append(f"{tool} is missing the required argument {k!r}")
            if tool == "kanban_request_review" and args.get("reviewer") != "reviewer":
                out.append('kanban_request_review must pass reviewer="reviewer"')
        elif tool == "kanban_block":
            if args.get("kind") not in BLOCK_KINDS:
                out.append(f"block kind {args.get('kind')!r} is not one of {BLOCK_KINDS}")
            out += _check_reason(args.get("reason") or "")
        else:
            out.append(f"{tool} is not a finishing call for {role!r}; "
                       f"expected one of {finishers + TERMINAL}")
    return out


def _check_reason(reason):
    if reason.startswith(ct.DECISION_PREFIX):
        try:
            import decisions as dc
            d = dc.parse_decision(reason)
        except Exception as e:                       # noqa: BLE001 - a parse failure IS the finding
            return [f"DECISION block does not parse: {e}"]
        if not d or not d.get("options"):
            return ["DECISION block parses to no options"]
        return []
    # BLOCKER and NEED are mandatory; TRIED is optional and positional - lead's SOUL
    # says "drop TRIED if there was nothing to try" and a research block has no attempts
    # to list (spec section 4.8).
    for token in ("BLOCKER:", "NEED:"):
        if token not in reason:
            return [f"block reason is missing {token}"]
    if reason.index("BLOCKER:") > reason.index("NEED:"):
        return ["block reason has BLOCKER after NEED"]
    if "TRIED:" in reason and not (
            reason.index("BLOCKER:") < reason.index("TRIED:") < reason.index("NEED:")):
        return ["block reason has TRIED outside the BLOCKER/NEED pair"]
    return []
