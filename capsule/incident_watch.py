"""Korin's watch closure (spec 2026-10-24 section 6): quiet-window checks, failed-watch lookup.
`closure = watch` closes an unproven transient after `watch_days` with no recurrence."""
import time

DAY = 86400
WATCH_DAYS = 14
BARRED = ("repeat", "escaped_bug")     # a watch closure is never allowed for these
FAILED = "watch_failed"                # closure of a watch a recurrence superseded
SUPERSEDED = "superseded"              # its status: neither open nor closed (no closed_at either)


def gaps(conn, incident, watch_days, now):
    """Quiet-window checks of a `closure = watch` incident (oracle A: the incidents table)."""
    if incident.get("closure") != "watch":
        return []
    now = int(now if now is not None else time.time())
    end = incident["opened_at"] + watch_days * DAY
    out = []
    if end > now:
        out.append(f"watch window open until {time.strftime('%Y-%m-%d', time.gmtime(end))}"
                   f" ({watch_days} days from opening)")
    if recurred(conn, incident):
        out.append("the same fingerprint reopened after this incident: the watch failed")
    return out


def recurred(conn, incident):
    """True when a later incident with this one's fingerprint exists (a watch there cannot hold)."""
    return conn.execute(
        "SELECT 1 FROM incidents WHERE fingerprint=? AND id!=? AND opened_at>? LIMIT 1",
        (incident["fingerprint"], incident["id"], incident["opened_at"])).fetchone() is not None


def watched(conn, fingerprint):
    """The still-open watched incident of this fingerprint (a recurrence is a `repeat`), or None."""
    return conn.execute(
        "SELECT id FROM incidents WHERE fingerprint=? AND closure='watch'"
        " AND status NOT IN ('closed','superseded')"
        " ORDER BY opened_at DESC LIMIT 1", (fingerprint,)).fetchone()
