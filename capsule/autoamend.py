"""Auto-amend (factory-autonomy spec 2): the tick applies a scope-neutral text fix the
guide proposes, instead of parking the card needs_input."""
import difflib
import hashlib
import re
from collections import Counter
from dataclasses import dataclass

TARGETS = ("card", "spec_section")
MAX_HUNKS = 3
MAX_LINES_DEFAULT = 6
# The ceiling itself lives in capsule.config (MAX_LINES_CEILING there) - config validates
# autonomy.amend_max_lines against it; nothing here reads a copy of the same number.
MAX_GROWTH = 400
CARD_CAP = 1
TTL_S = 72 * 3600
KEY = "auto_amend:{}:{}"

# Structural labels, in every shape the real plans and cards use (`- Files:`, `- Interfaces:`,
# `- [ ] Tests first`, `**Files:**`, `## Done when`, `### Acceptance criteria` ...).
STRUCT_WORDS = ("files", "done when", "done-when", "definition of done", "acceptance criteria", "acceptance",
                "success criteria", "expected", "interfaces", "interface", "global constraints", "constraints",
                "tests first", "test names", "test name", "tests", "test", "verification", "verify")
SEMANTIC = {"not", "no", "never", "none", "must", "should", "may", "shall", "only", "all", "every", "any",
            "without", "except", "unless", "optional", "skip", "skipped", "cannot", "can't", "don't", "doesn't"}
# Words that state a count or an order (oracle 1's own list - units, teens, tens to
# ninety, hundred/thousand/million/billion, and ordinals to twentieth plus
# thirtieth..ninetieth, hundredth, thousandth).
NUMBER_WORDS = {"zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
                "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
                "eighteen", "nineteen", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
                "eighty", "ninety", "hundred", "thousand", "million", "billion",
                "dozen", "twice", "half", "once", "double", "triple",
                "first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth",
                "eleventh", "twelfth", "thirteenth", "fourteenth", "fifteenth", "sixteenth", "seventeenth",
                "eighteenth", "nineteenth", "twentieth",
                "thirtieth", "fortieth", "fiftieth", "sixtieth", "seventieth", "eightieth", "ninetieth",
                "hundredth", "thousandth"}
_STRUCT = re.compile(r"^\s*(?:[-*+]\s+(?:\[[ xX]\]\s+)?)?(?:#+\s*|\*\*|__)?\s*(?:files|done[- ]when|definition of done"
                     r"|acceptance(?: criteria)?|success criteria|expected|interfaces?|global constraints|constraints"
                     r"|tests?(?: first)?|test names?|verify|verification)\b", re.I)
_HEADING = re.compile(r"^\s*(#{1,6})\s")
_LABEL = re.compile(r"^\s*(?:\*\*|__)[^*_\n]+:?(?:\*\*|__)\s*:?\s*$|^\s*(?:\*\*|__)[^*_\n]+:(?:\*\*|__)")
_ITEM = re.compile(r"^(\s*)(?:[-*+]|\d+\.)\s")
_FENCE = re.compile(r"^\s*(?:```|~~~)")
_PATH = re.compile(r"(?<![\w/.-])(?:[\w.-]+/)*[\w.-]+\.(?:py|sh|json|jsonl|md|html|css|js|ts|yaml|yml|toml|sql|txt)\b")
_TEST = re.compile(r"\btest_\w+")
_INLINE = re.compile(r"`[^`\n]+`")
_TOKEN = re.compile(r"\w+(?:'\w+)?|[^\w\s]")
_ROW_CITE = re.compile(r"\brows?\s+\d+(?:\s*(?:,|and)\s*(?:rows?\s+)?\d+)*", re.I)
# The whole line or nothing: trailing text after the date means it is not a provenance line.
_AMENDED_LINE = re.compile(r"^[ \t]*Amended per (rows? \d+(?:(?:,| and) (?:row )?\d+)*)(?: \(\d{4}-\d{2}-\d{2}\))?\.?"
                           r"[ \t]*$", re.I | re.M)


def _cite_matches(text):
    """Every `_ROW_CITE` match in `text`, in order, as `(start, end, number-set)`."""
    return [(m.start(), m.end(), {int(n) for n in re.findall(r"\d+", m.group(0))}) for m in _ROW_CITE.finditer(text)]


