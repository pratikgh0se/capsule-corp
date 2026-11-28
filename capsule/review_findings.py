"""Reviewer document gates (Zenkai Z2, Z3; row 509 Z7 - new logic in a new module, tick.py gets
one call site): `refuse` runs the row 362 gate (a `pass` with non-empty `findings`) and the
row 540 gate (a late non-safety `fail`) before `intent.check` ever sees the document. Each
fails as an ordinary invalid document, retried once with its message, instead of a
contradiction routed to a guide ruling.

Also the shape of what a failed round tells the implementer (R32, blind spec 4.3): `counted` picks
the validator assertions that are findings (never a blind row: those come as `blind_finding`)."""
import json

from capsule import invalid_doc as idoc, validator_report
from capsule.db import event, txn

MESSAGE = ("a `pass` verdict may not carry `findings`: move each finding you judge settled by a"
          " cited row, already fixed in this diff, or purely informational into `notes` (with its"
          " citation) - `findings` holds only what must still change, and a pass carries none"
          " (row 362)")


def is_violation(doc):
    """`doc` (the reviewer's judged document, out-of-set findings already moved by
    `review_scope.judged_document`) is `verdict: pass` with `findings` still non-
    empty (row 362: a finding means fail) - the pure predicate, with no DB access, so
    it is unit-testable on its own (Zenkai Z2's second oracle beside
    `contracts/intent/reviewer.jsonl`'s own `pass-with-findings` pattern)."""
    return doc.get("verdict") == "pass" and bool(doc.get("findings"))


LATE_ROUND = 4
LATE_MESSAGE = (
    "from review round 4 on, a `fail` carries only a regression, a `safety: true` finding or a"
    " class `security` finding (row 540): move each other non-safety finding into `notes`, or"
    " tag a true safety finding `safety: true`, or file a decision.v1 for the owner; a true"
    " regression and a class `security` finding stay in `findings`")


def _is_late_fail(doc, round):
    """True for a non-safety `fail` at round 4 or later (row 540): findings non-empty,
    none `safety: true` or class `security`."""
    findings = doc.get("findings") or []
    safety = any(f.get("safety") is True or f.get("class") == "security" for f in findings)
    return (round >= LATE_ROUND and doc.get("verdict") == "fail" and bool(findings) and not safety)


def _earlier_tries(conn, run):
    """Reviewer runs of the same round (same `review_of`, after the latest accept's `after_run`)
    before `run`, rate limits excluded."""
    of = (json.loads(run["metadata"] or "{}")).get("review_of")
    floor = 0
    ev = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='request_review'"
                      " AND run_id=(SELECT id FROM task_runs WHERE task_id=? AND run_id=?)"
                      " ORDER BY id DESC LIMIT 1", (run["task_id"], run["task_id"], of)).fetchone()
    if ev is not None and ev[0]:
        floor = json.loads(ev[0]).get("after_run") or 0
    rows = conn.execute("SELECT metadata FROM task_runs WHERE task_id=? AND profile='reviewer'"
                        " AND id>? AND id<? AND COALESCE(outcome,'')!='rate_limited'",
                        (run["task_id"], floor, run["id"])).fetchall()
    return sum(1 for r in rows if (json.loads(r["metadata"] or "{}")).get("review_of") == of)


TWO_FIXES_MESSAGE = (
    "each finding names one fix: keep the fix you judge right and delete the others, or file a"
    " decision.v1 for the owner when the choice is not yours (inc_10c5859d)")
# Closed list, case-sensitive: the explicit phrases a finding uses to offer a second fix. A
# finding that quotes one of them as plain prose is refused too; it is retried once, and a later
# try keeps its current path, so the cost of a false hit is one retry.
TWO_FIXES_MARKERS = ("The other option", "Another option is", "Alternatively,", "Either fix",
                     "or instead,")


def _offers_two_fixes(doc):
    """True for a `fail` whose finding `text` holds one of `TWO_FIXES_MARKERS`."""
    return doc.get("verdict") == "fail" and any(
        m in str(f.get("text") or "") for f in doc.get("findings") or [] for m in TWO_FIXES_MARKERS)


def refuse(conn, run, doc, sp, now):
    """Row 362 gate first, then the one-fix gate, then the row 540 late-fail gate: all but the
    first refused on the first reviewer try of a round only (a later try keeps its path)."""
    if is_violation(doc):
        return _refuse_with(conn, run, MESSAGE, now)
    if _offers_two_fixes(doc) and not _earlier_tries(conn, run):
        return _refuse_with(conn, run, TWO_FIXES_MESSAGE, now)
    try:
        inputs = (json.loads((sp / "job.json").read_text()) or {}).get("inputs") or {}
    except (OSError, ValueError):
        return False
    if not _is_late_fail(doc, inputs.get("round") or 0) or _earlier_tries(conn, run):
        return False
    return _refuse_with(conn, run, LATE_MESSAGE, now)


def _refuse_with(conn, run, message, now):
    """Refuse `run`, reusing `idoc.close_failed_run`'s review branch (`run_json={}`/
    `error_class=None` skip its crash case; `review=True, is_invalid=True` skip its
    retry/record_failure) so the run ends exactly like a schema-invalid report: no handoff,
    `error_class` `invalid_output`, retried once by `Tick._review_one` with this message
    (`capsule.invalid_doc.reason`)."""
    with txn(conn):
        event(conn, run["task_id"], "invalid_document", {"reason": message[:500]},
              run_pk=run["id"], run_id=run["run_id"], now=now)
    idoc.close_failed_run(conn, None, run, {}, None, run["profile"], run["task_id"], True,
                          message, True, now)
    return True


def blind_finding(facts):
    """The one finding a failing blind test gives the implementer: its id, given, expect and
    observed (`facts` holds exactly these four strings), and nothing else."""
    return (f"blind:{facts['id']} failed\ngiven: {facts['given']}\nexpect: {facts['expect']}"
            f"\nobserved: {facts['observed']}")


def counted(assertions, judged_only=False):
    """The validator assertions that are findings: a `fail`, and a `not_run` unless `judged_only`.
    A blind one (`blind:<id>`) never is: its finding is `blind_finding`, built by the tick. A cmd
    `withhold` renamed (`WITHHELD_CMD`: no row of the job, perhaps a typo of a plain one) is no
    blind row: a `fail` on it stays a failure."""
    return [a for a in assertions if (a["cmd"] == validator_report.WITHHELD_CMD
                                      or not validator_report.is_blind(a["cmd"]))
            and (a["result"] == "fail" or (a["result"] == "not_run" and not judged_only))]


def validator_lines(doc, judged_only, gate):
    """The validator's findings for the implementer. Report mode: `validator_report.findings`,
    a blind row being the one fixed phrase (D161). Gate mode (`gate`): the plain rows only, the
    failing blind tests come from the job's own rows (`check_gate.findings`, R32)."""
    if gate:
        doc = {**doc, "assertions": counted(doc["assertions"], judged_only)}
    return validator_report.findings(doc, judged_only)


def fallback(role, doc, judged_only=False):
    """The generic line for a failed document that yielded no finding: `<role>: verdict fail`,
    unless `judged_only` or the document failed only on blind tests (their finding is the whole
    story: no extra line)."""
    blind = role == "validator" and not counted(doc["assertions"]) and any(
        a["result"] == "fail" for a in doc["assertions"])
    return [] if judged_only or blind else [f"{role}: verdict fail"]

