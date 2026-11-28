"""Deterministic risk classifier (S4a; D15 risk classes, D21 big). Pure: no DB, no net, no model.

`classify(title, body, files)` judges one card from its text and its declared files and says
which of the four D15 classes it falls in. The flags use the same words as the card field
`risk_flags` (`judges|data|public|big`), so the intake step (R21) can store them as they are.

- judges: a PROTECTED path (`workspace.PROTECTED`, also when only its bare name is written),
  a gate, hook, guard, lint or allowlist file, a gate or the meters named in the title, or text
  that deletes, skips or relaxes a test, guard or gate. Rows added to `contracts/intent/` are
  not a judge (the stamp already gates them), and an ordinary test file is not either: nearly
  every card adds one and the mutation gate judges those.
- data: schema, column, migration, retention, backup, purge, `kanban.db` or a `.sql` / `db.py`.
- public: a push to main, `.github/`, `release/`, or text about publishing, payments, pricing,
  email, DNS or a public repo (a README typo is not). Slack messages to the owner are not
  public, and a notional cost report is not money.
- big (D21): 10 or more declared files, or an estimate of 3 or more hours.

Declared files are the `files` argument plus the card body's `File set:` / `Files:` block.
Judges, data and public look at declared files and at paths written in the text (sentences that
say "do not touch" or "out of scope", and a whole `## Do not touch` or `## Prior verdicts`
section, are dropped first). Big counts declared files only: prose names too many.
"""
import itertools
import re

from capsule import workspace

FLAGS = ("judges", "data", "public", "big")
BIG_FILES, BIG_HOURS = 10, 3.0

_TOP = ("capsule|contracts|docker|docs|maintenance|claude|bin|tests|observability|launchd|"
        "calibration|scripts|uat|release|\\.claude|\\.github")
_PATH_RE = re.compile(rf"(?<![\w/.-])(?:(?:{_TOP})/[\w./*-]*[\w*]|\.?[\w-]+\.(?:py|sh|jsonl?|sql|"
                      r"md|plist|ya?ml|toml|txt|lock))")
_FILESET_RE = re.compile(r"^[ \t]*(?:#+[ \t]*|[-*][ \t]*)?\**[ \t]*(?:file set|files(?: touched)?"
                         r"|affected files)[ \t]*\**[ \t]*(?::[ \t]*\**(.*)|$)", re.I)
_BULLET_RE = re.compile(r"[ \t]*(?:[-*]|\d+[.)])[ \t]+")
# An excluded span runs to the end of its sentence, or to a turn ("..., so edit X", " - X").
_SENTENCE = (r"(?:[^.;\n]|\.(?!\s|$))*?(?=\.(?:\s|$)|[;\n]|$|,\s*(?:so|and|then|but|only|instead)\b"
             r"|\s+-\s|\s+(?:so|but|instead|only)\s)")
_EXCLUDED_RE = re.compile(rf"(?:do not (?:touch|change|edit|rewrite)|never (?:edit|change|touch)|"
                          rf"must not (?:touch|edit|change|modify|write)|out of scope|"
                          rf"not in this card|explicitly exclude){_SENTENCE}", re.I)
_DB_SENTENCE_RE = re.compile(r"(?:[^.\n]|\.(?!\s|$))*kanban\.db(?:[^.\n]|\.(?!\s|$))*")
_READ_ONLY_RE = re.compile(r"mode=ro|read-only|\breads?\b", re.I)
_NOT_A_CHANGE_RE = re.compile(r"passed its schema|schema violation", re.I)
_SKIP_SECTION_RE = re.compile(r"do not touch|out of scope|prior verdicts", re.I)
_HEADING_RE = re.compile(r"#+\s")

_GATE_NAMES = ("simplicity_check|size_check|mutation_check|mutation gate|ruff_gate|check_gate|"
               "lander")
_GATE_RE = re.compile(r"(?:^|/)hooks/|(?:^|/)conftest\.py$|^tests/(?!test_)[^/]+\.\w+$|"
                      r"(?:^|/)golden/|^bin/|^\.?claude/|deps-allowlist\.txt$|\.gitleaks\.toml$|"
                      r"_lint\.py$|(?:^|/)size_baseline[\w-]*\.\w+$|settings(?:\.fragment)?\.json$|"
                      r"(?:^|/)[\w-]*(?:guard|_gate|_check)[\w-]*\.(?:py|sh)$|"
                      r"^capsule/lander\w*\.py$")
_JUDGE_TITLE_RE = re.compile(rf"{_GATE_NAMES}|PreToolUse|\bmeters?\b|ZENKAI|calibration|"
                             r"interrupt|contract", re.I)
