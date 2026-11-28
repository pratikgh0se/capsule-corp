"""Cost per card and its advisory line (row 584, Z7), host-measured from `task_runs`.
Fresh = in + out + cache write, cache reads apart; a run killed before run.json adds no tokens."""
import json
import re
import sqlite3
import statistics
import subprocess
import sys

from capsule import blocks, workspace
from capsule.db import event

WINDOW, MIN_LANDED, FACTOR = 20, 5, 2
_NET = re.compile(r"^\+(\d+)/-(\d+)$")
_NOT_PRODUCTION = re.compile(r"(^|/)tests?/|^(docs|scratchpad|contracts/intent)/|\.md$")
_FRESH = "COALESCE(tokens_in,0) + COALESCE(tokens_out,0) + COALESCE(tokens_cache_write,0)"
_LANDED = ("SELECT t.id FROM tasks t WHERE t.status='done' AND t.id != ? AND EXISTS"
           " (SELECT 1 FROM task_events e WHERE e.task_id=t.id AND e.kind='completed'"
           " AND json_extract(e.payload,'$.gates') IS NOT NULL AND "
           + blocks.not_closed_after("t.id", "e.id") + ")"
           " ORDER BY t.completed_at DESC, t.id DESC LIMIT ?")


def card_cost(conn, task_id, since=0):
    """{tokens, cache_read, wall_s, runs} over every run of the card started at or after `since`
    (all of them by default), failed and redone included."""
    row = conn.execute(f"SELECT COALESCE(SUM({_FRESH}),0), COALESCE(SUM(tokens_cache_read),0),"
                       " COALESCE(SUM(ended_at - started_at),0), COUNT(*)"
                       " FROM task_runs WHERE task_id=? AND started_at >= ?",
                       (task_id, since)).fetchone()
    return dict(zip(("tokens", "cache_read", "wall_s", "runs"), tuple(row)))


def landed_costs(conn, task_id):
    """`card_cost` of the last WINDOW cards that landed, not `task_id`."""
    return [card_cost(conn, r[0]) for r in conn.execute(_LANDED, (task_id, WINDOW)).fetchall()]


def trailing_median(conn, task_id):
    """(median fresh tokens, count) of the last WINDOW cards that landed, not `task_id`."""
    costs = [c["tokens"] for c in landed_costs(conn, task_id)]
    return (statistics.median(costs) if costs else 0), len(costs)


def _host_net(span):
    """(added, deleted) production lines of `base..head`, counted by git (the second oracle to the
    implementer's `net_lines`); None when git cannot say. Tests, docs, intent examples
    and binaries do not count. `span` is (ws, base, head)."""
    if span is None:
        return None
    ws, base, head = span
    try:
        out = workspace._git("diff", "--numstat", "--no-renames", "--end-of-options",
                             f"{base}..{head}", ws=ws).stdout
    except (RuntimeError, subprocess.SubprocessError, OSError):
        return None
    rows = [line.split("\t", 2) for line in out.splitlines()]
    kept = [(a, d) for a, d, path in rows if a.isdigit() and not _NOT_PRODUCTION.search(path)]
    return sum(int(a) for a, _ in kept), sum(int(d) for _, d in kept)


def has_gain(conn, task_id, net):
    """Host net lines delete as many as they add, or an incident fix (a touched test is no signal:
    35 of 38 cards did)."""
    fixes = conn.execute("SELECT 1 FROM incidents WHERE fix_card=?", (task_id,)).fetchone()
    deletes = bool(net and any(net) and net[1] >= net[0])
    return deletes or bool(fixes)


def _mismatch(net, report):
    """The report's `net_lines` when it disagrees with git's count (never trusted), else ''."""
    said = report.get("net_lines") or ""
    ours = f"+{net[0]}/-{net[1]}" if net else None
    return said if _NET.match(said) and net and said != ours else ""


def _measure(conn, task_id, net):
    spent, (median, landed) = card_cost(conn, task_id), trailing_median(conn, task_id)
    text = f"{spent['tokens']} tokens / {round(spent['wall_s'] / 60)} min"
    over = landed >= MIN_LANDED and spent["tokens"] > FACTOR * median > 0
    if over and not has_gain(conn, task_id, net):
        text += f" ({spent['tokens'] / median:.1f}x median), no measured gain"
    return spent, text


def record(conn, task_id, impl, span=None, now=None):
    """Write the `cost` event, return the landing text; failures go to stderr, never the landing.
    `span` is (ws, base, head): git counts the production lines there, and the report's own
    `net_lines` is only compared with that count."""
    try:
        net = _host_net(span)
        spent, text = _measure(conn, task_id, net)
        mismatch = _mismatch(net, json.loads(impl["handoff"] or "{}"))
        event(conn, task_id, "cost", {**spent, "line": f"cost: {text}",
                                      "net": list(net) if net else None, "net_mismatch": mismatch},
              run_pk=impl["id"], run_id=impl["run_id"], now=now)
        return text
    except (sqlite3.Error, ValueError, TypeError, KeyError) as exc:
        print(f"capsule cost: {task_id} not recorded: {exc}", file=sys.stderr)
        return "not measured"
