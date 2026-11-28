"""The section builders every bundle shares (card, envelope, human answers, schema fields), moved
out of capsule/bundles.py (its file cap). `bundles` imports and re-exports them; this module
imports nothing from `bundles`."""
import hashlib
import json
import time

from capsule import contracts, human, paths
from capsule.spool import REQUIRED_FIELDS

# The envelope said "write to the spool's report/", but
# the worker files the model's structured output there (worker.run_one) and no runner
# makes report/ writable (runners.host, runners.docker: requests/ only), so a role that
# looked for it blocked needs_input.
REPORT_CHANNEL = ("Your report is your final structured output: the harness validates it and files"
                  " it in <spool>/report/ itself. report/ is not writable from your run and you"
                  " need not look for it; never list \"report/ not writable\" as left undone or"
                  " an issue.")
ROW_TEXT_MISSING = "(text not found in the decisions note; judge by the number and say so)"


def _header(task):
    return f"# Task {task.get('id', '')}: {task.get('title', '')}"


def _fields_section(task):
    """The card's R04 fields a role may need; "" when none is set (legacy cards render as before).
    `passes` is never rendered: the tick, not a role, owns it."""
    keys = ("check_cmd", "check_expect", "size_tag", "risk_flags", "workload")
    lines = [f"- {k}: {task.get(k)}" for k in keys if task.get(k)]
    return "## Card fields\n" + "\n".join(lines) if lines else ""


def card_hash(body):
    """sha256 of the whole card text (its body, not only the acceptance): the same hash `amend`
    records as `body_hash_before/after`."""
    return hashlib.sha256((body or "").encode("utf-8")).hexdigest()


CARD_DOCUMENT_MISSING = "(missing: the card has no body to judge alignment against)"


def _card_section(task, recorded=None):
    """The card as it is now (the worker's live tasks row), under the hash of exactly
    this text (a review judged a card text it was never
    handed)."""
    body = task.get("body") or ""
    live = card_hash(body)
    lines = [f"## Card as it is now (sha256 {live})", body if body else CARD_DOCUMENT_MISSING]
    if recorded and recorded != live:
        lines.append(f"(The card changed after this review started: job recorded sha256 {recorded}."
                     " Judge the text above, the card as it is now.)")
    return "\n".join(lines)


def _spec_heading(spec_path=None, spec_sha=None):
    return (f"## Spec section ({spec_path} at {spec_sha})" if spec_path and spec_sha
            else "## Spec section")


def _schema_fields(name):
    schema = json.loads((paths.SCHEMAS / f"{name}.json").read_text())
    return list(schema["properties"])


def _schema_max_length(name, field):
    schema = json.loads((paths.SCHEMAS / f"{name}.json").read_text())
    return schema["properties"][field]["maxLength"]


def _decision_caps():
    """decision.v1's length/count caps, read live from the schema (row 507(b):
    never a hand-copied number in a contract file that could drift from a schema edit - card
    a past option `label` over this exact cap)."""
    schema = json.loads((paths.SCHEMAS / "decision.v1.json").read_text())
    props = schema["properties"]
    return {"question": props["question"]["maxLength"], "min_options": props["options"]["minItems"],
            "max_options": props["options"]["maxItems"],
            "label": props["options"]["items"]["properties"]["label"]["maxLength"],
            "context": props["context"]["maxLength"],
            "id": props["options"]["items"]["properties"]["id"]["pattern"],
            "provisional": props["provisional"]["enum"]}


def _provisional_line():
    """The `provisional` limit, read live from the schema enum; "" if the enum is empty."""
    values = [f"`{v}`" if v is not None else "null"
              for v in _decision_caps()["provisional"]]
    if not values:
        return ""
    head = ", ".join(values[:-1])
    return ("`provisional` is only for a stall ruling: one of "
            f"{head + ' or ' if head else ''}{values[-1]}; omit it in any other decision.")


def _request_lines(verbs):
    """The request/decision document shapes, taken from the schemas themselves. Pilot
    run 2's guide, told only the schema names, invented `to`/`subject`/`body`/`asks` and its whole
    run failed on the invalid document."""
    lines = []
    if verbs:
        fields = ", ".join(f"`{f}`" for f in _schema_fields("request.v1"))
        per_verb = ", ".join(f"{v} ({', '.join(f'`{f}`' for f in REQUIRED_FIELDS.get(v, ()))})"
                             if REQUIRED_FIELDS.get(v) else v for v in sorted(verbs))
        lines.append(f"request.v1 fields: {fields}. No other field: any other (a `to`, `subject`,"
                     " `body`, `asks`) makes the document invalid and fails your run; address and"
                     " explain in `text`.")
        lines.append(f"Your verbs and the fields each needs: {per_verb}.")
        if "untrack" in verbs:
            lines.append("`untrack` only sticks once `path` is also `.gitignore`d - otherwise the"
                         " next commit's `git add -A` re-adds it.")
            lines.append("`untrack` takes effect after your run ends: the host runs"
                         " `git rm --cached` before review. `git ls-files` in this run still lists"
                         " the path. Do not block on it; finish your report.")
    fields = ", ".join(f"`{f}`" for f in _schema_fields("decision.v1"))
    caps = _decision_caps()
    lines.append(f"decision.v1 fields (a choice put to the owner): {fields}; `options` items are"
                 " {`id`, `label`}, `recommended` is one option `id`. No other field."
                 f" Length caps: `question` <={caps['question']} chars, {caps['min_options']}-"
                 f"{caps['max_options']} `options` each with a `label` <={caps['label']} chars,"
                 f" `context` <={caps['context']} chars - over any cap is an invalid document;"
                 f" each option `id` and `recommended` match `{caps['id']}`"
                 " (lower-case letters, digits and `-`; no `_`).")
    if line := _provisional_line():
        lines.append(line)
    return lines


