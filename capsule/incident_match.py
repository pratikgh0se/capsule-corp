"""The pattern language of `contracts/intent/*.jsonl` as Korin's examples use it (split out of
incidents.py, which is at its size cap)."""

INTENT_WORDS = ("<empty>", "<non-empty>", "<any>")
OPERATORS = ("<not>", "<each>", "<some>")
MISSING = object()


def matches(pattern, value):
    """The pattern language of `contracts/intent/*.jsonl` (capsule/intent.py on
    capsule/intent-examples; the subset Korin's examples use). Swap for `intent.matches` once
    that branch is on main."""
    if isinstance(pattern, str) and pattern in INTENT_WORDS:
        if pattern == "<any>":
            return value is not MISSING
        empty = value is MISSING or value is None or value in ("", [], {})
        return empty if pattern == "<empty>" else not empty
    if isinstance(pattern, dict) and len(pattern) == 1 and next(iter(pattern)) in OPERATORS:
        (op, inner), = pattern.items()
        if op == "<not>":
            return not matches(inner, value)
        if not isinstance(value, list) or not value:
            return False
        hits = [matches(inner, v) for v in value]
        return all(hits) if op == "<each>" else any(hits)
    if isinstance(pattern, dict):
        return isinstance(value, dict) and all(
            matches(p, value.get(k, MISSING)) for k, p in pattern.items())
    return value is not MISSING and value == pattern
