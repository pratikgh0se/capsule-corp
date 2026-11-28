"""ASD-STE100 output style (rows 570, 573)."""
import datetime
import json
import re
import sys
from pathlib import Path

from capsule import paths
from capsule.db import event, txn

# two weeks after row 570 (2026-10-07); the one-line switch
BLOCKING_FROM = datetime.date(2026, 10, 21)
# the review roles: their retry carries the reason
BLOCKING_SCHEMAS = ("review_verdict.v1", "validator_result.v1", "alignment_verdict.v1",
                    "blind_tests.v1")
# row 575: the deterministic rules; "sentence", "step", "paragraph" only warn
BLOCKING_RULES = ("verdict-first", "hedge")
MAX_STYLE_BYTES = 500
MAX_SENTENCE, MAX_STEP, MAX_PARAGRAPH = 25, 20, 6
RULES = 12
ENVELOPE = ("schema", "run_id", "task_id", "claim_lock", "role")
# Keys whose strings are identifiers, enums, commands or command output, not prose.
NOT_PROSE = set(ENVELOPE) | {"verdict", "status", "confidence", "file", "line", "class", "cmd", "commands",
                             "net_lines", "recurrence_test", "checkpoint", "id", "incident_id", "excerpt",
                             "result", "exit"}
HEDGE_KEYS = {"text", "root_cause", "lesson", "mitigation", "error", "issues", "left_undone", "reason"}
HEDGE = re.compile(r"\b(might|probably|perhaps|possibly|maybe|likely|seems?|appears? to|should be fine|i think)\b", re.I)
_CODE = re.compile(r"`[^`]*`")
_ITEM = re.compile(r"^\s*(?:(\d+)[.)]|[-*])\s+")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9`*\"(])")


def style_path():
    return paths.REPO / "contracts" / "_style.md"


def style_text(path=None):
    return (path or style_path()).read_text()


def append(rendered):
    """`rendered` (one role's system prompt) plus the checklist, once, at the end."""
    return rendered + "\n\n" + style_text().strip()


def lint_file(path=None):
    """[] or one message per problem with the checklist file: over its byte cap or not 12 numbered rules."""
    path = path or style_path()
    text = style_text(path)
    errors = []
    if len(text.encode()) > MAX_STYLE_BYTES:
        errors.append(f"{path}: {len(text.encode())} bytes, over the {MAX_STYLE_BYTES} byte cap")
    found = [int(n) for n in re.findall(r"^(\d+)\. ", text, re.M)]
    if found != list(range(1, RULES + 1)):
        errors.append(f"{path}: expected rules numbered 1..{RULES}, found {found}")
    return errors


def _units(paragraph):
    """[(text, is_step)] for one paragraph: each list item, and each sentence of the plain lines."""
    items, plain = [], []
    for line in paragraph.splitlines():
        m = _ITEM.match(line)
        if m:
            items.append([_ITEM.sub("", line), bool(m.group(1))])
        elif items:
            items[-1][0] += " " + line.strip()
        else:
            plain.append(line.strip())
    sentences = [s for s in _SENTENCE_END.split(" ".join(plain)) if s.strip()]
    return [(s, False) for s in sentences], [(t, step) for t, step in items]


def _words(text):
    return len(_CODE.sub("code", text).split())


def check_text(text, where=""):
    """Problems in one text: sentence and step length, paragraph length."""
    return [msg for _, msg in _text_findings(text, where)]


def _text_findings(text, where=""):
    problems, fenced = [], False
    kept = []
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
        elif not fenced and not line.lstrip().startswith(("#", "|")):
            kept.append(line)
    for paragraph in re.split(r"\n\s*\n", "\n".join(kept)):
        sentences, items = _units(paragraph)
        for unit, step in [(s, False) for s, _ in sentences] + items:
            limit = MAX_STEP if step else MAX_SENTENCE
            n = _words(unit)
            if n > limit:
                rule = "step" if step else "sentence"
                problems.append((rule, f"{where}{rule} of {n} words (max {limit}): {unit.strip()[:50]!r}"))
        if len(sentences) > MAX_PARAGRAPH:
            problems.append(("paragraph", f"{where}paragraph of {len(sentences)} sentences (max {MAX_PARAGRAPH}): {paragraph.strip()[:40]!r}"))
    return problems


def _strings(value, path=""):
    """(path, key, string) for every prose string in a document."""
    if isinstance(value, dict):
        for key, inner in value.items():
            if key not in NOT_PROSE:
                yield from _strings(inner, f"{path}.{key}" if path else key)
    elif isinstance(value, list):
        for n, inner in enumerate(value):
            yield from _strings(inner, f"{path}[{n}]")
    elif isinstance(value, str):
        yield path, value


def findings(doc):
    """[(rule, message)] for a parsed report, review or post-mortem document; rule is one of
    verdict-first, sentence, step, paragraph, hedge."""
    if not isinstance(doc, dict):
        return []
    found = []
    lead = [k for k in doc if k not in ENVELOPE]
    verdict = "verdict" if "verdict" in doc else "status" if doc.get("schema") == "report.v1" and "status" in doc else None
    if verdict and lead and lead[0] != verdict:
        found.append(("verdict-first", f"verdict first: `{verdict}` is key {lead.index(verdict) + 1} after the envelope,"
                                       f" after `{lead[0]}`"))
    for where, text in _strings(doc):
        found += _text_findings(text, f"{where}: ")
        leaf = re.sub(r"\[\d+\]$", "", where).split(".")[-1]
        m = HEDGE.search(_CODE.sub("", text)) if leaf in HEDGE_KEYS else None
        if m:
            found.append(("hedge", f"{where}: hedge word {m.group(0)!r} in a verdict field; name the missing evidence instead"))
    return found


def _today(today, now):
    if today is not None:
        return today
    return datetime.date.fromtimestamp(now) if now is not None else datetime.date.today()


def blocking(today=None, now=None):
    return _today(today, now) >= BLOCKING_FROM


def _retried(run, sp=None):
    """True when `run` is a retry after an invalid document: its metadata, or its job inputs
    (`sp/job.json`, where reviewers and validators get the error), carry `invalid_document_error`."""
    try:
        if json.loads(run["metadata"] or "{}").get("invalid_document_error"):
            return True
    except (ValueError, TypeError, AttributeError, KeyError):
        pass
    try:
        with open(Path(sp) / "job.json") as fh:
            return bool(((json.load(fh) or {}).get("inputs") or {}).get("invalid_document_error"))
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def gate(doc, run, today=None, conn=None, now=None, sp=None):
    """None, or the refusal reason."""
    if not isinstance(doc, dict) or doc.get("status") == "failed":
        return None
    found = findings(doc)
    if not found:
        return None
    role = run["profile"]
    hard = [msg for rule, msg in found if rule in BLOCKING_RULES]
    if hard and blocking(today, now) and doc.get("schema") in BLOCKING_SCHEMAS and not _retried(run, sp):
        why = (f"style_lint ({role}, rows 570, 575): " + "; ".join(hard))[:1700] + \
              ". Rewrite the document with these fixed; keep the same facts."
        # the retry bundle's `invalid_document_error` reads this event (row 507(b))
        if conn is not None:
            with txn(conn):
                event(conn, run["task_id"], "invalid_document", {"reason": why[:500]},
                      run_pk=run["id"], run_id=run["run_id"], now=now)
        return why
    mode = "advisory" if not blocking(today, now) else "accepted on retry or for a non-blocking rule"
    print(f"style_lint {mode} ({role}, blocking from {BLOCKING_FROM}): " + "; ".join(m for _, m in found)[:1800], file=sys.stderr)
    if conn is not None:
        rules = sorted({rule for rule, _ in found})
        with txn(conn):
            event(conn, run["task_id"], "style_warning", {"role": role, "rules": rules, "count": len(found)},
                  run_pk=run["id"], run_id=run["run_id"], now=now)
    return None


def main(argv, today=None):
    """Check the files named in `argv` (`.json` as documents, others as text). Exit 0 while
    advisory; once blocking, 1 when a file breaks a blocking rule (text files have none)."""
    hard = 0
    for name in argv:
        with open(name) as fh:
            text = fh.read()
        found = findings(json.loads(text)) if name.endswith(".json") else [(r, m) for r, m in _text_findings(text)]
        for rule, problem in found:
            print(f"{name}: {problem}")
        hard += sum(1 for rule, _ in found if rule in BLOCKING_RULES)
    return 1 if hard and blocking(today) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
