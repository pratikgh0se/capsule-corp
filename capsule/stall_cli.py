"""`python3 -m capsule stalls [--board B] [--json]`: the open stalls, read-only (`stall.find`)."""
import json
import sys
import time

from capsule import answer_base, config, dispatch, paths
from capsule.stall import clock, find


def _board_cfg(board, home):
    """(board name, config) for `stalls`; a board with no readable config still gets its stalls
    from the defaults. The legacy form (no `--board`) reads the configured board's name."""
    try:
        cfg = config.load(paths.resolve_config(home, board))
    except (OSError, ValueError, KeyError):
        cfg = {"board": board or "", "epics": []}
    return board or cfg["board"], cfg


def _items(name, cfg, home, now):
    conn = answer_base.connect_ro_tolerant(paths.board_db(name, home))
    try:
        return find(conn, cfg, now, {"work_root": dispatch.root(name, home),
                                     "spool_root": paths.board_dir(name, home) / "spool",
                                     "home": home})
    finally:
        conn.close()


def _all_boards(home):
    return sorted(p.parent.name for p in (home / "boards").glob("*/kanban.db"))


def main(board=None, home=None, as_json=False, now=None):
    """Print the open stalls. Exit 0 with none at L2, 1 with one or more at L2, 2 with no board.
    With no `--board`, every board under the home is read and each item carries its `board`."""
    home = paths.home(home)
    now = now if now is not None else time.time()
    names = [board] if board else _all_boards(home)
    if not names:
        name, _ = _board_cfg(None, home)
        print(f"capsule stalls: no board file for {name or 'the configured board'}",
              file=sys.stderr)
        return 2
    items = []
    for name in names:
        name, cfg = _board_cfg(name, home)
        if not paths.board_db(name, home).is_file():
            print(f"capsule stalls: no board file for {name}", file=sys.stderr)
            return 2
        got = _items(name, cfg, home, now)
        items += [dict(it, board=name) for it in got] if not board else got
    if as_json:
        print(json.dumps(items))
    for it in ([] if as_json else items):
        tag = f"{it['board']}: " if "board" in it else ""
        print(f"{tag}{it['level']:5} {it['class']} {it['subject']} {clock(it['age_s'])}: "
              + "; ".join(it["evidence"]))
    return int(any(i["level"] == "L2" for i in items))
