"""The guide, Korin and guide-ruling bundles, moved out of capsule/bundles.py (its file cap).
`bundles` re-exports every name here; this module imports nothing from `bundles`."""
import json

from capsule import contracts
from capsule.blind import text as blind_text
from capsule.bundle_sections import (
    FULL_SUITE_NOTE, REPORT_CHANNEL, _answers_for_review, _card_section, _envelope,
    _follow_ups_text, _header, _human_section, _invalid_document_section, _provisional_line,
    _rows_section, _schema_fields, _schema_max_length, _spec_heading, _vault_context_section)


NO_CITATIONS_NOTE = (
    "No spec citations available for this card; judge alignment against the card's own acceptance"
    " criteria and the epic title.")

FIRST_SCREEN_NOTE = ("## First screen (row 369)\n"
                     "This card renders its UI epic's first screen. When the review passes, the"
                     " tick holds the"
                     " epic and sends the screen to the owner; the next card waits for his"
                     " `screen-ok` (a"
                     " `screen-fix` returns this card with his note). Judge alignment as usual;"
                     " taste is his.")


IMPL_REPORT_MISSING = "(missing: no implementer report is available for this review)"


IMPL_REPORT_CAP = 4000   # report.v1 already caps each field (up to 50 items of
                         # ~1000 chars, 100 commands), so the whole report inlined could
                         # still run far larger than a guide bundle should carry; this
                         # bounds the section, never the individual fields the schema
                         # itself already caps.


def _impl_report_section(report):
    """The implementer's report under review, labelled
    and inlined so the guide judges alignment against what was actually done, not
    just the diff or the card."""
    if not report:
        return "## Implementer's report\n" + IMPL_REPORT_MISSING
    lines = ["## Implementer's report", f"status: {report.get('status', '')}"]
    for key in ("implemented", "left_undone", "issues", "notes", "host_only"):
        items = report.get(key) or []
        if items:
            lines.append(f"{key}:\n" + "\n".join(f"- {i}" for i in items))
    if report.get("follow_ups"):
        lines.append(_follow_ups_text(report["follow_ups"]))
    commands = report.get("commands") or []
    if commands:
        cmd_lines = [f"- `{c['cmd']}` exit {c['exit']}"
                     + (f": {c['excerpt']}" if c.get("excerpt") else "") for c in commands]
        lines.append("commands:\n" + "\n".join(cmd_lines))
    procedures = report.get("procedures_followed") or []
    if procedures:
        lines.append("procedures_followed:\n" + "\n".join(f"- {p}" for p in procedures))
    text = "\n".join(lines)
    if len(text) > IMPL_REPORT_CAP:
        marker = f" [truncated, {len(text)} chars]"
        text = text[:IMPL_REPORT_CAP - len(marker)] + marker
    return text


def _doc_length_note():
    """Guide-rows review, deploy: the guide's first output after row texts were added
    blew the request.v1 `text` cap and used its retry."""
    reason_cap = _schema_max_length("alignment_verdict.v1", "reason")
    text_cap = _schema_max_length("request.v1", "text")
    return ("## Answer length\n"
            f"Your alignment_verdict.v1 `reason` is capped at {reason_cap} chars; a request.v1"
            f" `text` you write"
            f" (a comment, for example) is capped at {text_cap} chars. Either cap failing is an"
            f" invalid"
            " document and fails your run. Write verdict-first: your first sentence states aligned"
            " or drifted"
            " (naming the row) before any explanation; then be terse - a full account is not the"
            " goal here.")


# Distinct from `intent.PRECEDENCE` ("intent rows > spec
# > contract > script > code", Z3's tie-break for a document an intent example calls
# wrong - "intent rows" there names the intent-EXAMPLES artifact,
# contracts/intent/*.jsonl, never a vault decision row).
DECISION_ROW_PRECEDENCE = ("a cited decision row outranks the spec section it contradicts"
                           " (row 455): the spec is stale, not the row")

