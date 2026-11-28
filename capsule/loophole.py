"""`python3 -m capsule loophole <file.jsonl>...` (BLD-20, row 602): the deterministic part of the
loophole hunt that runs before a contract or intent stamp. Read-only: it reads proposed intent
rows and lists the cheap loopholes a wrong or do-nothing implementation could hide behind. Every
finding is input for the stamp reviewer, never a verdict; the model-run hunt (a fresh cheap
agent trying to break the wording) stays a manual step. `capsule.intent.lint` still owns schema
and shape; this asks a different question: could the row pass without the behaviour?

Two severities: a finding (malformed-row, duplicate-id, duplicate-content, conflicting-expect,
noop-satisfies-expect, expect-restates-given, no-negative-case, literal-ban-on-free-text,
ban-covers-one-input) and a `hint:` (HINTS: noisy
heuristics, advisory). Exit 0 no findings (hints allowed), 1 findings, 2 a file could not be
read."""
import json
import sys
from pathlib import Path

from capsule import intent

NEG_WORDS = frozenset("""not never no denied deny denies block blocks blocked refuse refuses
refused reject rejects rejected fail fails failing missing invalid without outside empty hold
holds error unsafe forbidden stale""".split())
NEG_VALUES = frozenset("""blocked denied deny hold held refused rejected error fail failed
failure invalid""".split())
EXIT_KEYS = ("exit", "rc", "returncode", "exit_code")
HINTS = frozenset({"noop-friendly-expect", "presence-only-expect",
                   "label-only-expect", "not-an-intent-file"})   # noisy: printed, exit 0
REFUSAL_KEYS = frozenset("refused blocked denied rejected failed flagged".split())
COUNT_KEYS = frozenset("problems errors violations findings".split())
SCHEMAS = Path(__file__).resolve().parent.parent / "contracts" / "schemas"


def add_parser(sub):
    p = sub.add_parser("loophole", help="list cheap loopholes in proposed intent rows (row 602)")
    p.add_argument("files", nargs="+", metavar="file.jsonl")
    p.add_argument("--json", action="store_true", help="print the findings as one JSON list")


def _leaves(value, path=()):
    """[(path, leaf)]: dicts recurse; an operator dict, a list or a scalar is one leaf."""
    if isinstance(value, dict) and value and intent._op(value)[0] is None:
        return [leaf for k, v in value.items() for leaf in _leaves(v, path + (k,))]
    return [(path, value)]


def _zero(v):
    return v is None or v is False or v in ("", [], {}) or (type(v) in (int, float) and v == 0)


def _nothing(v):
    """A value a do-nothing implementation would also produce (or leave missing)."""
    return _zero(v) or intent.matches(v, intent.MISSING)


def _has_not(value):
    """`<not>` anywhere inside an operator tree (for example under `<each>`)."""
    if isinstance(value, dict):
        return "<not>" in value or any(_has_not(v) for v in value.values())
    return False


def _excludes(path, leaf, given):
    """An expect list that is a strict subset of a list in given (what must not be flagged)."""
    return isinstance(leaf, list) and path[-1:] and path[-1] in REFUSAL_KEYS and any(
        isinstance(g, list) and len(leaf) < len(g) and all(x in g for x in leaf)
        for g in given.values())


def _negative(path, leaf, given=None):
    last = path[-1] if path else None
    if last in EXIT_KEYS and type(leaf) is int:
        return leaf != 0
    if last in REFUSAL_KEYS and leaf is True:
        return True
    if last in COUNT_KEYS and type(leaf) is int:
        return leaf != 0
    if isinstance(leaf, str):
        return leaf.lower() in NEG_VALUES or leaf == "<empty>"
    return (leaf is False or _has_not(leaf) or _excludes(path, leaf, given or {}))


def _banned(e, g):
    """True when a given value g at the same path can never satisfy the expect value e."""
    if isinstance(g, str) and isinstance(e, str) and {g, e} == {"<empty>", "<non-empty>"}:
        return True
    if e == {"<not>": g}:
        return True
    return (isinstance(g, (str, int, list)) and not (isinstance(g, str) and g.startswith("<"))
            and not intent.matches(e, g))


