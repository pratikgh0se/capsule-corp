"""Role contracts: parse, lint and render (spec §4, row 367; verbatim from
Appendix A)."""
import json
import re
from pathlib import Path

from capsule import paths, style_lint

FIELDS = ("gets", "sees", "done-when", "reports", "may-not")
MAX_BYTES = 2048
# M141 (redesign mandate 2026-09-09): a role contract aims at 650 words; over 900 is a failure.
WORD_TARGET = 650
WORD_CAP = 900
LINE_C = "never write a contract, SOUL or CLAUDE.md; never write outside `sees`; never merge or approve a PR."
# A line `render` does not itself emit is never seen by any worker (row 537).
_RENDERED_LINE = re.compile(rf"^(# |schema:|verbs:|- ({'|'.join(map(re.escape, FIELDS))}):)")


def stem(role):
    """The file stem the role's contract and intent examples share."""
    return "security-reviewer" if role == "security" else role


def contract_path(role):
    return paths.REPO / "contracts" / f"{stem(role)}.md"


def parse(path):
    """Read the `schema:`/`verbs:` header and the five `- <field>:` lines. Missing lines are absent, not empty."""
    text = Path(path).read_text()
    out = {}
    # `[ \t]*`, not `\s*`: an empty `verbs:` line must not swallow the next line (row 447)
    m = re.search(r"^schema:[ \t]*(.*)$", text, re.M)
    if m:
        out["schema"] = m.group(1).strip()
    m = re.search(r"^verbs:[ \t]*(.*)$", text, re.M)
    if m:
        out["verbs"] = m.group(1).strip()
    for field in FIELDS:
        m = re.search(rf"^- {re.escape(field)}:[ \t]*(.*)$", text, re.M)
        if m:
            out[field] = m.group(1).strip()
    # extra `- may-not:` lines (the Zenkai line, row 455) are appended, never replace the first
    extra = [x.strip() for x in re.findall(r"^- may-not:[ \t]*(.*)$", text, re.M)[1:] if x.strip()]
    if extra and "may-not" in out:
        out["may-not"] = "; ".join([out["may-not"].rstrip(".")] + extra)
    return out


def lint(path, schemas):
    """Return an empty list, or one message per problem: a missing/empty field, over
    MAX_BYTES, a `may-not` not starting with `C;`, a bad or unknown `schema:`, a
    duplicate `schema:`/`verbs:`/`- <field>:` line (`render` keeps only the first -
    note 4), or a `# ` header line anywhere but line 1."""
    path = Path(path)
    errors = []
    size = path.stat().st_size
    if size > MAX_BYTES:
        errors.append(f"{path}: {size} bytes, over the {MAX_BYTES} byte cap")
    fields = parse(path)
    for field in FIELDS:
        if not fields.get(field, "").strip():
            errors.append(f"{path}: missing or empty field {field!r}")
    may_not = fields.get("may-not", "")
    if may_not and not may_not.startswith("C;"):
        errors.append(f"{path}: 'may-not' must start with 'C;' (line C, every contract)")
    schema = fields.get("schema", "")
    if not schema:
        errors.append(f"{path}: missing 'schema:' line")
    elif schema not in schemas:
        errors.append(f"{path}: schema {schema!r} is not one of {sorted(schemas)}")
    lines = path.read_text().splitlines()
    # may-not is exempt: a second `- may-not:` line is the legitimate Zenkai line (row 455),
    # folded into the rendered value by `parse`, never dropped.
    for prefix in ("schema:", "verbs:") + tuple(f"- {f}:" for f in FIELDS if f != "may-not"):
        if sum(1 for line in lines if line.startswith(prefix)) > 1:
            label = prefix if not prefix.startswith("- ") else f"'{prefix[2:-1]}'"
            errors.append(f"{path}: duplicate {label} line (render keeps only the first)")
    for n, line in enumerate(lines, 1):
        if not line.strip():
            continue
        if n != 1 and line.startswith("# "):
            errors.append(f"{path}:{n}: '# ' header line only allowed on line 1: {line[:60]!r}")
        elif not _RENDERED_LINE.match(line):
            errors.append(f"{path}:{n}: not rendered by contracts.render: {line[:60]!r}")
    return errors


def schema_for(role):
    return parse(contract_path(role)).get("schema", "")


def verbs_for(role):
    verbs = parse(contract_path(role)).get("verbs", "")
    return {v.strip() for v in verbs.split(",") if v.strip()}


def _expand_may_not(may_not):
    if may_not.startswith("C;"):
        rest = may_not[len("C;"):].strip()
        return f"{LINE_C} {rest}".strip()
    return may_not


def _shown_examples(role, directory=None):
    """The role's few-shot examples from `contracts/shown/` (row 604), never `contracts/intent/`.
    `[]` without a file; ValueError on a missing field, a bad `kind` or a source intent refuses."""
    from capsule import intent   # late: intent imports contracts
    path = Path(directory or paths.REPO / "contracts" / "shown") / f"{role}.jsonl"
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    for r in rows:
        if r.get("kind") not in ("good", "bad") or not intent._valid_source(r.get("source")) \
                or not all(str(r.get(k, "")).strip() for k in ("id", "given", "expect", "why")):
            raise ValueError(f"{path}: bad shown example {r.get('id')!r}")
    return rows


def render(role):
    """The system prompt for `role`: its header, schema, verbs and five fields, `may-not`'s leading `C;` expanded to LINE_C."""
    fields = parse(contract_path(role))
    lines = [f"# {role}", f"schema: {fields.get('schema', '')}", f"verbs: {fields.get('verbs', '')}"]
    for field in FIELDS:
        value = fields.get(field, "")
        if field == "may-not":
            value = _expand_may_not(value)
        lines.append(f"- {field}: {value}")
    shown = _shown_examples(role)
    if shown:
        lines.append("Shown examples (row 604; a few cases, not a full list):")
        lines += [f"- {r['kind']} ({r['source']}): given {r['given']} | expect {r['expect']}"
                  f" | why {r['why']}" for r in shown]
    return style_lint.append("\n".join(lines))   # rows 570, 573: the shared style checklist