def _citation_mask_plan(find_groups, replace_groups, rows):
    """Which of `replace_groups` (a list of number-sets, in document order) may mask to
    a placeholder, aligned to `find_groups` by CONTENT via `difflib.SequenceMatcher`
    (this oracle's own matcher - oracle 2's `structure_drift` keeps a separate one,
    over its own extraction) so a drop or add elsewhere in the text never misaligns
    an unrelated, unchanged citation: `equal` and `delete` opcodes need nothing
    (unchanged citations mask trivially; find's own citations may always drop - see
    `provenance_masked`); an `insert` may mask only if its numbers are all in `rows`;
    a `replace` pair may mask only if the numbers it ADDS beyond its aligned find
    group are all in `rows` (may keep or drop a number, may add only from `rows`,
    never swap for a different pre-existing one)."""
    allowed = [False] * len(replace_groups)
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=find_groups, b=replace_groups, autojunk=False).get_opcodes():
        if tag == "equal":
            for j in range(j1, j2):
                allowed[j] = True
        elif tag == "insert":
            for j in range(j1, j2):
                allowed[j] = replace_groups[j] <= rows
        elif tag == "replace":
            base = find_groups[i1:i2]
            for k, j in enumerate(range(j1, j2)):
                allowed[j] = replace_groups[j] - (base[k] if k < len(base) else set()) <= rows
    return allowed


def provenance_masked(find, replace, rows=()):
    """(find, replace) with row citations masked so `numeric` sees only a genuine
    change: a citation may be kept, dropped, or grow by a number from the verdict's
    `rows` - never swapped for a different pre-existing number just because that
    number is cited elsewhere."""
    rows = {int(n) for n in rows}

    def line_masker(m):
        nums = {int(n) for n in re.findall(r"\d+", m.group(m.lastindex or 0))}
        return "ROWCITE" if nums <= rows else m.group(0)

    f_line = _AMENDED_LINE.sub(line_masker, find)
    r_line = _AMENDED_LINE.sub(line_masker, replace)
    r_groups = _cite_matches(r_line)
    allowed = _citation_mask_plan([frozenset(g[2]) for g in _cite_matches(f_line)],
                                  [frozenset(g[2]) for g in r_groups], rows)

    out, last = [], 0
    for i, (s, e, _) in enumerate(r_groups):
        out.append(r_line[last:s])
        out.append("ROWCITE" if allowed[i] else r_line[s:e])
        last = e
    out.append(r_line[last:])
    return _ROW_CITE.sub("ROWCITE", f_line), "".join(out)


@dataclass(frozen=True)
class Hunk:
    target: str
    find: str
    replace: str


def parse_hunks(raw):
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_HUNKS:
        raise ValueError(f"amendment: a list of 1-{MAX_HUNKS} hunks")
    out = []
    for i, h in enumerate(raw):
        if not isinstance(h, dict) or set(h) != {"target", "find", "replace"}:
            raise ValueError(f"amendment[{i}]: exactly target, find, replace")
        if h["target"] not in TARGETS:
            raise ValueError(f"amendment[{i}].target: one of {TARGETS}")
        if not isinstance(h["find"], str) or not h["find"] or not isinstance(h["replace"], str):
            raise ValueError(f"amendment[{i}]: find is a non-empty string, replace a string")
        if h["find"] == h["replace"]:
            raise ValueError(f"amendment[{i}]: find equals replace, a no-op hunk")
        out.append(Hunk(h["target"], h["find"], h["replace"]))
    return out


def token_change(find, replace):
    """(removed tokens, added tokens) of a word-level diff: the change itself, free of context."""
    a, b = _TOKEN.findall(find), _TOKEN.findall(replace)
    removed, added = [], []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if op in ("replace", "delete"):
            removed += a[i1:i2]
        if op in ("replace", "insert"):
            added += b[j1:j2]
    return removed, added


def fingerprint(hunk):
    """Context-free and symmetric: A->B and B->A, with any surrounding words, hash the same."""
    removed, added = token_change(hunk.find, hunk.replace)
    one, two = sorted((" ".join(sorted(t.lower() for t in removed)), " ".join(sorted(t.lower() for t in added))))
    return hashlib.sha256(f"{hunk.target}\0{one}\0{two}".encode("utf-8")).hexdigest()[:16]


