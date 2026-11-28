"""`python3 -m capsule tokens [--day YYYY-MM-DD] [--root DIR] [--json]`: the token
accountant (row 558)."""
import datetime
import json
import re
import statistics
import sys
from pathlib import Path

CARD_RE = re.compile(r"t_[0-9a-f]{8}")
ROLE_RE = re.compile(r"\b(implementer|reviewer|keeper)\b", re.IGNORECASE)
UNATTRIBUTED = "unattributed"
ROLES = ("implementer", "reviewer", "keeper", "other")
OUTCOMES = ("landed", "failed", "redone", "unknown")
PROMPT_CHARS = 4000
NOTE_RE = re.compile(r"<task-notification>(.*?)</task-notification>")


def default_root():
    return Path.home() / ".claude" / "projects"


def _lines(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            yield from fh
    except OSError:
        return


def _json(line):
    try:
        doc = json.loads(line)
    except ValueError:
        return None
    return doc if isinstance(doc, dict) else None


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content if isinstance(b, dict) and isinstance(b.get("text"), str))
    return ""


def _contained(path, root):
    """True when `path` sits under `root` with no symlink below it (root itself may be one)."""
    try:
        return path.resolve() == root.resolve().joinpath(*path.relative_to(root).parts)
    except (OSError, ValueError, RuntimeError):
        return False


def _notifications(root, day):
    """tool-use-id -> (status, subagent_tokens), from session files touched on or after `day`."""
    notes = {}
    for session in sorted(p for p in Path(root).glob("*/*.jsonl") if _contained(p, Path(root))):
        try:
            mtime = datetime.datetime.fromtimestamp(session.stat().st_mtime, datetime.timezone.utc).date().isoformat()
        except OSError:
            continue
        # speed only: a file last written before `day` cannot hold that day's notifications
        if mtime < day:
            continue
        for line in _lines(session):
            # one block at a time: ids and statuses never cross-pair
            for block in NOTE_RE.findall(line):
                tool, status, toks = (re.search(p, block) for p in (
                    r"<tool-use-id>([^<]+)</tool-use-id>", r"<status>([^<]+)</status>", r"<subagent_tokens>(\d+)<"))
                if tool and status:
                    notes[tool.group(1)] = (status.group(1), int(toks.group(1)) if toks else 0)
    return notes


