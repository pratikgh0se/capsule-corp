"""Pure helpers that read decision rows and spec sections out of note text for the guide.

Moved verbatim from `capsule/tick.py`; `capsule.tick` re-exports every name.
"""
import re


def decision_rows(note_text, numbers):
    """{"N": text} for each decision row N found in the decisions note: the rest of its table line
    after `| N |`, whole, so the guide reads the rows it cites. A row not in the note is left
    out. A row number written twice takes its LAST line, the amended text; a match never spans
    lines."""
    found = {}
    for n in numbers or []:
        hits = re.findall(rf"^\|[ \t]*{int(n)}[ \t]*(\|[^\n]*?)[ \t]*$", note_text or "", re.M)
        if hits:
            found[str(int(n))] = hits[-1]
    return found


ROW_TEXT_CAP = 600            # an uncapped row text can outrun the guide's own request.v1 `text`
                              # field once card and epic rows union in more rows
ROW_TEXT_BUDGET = 2400        # combined cap across every cited row, so a many-row card still fits
ROW_TEXT_OVER_BUDGET = (f"(omitted: the combined {ROW_TEXT_BUDGET}-char row-text budget "
                        "was already spent)")


def _row_columns(text):
    """A captured row's pipe-delimited remainder (`decision_rows`: "| title | context | decision |")
    split into its columns, honoring an escaped `\\|` inside a column. The fencing `|` at each end
    leaves an empty first/last segment; dropped."""
    cols = [c.strip() for c in re.split(r"(?<!\\)\|", text)]
    if cols and cols[0] == "":
        cols = cols[1:]
    if cols and cols[-1] == "":
        cols = cols[:-1]
    return cols


def _truncate_row_text(text, cap=None):
    """Cut a cited row's text to `cap` (`ROW_TEXT_CAP`) chars when it runs over, squeezing the
    CONTEXT column(s) - everything between the title and the decision - first, down to nothing
    if it must. The decision column is never cut, so the text may run a little over `cap`. A
    row with no recognizable column structure falls back to a plain right-hand cut."""
    cap = ROW_TEXT_CAP if cap is None else cap
    if len(text) <= cap:
        return text
    marker = f" [truncated, {len(text)} chars]"
    cols = _row_columns(text)
    if len(cols) < 2:
        keep = max(cap - len(marker), 0)
        return text[:keep] + marker
    title, decision = cols[0], cols[-1]
    context = " | ".join(cols[1:-1])
    bare = f"| {title} | {decision} |"
    if not context:
        return bare + marker
    budget = cap - len(marker) - len(f"| {title} |  | {decision} |")
    if budget <= 0:
        return bare + marker
    return f"| {title} | {context[:budget]} | {decision} |" + marker


def _cap_row_texts(found, priority=None):
    """`found` ({"N": text}, from `decision_rows`), each text capped at `ROW_TEXT_CAP` chars
    (`_truncate_row_text`); the combined total stays under `ROW_TEXT_BUDGET`, spent in
    `priority` order when given: the card's own cited rows before an epic-fallback row, newest
    first within each. A row `found` names but `priority` omits is spent last, in `found`'s
    own order. A row cut for the total budget is named `ROW_TEXT_OVER_BUDGET`, never dropped
    silently."""
    capped = {n: _truncate_row_text(text) for n, text in found.items()}
    order = list(priority or [])
    order += [n for n in found if n not in order]
    used, out = 0, {}
    for n in order:
        if n not in capped:
            continue
        text = capped[n]
        if used + len(text) > ROW_TEXT_BUDGET:
            out[n] = ROW_TEXT_OVER_BUDGET
            continue
        out[n] = text
        used += len(text)
    return out


def spec_sections(text, numbers):
    """The markdown under each heading numbered in `numbers` (e.g. "6.8"), to the next heading at
    its level or above."""
    out, keep, level = [], False, 0
    for line in text.splitlines():
        m = re.match(r"(#+)\s+(\d+(?:\.\d+)*[a-z]?)[.\s]", line)
        if m:
            if keep and len(m.group(1)) <= level:
                keep = False
            if m.group(2) in numbers:
                keep, level = True, len(m.group(1))
        if keep:
            out.append(line)
    return "\n".join(out)


EPIC_SECTION_MAX_LINES = 80


def epic_spec_section(text, heading, max_lines=EPIC_SECTION_MAX_LINES):
    """The markdown under the heading whose text contains `heading` (case-insensitive), to the next
    heading at its level or above, capped at `max_lines` (the epic-level guide fallback for a
    card that cites no §N/row)."""
    out, keep, level = [], False, 0
    needle = (heading or "").strip().lower()
    if not needle:
        return ""
    for line in text.splitlines():
        m = re.match(r"(#+)\s+(.*)", line)
        if m:
            if keep and len(m.group(1)) <= level:
                break
            if not keep and needle in m.group(2).strip().lower():
                keep, level = True, len(m.group(1))
        if keep:
            out.append(line)
            if len(out) >= max_lines:
                break
    return "\n".join(out)
