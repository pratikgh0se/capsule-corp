"""The blind leak-lint incident (R32, blind spec 4.4 items 4 and 8). A gate-mode blind run
(`capsule.blind.run`) writes one `blind_gated` event with `leaks`: how many failing tests had a
finding holding a whole line of the snapshot (those findings were withheld). A count above 0 opens
a `review_infra` incident: fingerprint = check name + card (the card rides in the role); the text
holds the count, never a line. `incidents._classify` ends in `classify`: other events are None."""

KIND = "blind_gated"
CHECK = "blind_leak_lint"


def evidence(row, payload):
    """The incident's own text for a leak: the count (never a line), else []. Evidence reaches the
    post-mortem, which otherwise sees only `event:<id>` and a spool path."""
    if classify(row, payload) is None:
        return []
    return [f"{CHECK}: {payload['leaks']} failing test(s) had a finding holding a line of the"
            " blind tests (withheld; no line is kept here)"]


def classify(row, payload):
    """(kind, role, key) of a `blind_gated` event whose leak count is above 0, else None."""
    if row["kind"] != KIND or not payload.get("leaks"):
        return None
    return ("review_infra", f"{CHECK}:{row['task_id']}",
            f"{CHECK}: {payload['leaks']} finding(s) held a line of the blind tests")