def _read_agent(path):
    """(first timestamp, first user text, fresh tokens, cache-read tokens) of one subagent transcript."""
    first_ts, prompt, tokens, cache_read = None, "", 0, 0
    for line in _lines(path):
        doc = _json(line)
        if doc is None:
            continue
        first_ts = first_ts or (doc.get("timestamp") if isinstance(doc.get("timestamp"), str) else None)
        msg = doc.get("message") if isinstance(doc.get("message"), dict) else {}
        if doc.get("type") == "user" and not prompt:
            prompt = _text(msg.get("content"))[:PROMPT_CHARS]
        use = msg.get("usage") if doc.get("type") == "assistant" and isinstance(msg.get("usage"), dict) else {}
        cache_read += int(use.get("cache_read_input_tokens") or 0)
        tokens += sum(int(use.get(k) or 0) for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens"))
    return first_ts, prompt, tokens, cache_read


def _first(pattern, *texts):
    for text in texts:
        m = pattern.search(text)
        if m:
            return m.group(0).lower()
    return None


def scan(root, day):
    """One dict per subagent run that started on `day` (UTC): agent, card, role, tokens,
    notified, status, outcome, started."""
    root = Path(root)
    notes = _notifications(root, day)
    runs = []
    for path in sorted(p for p in root.glob("*/*/subagents/agent-*.jsonl") if _contained(p, root)):
        started, prompt, tokens, cache_read = _read_agent(path)
        if not started or started[:10] != day:
            continue
        meta_path = path.with_suffix(".meta.json")
        meta = (_json(meta_path.read_text(encoding="utf-8", errors="replace"))
                if meta_path.is_file() and _contained(meta_path, root) else None) or {}
        desc = meta.get("description") if isinstance(meta.get("description"), str) else ""
        status, notified = notes.get(meta.get("toolUseId"), (None, 0))
        runs.append({"agent": path.stem[len("agent-"):], "card": _first(CARD_RE, desc, prompt) or UNATTRIBUTED,
                     "role": _first(ROLE_RE, desc, prompt) or "other", "tokens": tokens, "notified": notified, "cache_read": cache_read,
                     "status": status, "started": started,
                     "outcome": "unknown" if status is None else "landed" if status == "completed" else "failed"})
    _mark_redone(runs)
    return runs


def _mark_redone(runs):
    groups = {}
    for run in sorted(runs, key=lambda r: (r["started"], r["agent"])):
        if run["card"] != UNATTRIBUTED:
            groups.setdefault((run["card"], run["role"]), []).append(run)
    for group in groups.values():
        for run in group[:-1]:
            if run["outcome"] in ("landed", "unknown"):
                run["outcome"] = "redone"


def report(runs):
    """The table as data: cards (largest first), roles, outcomes, median, grand_total, top3."""
    cards = {}
    for run in runs:
        c = cards.setdefault(run["card"], {"card": run["card"], "tokens": 0, "runs": 0, "failed": 0, "redone": 0,
                                                  "notified": 0, "cache_read": 0})
        c["tokens"] += run["tokens"]
        c["notified"] += run.get("notified", 0)
        c["cache_read"] += run.get("cache_read", 0)
        c["runs"] += 1
        c["failed"] += run["outcome"] == "failed"
        c["redone"] += run["outcome"] == "redone"
    rows = sorted(cards.values(), key=lambda c: (-c["tokens"], c["card"]))
    attributed = [c["tokens"] for c in rows if c["card"] != UNATTRIBUTED]
    median = statistics.median(attributed) if attributed else 0
    for c in rows:
        c["flags"] = ([] if not c["failed"] else ["failed_run"]) + (
            ["over_2x_median"] if median and c["card"] != UNATTRIBUTED and c["tokens"] > 2 * median else [])
        c["reason"] = (c["flags"][0] if c["flags"] else "redo" if c["redone"]
                       else UNATTRIBUTED if c["card"] == UNATTRIBUTED else "ordinary")
    return {"cards": rows, "median": median, "grand_total": sum(r["tokens"] for r in runs),
            "notified": sum(c["notified"] for c in rows), "cache_read": sum(c["cache_read"] for c in rows),
            "roles": _sum_by(runs, "role", ROLES), "outcomes": _sum_by(runs, "outcome", OUTCOMES), "top3": rows[:3]}


def _sum_by(runs, key, order):
    out = {k: 0 for k in order}
    for run in runs:
        out[run[key]] += run["tokens"]
    return {k: v for k, v in out.items() if v}


def _table(title, rows):
    width = max([len(str(r[0])) for r in rows] + [5])
    return [title] + [f"  {str(name):<{width}}  {tokens:>12,}" + (f"  {extra}" if extra else "")
                      for name, tokens, extra in rows]


def render(rep, day):
    if not rep["cards"]:
        return f"tokens for {day}: no subagent runs found"
    out = [f"tokens for {day} (fresh input + output + cache-creation; median card {rep['median']:,.0f})", ""]
    out += _table("per card", [(c["card"], c["tokens"], f"{c['runs']} runs" + "".join(f" {f}" for f in c["flags"]))
                               for c in rep["cards"]])
    out += [""] + _table("per role", [(k, v, "") for k, v in rep["roles"].items()])
    out += [""] + _table("per outcome", [(k, v, "") for k, v in rep["outcomes"].items()])
    out += ["", "top 3 spenders"] + [f"  {i}. {c['card']}  {c['tokens']:,}  {c['reason']}" for i, c in enumerate(rep["top3"], 1)]
    return "\n".join(out + ["", f"total {rep['grand_total']:,}"])


def add_parser(sub):
    p = sub.add_parser("tokens", help="subagent token spend per card, role and outcome for one day (row 558);"
                                      " read-only over Claude Code transcripts")
    p.add_argument("--day", default=None, help="UTC date YYYY-MM-DD; default today")
    p.add_argument("--root", default=None, help="transcripts root; default ~/.claude/projects")
    p.add_argument("--json", action="store_true", help="print the report as JSON")


def handle(args, parser):    # signature matches every other DISPATCH entry; parser unused here
    day = args.day or datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    try:
        datetime.date.fromisoformat(day)
    except ValueError:
        print(f"capsule tokens: --day must be YYYY-MM-DD, got {day!r}", file=sys.stderr)
        return 1
    root = Path(args.root) if args.root else default_root()
    home = Path.home().resolve()
    if root.resolve() == home or root.resolve() in home.parents:
        print(f"capsule tokens: refusing --root {root}: it is the home directory or one of its ancestors", file=sys.stderr)
        return 2
    if not root.is_dir():
        print(f"capsule tokens: no such directory: {root}", file=sys.stderr)
        return 1
    rep = report(scan(root, day))
    print(json.dumps(dict(rep, day=day)) if args.json else render(rep, day))
    return 0
