"""The guide's `amendment`/`notes` scope check, kept in its own
small module - a second oracle (Z2), independent of `contracts/intent/guide.jsonl`'s
`aligned-verdict-carries-no-amendment` pattern, for the same fact."""
from capsule.db import event, txn


def drop_no_op_hunks(role, conn, task_id, run, report, now):
    """A guide `report` without `amendment` hunks whose find equals replace (they change nothing,
    inc_10699f92); `amendment` is removed when none remain, and one `amendment_no_op_dropped`
    event carries the count. Any other role's report is returned as is; `report` is never
    mutated."""
    hunks = report.get("amendment")
    if role != "guide" or not isinstance(hunks, list):
        return report
    kept = [h for h in hunks if not (isinstance(h, dict) and h.get("find") == h.get("replace"))]
    if len(kept) == len(hunks):
        return report
    with txn(conn):
        event(conn, task_id, "amendment_no_op_dropped", {"count": len(hunks) - len(kept)},
              run_pk=run["id"], run_id=run["run_id"], now=now)
    out = {k: v for k, v in report.items() if k != "amendment"}
    if kept:
        out["amendment"] = kept
    return out


def amendment_scope_violations(report):
    """[] or one contradiction-shaped finding ({id, source, differs}, `intent.check`'s own shape,
    row 454): an `aligned` verdict may not carry a non-empty `amendment` - that field is for
    `drifted` only, a card-text correction on an aligned verdict belongs in `notes`."""
    amendment = report.get("amendment") or []
    if report.get("verdict") == "aligned" and amendment:
        return [{"id": "aligned-amendment-scope", "source": "inc_fbebe355",
                 "differs": [f"verdict aligned but amendment carries {len(amendment)} hunk(s);"
                            " a card-text correction on an aligned verdict belongs in `notes`,"
                            " never `amendment` (amendment exists only for drifted)"]}]
    return []


def rows_not_handed(report, inputs):
    """[] or one finding when a `drifted` verdict's `rows` name a row outside `inputs["rows"]`
    (row 468: the guide cannot search the vault, so it may only judge the rows it was handed).
    Only `drifted`, the verdict that holds the card (an `aligned` one holds nothing); sibling of
    the `spec_conflict_rows` subset check in `Tick._guide_structural_checks`. The intent
    example only fires on an empty bundle, this covers a non-empty one too."""
    handed = inputs.get("rows") or []
    stray = [r for r in report.get("rows") or [] if r not in handed]
    if report.get("verdict") != "drifted" or not stray:
        return []
    return [{"id": "rows-not-handed", "source": "row 468",
             "differs": [f"rows names {stray}, not among the rows handed {sorted(handed)}"]}]
