"""Decision-row citations parsed out of card bodies (rows 454, 519): "row
361", "rows 352, 376 and 417", "rows 1, 2, and 3" (Oxford comma), and a comma before
"&"/"or", plus a range's endpoints ("rows 12-15" -> 12, 15, not every row between -
pre-existing contract, unchanged)."""
import os
import re
from pathlib import Path

from capsule.tick_rows import spec_sections

# A trailing ISO date right after a citation ("row 452, 2026-09-27", an amend timestamp,
# row 452) must not be misread as another cited row: a number token is refused wherever
# the text at that point starts a whole YYYY-MM-DD date.
_NOT_ISO_DATE = r"(?!\d{4}-\d{2}-\d{2}(?!\d))"
_NUM = _NOT_ISO_DATE + r"\d+(?:-\d+)?"
# An optional comma before and/or/& counts as one separator (the Oxford comma, "1, 2, and 3").
_SEP = r"(?:,?\s*(?:&|\band\b|\bor\b)|,)"
_CITE_RE = re.compile(rf"\brows?\s+{_NUM}(?:\s*{_SEP}\s*{_NUM})*", re.I)


def cited_rows(body):
    """Decision rows a card body names ("row 361", "rows 352, 376 and 417", "rows 1 & 2",
    "rows 1 or 2"), each row number once, in first-seen order."""
    rows = []
    for m in _CITE_RE.finditer(body or ""):
        for n in re.findall(r"\d+", m.group(0)):
            if int(n) not in rows:
                rows.append(int(n))
    return rows


def cited_sections(body):
    """Spec section numbers a card cites: `§N.N` anywhere, or the list after `spec`/`specs` on a
    `cites:` line (`spec 3, 5.3, 6a (note); kit ...`), which stops at the first non-number."""
    out = set(re.findall(r"§\s*(\d+(?:\.\d+)*)", body))
    for line in re.findall(r"(?im)^\s*cites:(.*)$", body):
        m = re.search(r"(?i)\bspecs?\s+([^;]*)", line)
        for item in re.sub(r"\([^)]*\)", "", m[1]).split(",") if m else ():
            s = re.match(r"\s*(?:section\s+)?§?\s*(\d+(?:\.\d+)*[a-z]?)(?![\w-])", item, re.I)
            if not s:
                break
            out.add(s[1])
    return out


SPEC_MAX_BYTES = 200_000


def card_spec_section(body, board_spec, at_tip, numbers, read, repo=None):
    """The cited sections from the card's own `spec:` file, else the board spec via `at_tip` (which
    reads at the tick's sha, never the working tree). The own path must be a `.md` file inside the
    board repo (`repo`, when a local path): an absolute path into it maps to its repo-relative
    path. A path that is outside, not `.md`, missing, unreadable or over SPEC_MAX_BYTES falls
    back to the board spec, and the brief says so."""
    m = re.search(r"(?im)^\s*spec:[ \t]*(\S[^\n]*?)[ \t]*$", body or "")
    own, text, note = (m[1] if m else ""), "", ""
    if own:
        rel = own
        if Path(own).is_absolute():
            try:
                rel = str(Path(os.path.realpath(own)).relative_to(os.path.realpath(repo)))
            except (TypeError, ValueError, OSError):
                rel = ""
        if rel and rel.endswith(".md"):
            try:
                text = at_tip(rel)
            except (OSError, RuntimeError, ValueError):
                text = ""
        if len(text.encode("utf-8", "replace")) > SPEC_MAX_BYTES:
            text = ""
        if not text:
            read.clear()
            note = (f"(The card's spec file {own} was not found or not allowed. "
                    f"Fallback: {board_spec or 'none'}.)\n")
    if not text and board_spec:
        text = at_tip(board_spec)
    return note + spec_sections(text, numbers) if (text or note) else ""
