"""`capsule incidents duplicate <id> --of <canonical>`: mark an incident that repeats a live one
as `superseded` by it. Not a close: no `closed_at`, so repeat counting (row 456) is unchanged."""
import time

from capsule import blocks, db, incident_watch, incidents

CLOSURE = "duplicate"
LIVE = ("open", "fix_pending")     # postmortem: a live Korin run needs the row


def _same_problem(inc, canon):
    """"fingerprint" when the two share one, else None. A fix card or kind+card match alone is not
    enough: with a different fingerprint the later repeat would be hidden (repeats match by it)."""
    same = inc["fingerprint"] and inc["fingerprint"] == canon["fingerprint"]
    return "fingerprint" if same else None


def _refusal(inc, canon, canon_id, reason):
    if canon is None:
        return f"canonical incident {canon_id!r} does not exist on this board"
    if canon["id"] == inc["id"]:
        return "an incident cannot be a duplicate of itself"
    if inc["closure"] == "watch":
        return f"incident {inc['id']} is a watch closure: it cannot become a duplicate"
    if inc["status"] not in LIVE:
        return (f"incident {inc['id']} is {inc['status']}:"
                " only a live incident can become a duplicate")
    if canon["closed_at"] and canon["closed_at"] < inc["opened_at"]:
        return (f"canonical {canon['id']} was closed before {inc['id']} opened: that is a repeat,"
                " not a duplicate")
    if canon["status"] == incident_watch.SUPERSEDED:
        pointer = (canon["evidence"] or "").count("duplicate_of")
        return (f"canonical {canon['id']} is itself superseded"
                + (" (see its duplicate_of evidence line)" if pointer else "")
                + ": use the incident it points to")
    if not reason and _same_problem(inc, canon) is None:
        return (f"{inc['id']} and {canon['id']} share no fingerprint, fix card or kind+card:"
                " pass --reason to say why they are the same problem")
    return None


def mark(conn, incident_id, canonical_id, *, author, reason=None, now=None):
    """Supersede `incident_id` by `canonical_id`; ValueError with a clear message on a refusal."""
    now = int(now if now is not None else time.time())
    reason = (reason or "").strip()
    with db.txn(conn):
        inc = incidents.get(conn, incident_id)
        if inc is None:
            raise ValueError(f"no incident {incident_id!r}")
        canon = incidents.get(conn, canonical_id)
        why = _refusal(inc, canon, canonical_id, reason)
        if why:
            raise ValueError(why)
        done = conn.execute(
            "UPDATE incidents SET status=?, closure=? WHERE id=?"
            " AND status IN ('open','postmortem','fix_pending')",
            (incident_watch.SUPERSEDED, CLOSURE, incident_id))
        if done.rowcount != 1:
            raise ValueError(f"incident {incident_id} changed status meanwhile: unchanged")
        shared = _same_problem(inc, canon) or "reason"
        own = inc["fix_card"] if inc["fix_card"] != canon["fix_card"] else None
        parked = own and ":" not in own      # a cross-board card is not on this board: not parked
        incidents.attach(conn, incident_id, [
            f"duplicate_of {canonical_id} by {author} at {now} ({shared})"
            + (f": {reason}" if reason else "")
            + (f"; fix card {own} is on another board: not parked" if own and not parked else "")])
        cards = (inc["fix_card"], inc["task_id"], canon["fix_card"], canon["task_id"])
        card = next((c for c in cards if c and conn.execute(
            "SELECT 1 FROM tasks WHERE id=?", (c,)).fetchone()), None)
        if card:
            db.event(conn, card, "incident_duplicate", {
                "incident": incident_id, "duplicate_of": canonical_id, "by": author,
                "basis": shared, "reason": reason}, now=now)
        if parked:      # a stall release must not unblock the twin's own fix card
            blocks.park(conn, inc["fix_card"], f"duplicate of {canonical_id}: its fix is on the"
                        f" canonical's card", now=now, by=author)
    return canonical_id