# Row 472: `verdict` stays `aligned`/`drifted`
# (never a third value) but the two must not be defined so they overlap - the case that
# actually matters, the work following a cited row the spec disagrees with, has to land
# on exactly one of them, or two guide runs on the same bundle can disagree and route
# differently.
VERDICT_NOTE = (
    "## Verdict (row 455)\n"
    f"Precedence for ties: {DECISION_ROW_PRECEDENCE}. Decide in this order: (1) the work"
    f" contradicts a"
    " cited row - `drifted`, name that row, and do not list the row you name as drifted in"
    " `spec_conflict_rows`; (2) the work contradicts the spec section on a point no"
    " cited row speaks to - `drifted`; (3) the work follows a cited row, and that row's own text"
    " is what"
    " contradicts the spec section - `aligned`, and list that row in `spec_conflict_rows` (never"
    " `drifted` for this case: the row wins over the spec, so the spec is stale, not the work); (4)"
    " otherwise - `aligned`. `spec_conflict_rows` (optional, a SUBSET of the rows you cite - every"
    " item"
    " must also appear in `rows`) never changes `drifted` into `aligned` by itself outside rule 3,"
    " and"
    " you never edit the spec section yourself: that is the owner's call, routed to him with a"
    " remedy."
    " If the only drift is a cited row or card wording, add `amendment`: up to 3 hunks {target:"
    " card|spec_section, find: exact text occurring once, replace: new text}. A count goes to a"
    " decision, never an amendment. Never touch a Files list, Done-When, Interfaces, test names,"
    " constraints or a code block: the harness refuses those and asks the owner. Record provenance"
    " by"
    " appending a new Amended line; never edit an existing one. `amendment` is for `drifted` only:"
    " if the verdict is `aligned` and the card text itself needs a correction (a stale anchor, a"
    " wording slip), put it in `notes` instead and leave `amendment` empty."
    " Leave `amendment` empty means omit the key: the schema refuses [], and a hunk where"
    " find equals replace is not \"no change\".")


MAC_CHECKS_NOTE = (
    "## Mac card checks\n"
    "Do not ask the implementer to run or verify the board's container-only contract checks"
    " (lint, the full test suite, the web UI suite); a failing container check that the card's"
    " files do not touch goes to the validator's re-run, not into the fix list.")


def guide(task, spec_section, rows, *, first_screen=False, row_texts=None, card_hash=None,
          spec_sha=None, spec_path=None, impl_report=None, retry_error=None, out_of_set=None,
          interventions=None, blind=None, vault_context=None):
    """The four row-364 inputs, each labelled explicitly: the card (as it is now, by
    hash), spec section, decision rows (with text), and the implementer's report
    under review; plus project rules (UI tasks) and prerequisites; plus the first-
    screen flag on a UI epic's first screen (row 369)."""
    parts = [_header(task), _card_section(task, card_hash)]
    invalid_doc_section = _invalid_document_section(retry_error)
    if invalid_doc_section:
        parts.append(invalid_doc_section)
    if spec_section or rows:
        parts.append(_spec_heading(spec_path, spec_sha) + "\n" + (spec_section or ""))
        parts.append(_rows_section(rows, row_texts))
    else:
        parts.append("## Spec citations\n" + NO_CITATIONS_NOTE)
    parts += _vault_context_section(vault_context)
    parts += _answers_for_review(interventions)
    parts.append(_impl_report_section(impl_report))
    if out_of_set:   # the prior round's own out-of-set findings, for context
        parts.append("## Out-of-set findings (not counted, for context)\n" + "\n".join(
            f"- {f['file']}:{f['line']} [{f['class']}] {f['text']}" for f in out_of_set))
    parts += blind_text.counts_parts(blind)
    parts.append(VERDICT_NOTE)
    parts.append(FULL_SUITE_NOTE + " A skipped full-suite run is not drift,"
                 " even when the card names `make test`.")
    parts.append(_doc_length_note())
    if task.get("tier") == "mac":
        parts.append(MAC_CHECKS_NOTE)
    project_rules = task.get("project_rules")
    if task.get("ui") and project_rules:
        parts.append("## Project rules\n" + project_rules)
    prerequisites = task.get("prerequisites") or []
    if prerequisites:
        parts.append("## Prerequisites\n" + "\n".join(f"- {p}" for p in prerequisites))
    if first_screen:
        parts.append(FIRST_SCREEN_NOTE)
    parts.append(_envelope("guide", task))
    return {"system": contracts.render("guide"), "prompt": "\n\n".join(parts)}


