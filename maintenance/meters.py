#!/usr/bin/env python3
"""meters.py - factory meters v1 (W323): a READ-ONLY report on how the factory is doing.

Reads the board DB (`file:<path>?mode=ro`) and git history, never writes either. Old-system-free.
Run `python3 maintenance/meters.py --since 2026-09-05` (markdown; `--json`; `--out DIR` also
writes meters-<date>.{json,md}). Per ISO week and period: landed cards, wait vs work, tokens,
human/CoS decisions, day vs night outcomes, reverts. Definitions:
- landed (factory): done card whose `completed` event's run_id is a real run, at that time.
  Otherwise (stub-only or no run) the closed reason (or the stub's CLOSE: summary) decides:
  "landed ..." is landed_outside; obsolete/won't fix/already fixed is not_landed.
  `guide_ruling:` cards are excluded from every card metric.
- real runs only (no close stubs, spawn_failed, gave_up, scheduled). concurrency: max overlap
  of real runs of positive length, INFERRED (no lane field exists).
- work = sum of run durations, over every done non-ruling card with a real run. wait (factory-
  landed cards only) = created_at to first real run plus the union of review_deferred / blocked
  intervals up to the completed event (never up to a later CoS close).
- run outcome: failed = failed/timed_out/crashed or any error_class; completed; other.
- decision latency: intervention `at` minus the latest earlier request_review/blocked of the
  same card. Authors exist only from 2026-10-21; earlier ones are "not recorded", not zero.
- usd: NOTIONAL value, not a bill (subscription-only). Tokens of a landed card's runs priced at
  the list prices in prices.json; an unknown model uses its fallback model (counted in
  usd_assumed_runs); a null cache_write price is 1.25x the input price.
"""
import argparse
import json
import os
import re
import sqlite3
import statistics
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from operator import itemgetter
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pricing  # noqa: E402
from capsule import paths, safe_write  # noqa: E402

MAINT = Path(__file__).resolve().parent
DB_ENV = "CAPSULE_METERS_DB"
TZ = "Asia/Bangkok"
NIGHT = (23, 7)                      # local hours, [start, end) wrapping midnight
# (name, first local date it covers). legacy-era runs up to 2026-10-22; the baseline entry is
# appended only when a cutover date is given (default: open-ended off-legacy-push).
PERIODS = (("legacy-era", "0000-01-01"), ("off-legacy-push", "2026-10-23"))
BASELINE = "baseline"
FAILED = {"failed", "timed_out", "crashed"}
NOT_REAL = ("spawn_failed", "gave_up", "scheduled")
NO_AUTHOR = "not_recorded_before_2026-10-21"
NOT_LANDED_RE = re.compile(
    r"^\s*(won'?t fix|obsolete|already|merged into|superseded|pratik decided)", re.I)
DEFER_END = {"review_started", "claimed", "completed", "closed"}
BLOCK_END = {"unblocked", "claimed", "completed", "closed"}
CARD_RE = re.compile(r"\bW\d{3,4}\b|\bt_[0-9a-f]{8}\b")
TOKEN_COLS = ("tokens_in", "tokens_out", "tokens_cache_read", "tokens_cache_write")
NOTIONAL = "notional value at API list prices; subscription-only, not a bill"



class _Meters:
    """Local-time helpers: ISO week, period tag and day/night, all in one timezone."""

    def __init__(self, tz=TZ, night=NIGHT, cutover=None, push_start=None):
        self.tz = ZoneInfo(tz)
        self.night = night
        starts = list(PERIODS)
        if push_start:
            starts[1] = (starts[1][0], push_start)
        if cutover:
            starts.append((BASELINE, cutover))
        self.starts = starts

    def local(self, ts):
        return datetime.fromtimestamp(ts, self.tz)

    def period(self, ts):
        day = self.local(ts).date().isoformat()
        return [name for name, start in self.starts if start <= day][-1]

    def week(self, ts):
        return self.local(ts).strftime("%G-W%V")

    def is_night(self, ts):
        hour, (a, b) = self.local(ts).hour, self.night
        return (hour >= a or hour < b) if a > b else (a <= hour < b)


