"""`python3 -m capsule.plan_trace <plan>`: the intent trace and term lock lint for a signed plan
(gates design 2026-11-27, unit U1). Every designed item must have a card, a proof and a real
reference; every term must have one meaning. Pure string and number checks, no writes, no AI."""
import json
import re
import sys
from pathlib import Path

from capsule import plan_cut

KEYS = ("item", "name", "source", "token", "terms", "cards", "proof", "reference", "waiver")
PROOFS = ("still", "motion", "timing", "owner", "test")
SOURCE_RE = re.compile(r"^(.+):(\d+)(?:-(\d+))?$")
NONE_RE = re.compile(r"^none\s*\(\s*(\S.*?)\s*\)$")
BAD_JSON = "<invalid JSON>"
BIG = 10 ** 9


def _no_preview(p):
    """True when the plan has no Preview, or Preview is none (with or without a reason)."""
    return p is None or bool(re.match(r"(?i)^(none\b.*)?$", p.strip()))


def _span(source):
    """(file, a, b) for <file>:<a>[-<b>], or None. A number over 9 digits is out of range."""
    m = SOURCE_RE.match(str(source))
    if not m:
        return None
    a, b = (BIG if len(x) > 9 else int(x) for x in (m.group(2), m.group(3) or m.group(2)))
    return m.group(1), a, b


def _lines(path):
    """(lines, None) for a readable UTF-8 file, else (None, problem text)."""
    try:
        return Path(path).read_text(encoding="utf-8").splitlines(), None
    except (OSError, ValueError) as exc:
        return None, f"{path}: unreadable ({exc})"


def _jsonl(path):
    """([(line number, object or BAD_JSON)], problems) for a JSON Lines file."""
    lines, err = _lines(path)
    if err:
        return [], [err]
    rows = []
    for n, text in enumerate(lines, 1):
        if not text.strip():
            continue
        try:
            rows.append((n, json.loads(text)))
        except (ValueError, RecursionError):
            rows.append((n, BAD_JSON))
    return rows, []


def _shape(n, row, seen):
    """R1: one problem string, or None when the row has every key and a new item id."""
    if not isinstance(row, dict):
        return f"R1 trace line {n}: not a JSON object"
    miss = [k for k in KEYS if k not in row]
    if miss:
        who = f" item {row['item']}" if "item" in row else ""
        return f"R1 trace line {n}:{who} missing {', '.join(miss)}"
    ok = (isinstance(row["item"], str) and row["item"] and isinstance(row["token"], str)
          and row["token"].strip() and isinstance(row["cards"], list)
          and isinstance(row["terms"], list)
          and row["proof"] in PROOFS)
    if not ok:
        return f"R1 trace line {n}: item {row.get('item')!r} has a wrong value type or proof"
    if row["item"] in seen:
        return f"R1 trace line {n}: item {row['item']} is repeated"
    return None


def _design_lines(rel, ctx):
    """(lines, None) for a file under the design root, else (None, reason). A path outside the
    root, or one of the plan's own trace, items-check or plan files, is refused."""
    try:
        full = (ctx["design"] / rel).resolve()
        inside = full.is_relative_to(ctx["design"].resolve())
    except (OSError, ValueError):
        return None, "is missing or unreadable"
    if not inside or full in ctx["own"]:
        return None, "is outside the design root or is a plan file"
    lines, err = _lines(full)
    return lines, "is missing or unreadable" if err else None


def _source(item, ctx):
    """R2: the source range exists and holds the token."""
    span = _span(item["source"])
    if not span:
        return [f"R2 {item['item']}: source {item['source']!r} is not <file>:<a>-<b>"]
    (name, a, b), (lines, why) = span, _design_lines(span[0], ctx)
    if why:
        return [f"R2 {item['item']}: source file {name} {why}"]
    if a < 1 or b < a or b > len(lines):
        return [f"R2 {item['item']}: source range {a}-{b} is outside {name}"]
    if item["token"].lower() not in "\n".join(lines[a - 1:b]).lower():
        return [f"R2 {item['item']}: token {item['token']!r} is not in {name} lines {a}-{b}"]
    return []


def _reference(item, ctx):
    """R6: the reference file exists and holds the token."""
    ref = str(item["reference"])
    lines, why = _design_lines(ref, ctx)
    if why:
        return [f"R6 {item['item']}: reference file {ref} {why}"]
    if item["token"].lower() not in "\n".join(lines).lower():
        return [f"R6 {item['item']}: reference file {ref} does not contain {item['token']!r}"]
    return []


