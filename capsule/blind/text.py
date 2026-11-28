"""The words gate mode (R32) adds to the role bundles, split from `bundles` for its size."""

GATE_NOTE = (
    "Entries marked `mode: gate` (cmd `blind:<id>`) are blind acceptance results and they count."
    " You cannot see the tests, only given (the input), expect (the right outcome) and observed"
    " (what the product did). Write one assertion for each, `cmd` copied verbatim: `fail` for a"
    " non-zero exit, `pass` for exit 0, never `not_run`. A card with a failing blind test cannot"
    " get a `pass` verdict.")


def counts_parts(blind):
    """The reviewer's and guide's view of the blind run, as a list of prompt parts: counts and
    failing ids, nothing else. [] when there is no blind run."""
    if not blind:
        return []
    counts = ", ".join(f"{k} {n}" for k, n in blind["counts"].items())
    failing = ", ".join(blind["failing"]) or "none"
    ran = (f"Counts: {counts}. Failing: {failing}. Their content is not yours to judge."
           if blind["counts"] else "No blind acceptance tests ran for this card.")
    return ["## Blind tests (gate mode)\nAcceptance tests written and run without you seeing"
            f" them. {ran}\n" + acceptance_line(blind["counts"], len(blind["failing"]))]


def acceptance_line(counts, failing):
    """M115, gate mode: `acceptance: green` (every landed blind test passed), `acceptance: red
    (K of N failing)`, or `acceptance: none` (no landed blind tests). `counts` maps each outcome
    to its number of landed tests, `failing` is how many failed."""
    total = sum(counts.values())
    if not total:
        return "acceptance: none"
    return f"acceptance: red ({failing} of {total} failing)" if failing else "acceptance: green"