def _med_h(xs):
    return round(statistics.median(xs) / 3600, 2) if xs else None


def _union(intervals):
    total, end = 0, float("-inf")
    for s, e in sorted(intervals):
        total += max(0, e - max(s, end))
        end = max(end, e)
    return total


def _load_board(db):
    cols = ", ".join(("id", "task_id", "started_at", "ended_at", "outcome", "error_class", "model",
                      *TOKEN_COLS))
    real = f"profile IS NOT NULL AND COALESCE(outcome, '') NOT IN {NOT_REAL}"
    sql = {"tasks": "SELECT id, title, status, created_at, completed_at FROM tasks",
           "runs": f"SELECT {cols} FROM task_runs WHERE {real} ORDER BY started_at, id",
           "events": "SELECT * FROM task_events ORDER BY id"}
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return {k: [dict(r) for r in con.execute(q)] for k, q in sql.items()}
    finally:
        con.close()


def _load_reverts(repo):
    out = subprocess.run(
        ["git", "log", "--regexp-ignore-case", "--grep=revert", "--format=%H%x1f%ct%x1f%B%x1e"],
        cwd=repo, capture_output=True, text=True, check=True).stdout
    recs = [r.strip("\n").split("\x1f", 2) for r in out.split("\x1e") if r.strip()]
    return [{"ts": int(ts), "card": (CARD_RE.search(msg) or [None])[0]} for _, ts, msg in recs]


def _payload(ev):
    try:
        p = json.loads(ev["payload"] or "{}")
    except ValueError:
        p = None
    return p if isinstance(p, dict) else {}


def _card_times(task, runs, evs, done):
    """(work_s, wait_s) for one landed card, or None when it has no run to anchor the wait."""
    if not runs:
        return None
    work = sum(max(0, r["ended_at"] - r["started_at"]) for r in runs if r["ended_at"] is not None)
    wait = max(0, min(r["started_at"] for r in runs) - task["created_at"])
    ivs = []
    for i, ev in enumerate(evs):
        ends = {"review_deferred": DEFER_END, "blocked": BLOCK_END}.get(ev["kind"])
        if ends is None:
            continue
        end = next((e["created_at"] for e in evs[i + 1:] if e["kind"] in ends), done)
        ivs.append((ev["created_at"], min(max(end, ev["created_at"]), max(done, ev["created_at"]))))
    return work, wait + _union(ivs)


def _max_concurrency(runs, key):
    """key(started_at) -> max overlapping runs seen at any start in that group (inferred
    lanes); a run that never ran (zero length or unended) is not a lane."""
    marks = []
    for r in filter(lambda r: (r["ended_at"] or 0) > r["started_at"], runs):
        marks += [(r["started_at"], 1), (r["ended_at"], 0)]
    best, live = {}, 0
    for ts, is_start in sorted(marks):          # ends sort before starts at a tie
        live += 1 if is_start else -1
        if is_start:
            best[key(ts)] = max(best.get(key(ts), 0), live)
    return best


COUNTS = ("landed", "landed_outside", "not_landed", "landed_day", "landed_night",
          "cards_unmeasured",
          "cards_with_tokens", "runs", "runs_with_tokens", "ended_at_null", "reverts_linked",
          "reverts_unlinked", "max_concurrency_inferred")


def _add(a, counts, **lists):
    a["c"].update(counts)
    for key, vals in lists.items():
        a[key] += vals


def _landing(task, evs, real):
    """(class, ts): landed by the factory (first `completed` event on a real run id), landed
    outside it (close reason says landed) or not landed (any other close)."""
    done = [e["created_at"] for e in evs if e["kind"] == "completed" and e["run_id"] in real]
    if done:
        return "landed", min(done)
    said = [_payload(e).get("reason") or _payload(e).get("summary") or ""
            for e in evs if e["kind"] in ("closed", "completed")]
    why = re.sub(r"^\s*close:\s*", "", (said or [""])[-1], flags=re.I)
    ok = re.search(r"\blanded\b", why, re.I) and not NOT_LANDED_RE.match(why)
    return ("landed_outside" if ok else "not_landed"), task["completed_at"]


