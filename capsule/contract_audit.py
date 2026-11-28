"""D220 (row M111): the reviewer audits each numbered acceptance row of the card, in the optional
`contract_audit` array of `review_verdict.v1`. The check is soft: it adds notes and never refuses a
verdict (row 509 Z7: new logic in a new module, tick.py gets one call site)."""
import re

from capsule.bundle_sections import _schema_fields
from capsule.db import event, txn

_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*)$")
_SECTION = re.compile(r"acceptance|done[ -]when|definition of done", re.I)
_FENCE = re.compile(r"^\s{0,3}(```|~~~)")
_ITEM = re.compile(r"^\s{0,3}(?:(\d+)[.)]|[-*+](?:\s+\[[ xX]\])?)\s+(\S.*)$")


def rows(body):
    """`{number: text}` for the list items under the card's acceptance heading (`## Acceptance
    criteria`, `## Done when`). An item written `N.`/`N)` keeps its number when every item is
    numbered and no number repeats; otherwise each takes its 1-based place. Fenced code and items
    indented 4+ are skipped. Empty without a heading."""
    out, inside, fenced, listed = {}, False, False, []
    for line in (body or "").splitlines():
        if _FENCE.match(line):
            fenced = not fenced
            continue
        head = None if fenced else _HEADING.match(line)
        if head:
            inside = bool(_SECTION.search(head.group(1)))
            continue
        item = _ITEM.match(line) if inside and not fenced else None
        if item:
            listed.append((item.group(1), item.group(2).strip()))
    numbers = [int(n) for n, _ in listed if n]
    keep = len(numbers) == len(listed) and len(set(numbers)) == len(numbers)
    for place, (n, text) in enumerate(listed, 1):
        out[int(n) if keep else place] = text
    return out


def section(body):
    """The reviewer prompt's "Audit the contract" section; "" when the card has no rows or the
    loaded `review_verdict.v1` does not carry `contract_audit` yet (landing order is irrelevant)."""
    found = rows(body)
    if not found or "contract_audit" not in _schema_fields("review_verdict.v1"):
        return ""
    listing = "\n".join(f"{n}. {t}" for n, t in found.items())
    return ("## Audit the contract\n"
            "The card's numbered acceptance rows:\n" + listing + "\n"
            "For each row, add one entry to `contract_audit`: `row` (its number), `met` (`yes`,"
            " `no` or `unproven`) and `evidence` (a test id or file:line, 300 characters at"
            " most). A row marked `no` is a `design_fidelity` finding (from round 4, a note"
            " unless it is a regression, row 540). A row marked `unproven` also goes in `notes`.")


def note(conn, task_id, run, role, doc, body, now):
    """One `notes` event when a reviewer's `doc` leaves an acceptance row out of `contract_audit`,
    or marks a row `no` on a `pass`. A no-op for any other role, a card without rows, or a clean
    audit; the verdict itself is never refused."""
    wanted = rows(body)
    audit = doc.get("contract_audit") or []
    missing = sorted(set(wanted) - {a.get("row") for a in audit})
    no = sorted(a["row"] for a in audit if a.get("met") == "no")
    notes = []
    if role == "reviewer" and wanted and missing:
        notes.append("contract_audit does not cover acceptance row(s) "
                     + ", ".join(map(str, missing)))
    if role == "reviewer" and no and doc.get("verdict") == "pass":
        notes.append("verdict is pass but contract_audit marks row(s) "
                     + ", ".join(map(str, no)) + " as not met")
    if notes:
        with txn(conn):
            event(conn, task_id, "notes", {"entries": notes}, run_pk=run["id"],
                  run_id=run["run_id"], now=now)