def _bans(row):
    """[(path, expect leaf, given leaf)]: expect leaves that ban the very value given sets."""
    if "schema" not in row["given"]["document"]:
        return []   # a standalone file: given is the input and expect the output, not a ban
    given = dict(_leaves(row["given"]["document"]))
    return [(p, e, given[p]) for p, e in _facts(row["expect"])
            if p in given and _banned(e, given[p])]


def _facts(expect):
    """The leaves of `expect` apart from its `$outcome` routing judgement."""
    return _leaves({k: v for k, v in expect.items() if k != "$outcome"})


def _row_negative(row):
    words = set(row["id"].split("-"))
    expect = row["expect"]
    given = dict(_leaves(row["given"]["document"]))
    return bool(words & NEG_WORDS or expect.get("$outcome") in ("hold", "review") or _bans(row)
                or any(_negative(p, v, given) for p, v in _facts(expect)))


def _severity(check):
    return "hint" if check in HINTS else "finding"


def _well_formed(row):
    return (isinstance(row, dict) and isinstance(row.get("id"), str)
            and isinstance(row.get("expect"), dict) and isinstance(row.get("given"), dict)
            and isinstance(row["given"].get("document"), dict))


def _key(value, ordered=False):
    """Compare key `sort_keys` erases key order; pass ordered=True where order is data."""
    return json.dumps(value, sort_keys=not ordered)


def _free_text(doc, path):
    """True when the role schema types `path` as a plain string with no enum, const or pattern."""
    try:
        node = json.loads((SCHEMAS / f"{doc['schema']}.json").read_text())
        for k in path:
            node = node["properties"][k]
    except (OSError, ValueError, KeyError, TypeError):
        return True   # fail closed: a path the schema walk cannot follow is judged free text
    kind = node.get("type")
    return (kind == "string" or (isinstance(kind, list) and "string" in kind)
            ) and not {"enum", "const", "pattern"} & set(node)


def _ban_findings(row):
    """A ban row is not a no-op; its weakness is a ban that rewording or a narrow given dodges."""
    doc, out = row["given"]["document"], []
    for p, e, g in _bans(row):
        if e == {"<not>": g} and isinstance(g, str) and _free_text(doc, p):
            out.append(("literal-ban-on-free-text", f"`{'.'.join(p)}` is free text, so rewording "
                        "evades this ban; back it with a code check"))
    bans = _bans(row)
    narrow = [q for q, v in _leaves(row["given"].get("inputs", {})) if v == "<empty>"
              and any(q[-1:] == p[-1:] for p, _, _ in bans)]
    if narrow and any(e == "<empty>" or isinstance(e, dict) for _, e, _ in bans):
        name = ".".join(narrow[0])
        out.append(("ban-covers-one-input", f"the ban applies only while inputs.{name} is empty;"
                    " a non-empty input dodges it"))
    return out


def _row_findings(row, others=()):
    """[(check, text)] for one well-formed row (`others`: the other rows of its file)."""
    rest = {k: v for k, v in row["expect"].items() if k != "$outcome"}
    if not rest:
        return []
    leaves = _leaves(rest)
    given = dict(_leaves(row["given"]["document"]))
    out = _ban_findings(row)
    paired = any(not _nothing(v) and p == q for o in others for p, v in _leaves(
        {k: x for k, x in o["expect"].items() if k != "$outcome"}) for q, _ in leaves)
    if _bans(row) or (paired and intent.matches(rest, {})):
        pass
    elif intent.matches(rest, {}):
        out.append(("noop-satisfies-expect", "an empty document already satisfies expect, so a"
                    " do-nothing implementation passes; assert something that is present"))
    elif all(_nothing(v) for _, v in leaves):
        out.append(("noop-friendly-expect", "expect asks only for nothing-happened values (false,"
                    " 0, empty, exit 0); add one positive fact a do-nothing run would not make"))
    if all(v in ("<any>", "<non-empty>") for _, v in leaves):
        out.append(("presence-only-expect",
                    "expect only asks that fields exist, so any content passes"))
    if all(p in given and intent._same(given[p], v) for p, v in leaves):
        out.append(("expect-restates-given", "every expect value is already in given, so the row"
                    " cannot tell the behaviour from its absence"))
    return out