def _fenced(lines):
    out, inside = set(), False
    for i, line in enumerate(lines):
        if _FENCE.match(line):
            out.add(i)
            inside = not inside
        elif inside:
            out.add(i)
    return out


def _structural(lines):
    """Line indexes inside a structural block (spec 2.2): - a structural HEADING (`##
    Done when`) runs to the next heading of the same or higher level; - a structural
    LIST ITEM (`- Files: ...`, `- [ ] Tests first ...`) is that item: its line and
    the lines indented deeper than its bullet; - a structural bold/plain LABEL line
    (`**Files:**`) runs over the list items and indented lines that follow it, to a
    heading, another label, or the first unindented non-list line after a blank line."""
    out = set()
    mode, level, indent, prev_blank = None, 0, 0, False
    for i, line in enumerate(lines):
        h = _HEADING.match(line)
        item = _ITEM.match(line)
        if h:
            if mode == "heading" and len(h.group(1)) > level:
                out.add(i)
            else:
                mode = "heading" if _STRUCT.match(line) else None
                level = len(h.group(1))
                if mode:
                    out.add(i)
        elif mode == "heading":
            out.add(i)
        elif item and _STRUCT.match(line):
            mode, indent = "item", len(item.group(1))
            out.add(i)
        elif _LABEL.match(line) or (not item and _STRUCT.match(line)):
            mode = "label" if _STRUCT.match(line) else None
            if mode:
                out.add(i)
        elif mode == "item":
            lead = len(line) - len(line.lstrip())
            if line.strip() and lead > indent:
                out.add(i)
            elif line.strip():
                mode = None
        elif mode == "label":
            if prev_blank and line.strip() and not item and not line.startswith((" ", "\t")):
                mode = None
            else:
                out.add(i)
        prev_blank = not line.strip()
    return out


def _is_label(line):
    """Oracle 2's own label test: str methods only (no regex shared with oracle 1)."""
    s = line.strip()
    for prefix in ("- [ ] ", "- [x] ", "- [X] ", "* [ ] ", "- ", "* ", "+ "):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    s = s.lstrip("#").strip().strip("*_").strip().lower()
    for word in STRUCT_WORDS:
        if s.startswith(word) and (len(s) == len(word) or not s[len(word)].isalnum()):
            return word
    return None


_EXT = ("py", "sh", "json", "jsonl", "md", "html", "css", "js", "ts", "yaml", "yml", "toml", "sql", "txt")
# Oracle 2's OWN number-word literal: the exact same words as `NUMBER_WORDS`, written
# out separately so a gap in one list is never a gap in both (Z2 - the two oracles share
# no code or data for this check, only the `rows` input).
_STRUCT_NUMBER_WORDS = {"zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
                        "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
                        "eighteen", "nineteen", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
                        "eighty", "ninety", "hundred", "thousand", "million", "billion",
                        "dozen", "twice", "half", "once", "double", "triple",
                        "first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth",
                        "tenth", "eleventh", "twelfth", "thirteenth", "fourteenth", "fifteenth", "sixteenth",
                        "seventeenth", "eighteenth", "nineteenth", "twentieth",
                        "thirtieth", "fortieth", "fiftieth", "sixtieth", "seventieth", "eightieth", "ninetieth",
                        "hundredth", "thousandth", "millionth", "billionth"}
# Oracle 2's own SECOND detection path (a different method from the list): a hyphenated
# compound (`thirty-five`, `twenty-first`) is a number word if ANY of its `-` parts is.
def _looks_numberish(part):
    return part.lower() in _STRUCT_NUMBER_WORDS


def _amended_rows(line):
    """Oracle 2's str-only reading of a whole "Amended per row N (date)" line: its row numbers, or
    None when the line has any other shape."""
    s = line.strip()
    if not s.lower().startswith("amended per row"):
        return None
    rest = s[len("amended per row"):].lstrip("s").strip()
    if rest.endswith("."):
        rest = rest[:-1]
    if rest.endswith(")") and " (" in rest:
        rest, date = rest[:-1].rsplit(" (", 1)
        parts = date.split("-")
        if [len(p) for p in parts] != [4, 2, 2] or not all(p.isdigit() for p in parts):
            return None
    nums = []
    for chunk in rest.replace(" and ", ",").split(","):
        chunk = chunk.strip()
        chunk = chunk[4:].strip() if chunk[:4].lower() == "row " else chunk
        if not chunk.isdigit():
            return None
        nums.append(int(chunk))
    return nums