def _proof(item, mapped, preview):
    """R5: the proof kind has a mapped card that runs the matching judge."""
    judge = {"motion": "ui_motion", "timing": "ui_timing"}.get(item["proof"])
    if judge and not any(judge in c["fields"].get("Check", "")
                         and "UI-PROOF PASS" in c["fields"].get("Expect", "") for c in mapped):
        return [f"R5 {item['item']}: proof {item['proof']} needs a card with {judge} in Check "
                "and 'UI-PROOF PASS' in Expect"]
    if item["proof"] == "owner" and _no_preview(preview):
        return [f"R5 {item['item']}: proof owner needs a Preview: line that is not none"]
    return []


def _waiver(item):
    """R7: a waiver has a kind, a reason and an approval date; dropped has no cards."""
    w, name = item["waiver"], item["item"]
    if w is None:
        return []
    if not isinstance(w, dict) or w.get("kind") not in ("dropped", "narrowed"):
        return [f"R7 {name}: waiver kind must be dropped or narrowed"]
    bad = []
    if not str(w.get("reason") or "").strip():
        bad.append(f"R7 {name}: waiver has no reason")
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", str(w.get("approved") or "")):
        bad.append(f"R7 {name}: waiver has no approved date")
    if w["kind"] == "dropped" and item["cards"]:
        bad.append(f"R7 {name}: waiver dropped, but the item also has cards")
    if w["kind"] == "narrowed" and not item["cards"]:
        bad.append(f"R7 {name}: waiver narrowed needs at least one card")
    return bad


def _cards(item, by_id, preview):
    """R3, R4 and R5 for one item."""
    name, ids = item["item"], [str(x) for x in item["cards"]]
    if not ids:
        return [] if item["waiver"] else [f"R3 {name}: item has no card and no waiver"]
    bad = [f"R3 {name}: card {x} is not in the plan" for x in ids if x not in by_id]
    mapped = [by_id[x] for x in ids if x in by_id]
    for c in mapped:
        if name not in re.split(r"[,\s]+", c["fields"].get("Cites", "")):
            bad.append(f"R4 {name}: card {c['id']} does not name {name} in Cites:")
    return bad + _proof(item, mapped, preview)


def _item(item, ctx):
    bad = _waiver(item) + _cards(item, ctx["by_id"], ctx["preview"])
    if ctx["design"] is None:
        return bad
    return bad + _source(item, ctx) + _reference(item, ctx)


def _overlap(check, items):
    """R8: Items-check entries that share a file and a line with a trace item."""
    spans = []
    for it in items:
        if _span(it["source"]):
            spans.append(_span(it["source"]))
    bad = []
    for n, e in check:
        sp = _span(e.get("source", "")) if isinstance(e, dict) else None
        if not sp or not any(f == sp[0] and sp[1] <= y and x <= sp[2] for f, x, y in spans):
            bad.append(f"R8 items-check line {n}: {e.get('name') if isinstance(e, dict) else e!r} "
                       "overlaps no trace item")
    return bad


def _rows(rows, path, ctx):
    """(items, problems): R1 and the per-item rules, in trace file order."""
    items, bad, seen = [], [], set()
    for n, row in rows:
        err = f"{path} line {n}: not valid JSON" if row == BAD_JSON else _shape(n, row, seen)
        if err:
            bad.append(err)
            continue
        seen.add(row["item"])
        items.append(row)
        bad += _item(row, ctx)
    if not rows:
        bad.append(f"R0 trace file {path} has no item")
    return items, bad


def _check_file(h, base, items, own):
    """R0 and R8: a trace file needs its own Items-check file with at least one entry."""
    name = h.get("Items-check")
    if not name:
        return ["R0 plan has a Trace file but no Items-check: line"]
    path = base / name
    if path.resolve() in own:
        return [f"R0 Items-check {name} is the trace file or the plan file"]
    check, bad = _jsonl(path)
    if not bad and not check:
        bad.append(f"R0 Items-check file {path} has no entry")
    return bad + _overlap(check, items)


def _trace_rules(plan_path, h, cards, spec_path=None):
    """(problems, items, none reason): R0 to R8 for the plan's Trace file."""
    base, trace, preview = Path(plan_path).parent, h.get("Trace", ""), h.get("Preview")
    none = NONE_RE.match(trace)
    if none and not _no_preview(preview):
        return ["R0 Trace: none is not allowed while the plan has a Preview: line"], [], None
    if none:
        return [], [], none.group(1)
    if not trace or trace.lower().startswith("none"):
        return ["R0 plan has no Trace: line (or Trace: none has no reason)"], [], None
    design = base / h["Design"] if h.get("Design") else None
    own = {(base / trace).resolve(), Path(plan_path).resolve()}
    named = {(base / h["Items-check"]).resolve()} if h.get("Items-check") else set()
    named |= {Path(spec_path).resolve()} if spec_path is not None else set()
    ctx = {"by_id": {c["id"]: c for c in cards}, "preview": preview, "design": design,
           "own": own | named}      # a source or reference may not be the items-check or spec file
    rows, bad = _jsonl(base / trace)
    if design is None:
        bad.append("R0 plan has a Trace but no Design: line")
    items, more = _rows(rows, base / trace, ctx)
    if bad:
        return bad, items, None
    return more + _check_file(h, base, items, own), items, None