def _run_class(r):
    done = "completed" if r["outcome"] == "completed" else "other"
    return "failed" if r["error_class"] or r["outcome"] in FAILED else done


def _span(since, until, tz):
    """[lo, hi) epoch bounds for inclusive local dates; None is open."""
    def at(day, plus):
        dt = datetime.fromisoformat(day).replace(tzinfo=tz) + timedelta(days=plus)
        return int(dt.timestamp())
    return at(since, 0) if since else None, at(until, 1) if until else None


class _Report:
    """One pass over the board: every metric lands in per-(week, period) and per-period groups."""

    def __init__(self, m, since, until, prices):
        self.m, self.prices = m, prices
        self.lo, self.hi = _span(since, until, m.tz)
        self.keys = {"week": lambda ts: (m.week(ts), m.period(ts)),
                     "period": lambda ts: (m.period(ts),)}
        def blank():
            return {"c": Counter(), "work": [], "wait": [], "latency": []}
        self.groups = {n: defaultdict(blank) for n in self.keys}
        self.days = defaultdict(Counter)

    def inside(self, ts):
        return ts is not None and (self.lo or ts) <= ts < (self.hi or ts + 1)

    def put(self, ts, fn, *args, **kw):
        for name, kf in self.keys.items():
            fn(self.groups[name][kf(ts)], *args, **kw)

    def _cards(self, board, by_runs, by_ev):
        real = {r["id"] for r in board["runs"]}
        for t in board["tasks"]:
            evs, runs = by_ev[t["id"]], by_runs[t["id"]]
            if t["status"] != "done" or t["title"].startswith("guide_ruling:"):
                continue
            cls, ts = _landing(t, evs, real)
            if not self.inside(ts):
                continue
            times, fac = _card_times(t, runs, evs, ts), cls == "landed"
            has_tok = fac and any(r["tokens_in"] is not None for r in runs)
            tok = {c: sum(r[c] or 0 for r in runs) for c in TOKEN_COLS} if has_tok else {}
            counts = {cls: 1, "cards_unmeasured": times is None, "cards_with_tokens": has_tok,
                      **tok}
            if has_tok:
                priced = [pricing.price_row(  # the one pricing rule, shared with the sampler
                    {"model": r["model"], "input": r["tokens_in"], "output": r["tokens_out"],
                     "cache_read": r["tokens_cache_read"], "cache_write": r["tokens_cache_write"]},
                    self.prices) for r in runs if r["tokens_in"] is not None]
                counts["usd"] = sum(p["usd_estimate"] for p in priced)
                counts["usd_assumed_runs"] = sum(bool(p.get("price_assumed")) for p in priced)
            if fac:
                counts["landed_night" if self.m.is_night(ts) else "landed_day"] = 1
            self.put(ts, _add, counts, work=times[:1] if times else (),
                     wait=times[1:] if times and fac else ())

    def _runs(self, board):
        scoped = [r for r in board["runs"] if self.inside(r["started_at"])]
        for r in scoped:
            side = "night" if self.m.is_night(r["started_at"]) else "day"
            counts = {"runs": 1, "runs_with_tokens": r["tokens_in"] is not None,
                      "ended_at_null": r["ended_at"] is None, f"{side}_{_run_class(r)}": 1}
            self.put(r["started_at"], _add, counts)
        for name, kf in self.keys.items():
            for k, v in _max_concurrency(scoped, kf).items():
                self.groups[name][k]["c"]["max_concurrency_inferred"] = v

    def _decision(self, ev, ask):
        p = _payload(ev)
        at = p["at"] if isinstance(p.get("at"), int) else ev["created_at"]
        if not self.inside(at):
            return
        who = p.get("author") if p.get("author") in ("human", "cos") else NO_AUTHOR
        self.days[self.m.local(at).date().isoformat()][who] += 1
        late = [at - ask] if ask is not None and at >= ask else []
        self.put(at, _add, {f"decision_{who}": 1}, latency=late)

    def _decisions(self, by_ev):
        for evs in by_ev.values():
            ask = None                    # latest request_review/blocked since the last decision
            for e in evs:
                if e["kind"] in ("request_review", "blocked"):
                    ask = e["created_at"]
                elif e["kind"] == "intervention":
                    self._decision(e, ask)
                    ask = None

    def _reverts(self, repo):
        for rv in filter(lambda r: self.inside(r["ts"]), _load_reverts(repo)):
            key = "reverts_linked" if rv["card"] else "reverts_unlinked"
            self.put(rv["ts"], _add, {key: 1})