def _row_citation_groups(words):
    """Oracle 2's str-only scan for 'row N' / 'rows N, M and K': the word indexes of
    each phrase's numbers."""
    groups, i = [], 0
    while i < len(words):
        bare = words[i].strip(",.;:()").lower()
        if bare in ("row", "rows"):
            idxs, j, want_number = [], i + 1, True
            while j < len(words):
                raw = words[j]
                core = raw.strip(",.;:()")
                low = core.lower()
                if want_number:
                    if not core.isdigit():
                        break
                    idxs.append(j)
                    ends_sentence = raw.rstrip(")").endswith((".", ";"))
                    j += 1
                    want_number = False
                    if ends_sentence:
                        break
                    # a list continues after a glued comma ("452,") or an "and"/"row"/"rows" word
                    if raw.rstrip(".;:()").endswith(","):
                        want_number = True
                        continue
                    if j < len(words) and words[j].strip(",.;:()").lower() in ("and", "row", "rows"):
                        continue
                    break
                elif low in ("and", "row", "rows"):
                    j += 1
                    want_number = True
                else:
                    break
            if idxs:
                groups.append(idxs)
                i = j
                continue
        i += 1
    return groups


def structure(text, rows=()):
    """Oracle 2 (Z2): label lines, fences and fenced lines, paths, test names, inline
    code, every number word, and each row-citation PHRASE as one `("cite", (n, m,
    ...))` group so `structure_drift` can align each citation to the matching one in
    the other text instead of comparing a loose bag of numbers; a whole Amended line
    whose rows are all in the verdict's `rows` contributes no numbers."""
    rows = {int(n) for n in rows}
    out, fence = [], False
    for line in text.split("\n"):
        s = line.strip()
        if s.startswith("```") or s.startswith("~~~"):
            fence = not fence
            out.append(("fence", s))
            continue
        if fence:
            out.append(("code", line))
            continue
        label = _is_label(line)
        if label:
            out.append(("label", label, s))
        words = line.split()
        amended = _amended_rows(line)
        skip = amended is not None and set(amended) <= rows
        cited = set()
        for group in ([] if skip else _row_citation_groups(words)):
            cited.update(group)
            out.append(("cite", tuple(int(words[i].strip(",.;:()")) for i in group)))
        for i, word in enumerate(words):
            w = word.strip("`'\"(),;:[]")
            if "." in w and w.rsplit(".", 1)[1] in _EXT:
                out.append(("path", w))
            if w.startswith("test_"):
                out.append(("test", w))
            bare = w.strip(".,;:!?")
            if not (skip or i in cited) and bare and (any(c.isdigit() for c in bare)
                                                        or any(_looks_numberish(p) for p in bare.split("-"))):
                out.append(("num", bare.lower()))
        out += [("inline", part) for part in line.split("`")[1::2]]
    return out


def _amended_line_texts(text):
    """Every whole "Amended per row N (date)" line in `text`, verbatim: the append-only rule
    (record provenance by appending a new Amended line; never edit an existing one) means each one
    found here must still be present, unchanged, in `after`."""
    return {line.strip() for line in text.split("\n") if _amended_rows(line) is not None}


def structure_drift(text, after, rows=()):
    """Oracle 2's verdict: any change of `structure()`'s non-citation entries, or any
    citation-group change beyond what `rows` allows."""
    rows = {int(n) for n in rows}
    before, now = structure(text, rows), structure(after, rows)
    b_cites = [set(e[1]) for e in before if e[0] == "cite"]
    a_cites = [set(e[1]) for e in now if e[0] == "cite"]
    rest = [[e for e in s if e[0] != "cite"] for s in (before, now)]
    if rest[0] != rest[1]:
        return True
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=[frozenset(g) for g in b_cites],
                                                        b=[frozenset(g) for g in a_cites], autojunk=False).get_opcodes():
        if tag == "insert" and any(not a_cites[j] <= rows for j in range(j1, j2)):
            return True
        if tag == "replace":
            base = b_cites[i1:i2]
            for k, j in enumerate(range(j1, j2)):
                if not a_cites[j] - (base[k] if k < len(base) else set()) <= rows:
                    return True
    return not _amended_line_texts(text) <= _amended_line_texts(after)