_WEAKEN_RE = re.compile(
    r"\b(?:delete|remove|drop|relax|loosen|weaken|disable|skip|bypass|xfail|comment out|mark|"
    r"turn off)\s+(?:\w+\s+){0,3}"
    rf"(?:tests?|test_\w+|assertions?|guards?|gates?|checks?|xfail|{_GATE_NAMES})\b|"
    r"\b(?:lower|relax|raise|loosen)\s+(?:\w+\s+){0,3}(?:threshold|baseline)\b|"
    r"@(?:[\w.]+\.)?(?:skip|skipIf|skipUnless|xfail)\b|"
    r"\b(?:role|reviewer|implementer|validator|guide|auditor|advisor) contract\b", re.I)

_DATA_PATH_RE = re.compile(r"\.sql$|migrations?/|^contracts/schemas/|(?:^|/)RETENTION\.md$|"
                           r"(?:^|/)(?:backup|restore)[\w-]*\.sh$|(?:^|/)db(?:_migrate)?\.py$|"
                           r"^observability/postgres/")
_DATA_TEXT_RE = re.compile(
    r"\b(?:migrations?|nullable column|alter table|drop table|retention|retains?|init\.sql|"
    r"schemas?|create (?:table|index)|purge|prune|report\.v\d|"
    r"(?:add|drop|rename|alter|new|nullable|change|remove)\w*\s+(?:[\w`.]+\s+){0,2}columns?|"
    r"backfill(?:s|ed|ing)?\s+(?:\w+\s+)?(?:rows|data|columns?|tables?|records|cards)|"
    r"(?:kept|keeps?|retain)\b[^.\n]*\b\d+\s*(?:days?|weeks?|months?)|"
    r"delete\w*\s+(?:anything|everything|rows|files|logs)\b[^.\n]*\bolder|"
    r"older than \d+\s*(?:days?|weeks?|months?))\b|kanban\.db|"
    r"[\w`.]+`?\s+columns?\s*\(|\bin an? [\w`.]+ column\b", re.I)

_PUBLIC_PATH_RE = re.compile(r"^\.github/|^release/")
_PUBLIC_TEXT_RE = re.compile(
    r"\bgit push\b[^.\n]*\bmain\b|\bpush(?:es)?\s+(?:to\s+)?(?:origin/)?main\b|"
    r"\bpublicly\b|\bpublic (?:github )?(?:repo|release|api|site|post)|"
    r"\brepo(?:sitory)?\s+(?:is\s+)?public\b|\bmake\s+(?:[\w-]+\s+){0,4}public\b|\bgo public\b|"
    r"\bopen[- ]source\b|\b(?:stripe|pricing|payments?|billing|invoices?|refunds?|webhooks?)\b|"
    r"\bcredit card\b|\bcharg(?:e|es|ed|ing)\b|\bpay(?:s|ing)? for\b|"
    r"\b(?:buy|purchase[sd]?|purchasing)\b|\bsubscriptions?\b(?!-only)|"
    r"\bcheckout (?:page|flow|session|link)\b|\b(?:pypi|npm|crates\.io|docker hub|ghcr)\b|"
    r"\bpublish(?:es|ed|ing)?\b[^.\n]*\b(?:pypi|npm|pages|registry|public|world|internet|site)\b|"
    r"\btag v\d|\brelease (?:v\d|notes|candidate|to (?:pypi|npm|github|production|public))|"
    r"\be-?mail(?:s|ing)?\s+(?:the|a|an|it|them|to)\b|"
    r"\bsend(?:s|ing)?\s+(?:\w+\s+){0,2}e-?mails?\b|"
    r"\bcloudflare\b|\bdns\b|\bcname\b", re.I)

_NUM = r"(\d+(?:\.\d+)?)"
_HOURS_RE = re.compile(rf"\b(?:size|estimate[d]?|effort)\s*:\s*(?:about|around|~)?\s*{_NUM}\s*"
                       rf"(?:-\s*{_NUM}\s*)?(?:h|hrs?|hours?)\b|~\s*{_NUM}\s*h\b|"
                       rf"\b{_NUM}\+?\s*(?:h|hours?)\s+of\s+(?:agent\s+)?(?:time|work)\b", re.I)


def classify(title="", body="", files=()):
    """One card in, `{"level": "low"|"risky", "flags": [...], "reasons": [...]}` out.
    `files` is the card's declared file set (the `file_set` field), if any."""
    body = body or ""
    declared = _declared(body, files)
    text = _kept(f"{title}\n{body}")
    paths = list(dict.fromkeys([*declared, *_paths(text)]))
    found = {"judges": _judges(paths, title or "", text), "data": _data(paths, text),
             "public": _public(paths, text), "big": _big(declared, text)}
    flags = [f for f in FLAGS if found[f]]
    return {"level": "risky" if flags else "low", "flags": flags,
            "reasons": [r for f in flags for r in found[f]]}


def _norm(path):
    """A bare `name.py` stands for `capsule/name.py`; everything else is kept as written."""
    return path if "/" in path else f"capsule/{path}"