def _finish(a):
    c = a["c"]
    n_tok, n_run = c["cards_with_tokens"], c["runs"]
    out = {k: c[k] for k in COUNTS}
    out.update(
        work_h_median=_med_h(a["work"]), wait_h_median=_med_h(a["wait"]),
        work_h_total=round(sum(a["work"]) / 3600, 2), wait_h_total=round(sum(a["wait"]) / 3600, 2),
        tokens={t: c[t] for t in TOKEN_COLS},
        tokens_per_card={t: round(c[t] / n_tok) if n_tok else None for t in TOKEN_COLS},
        usd=round(c["usd"], 4), usd_per_card=round(c["usd"] / n_tok, 4) if n_tok else None,
        usd_assumed_runs=c["usd_assumed_runs"], usd_basis=NOTIONAL,
        token_coverage=round(c["runs_with_tokens"] / n_run, 3) if n_run else None,
        ended_at_null_rate=round(c["ended_at_null"] / n_run, 3) if n_run else None,
        runs_by_daynight={k: v for k, v in sorted(c.items()) if k.startswith(("day_", "night_"))},
        decisions={w: c[f"decision_{w}"] for w in ("human", "cos", NO_AUTHOR)},
        decision_latency_n=len(a["latency"]), decision_latency_h_median=_med_h(a["latency"]))
    return out


def _compute(db, repo, m, since=None, until=None, prices=None):
    board = _load_board(db)
    by_runs, by_ev = defaultdict(list), defaultdict(list)
    for key, table in (("runs", by_runs), ("events", by_ev)):
        for row in board[key]:
            table[row["task_id"]].append(row)
    rep = _Report(m, since, until, prices or pricing.load_prices())
    rep._cards(board, by_runs, by_ev)
    rep._runs(board)
    rep._decisions(by_ev)
    rep._reverts(repo)
    g = rep.groups
    return {"meta": {"db": str(db), "since": since, "until": until, "tz": str(m.tz),
                     "night_hours": list(m.night), "periods": m.starts,
                     "lanes": "max_concurrency_inferred is inferred from overlapping task_runs",
                     "usd": NOTIONAL},
            "periods": [dict(period=k[0], **_finish(v))
                        for k, v in sorted(g["period"].items())],
            "weeks": [dict(week=k[0], period=k[1], **_finish(v))
                      for k, v in sorted(g["week"].items())],
            "interventions_by_day": {d: dict(c) for d, c in sorted(rep.days.items())}}


def _cell(v):
    return "-" if v is None else str(v)


def _tokens(key):
    return lambda r: r["tokens_per_card"][key]


def _outcomes(side):
    names = [f"{side}_{c}" for c in ("completed", "failed", "other")]
    return lambda r: "/".join(str(r["runs_by_daynight"].get(n, 0)) for n in names)