def _envelope(role, task):
    # request.v1 carries a `verb` field, gated by the role's contract (spec §6.2); decision.v1
    # has none, and the spec preamble lets every role drop one into its spool regardless of verbs.
    verbs = contracts.verbs_for(role)
    requests_docs = "decision.v1" if not verbs else "request.v1/decision.v1"
    lines = [
        "## Envelope",
        f"Copy these fields into your `{contracts.schema_for(role)}` report (and every"
        f" {requests_docs} document you write to requests/):",
        f"schema: {contracts.schema_for(role)}",
        f"task_id: {task.get('id', '')}",
        "run_id: <run_id, given at dispatch>",
        "claim_lock: <claim_lock, given at dispatch>",
        "Spool path: <spool>/requests/ for requests (<spool>, given at dispatch); the exec shell"
        " sees the same folder at /requests.",
        REPORT_CHANNEL,
    ] + _request_lines(verbs)
    return "\n".join(lines)


def _human_section(interventions):
    """The card's human entries, oldest first, each naming its author, so an answer reaches the
    rerun (pilot run 1: `unblock "<answer>"` re-dispatched the card with the answer nowhere in the
    prompt) and a CoS note is not read as the owner's (inc_22b42704)."""
    if not interventions:
        return None
    lines = ["## Human answers",
             "Interventions on this card, oldest first. Each names its author: `owner` is the"
             " owner; a `cos` entry is the chief of staff's note, not the owner's ruling, and does"
             " not override a spec. They answer the card's open questions and earlier holds; follow"
             " them where they settle something the card leaves open."]
    for doc in interventions:
        at = doc.get("at")
        stamp = (time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(at))
                 if isinstance(at, (int, float)) else "undated")
        by = human.canonical_author(doc.get("author") or "owner")      # no author: a pre-flag file
        lines.append(f"- [{stamp}] {doc.get('kind', '')} by {by}: {doc.get('text', '')}")
    return "\n".join(lines)


# inc_a4dd62f9: an answer naming a path outside the file set was failed as scope creep.
ANSWER_SCOPE_NOTE = (
    "A later human answer that names a path outside the file set as work to do extends the set"
    " for that path: do not fail that path as scope creep. Your other findings on that path stay"
    " valid. No answer waives a finding with `safety: true` or class `security`.")


# inc_40e0f8a8: an answer replaced a criterion for the implementer only; the review kept failing it.
ANSWER_PRECEDENCE_NOTE = (
    "A later human answer that replaces a card criterion outranks that criterion's text."
    " Judge the work against the answer for that criterion. Your other criteria and your other"
    " findings stay valid.")


def _answers_for_review(interventions):
    """The human section and its two notes for a reviewer or guide; [] when there are no answers."""
    section = _human_section(interventions)
    notes = ANSWER_SCOPE_NOTE + "\n" + ANSWER_PRECEDENCE_NOTE
    return [section + "\n" + notes] if section else []


def _invalid_document_section(error):
    """This is the role's ONE retry after a document it
    wrote last time was rejected as invalid (never a verdict on the work - see
    docs/ZENKAI.md Z2)."""
    if not error:
        return None
    return ("## Fix your last document first\n"
            "Your previous run on this card wrote a document the harness rejected as invalid:\n"
            f"> {error}\n"
            "That text names the exact JSON path that failed. Fix only that field (e.g. shorten a"
            " label under its cap) and write valid JSON this time; do not repeat the run's other"
            " work unless it also needs redoing.")


def _rows_section(rows, row_texts):
    """Row 364: the guide "reads ... the decision rows it cites" - each row's text inlined whole
    (a role with the numbers only cannot name a row)."""
    texts = row_texts or {}
    return "## Decision rows cited\n" + "\n".join(
        f"- row {r}: {texts.get(str(r)) or ROW_TEXT_MISSING}" for r in (rows or []))


def _follow_ups_text(items):
    return "follow_ups:\n" + "\n".join(
        f"- {f['title']} [{', '.join(f['files'])}]: {f['body']}" for f in items)


FULL_SUITE_NOTE = (
    "## Full suite (inc_a0206824)\n"
    "Run the tests the card names and the test modules you changed."
    " The validator and the lander run the full suite."
    " A skipped full-suite run goes in `host_only`,"
    " never in `left_undone` or `issues`."
    " A failing test you saw stays in `issues`.")


def _vault_context_section(text):
    """The retrieved vault context (row 626, advisory data) as brief parts; none when absent."""
    return [text.strip()] if text else []
