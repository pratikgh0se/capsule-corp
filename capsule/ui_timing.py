"""Timing judge for the UI proof gate: hang and click (F4 b and c).

Usage: python3 ui_timing.py judge <trace dir> --budget <budget file>

The judge reads numbers that an emitter recorded on a cold app. It prints one
line for each measure and one verdict line. Exit 0 is a pass, exit 1 is a
fail, exit 2 is bad input. It imports only the standard library, so the Mac
lane can run this file alone. It writes nothing.
"""
import argparse
import hashlib
import json
import math
import statistics
import sys
from pathlib import Path

RUNS = ("run1", "run2", "run3")
HANG_SCHEMA = "ui_hang.v1"
CLICK_SCHEMA = "ui_click.v1"
MAX_PERIOD_MS = 50
LINK_MS = 100


class BadInput(Exception):
    """The input is malformed. The judge exits 2 and never passes."""


def _parse(raw, label):
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        raise BadInput("cannot parse %s" % label) from None


def _read_file(root, rel):
    try:
        return _parse((root / rel).read_bytes(), rel)
    except OSError:
        raise BadInput("cannot read %s" % rel) from None


def _need(data, key, kind, where):
    value = data.get(key) if isinstance(data, dict) else None
    ok = isinstance(value, kind) and not isinstance(value, bool)
    if ok and isinstance(value, float) and not math.isfinite(value):
        ok = False
    if not ok:
        raise BadInput("%s: bad or missing '%s'" % (where, key))
    return value


def _unique(items, key, where):
    names = [i[key] for i in items]
    if len(set(names)) != len(names):
        raise BadInput("%s: duplicate '%s'" % (where, key))


def _load_budget(path):
    try:
        raw = Path(path).read_bytes()
    except OSError:
        raise BadInput("budget file cannot be read") from None
    data = _parse(raw, "budget")
    num = (int, float)
    out = {k: _need(data, k, num, "budget") for k in ("hang_ms", "first_ms", "done_ms")}
    out["tour"] = _need(data, "tour", list, "budget")
    out["controls"] = _need(data, "controls", list, "budget")
    if not out["tour"] or not out["controls"]:
        raise BadInput("budget: empty tour or controls, nothing to judge")
    tour = out["tour"]
    if not all(isinstance(t, str) for t in tour) or len(set(tour)) != len(tour):
        raise BadInput("budget: tour needs unique strings")
    for ctl in out["controls"]:
        _need(ctl, "id", str, "budget control")
        if "expect_label" in ctl:
            _need(ctl, "expect_label", str, "budget control")
    _unique(out["controls"], "id", "budget")
    out["digest"] = hashlib.sha256(raw).hexdigest()[:12]
    return out


def _check_hang(data, where):
    if not isinstance(data, dict) or data.get("schema") != HANG_SCHEMA:
        raise BadInput("%s: unknown schema" % where)
    for step in _need(data, "steps", list, where):
        _need(step, "name", str, where)
        _need(step, "start_ms", (int, float), where)
        _need(step, "end_ms", (int, float), where)
    _unique(data["steps"], "name", where)
    for rec in ("watchdog", "runloop"):
        for stall in _need(_need(data, rec, dict, where), "stalls", list, where):
            _need(stall, "at_ms", (int, float), where)
            _need(stall, "dur_ms", (int, float), where)
    wd = data["watchdog"]
    _need(wd, "period_ms", (int, float), where)
    _need(wd, "ticks", (int, float), where)


def _check_click(data, where):
    if not isinstance(data, dict) or data.get("schema") != CLICK_SCHEMA:
        raise BadInput("%s: unknown schema" % where)
    for ctl in _need(data, "controls", list, where):
        _need(ctl, "id", str, where)
        for key in ("event_ms", "first_change_ms", "settled_ms"):
            _need(ctl, key, (int, float), where)
        for key in ("before", "at_100"):
            _need(_need(ctl, "frames", dict, where), key, str, where)
        _need(ctl, "labels_at_300", list, where)
    _unique(data["controls"], "id", where)


