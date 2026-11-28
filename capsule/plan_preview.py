"""`python3 -m capsule.plan_preview <plan>`: the plan side of the owner preview gate (F1). It reads
the `Slice:` and `Preview:` header lines of a plan, refuses a plan that names no first vertical
slice, and derives the two preview cards (PV1, PV2) with their dependency edges. Pure text work:
it reads the plan file and writes nothing."""
import json
import re
import sys
from pathlib import Path

from capsule import plan_cut

NONE_RE = re.compile(r"^none\b\s*(?:\((.*)\))?\s*$", re.IGNORECASE)
MARKER = "owner_look"


def _slice_ids(header):
    return list(dict.fromkeys(plan_cut._csv(header.get("Slice"))))


def _preview_off(header):
    """None when the line is not `none`; else the reason (an empty text when it is missing)."""
    value = header.get("Preview", "")
    m = NONE_RE.match(value)
    if m is None:
        return "" if re.match(r"none\b", value, re.IGNORECASE) else None
    return (m.group(1) or "").strip()


def _slice_problems(header, cards):
    ids = [c["id"] for c in cards]
    chosen = _slice_ids(header)
    if not chosen:
        return ["Slice: missing or empty (Preview: is not none, so a first slice is required)"]
    bad = [f"Slice: names {i}, which is not a card in this plan" for i in chosen if i not in ids]
    if not bad and set(chosen) >= set(ids):
        bad.append("Slice: holds every card; the slice must be a first step, not the whole plan")
    inside = [c for c in cards if c["id"] in chosen]
    bad += [f"{c['id']}: in the Slice but Deps names {dep}, outside the Slice"
            for c in inside for dep in plan_cut._csv(c["fields"].get("Deps"))
            if dep not in chosen]
    return bad


def problems(header, cards):
    """Refusal lines (rules P1 to P7); an empty list means the plan may be cut."""
    bad = [f"{c['id']}: id is reserved for the preview cards" for c in cards
           if c["id"] in ("PV1", "PV2")]
    if not header.get("Preview"):
        return ["Preview: line missing (give one line, or 'Preview: none (<reason>)')"] + bad
    off = _preview_off(header)
    if off is None:
        return _slice_problems(header, cards) + bad
    if not off:
        bad.append("Preview: none needs a reason, as 'Preview: none (<reason>)'")
    return bad


def derive(header, cards):
    """(extra cards, edges) for a valid plan; each edge is [child, parent]."""
    if _preview_off(header) is not None:
        return [], []
    look, chosen = header["Preview"], _slice_ids(header)
    ids = [c["id"] for c in cards]
    extra = [
        {"id": "PV1", "title": "Owner preview: first slice", "marker": MARKER,
         "body": f"{look}\nSlice: {', '.join(chosen)}"},
        {"id": "PV2", "title": "Owner preview: before done", "marker": MARKER, "body": look},
    ]
    edges = [["PV1", i] for i in chosen]
    edges += [[i, "PV1"] for i in ids if i not in chosen]
    edges += [["PV2", i] for i in ids] + [["PV2", "PV1"]]
    return extra, edges


def main(argv):
    if len(argv) != 1:
        print("usage: python3 -m capsule.plan_preview <plan>", file=sys.stderr)
        return 2
    try:
        text = Path(argv[0]).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"plan {argv[0]}: unreadable ({exc})")
        return 1
    header, cards, bad = plan_cut.parse_plan(text)
    bad = bad or problems(header, cards)
    if bad:
        print("\n".join(bad))
        return 1
    extra, edges = derive(header, cards)
    print(json.dumps({"cards": extra, "edges": edges}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
