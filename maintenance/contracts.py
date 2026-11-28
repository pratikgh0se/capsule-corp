"""Every string shape the roles exchange, in one place (CI spec section 2).
Parsers import these; the SOUL prompts are grepped against them in
tests/test_contracts.py, so a prompt edit and a parser edit cannot drift."""

READY_PREFIX = "READY: "
DECISION_PREFIX = "DECISION:"
GATE_PR_READY = "gate: PR ready"
GATE_NOT_MERGED = "gate: not merged - "
GATE_CHANGES = "gate: human asked for changes: "
DECISION_OPEN = "gate: decision open "
DECISION_ANSWER = "human: decision "
# C3/M042 (row 127): with OPS_VERDICT_STORE on, the board comment is only a mirror of
# the host-only verdict record (`maintenance/verdicts.py`) - any agent with Edit on the
# card can write the comment, nobody but the host can write the record.
VERDICT_MIRROR_SUFFIX = " [advisory mirror - the host record is authoritative]"
# The `hold:` board-comment prefix (decisions.py reads it).
HOLD_PREFIX = "hold: "
QUALITY_PASS = "quality: pass - "
QUALITY_FAIL = "quality: fail round "
# The reviewer's diff packet (row 125 / C6).
DIFF_PACKET = "diff: "
DIFF_UNAVAILABLE = DIFF_PACKET + "unavailable - "
REVIEW_PASS_MARK = "review: PASS"
REVIEW_PASS = REVIEW_PASS_MARK + " round "
REVIEW_ROUND = "review: round "
CHANGES_REQUESTED_EVENT = "changes_requested"
REVIEW_REQUESTED_EVENT = "review_requested"
HUDDLE_PREFIX = "huddle: "
HUDDLE_CAP_COMMENT = "huddle: over cap - lead must raise a DECISION now"
# C7/M143 (row 135), deferred from row 109 / C3's M037 (row 127): the string a role is
# told to write when it recognizes an injection attempt in untrusted input, so the
# ingest side can grep for the exact same literal.
INJECTION_ATTEMPT_PREFIX = "injection-attempt: "
META_KEYS = ("risk", "spec", "spec_sha256", "source", "area", "slug")
SUITE_KEYS = ("command", "passed", "failed")
UI_KEYS = ("passed", "failed", "screenshots")


R_CLASSES = ("R1", "R2", "R3")   # M028, row 126: reversible / costly / one-way


def format_decision(question, why, options, recommend, reasoning, learn, cls="R3"):
    """The DECISION block every role writes. `cls` is M028's R-class and
    defaults to R3, the most conservative reading: an unclassified decision is
    treated as one-way until somebody says otherwise."""
    cls = str(cls).upper()
    if cls not in R_CLASSES:
        raise ValueError(f"class must be one of {R_CLASSES}, got {cls!r}")
    opts = "\n".join(f"{i}. {o} - {t}" for i, (o, t) in enumerate(options, 1))
    return (f"{DECISION_PREFIX} {question}\nWhy: {why}\nClass: {cls}\n{opts}\n"
            f"Recommend: {recommend} - {reasoning}\nLearn: {learn}")