def _load_runs(trace):
    root = Path(trace)
    runs = []
    for name in RUNS:
        if not (root / name).is_dir():
            raise BadInput("missing run folder %s" % name)
        hang = _read_file(root, name + "/hang.json")
        click = _read_file(root, name + "/click.json")
        _check_hang(hang, name + "/hang.json")
        _check_click(click, name + "/click.json")
        runs.append((hang, click))
    return runs


def _longest(stalls, step):
    """Longest stall that overlaps the step, or 0."""
    lo, hi = step["start_ms"], step["end_ms"]
    return max([s["dur_ms"] for s in stalls
                if s["at_ms"] <= hi and s["at_ms"] + s["dur_ms"] >= lo] or [0])


def _linked(stall, others):
    """True if the other record has a stall of half the length near this one."""
    return any(abs(o["at_ms"] - stall["at_ms"]) <= LINK_MS
               and o["dur_ms"] >= stall["dur_ms"] / 2.0 for o in others)


def _disagree(hang, limit):
    wd, rl = hang["watchdog"]["stalls"], hang["runloop"]["stalls"]
    for one, other in ((wd, rl), (rl, wd)):
        for stall in one:
            if stall["dur_ms"] > limit and not _linked(stall, other):
                return stall
    return None


def _hang_setup_bad(hang, budget):
    """Return a reason when the run does not prove a cold, watched tour."""
    steps = {s["name"] for s in hang["steps"]}
    missing = [n for n in budget["tour"] if n not in steps]
    wd = hang["watchdog"]
    span = max([s["end_ms"] for s in hang["steps"]] or [0]) \
        - min([s["start_ms"] for s in hang["steps"]] or [0])
    if hang.get("cold") is not True:
        return "cold is not true"
    if missing:
        return "tour step missing: %s" % missing[0]
    if wd["period_ms"] <= 0 or wd["period_ms"] > MAX_PERIOD_MS:
        return "watchdog period %s ms is not in 1..%s" % (wd["period_ms"], MAX_PERIOD_MS)
    if wd["ticks"] < span / wd["period_ms"] / 2.0:
        return "watchdog ticks %s are too few, it did not run" % wd["ticks"]
    return None


def _judge_hang(runs, budget):
    """Return (lines, failed rule names) for H1 to H3."""
    lines, failed, limit = [], set(), budget["hang_ms"]
    hangs = [r[0] for r in runs]
    for idx, hang in enumerate(hangs, 1):
        why = _hang_setup_bad(hang, budget)
        if why:
            lines.append("FAIL H3 run%d: %s" % (idx, why))
            failed.add("H3")
        bad = _disagree(hang, limit)
        if bad:
            lines.append("FAIL H2 contradiction run%d: stall %s ms at %s ms has no match "
                         "in the other record" % (idx, bad["dur_ms"], bad["at_ms"]))
            failed.add("H2")
    if "H3" in failed:
        return lines, failed
    for name in budget["tour"]:
        lines += _step_lines(hangs, name, limit, failed)
    return lines, failed


def _step_lines(hangs, name, limit, failed):
    out, meds = [], {}
    for rec in ("watchdog", "runloop"):
        vals = []
        for hang in hangs:
            step = next(s for s in hang["steps"] if s["name"] == name)
            vals.append(_longest(hang[rec]["stalls"], step))
        meds[rec] = statistics.median(vals)
        out.append("hang step=%r %s median=%s max=%s runs=%s limit=%s" % (
            name, rec, meds[rec], max(vals), vals, limit))
        if meds[rec] > limit:
            out.append("FAIL H1 step=%r %s stall %s ms > %s ms" % (name, rec, meds[rec], limit))
            failed.add("H1")
    return out


def _order_bad(ctl):
    ev, fc, st = ctl["event_ms"], ctl["first_change_ms"], ctl["settled_ms"]
    return min(ev, fc, st) < 0 or not (ev <= fc <= st)