RULING_SCHEMA = "decision.v1"
ASK_OWNER = "ask-owner"    # the guide leaves the card to the owner
LEGACY_ASK = "ask-pratik"    # guide outputs and queued rulings stored before the rename say this
ASKS = (ASK_OWNER, LEGACY_ASK)


def accepted(offered):
    """The choices a guide report may carry: the offered ones, and the old ask verb a guide
    output stored before the rename still holds."""
    return (*offered, LEGACY_ASK) if ASK_OWNER in offered else offered

RULING_OPTIONS = (
    ("document-wrong", "the document is wrong: a reviewer verdict reruns the reviewer, any other"
                       " role resets the card; the reason reaches the rerun"),
    ("example-wrong", "the intent example is wrong: accept the card, and a change to the intent"
                      " file goes through the row 393 gate"),
    (ASK_OWNER, "the spec is silent or ambiguous here: the owner rules"),
)
RUNAWAY_OPTIONS = (    # capsule/runaway.py OPTIONS ids
    ("continue", "exactly one more implementer run; `context` is the one complete fix list"),
    ("split", "the card is too big or wrong: `context` proposes the child cards, the owner"
              " decides"),
    (ASK_OWNER, "only the owner can decide this: `context` says why"),
)
STALL_OPTIONS = (    # capsule/stall_ruling.py IDS: the L1 stall ruling (one per stall)
    ("release", "a routine card-side hold (a chance failure, a question the card and the pushed"
                " docs answer, ordinary fixes): `context` is the one complete note or fix list"),
    ("release-capsule-fault", "Capsule caused the hold and the card is innocent: `context` names"
                              " the Capsule defect and says what the implementer must not change"),
    (ASK_OWNER, "judgement, product scope, an unclear spec, or a Capsule fault the same run would"
                " hit again: `question` is the owner's question in plain words; also write"
                " `provisional` (`release`, `release-capsule-fault` or null) and `reversible`"
                " (true when the provisional release can be undone the same day)"),
)
RED_EXEMPT_OPTIONS = (    # capsule/blind/exempt.py IDS
    ("red-exempt", "the product already has this behaviour before the card: its tests may pass on"
                   " the base"),
    ("not-exempt", "the behaviour is new: its tests must fail on the base, as every test does"),
)


def _claims_parts(ruling):
    return ["## Card held before dispatch (claims check)",
            "A plain script checked the paths this card's text names against the repo at its"
            " branch head and found the ones listed below missing. You cannot see the code: decide"
            " from the card text alone. If each missing path is plainly something the card has the"
            " implementer create, or a stale name with an obvious current one, choose `continue`"
            " and write in `context` the corrected instruction (it reaches the implementer as a"
            " guide ruling). If you cannot tell what the card should say, choose `ask-owner` and"
            " say why in `context`. Do not choose `split`.",
            "### The card\n" + ruling.get("card", ""),
            "### Not in the repo\n" + "\n".join(f"- {m}" for m in ruling.get("missing") or [])]


def _runaway_parts(ruling):
    """The stopped card, why it tripped, its cost and every finding of every round."""
    if ruling.get("kind") == "claims":
        return _claims_parts(ruling)
    return ["## Runaway card (stop-the-line)",
            f"The harness stopped this card: {ruling.get('detail')}."
            f" Cost so far: {ruling.get('cost')}. Read every finding below, then write ONE"
            " complete fix list in `context`: each finding AND every sibling instance of it (the"
            " same defect class elsewhere in the same files). Choose `continue` only when one"
            " more implementer run with that list can finish the card.",
            "### Findings, every round\n"
            + "\n".join(f"- {line}" for line in ruling.get("findings") or [])]


def _stall_parts(ruling):
    """The held card, its hold, the findings and the one question: whose fault is the hold."""
    return ["## Stalled card (stall ruling)",
            f"This card has been held {ruling.get('age_s') or 0} s ({ruling.get('class')},"
            f" {ruling.get('hold')}: {ruling.get('reason')}). `suspect_infra` is"
            f" {ruling.get('suspect_infra')}. Cost so far: {ruling.get('cost')}. You get one"
            " ruling for this stall and the card gets exactly one more run. Answer: "
            + str(ruling.get("question")),
            "### Earlier rulings\n" + "\n".join(f"- {x}" for x in ruling.get("earlier") or []),
            "### Findings, every round\n"
            + "\n".join(f"- {line}" for line in ruling.get("findings") or [])]