def _pair_findings(entries):
    """Duplicate ids, copied rows and one given with two different expects."""
    out, by_id, by_given = [], {}, {}
    for line, row in entries:
        if row["id"] in by_id:
            first_line = by_id[row["id"]]
            out.append((line, row["id"], "duplicate-id", f"id first used on line {first_line}"))
        by_id.setdefault(row["id"], line)
        first = by_given.setdefault(_key(row["given"], ordered=True), (line, row))
        if first[0] != line and row["id"] != first[1]["id"]:
            same = _key(row["expect"]) == _key(first[1]["expect"])
            check, text = ("duplicate-content", "same given and expect as") if same else \
                ("conflicting-expect", "same given as")
            out.append((line, row["id"], check, f"{text} line {first[0]} ({first[1]['id']})"))
    return out


def _file_findings(rows):
    if rows and not any(_row_negative(r) for r in rows):
        prose = [(p, v) for r in rows for p, v in _facts(r["expect"])]
        if all(p == ("pass",) and isinstance(v, str) and len(v.split()) >= 4 for p, v in prose):
            return [("label-only-expect", "every expect value is prose, a label for a check the"
                     " replay test names; the oracle lives in that test")]
        return [("no-negative-case", "no row expects a refusal, failure, hold or empty result, so"
                 " an accept-everything implementation passes the file")]
    return []


def _is_situation_row(row):
    """The lookout prose format (`situation` plus prose `expect`): not an intent row."""
    return isinstance(row, dict) and "situation" in row and "given" not in row


def _hunt(entries):
    """[{line, id, check, text, severity}] for [(line, parsed row)]."""
    found, good = [], []
    if entries and all(_is_situation_row(r) for _, r in entries):
        return [{"line": None, "id": None, "check": "not-an-intent-file", "severity": "hint",
                 "text": "rows carry `situation` and prose `expect`, not given.document; "
                         "capsule.intent does not replay this file"}]
    for line, row in entries:
        if _well_formed(row):
            good.append((line, row))
        else:
            rid = row.get("id") if isinstance(row, dict) else None
            found.append((line, rid, "malformed-row",
                          "needs id, given.document and expect objects"))
    for line, row in good:
        others = [r for _, r in good if r is not row]
        found += [(line, row["id"], c, t) for c, t in _row_findings(row, others)]
    found += _pair_findings(good)
    found += [(None, None, c, t) for c, t in _file_findings([r for _, r in good])]
    found.sort(key=lambda f: (f[0] is None, f[0] or 0))
    return [{"line": ln, "id": i, "check": c, "text": t, "severity": _severity(c)}
            for ln, i, c, t in found]


def _hunt_file(path):
    """`_hunt` on a JSONL file, with real line numbers; an unparseable line is a finding."""
    path, entries, bad = Path(path), [], []
    for n, text in enumerate(path.read_text().splitlines(), 1):
        if not text.strip():
            continue
        try:
            entries.append((n, json.loads(text)))
        except ValueError as exc:
            bad.append({"line": n, "id": None, "check": "malformed-row",
                        "text": f"not JSON ({exc})", "severity": "finding"})
    found = sorted(bad + _hunt(entries), key=lambda f: (f["line"] is None, f["line"] or 0))
    return [{"file": str(path), **f} for f in found]


def handle(args, parser):
    found, rc = [], 0
    for name in args.files:
        try:
            found += _hunt_file(name)
        except (OSError, UnicodeDecodeError) as exc:
            print(f"capsule loophole: cannot read {name}: {exc}", file=sys.stderr)
            rc = 2
    hard = [f for f in found if f["severity"] == "finding"]
    if args.json:
        print(json.dumps(found, indent=1))
    else:
        for f in found:
            tag = "hint: " if f["severity"] == "hint" else ""
            print(f"{tag}{f['file']}:{f['line'] or '-'} {f['id'] or '-'} {f['check']}: {f['text']}")
        if not hard and not rc:
            print("no loopholes found (deterministic checks only; the model-run hunt is manual)")
    return rc or (1 if hard else 0)