_G = itemgetter
DEC = f"decisions human / cos / {NO_AUTHOR}"
ROWS = {
    "landed (factory)": _G("landed"), "landed outside factory": _G("landed_outside"),
    "not landed": _G("not_landed"),
    "landed day / night": lambda r: f"{r['landed_day']} / {r['landed_night']}",
    "max concurrency (inferred)": _G("max_concurrency_inferred"),
    "work h median": _G("work_h_median"), "wait h median": _G("wait_h_median"),
    "cards with no run": _G("cards_unmeasured"), "ended_at null rate": _G("ended_at_null_rate"),
    "runs": _G("runs"), "token coverage (runs)": _G("token_coverage"),
    "tokens in / card": _tokens("tokens_in"), "tokens out / card": _tokens("tokens_out"),
    "cache read / card": _tokens("tokens_cache_read"),
    "cache write / card": _tokens("tokens_cache_write"),
    "USD notional total (assumed-model runs)": lambda r: f"${r['usd']} ({r['usd_assumed_runs']})",
    "USD notional / card": lambda r: "-" if r["usd_per_card"] is None else f"${r['usd_per_card']}",
    DEC:
        lambda r: " / ".join(str(r["decisions"][w]) for w in ("human", "cos", NO_AUTHOR)),
    "decision latency n, h median":
        lambda r: f"{r['decision_latency_n']}, {_cell(r['decision_latency_h_median'])}",
    "runs day completed/failed/other": _outcomes("day"),
    "runs night completed/failed/other": _outcomes("night"),
    "reverts linked / unlinked": lambda r: f"{r['reverts_linked']} / {r['reverts_unlinked']}",
}
WEEK_COLS = ("landed (factory)", "max concurrency (inferred)", "work h median", "wait h median",
             "tokens in / card", "USD notional total (assumed-model runs)",
             "USD notional / card", "token coverage (runs)", DEC,
             "reverts linked / unlinked", "landed day / night")


def _markdown(rep):
    meta, periods = rep["meta"], rep["periods"]
    night = meta["night_hours"]
    out = [f"# Factory meters (read-only), {meta['since'] or 'start'} to {meta['until'] or 'now'}",
           f"Lanes are INFERRED from overlapping runs. Night = local {night[0]}:00-"
           f"{night[1]}:00 ({meta['tz']}). USD is {NOTIONAL}.\n",
           "## Per period", "| metric | " + " | ".join(r["period"] for r in periods) + " |",
           "|---|" + "---|" * len(periods)]
    for label, f in ROWS.items():
        cells = [f(r) for r in periods]
        out.append(f"| {label} | " + " | ".join(_cell(c) for c in cells) + " |")
    out += ["\n## Per ISO week (lanes inferred)", "| week | period | " + " | ".join(WEEK_COLS)
            + " |", "|" + "---|" * (2 + len(WEEK_COLS))]
    out += [f"| {r['week']} | {r['period']} | " + " | ".join(_cell(ROWS[c](r)) for c in WEEK_COLS)
            + " |" for r in rep["weeks"]]
    out.append(f"\n## Interventions per day ({DEC[10:]})")
    out += [f"- {d}: " + " / ".join(str(c.get(w, 0)) for w in ("human", "cos", NO_AUTHOR))
            for d, c in rep["interventions_by_day"].items()]
    return "\n".join(out) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", default=None, help=f"default: ${DB_ENV}, else the capsule-corp board")
    ap.add_argument("--repo", default=str(MAINT.parent), help="git repo for revert commits")
    ap.add_argument("--since", help="first local date, YYYY-MM-DD")
    ap.add_argument("--until", help="last local date, inclusive")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out", help="directory for meters-<date>.json and .md")
    ap.add_argument("--tz", default=TZ)
    ap.add_argument("--night", default="23-7", help="local hours START-END")
    ap.add_argument("--cutover", help="start date of 'baseline' (default: unset, open-ended)")
    ap.add_argument("--push-start", help="start date of 'off-legacy-push' (default 2026-10-23)")
    a = ap.parse_args(argv)
    m = _Meters(a.tz, tuple(int(x) for x in a.night.split("-")), a.cutover, a.push_start)
    db = a.db or os.environ.get(DB_ENV) or paths.board_db("capsule-corp")  # follows paths.home()
    rep = _compute(Path(db), a.repo, m, a.since, a.until)
    as_json, md = json.dumps(rep, indent=2) + "\n", _markdown(rep)
    sys.stdout.write(as_json if a.json else md)
    if a.out:
        out, stamp = Path(a.out), datetime.now(m.tz).date().isoformat()
        for ext, body in (("json", as_json), ("md", md)):
            safe_write.atomic_write(out, out / f"meters-{stamp}.{ext}", body, create_dirs=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