def _red_exempt_parts(ruling):
    """The statements whose `red_exempt` tests pass on the code from before the card."""
    return ["## Red-exempt request",
            "The blind tester listed tests in `red_exempt`: tests that pass on the code from before"
            " the card because, it says, their statement describes behaviour the product already"
            " has. You see no code and no test source. Choose `red-exempt` only when every"
            " statement below plainly describes behaviour that existed before this card; one"
            " choice covers them all. Choose `not-exempt` when any is new behaviour.",
            "### Statements\n" + "\n".join(f"- {ex['id']}: " + "; ".join(ex.get("differs") or [])
                                           for ex in ruling.get("examples") or [])]


RULING_ANSWER_NOTE = (   # inc_eea0243d: a ruling reversed a CoS answer it never saw
    "Do not reverse a human answer above. If your ruling must reverse one, choose `ask-owner`"
    " and name that answer in `context`.")


def _contradiction_parts(ruling):
    return [
        "## Contradiction to rule (row 454)",
        f"A {ruling.get('role')} document passed its schema but an intent example calls it wrong."
        f" Precedence for ties: {ruling.get('precedence')}. A contract or script the spec"
        " contradicts is wrong by definition; choose ask-owner only when the spec is silent or"
        " ambiguous.",
        "### The document\n```json\n"
        + json.dumps(ruling.get("document"), ensure_ascii=False, indent=1) + "\n```"]


def guide_ruling(task, ruling, spec_section="", rows=(), row_texts=None, spec_sha=None,
                 spec_path=None, interventions=None):
    """Row 454 item 3: the guide rules on a contradiction between a schema-valid document and an
    intent example. It sees the document, each example line verbatim with what differed, the
    precedence and the card's spec citations; never code. It answers with one decision.v1."""
    runaway = ruling.get("kind") in ("runaway", "claims")
    stall = ruling.get("kind") == "stall"
    red = any(ex.get("kind") == "red_exempt" for ex in ruling.get("examples") or [])
    parts = [_header(task)] + (_runaway_parts(ruling) if runaway else _stall_parts(ruling)
                               if stall else _red_exempt_parts(ruling)
                               if red else _contradiction_parts(ruling))
    for ex in [] if red else ruling.get("examples") or []:
        parts.append(f"### Example {ex.get('id')} ({ex.get('source')})\n```json\n"
                     + json.dumps(ex.get("example"), ensure_ascii=False) + "\n```\nWhat differed: "
                     + "; ".join(ex.get("differs") or []))
    if spec_section or rows:
        parts.append(_spec_heading(spec_path, spec_sha) + "\n" + (spec_section or ""))
        parts.append(_rows_section(rows, row_texts))
    if section := _human_section(interventions):
        parts.append(section + "\n" + RULING_ANSWER_NOTE)
    offered = (RUNAWAY_OPTIONS if runaway else STALL_OPTIONS if stall else RED_EXEMPT_OPTIONS
               if red else RULING_OPTIONS)
    options = ", ".join(f"`{i}` ({label})" for i, label in offered)
    parts.append("## Your ruling\n"
                 f"Answer with ONE `{RULING_SCHEMA}` document with `options` exactly these"
                 f" {'two' if red else 'three'} ids and labels: {options}; `recommended` is your"
                 " ruling; `question` one line naming the contradiction; `context` your reason,"
                 " citing the row or spec section (it is inlined for the rerun on"
                 " document-wrong and in the intent-change card on example-wrong)."
                 " You never edit a contract or intent file.")
    parts.append("\n".join([
        "## Envelope",
        f"schema: {RULING_SCHEMA}",
        f"task_id: {task.get('id', '')}",
        "run_id: <run_id, given at dispatch>",
        "claim_lock: <claim_lock, given at dispatch>",
        REPORT_CHANNEL,
        f"{RULING_SCHEMA} fields: " + ", ".join(f"`{f}`" for f in _schema_fields(RULING_SCHEMA))
        + ". No other field."]
        + ([] if stall or not _provisional_line() else [_provisional_line()])))
    return {"system": contracts.render("guide"), "prompt": "\n\n".join(parts)}
