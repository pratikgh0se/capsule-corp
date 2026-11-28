"""Which human touches are defects (factory-autonomy spec 3)."""
import re

PRODUCT, TEXT, HARNESS = "product", "text", "harness"
# row 369: the first screen is the owner's by design (ruling Q6)
BY_DESIGN_PRODUCT = ("screen-ok", "screen-fix")
EXEMPT_TAGS = ("migration-reset", "handover-route")
HARNESS_MECHANICS = re.compile(
    r"\b(retry|rerun|re-run|reset|unblock|skip|override|merge|accept|reroute|restart|dispatch|park)\b", re.I)
HARNESS_HOLDS = ("review_infra", "spec_stale", "failure-limit", "needs_input/guide-drift", "needs_input/impl-hold",
                 "needs_input/korin-fix", "parked", "contradiction_loop", "guide_ruling")
TEXT_MECHANICS = re.compile(
    r"\b(amend|reword|rephrase|revert|restore|drop|remove|delete|dismiss|fix|update|correct)\b.{0,60}"
    r"\b(card|plan|spec|wording|text|line|count|hunk|doc|document|citation|row|file set)\b", re.I)

SHOULD_HAVE = (
    ("*", "needs_input/guide-drift", "apply the scope-neutral card or plan fix itself (auto-amend), or state"
                                     " why the drift is more than text"),
    ("*", "needs_input/impl-hold", "route an implementer's text-outside-file-set flag to an auto-amend or a"
                                   " one-verb decision with the exact lines (row 485)"),
    ("*", "needs_input/korin-fix", "route a Korin fix card to an epic and dispatch it without a human unblock"),
    ("*", "review_infra", "retry the review role on its fallback route before asking you (R2)"),
    ("*", "spec_stale", "propose the spec-section edit as an auto-amend hunk instead of a hold"),
    ("*", "failure-limit", "diagnose the repeated failure (Korin) before parking, and say what would unpark it"),
    ("answer", "*", "offer the lossless option as one verb or apply it itself (row 491)"),
    ("override", "*", "fix the check the override bypasses (its Z4 fix card)"),
    ("epic", "*", "put new cards on a declared epic when it files them"),
    ("park", "*", "park by a named trigger automatically"),
    ("amend", "*", "catch the stale card text before dispatch (a card lint)"),
)


def reason_class(was):
    was = was or {}
    status, kind, hold = was.get("status"), was.get("block_kind"), (was.get("hold") or "")
    if not status and not kind:
        return "unknown"
    if kind == "needs_input":
        for prefix, cls in (("alignment drifted", "guide-drift"), ("left undone / issues", "impl-hold"),
                            ("Korin fix card", "korin-fix")):
            if hold.startswith(prefix):
                return f"needs_input/{cls}"
        return "needs_input/other"
    if kind:
        return kind
    if status == "triage":
        return "failure-limit"
    return status


def _texts(decision):
    opts = [o for o in (decision or {}).get("options") or [] if isinstance(o, dict)]
    rec = next((o for o in opts if o.get("id") == (decision or {}).get("recommended")), None)
    return opts, rec


def lexical(decision):
    opts, rec = _texts(decision)
    if any(o.get("amendment") for o in opts):
        return "text"
    if rec is not None and TEXT_MECHANICS.search(f"{rec.get('id', '')} {rec.get('label', '')}"):
        return "text"
    return "not-text"


def classify(verb, decision, was=None):
    """(class, disagreement). `was` is the tick's own snapshot of the card at touch
    time."""
    if verb != "answer":
        return (PRODUCT if verb in BY_DESIGN_PRODUCT else HARNESS), False
    if not decision:
        return HARNESS, False
    declared, lex = decision.get("scope"), lexical(decision)
    _, rec = _texts(decision)
    mechanics = rec is not None and HARNESS_MECHANICS.search(f"{rec.get('id', '')} {rec.get('label', '')}")
    reason = reason_class(was)
    harness_hold = reason in HARNESS_HOLDS
    oracle_missing = rec is None or reason == "unknown"
    if lex == "text":
        return TEXT, declared == PRODUCT
    if declared == PRODUCT and (mechanics or harness_hold or oracle_missing):
        return HARNESS, True
    if declared == PRODUCT:
        return PRODUCT, False
    return (HARNESS if declared == HARNESS or mechanics else TEXT), False


def key(verb, rclass):
    return f"{verb}: {rclass}"


def should_have(verb, rclass):
    for v, c, text in SHOULD_HAVE:
        if v in ("*", verb) and (c == "*" or rclass == c):
            return text
    return (f"decide '{verb}' on a '{rclass}' card without the owner, or record why it is"
            " irreversible or product scope (Z6)")


def card(source_board, task_id, verb, rclass, text, incident_id):
    action = should_have(verb, rclass)
    title = f"Harness should have: {action}"[:120]
    body = "\n".join([
        f"Human touch on {source_board}:{task_id}: `{verb}` on a `{rclass}` card (incident {incident_id}).",
        f"What the human wrote: {' '.join((text or '').split())[:500]}",
        f"What the harness should have done itself: {action}.",
        "Done when the harness takes this decision without the owner if it is reversible"
        " and in spec, or records why it is not (Zenkai Z6), with a test that replays this touch.",
    ])
    return title, body