def _spec(spec_path):
    """([Term names], {Ambiguous word: [terms]}, [Conflict topics], [Settled topics], problems)."""
    if spec_path is None:
        return [], {}, [], [], []
    lines, err = _lines(spec_path)
    if err:
        return [], {}, [], [], [f"T0 spec {err}"]
    terms, amb, conflict, settled, bad = [], {}, [], [], []
    for x in (s.strip() for s in lines):
        if m := re.match(r"^Term:\s*(.+?)\s*=", x):
            terms.append(m.group(1).lower())
        elif m := re.match(r"^Ambiguous:\s*(.+?)\s*->\s*(.+)$", x):
            parts = [t.strip().lower() for t in m.group(2).split("|")]
            amb[m.group(1).lower()] = [t for t in parts if t]
        elif x.startswith("Conflict:"):
            m = re.match(r"^Conflict:\s*([^:]+):", x)
            if m:
                conflict.append(m.group(1).strip())
            else:
                bad.append(f"T4 conflict line has no topic: {x[:60]!r}")
        elif m := re.match(r"^Settled:\s*([^:]+):\s*\S", x):
            settled.append(m.group(1).strip().lower())
    return terms, amb, conflict, settled, bad


def _alone(word, full, text):
    """True when `word` is in `text` outside every full term."""
    low = text.lower()
    for t in sorted(full, key=len, reverse=True):
        low = low.replace(t, " ")
    return re.search(rf"\b{re.escape(word)}\b", low) is not None


def _term_rules(spec_path, items, cards):
    terms, amb, conflict, settled, bad = _spec(spec_path)
    for t in sorted({t for t in terms if terms.count(t) > 1}):
        bad.append(f"T1 term {t}: has more than one Term: line")
    for it in items:
        bad += [f"T2 {it['item']}: term {t} has no Term: line"
                for t in it["terms"] if str(t).lower() not in terms]
    places = [(f"card {c['id']} {part}", text) for c in cards
              for part, text in (("title", c["title"]), ("body", c["body"]))]
    places += [(f"item {it['item']} name", str(it["name"])) for it in items]
    for word, full in amb.items():
        if word in full:
            bad.append(f"T3 ambiguous word '{word}' is itself a listed term")
        bad += [f"T3 {where}: ambiguous word '{word}' stands alone"
                for where, text in places if _alone(word, full, text)]
    bad += [f"T4 conflict {c}: has no Settled: line" for c in conflict if c.lower() not in settled]
    return bad


def analyse(plan_path, header, cards, spec_path):
    """(problems, trace items, none reason). Pure: nothing is written. `header` is the
    `plan_cut.parse_plan` header, which holds the Trace, Design, Items-check and Preview lines."""
    try:
        bad, items, reason = _trace_rules(plan_path, header, cards, spec_path)
    except (OSError, ValueError) as exc:
        return [f"R0 a Trace:, Design: or Items-check: path cannot be used ({exc})"], [], None
    return bad + _term_rules(spec_path, items, cards), items, reason


def problems(plan_path, header, cards, spec_path):
    return analyse(plan_path, header, cards, spec_path)[0]


def summary(items, reason):
    """The pass lines: the count line, the none reason, then one line for each waiver."""
    waived = [i for i in items if i["waiver"]]
    out = [f"trace: ok ({len(items)} items, {len(waived)} waivers)"]
    out += [f"trace: none ({reason})"] if reason else []
    return out + [f"waiver: {i['item']} {i['waiver']['kind']}: {i['waiver']['reason']}"
                  for i in waived]


def _out(text):
    print(str(text).encode("utf-8", "backslashreplace").decode())


def main(argv):
    if len(argv) != 1:
        print("usage: python3 -m capsule.plan_trace <plan>")
        return 2
    plan = Path(argv[0])
    try:
        header, cards, _ = plan_cut.parse_plan(plan.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _out(f"{plan}: unreadable ({exc})")
        return 1
    spec = plan.parent / header["Spec"] if header.get("Spec") else None
    bad, items, reason = analyse(plan, header, cards, spec)
    if bad:
        _out("\n".join(bad))
        return 1
    for line in summary(items, reason):
        _out(line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