def _judge_click(runs, budget):
    """Return (lines, failed rule names) for C1 to C5."""
    lines, failed = [], set()
    for spec in budget["controls"]:
        cid = spec["id"]
        found = [next((c for c in r[1]["controls"] if c["id"] == cid), None) for r in runs]
        if None in found:
            lines.append("FAIL C5 control=%r run%d: control missing"
                         % (cid, found.index(None) + 1))
            failed.add("C5")
            continue
        bad = [i for i, c in enumerate(found, 1) if _order_bad(c)]
        if len(bad) >= 2:
            lines.append("FAIL C5 control=%r run%s: negative or out of order times"
                         % (cid, bad[0]))
            failed.add("C5")
            continue
        lines += _control_lines(cid, spec, found, budget, failed)
    return lines, failed


def _control_lines(cid, spec, found, budget, failed):
    badrun = [_order_bad(c) for c in found]
    inf = float("inf")
    first = [round(c["first_change_ms"] - c["event_ms"], 3) for c in found]
    done = [round(c["settled_ms"] - c["event_ms"], 3) for c in found]
    same = sum(1 for c in found if c["frames"]["before"] == c["frames"]["at_100"])
    miss = _label_missing(spec, found)
    mf = statistics.median([inf if b else v for v, b in zip(first, badrun)])
    md = statistics.median([inf if b else v for v, b in zip(done, badrun)])
    out = ["click control=%r first_ms median=%s max=%s runs=%s limit=%s; done_ms median=%s "
           "max=%s runs=%s limit=%s; frame_unchanged_runs=%d" % (
               cid, mf, max(first), first, budget["first_ms"],
               md, max(done), done, budget["done_ms"], same)]
    out += ["note C5 control=%r run%d: negative or out of order times (1 of 3)" % (cid, i)
            for i, b in enumerate(badrun, 1) if b]
    if "expect_label" not in spec:
        out.append("note C4 control=%r: no expect_label, label not checked" % cid)
    checks = (
        ("C1", mf > budget["first_ms"], "first change %s ms > %s ms" % (mf, budget["first_ms"])),
        ("C2", md > budget["done_ms"], "settled %s ms > %s ms" % (md, budget["done_ms"])),
        ("C3", same >= 2, "screen unchanged at 100 ms in %d of 3 runs" % same),
        ("C4", miss >= 2, "label %r missing at 300 ms in %d of 3 runs"
         % (spec.get("expect_label"), miss)),
    )
    for rule, broken, text in checks:
        if broken:
            out.append("FAIL %s control=%r %s" % (rule, cid, text))
            failed.add(rule)
    return out


def _label_missing(spec, found):
    want = spec.get("expect_label")
    if want is None:
        return 0
    return sum(1 for c in found if want not in c["labels_at_300"])


def _judge(trace, budget_path):
    budget = _load_budget(budget_path)
    runs = _load_runs(trace)
    head = "limits hang_ms=%s first_ms=%s done_ms=%s" % (
        budget["hang_ms"], budget["first_ms"], budget["done_ms"])
    hl, hf = _judge_hang(runs, budget)
    cl, cf = _judge_click(runs, budget)
    failed = sorted(hf | cf)
    tail = "budget=%s runs=%d" % (budget["digest"], len(runs))
    if failed:
        verdict = "UI-PROOF FAIL timing %s failed=%s" % (tail, ",".join(failed))
    else:
        verdict = "UI-PROOF PASS timing " + tail
    return [head] + hl + cl + [verdict], (1 if failed else 0)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="ui_timing.py")
    sub = parser.add_subparsers(dest="cmd", required=True)
    one = sub.add_parser("judge")
    one.add_argument("trace")
    one.add_argument("--budget", required=True)
    args = parser.parse_args(argv)
    try:
        lines, rc = _judge(args.trace, args.budget)
    except (BadInput, KeyError, TypeError, AttributeError, StopIteration) as exc:
        print("UI-PROOF ERROR %s" % exc)
        return 2
    print("\n".join(lines))
    return rc


if __name__ == "__main__":
    sys.exit(main())