def _protected_named(text):
    """PROTECTED entries the text names by any `/`-suffix of the entry (`meters.py`,
    `runners/sandbox.py`), with an extension in the mention (a bare variable name is no entry).
    A wildcard entry counts by its literal prefix (`slack_listen` for `capsule/slack_listen*`);
    a directory glob (`contracts/**`) or one that starts with a wildcard is left to the path
    regex."""
    out = []
    for entry in workspace.PROTECTED:
        head, star, _ = entry.partition("*")
        parts = head.split("/")
        if star and (entry.startswith("**") or not parts[-1]):
            continue
        # a real extension wins; otherwise a mention followed by `(` is a call (`events.log(`)
        tail = r"(?:\.?(?:py|pyc|pyd|so)\b|[\w.-]*(?![\w(-]))" if star else r"(?![\w-])"
        words = (rf"(?<![\w/.-]){re.escape('/'.join(parts[i:]))}{tail}" for i in range(len(parts)))
        if any(re.search(r"\.\w", m.group()) for w in words for m in re.finditer(w, text)):
            out.append(entry)
    return out


def _paths(text):
    return list(dict.fromkeys([*(_norm(p) for p in _PATH_RE.findall(text)),
                               *_protected_named(text)]))


def _kept(text):
    """The text without the sentences that say a path is off limits or out of scope, and without
    a whole `## Do not touch` section."""
    out, skipping = [], False
    for ln in text.splitlines():
        if _HEADING_RE.match(ln):
            skipping = bool(_SKIP_SECTION_RE.search(ln))
        if not skipping:
            out.append(_EXCLUDED_RE.sub("", ln))
    return "\n".join(out)


def _fileset_chunks(lines):
    """The text of each `File set:` / `Files:` header and of the bullets right under it (past a
    blank line when the header itself names nothing)."""
    for i, line in enumerate(lines):
        m = _FILESET_RE.match(line)
        if m:
            head, rest = m.group(1) or "", lines[i + 1:]
            if not head.strip():
                rest = list(itertools.dropwhile(lambda ln: not ln.strip(), rest))
            yield " ".join([head, *itertools.takewhile(_BULLET_RE.match, rest)])


def _declared(body, files):
    """`files` plus the paths in the body's `File set:` / `Files:` block."""
    chunks = [" ".join(map(str, files)), *_fileset_chunks(body.splitlines())]
    return list(dict.fromkeys(_norm(p) for c in chunks for p in _PATH_RE.findall(c)))


def file_set(body):
    """The paths in the body's `File set:` / `Files:` block, and nowhere else in the text, with
    each PROTECTED entry the block names bare (`deploy.sh`), as `_paths` adds them."""
    body = body or ""
    named = (p for c in _fileset_chunks(body.splitlines()) for p in _protected_named(c))
    return list(dict.fromkeys([*_declared(body, []), *named]))


def _judges(paths, title, text):
    held = [p for p in workspace.touches_protected(paths) if not p.startswith("contracts/intent/")]
    out = [f"{p} is a protected path" for p in held]
    out += [f"{p} is a gate, hook or guard" for p in paths if _GATE_RE.search(p)]
    m = _JUDGE_TITLE_RE.search(title)
    out += [f"the title names a gate or the meters ({m.group(0)})"] if m else []
    m = _WEAKEN_RE.search(text)
    out += [f"the text weakens or changes a judge ({m.group(0)})"] if m else []
    return list(dict.fromkeys(out))


def _read_only_db(m):
    """A sentence that names kanban.db but only reads it is not a data change."""
    s = m.group(0)
    return s.replace("kanban.db", "") if _READ_ONLY_RE.search(s) else s


def _data(paths, text):
    out = [f"{p} holds data, schema or retention" for p in paths if _DATA_PATH_RE.search(p)]
    m = _DATA_TEXT_RE.search(_DB_SENTENCE_RE.sub(_read_only_db, _NOT_A_CHANGE_RE.sub("", text)))
    return out + ([f"the text mentions {m.group(0)}"] if m else [])


def _public(paths, text):
    out = [f"{p} is public or external" for p in paths if _PUBLIC_PATH_RE.search(p)]
    m = _PUBLIC_TEXT_RE.search(text)
    return out + ([f"the text mentions {m.group(0)}"] if m else [])


def _hours(text):
    """The largest hour estimate the text gives (a range counts at its top), or 0."""
    best = 0.0
    for m in _HOURS_RE.finditer(text):
        best = max(best, *(float(g) for g in m.groups() if g))
    return best


def _big(declared, text):
    out, hours = [], _hours(text)
    if len(declared) >= BIG_FILES:
        out.append(f"{len(declared)} declared files (big is {BIG_FILES}+)")
    if hours >= BIG_HOURS:
        out.append(f"an estimate of {hours:g} hours (big is {BIG_HOURS:g}+)")
    return out
