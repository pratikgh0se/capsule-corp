"""Evals E2b (t_e4e9c324, row 617): the tokens oracle behind `eval_check`'s `fresh_tokens` metric.
A = the `task_runs` token columns. B = the Claude transcript of each run, joined by
`task_runs.run_id` -> `<spool>/<run_id>/run.json` `session_id` -> `<projects>/*/<session_id>.jsonl`,
assistant messages deduplicated by message id (the stream repeats a message's usage line).
Only the `usage` and `model` fields are read, never message text. All four usage fields stay
apart; fresh = input + output + cache_creation; no dollars."""
import collections
import json
import re
from pathlib import Path

FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
FRESH = FIELDS[:3]
_ID = _SESSION = re.compile(r"[0-9A-Za-z-]+")


def _transcript(spool, projects, run_id):
    """The run's transcript path, or None when run.json, its session id or the file is missing."""
    if not isinstance(run_id, str) or not _ID.fullmatch(run_id):
        return None
    try:
        sid = json.loads((Path(spool) / run_id / "run.json").read_text("utf-8")).get("session_id")
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(sid, str) or not _SESSION.fullmatch(sid):
        return None
    return next(iter(sorted(Path(projects).glob(f"*/{sid}.jsonl"))), None)


def compare(conn, spool, projects, since, until, unjoined_max):
    """{a, b, agree_join, detail} over the runs started in [since, until)."""
    rows = conn.execute(
        "SELECT task_id, run_id, model, COALESCE(tokens_in,0) + COALESCE(tokens_out,0)"
        " + COALESCE(tokens_cache_write,0) AS fresh FROM task_runs"
        " WHERE started_at>=? AND started_at<? ORDER BY id", (since, until)).fetchall()
    usage, unjoined = dict.fromkeys(FIELDS, 0), []
    card = collections.defaultdict(lambda: {"a": 0, "b": 0})
    model = {"a": collections.Counter(), "b": collections.Counter()}
    for r in rows:
        path = _transcript(spool, projects, r["run_id"])
        if path is None:
            unjoined.append({"task_id": r["task_id"], "run_id": r["run_id"], "a": r["fresh"]})
            continue
        card[r["task_id"]]["a"] += r["fresh"]
        model["a"][r["model"] or "?"] += r["fresh"]
        seen = {}    # one entry per message id; a repeated id keeps the per-field maximum
        for line in path.read_text("utf-8", errors="replace").splitlines():
            try:
                d = json.loads(line)
                m = d["message"] if d.get("type") == "assistant" else None
            except (ValueError, KeyError, TypeError):
                continue
            if isinstance(m, dict):
                got = {k: int((m.get("usage") or {}).get(k) or 0) for k in FIELDS}
                mid = m.get("id") or d.get("uuid")
                seen[mid] = (m.get("model") or "?", {
                    k: max(got[k], seen[mid][1][k]) if mid in seen else got[k] for k in FIELDS})
        for name, u in seen.values():
            n = sum(u[k] for k in FRESH)
            card[r["task_id"]]["b"] += n
            model["b"][name] += n
            for k in FIELDS:
                usage[k] += u[k]
    a = sum(r["fresh"] for r in rows)
    lost = sum(u["a"] for u in unjoined)
    return {"a": a, "b": sum(usage[k] for k in FRESH), "a_joined": a - lost,
            "agree_join": lost <= unjoined_max * a,
            "detail": {"usage": usage, "unjoined": unjoined, "per_card": dict(card),
                       "per_model": {k: dict(v) for k, v in model.items()}}}