def _hunk_refusals(text, h, span, fenced, struct, rows):
    """Oracle 1 on one hunk that occurs exactly once: the per-hunk rules of spec 2.2, in order."""
    pos = text.index(h.find)
    end = pos + len(h.find)
    touched = range(text.count("\n", 0, pos), text.count("\n", 0, max(pos, end - 1)) + 1)
    if span is not None and not (span[0] <= pos and end <= span[1]):
        yield "outside-section"
    if any(i in fenced for i in touched) or any(m in s for s in (h.find, h.replace) for m in ("```", "~~~")):
        yield "code-fence"
    if any(i in struct for i in touched):
        yield "structural"
    if Counter(_PATH.findall(h.find) + _TEST.findall(h.find)) != Counter(_PATH.findall(h.replace) + _TEST.findall(h.replace)):
        yield "path-or-test"
    if Counter(_INLINE.findall(h.find)) != Counter(_INLINE.findall(h.replace)):
        yield "inline-code"
    if {m.group(0) for m in _AMENDED_LINE.finditer(h.find)} - {m.group(0) for m in _AMENDED_LINE.finditer(h.replace)}:
        yield "numeric"   # rewriting or dropping an existing Amended line is never exempt
    m_removed, m_added = token_change(*provenance_masked(h.find, h.replace, rows))
    if any(any(c.isdigit() for c in t) or t.lower().strip(".,;:!?") in NUMBER_WORDS for t in m_removed + m_added):
        yield "numeric"
    removed, added = token_change(h.find, h.replace)
    if any(t.lower() in SEMANTIC for t in removed + added):
        yield "semantic"
    if len(h.replace) > len(h.find) + MAX_GROWTH:
        yield "too-long"


def check_scope(text, hunks, *, span=None, max_lines=MAX_LINES_DEFAULT, rows=()):
    reasons = []
    if not 1 <= len(hunks) <= MAX_HUNKS:
        reasons.append("hunk-count")
    if len({h.target for h in hunks}) > 1:
        reasons.append("mixed-targets")
    lines = text.splitlines()
    fenced, struct = _fenced(lines), _structural(lines)
    after = text
    for h in hunks:
        if h.find == h.replace:
            reasons.append("no-change")
        elif after.count(h.find) != 1 or text.count(h.find) != 1:
            reasons.append("not-unique")
        else:
            reasons += _hunk_refusals(text, h, span, fenced, struct, rows)
            after = after.replace(h.find, h.replace, 1)
    diff_lines = list(difflib.unified_diff(text.splitlines(), after.splitlines(), lineterm="", n=0))
    changed = sum(1 for ln in diff_lines[2:] if ln[:1] in "+-")
    if changed > max_lines:
        reasons.append("too-big")
    if structure_drift(text, after, rows):
        reasons.append("structure-drift")
    refused = list(dict.fromkeys(reasons))
    return {"ok": not refused, "refused": refused, "after": after if not refused else text,
            "diff": "\n".join(diff_lines), "lines_changed": changed,
            "fingerprints": [fingerprint(h) for h in hunks]}


def decide(text, hunks, *, applied_fps=(), card_count=0, span=None, max_lines=MAX_LINES_DEFAULT, rows=()):
    out = check_scope(text, hunks, span=span, max_lines=max_lines, rows=rows)
    if set(out["fingerprints"]) & set(applied_fps):
        out["refused"].append("repeat")
    if card_count >= CARD_CAP:
        out["refused"].append("card-cap")
    out["ok"] = not out["refused"]
    if not out["ok"]:
        out["after"] = text
    out["applied"] = out["ok"]
    return out


def recommended_option(doc):
    for opt in (doc or {}).get("options") or []:
        if isinstance(opt, dict) and opt.get("id") == doc.get("recommended"):
            return opt
    return None
