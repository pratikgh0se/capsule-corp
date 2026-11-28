"""Per-board tick-dispatcher config and liveness (factory-autonomy Task 10, spec 4.5). A
separate module, not another addition to `capsule/unblocked.py` (row 514: new logic goes into
new modules with one-line call sites - `unblocked.py` is already at its size baseline)."""
import json
import sys
from pathlib import Path

from capsule import config, paths, tick_guard


def caps_from(whole):
    """board/max_running/epic_branches/assignees/routing, tolerantly, or `None` (fix
    Row 492: every configured board routes assignees the same way,
    `load_caps`'s legacy board included - `board_items`' `unassigned` gate must not
    go silent just because a board is served through `boards/<board>/config.json`
    instead of the legacy top level)."""
    board, caps = whole.get("board"), whole.get("caps")
    if not isinstance(board, str) or not board or not isinstance(caps, dict):
        return None
    mr = caps.get("max_running")
    if not isinstance(mr, int) or isinstance(mr, bool) or mr <= 0:
        return None
    assignees, routing = whole.get("assignees"), whole.get("routing")
    if isinstance(assignees, dict):   # a non-object stays tolerant (reads as empty)
        config.validate_assignees(assignees)   # a non-string entry is a clear ValueError
    return {"board": board, "max_running": mr,
            "epic_branches": config.epic_branches_from_list(whole.get("epics")),
            "assignees": assignees if isinstance(assignees, dict) else {},
            "routing": routing if isinstance(routing, dict) else {},
            **({"design_briefs": True} if whole.get("design_briefs") is True else {})}


def load_caps_all(capsule_home, legacy):
    """Every configured board: `legacy` (`unblocked.load_caps`'s own result, or None)
    plus every `boards/*/config.json` (skipped, loudly to stderr, if it names another
    board)."""
    out = {}
    if legacy:
        out[legacy["board"]] = dict(legacy)
    root = paths.home() if capsule_home is None else Path(capsule_home)
    for f in sorted((root / "boards").glob("*/config.json")):
        try:
            whole = json.loads(f.read_text())
        except (OSError, ValueError) as exc:
            raise ValueError(f"{f}: {exc}") from exc
        try:
            caps = caps_from(whole) if isinstance(whole, dict) else None
        except ValueError as exc:
            raise ValueError(f"{f}: {exc}") from exc
        if caps is None:
            continue
        if caps["board"] != f.parent.name:
            print(f"capsule unblocked: {f} names board {caps['board']!r}; skipped", file=sys.stderr)
            continue
        out[caps["board"]] = caps
    return out


def state(board, capsule_home, *, check=None):
    """`check(board, home)` or `tick_guard.dispatcher` (flock + argv scan) - Task 11's cutover:
    no more `legacy` shortcut, every board is judged by the lock plus the argv scan."""
    home = paths.home() if capsule_home is None else Path(capsule_home)
    return check(board, home) if check else tick_guard.dispatcher(home, board)


def blocker(dispatcher_state):
    """`None` for live (today's gates apply); `(False, "no-dispatcher")` for none (unblocked,
    unowned); `(True, "dispatcher-uncertain")` otherwise. Task 11 removes the `legacy` case."""
    if dispatcher_state == "live":
        return None
    return (False, "no-dispatcher") if dispatcher_state == "none" else (True, "dispatcher-uncertain")
