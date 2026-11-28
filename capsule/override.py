"""The `override` verb: a reasoned, TTL'd, per-check bypass of a review-stage check (row
454 item 4: "contract-correct but intent-wrong scripts"; Zenkai Z4)."""
import json
import time

from capsule import identity, outbox
from capsule.db import event, txn

IRREVERSIBLE_DEFAULT = ("merge", "deploy", "give-back")
REPEAT_WINDOW_S = 14 * 24 * 3600
MAX_TTL_DEFAULT = 7 * 24 * 3600   # a hard ceiling; Zenkai Z4.3 fixes the rule's own TTL at 72h
NOTIFY_CHANNEL = "file"
TTL_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_ttl(text):
    """'72h' -> 259200 seconds; suffixes s/m/h/d, a bare number is seconds. Raises ValueError
    on anything else (zero, negative, empty, unknown suffix) - a bad TTL is a refusal, never a
    silent default."""
    text = (text or "").strip()
    if not text:
        raise ValueError("empty TTL")
    suffix = text[-1].lower()
    if suffix in TTL_UNITS:
        num, unit = text[:-1], suffix
    else:
        num, unit = text, "s"
    if not num or not num.lstrip("-").isdigit():
        raise ValueError(f"bad TTL {text!r}")
    n = int(num)
    if n <= 0:
        raise ValueError(f"TTL must be positive: {text!r}")
    return n * TTL_UNITS[unit]


def irreversible_checks(cfg):
    # a misconfigured string (not a list) must never silently become a set of characters.
    raw = cfg.get("irreversible_checks")
    return set(raw) if isinstance(raw, list) and all(isinstance(x, str) for x in raw) else set(IRREVERSIBLE_DEFAULT)


def max_ttl(cfg):
    val = cfg.get("override_max_ttl")
    return val if isinstance(val, int) and not isinstance(val, bool) and val > 0 else MAX_TTL_DEFAULT


def known_checks(cfg):
    """The only valid check ids - a contract command's own text, or its 0-based index as a
    string (stable even if the command's wording changes later)."""
    cmds = cfg.get("contract_commands") or []
    return set(cmds) | {str(i) for i in range(len(cmds))}


def normalize_check(check, cfg):
    """An index form ('0', '1', ...) resolves to its `contract_commands`
    text; text already matching a command passes through unchanged."""
    cmds = cfg.get("contract_commands") or []
    if check in cmds:
        return check
    if isinstance(check, str) and check.lstrip("-").isdigit():
        i = int(check)
        if 0 <= i < len(cmds):
            return cmds[i]
    return None


def grant(conn, cfg, board, task_id, *, check, reason, ttl, fix_card, issuer, now=None, identity_dir=None):
    """Validate and record one override, or refuse it."""
    now = int(now if now is not None else time.time())
    reason = (reason or "").strip()
    if not check or not reason or not fix_card or not issuer:
        return _refuse(conn, task_id, check, reason, "override needs check, reason, fix_card and issuer", now)
    normalized = normalize_check(check, cfg)
    if normalized is None:
        return _refuse(conn, task_id, check, reason,
                       f"check {check!r} matches no contract command (by text or index); known:"
                       f" {sorted(known_checks(cfg))}", now)
    check = normalized
    resolved = identity.verify(issuer, identity_dir=identity_dir)
    if resolved is None:
        return _refuse(conn, task_id, check, reason,
                       f"issuer {issuer!r} has no resolvable identity file"
                       " (~/.config/capsule/identities/<name>.env, mode 600, owner-only, a real file)", now)
    issuer = resolved
    try:
        ttl_s = parse_ttl(ttl)
    except ValueError as exc:
        return _refuse(conn, task_id, check, reason, f"bad ttl: {exc}", now)
    cap = max_ttl(cfg)
    if ttl_s > cap:
        return _refuse(conn, task_id, check, reason,
                       f"ttl {ttl!r} exceeds the {cap}s cap (Zenkai Z4.3: the override rule's own TTL is 72h;"
                       f" nothing here may outlive {cap}s)", now)
    fix_row = conn.execute("SELECT id, status FROM tasks WHERE id=?", (fix_card,)).fetchone()
    if fix_row is None or fix_row["status"] in ("done", "archived") or fix_card == task_id:
        return _refuse(conn, task_id, check, reason,
                       f"fix card {fix_card!r} must name an open task on this board, not the card itself", now)
    if check in irreversible_checks(cfg):
        operator = cfg.get("operator")
        if not operator or issuer != operator:
            return _refuse(
                conn, task_id, check, reason,
                f"check {check!r} is irreversible; only the operator ({operator or 'unset'}) may override it,"
                f" not {issuer!r}", now)
    prior = _last_grant(conn, board, check, since=now - REPEAT_WINDOW_S)
    if prior is not None:
        return _refuse(
            conn, task_id, check, reason,
            f"check {check!r} was already overridden on this board within 14 days"
            f" (task {prior['task_id']} at {prior['at']}): contract must change", now)
    expires_at = now + ttl_s
    with txn(conn):
        event(conn, task_id, "override",
              {"check": check, "reason": reason, "expires_at": expires_at, "fix_card": fix_card,
               "issuer": issuer, "board": board}, now=now)
        outbox.add(conn, NOTIFY_CHANNEL,
                   f"capsule: {task_id} override granted on check {check!r} until {expires_at}"
                   f" (fix card {fix_card}): {reason[:300]}", thread_key=task_id, now=now)
    return True, None


def _refuse(conn, task_id, check, reason, why, now):
    with txn(conn):
        event(conn, task_id, "override_refused", {"check": check, "reason": reason, "why": why}, now=now)
        outbox.add(conn, NOTIFY_CHANNEL, f"capsule: {task_id} override refused on check {check!r}: {why}",
                   thread_key=task_id, now=now)
    return False, why


def _last_grant(conn, board, check, *, since):
    """The most recent granted override of `check` anywhere on this board's db, at or after
    `since` - expired grants still count (the 14-day rule is about repeating the same bypass,
    not about it still being live)."""
    for r in conn.execute(
            "SELECT task_id, created_at, payload FROM task_events WHERE kind='override' AND created_at>=?"
            " ORDER BY id DESC", (since,)):
        payload = json.loads(r["payload"] or "{}")
        if payload.get("check") == check and payload.get("board") == board:
            return {"task_id": r["task_id"], "at": r["created_at"]}
    return None


def last(conn, task_id, check):
    """The most recent override payload for `(task_id, check)`, whether still active or already
    expired, or `None` if none was ever granted (lets the caller tell "never overridden"
    apart from "was, and lapsed")."""
    for r in conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='override' ORDER BY id DESC",
                          (task_id,)):
        payload = json.loads(r["payload"] or "{}")
        if payload.get("check") == check:
            return payload
    return None


def active(conn, task_id, check, now=None):
    """The unexpired override payload for `(task_id, check)`, or `None`. An expired one (or none
    at all) is `None` - the review stage then judges the check normally."""
    now = int(now if now is not None else time.time())
    payload = last(conn, task_id, check)
    return payload if payload is not None and payload.get("expires_at", 0) > now else None


def note_expiry(conn, task_id, check, payload, now):
    """One `override_expired` event plus one outbox line the first time a lapsed
    grant is seen again (keyed on check + its own `expires_at`, so a later, distinct
    grant of the same check after the 14-day window gets its own announcement); a
    later pass over the same lapsed grant is silent."""
    for r in conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='override_expired'",
                          (task_id,)):
        p = json.loads(r["payload"] or "{}")
        if p.get("check") == check and p.get("expires_at") == payload.get("expires_at"):
            return False
    with txn(conn):
        event(conn, task_id, "override_expired",
              {"check": check, "expires_at": payload.get("expires_at"), "fix_card": payload.get("fix_card")},
              now=now)
        outbox.add(conn, NOTIFY_CHANNEL,
                   f"capsule: {task_id} override on check {check!r} expired at {payload.get('expires_at')};"
                   " the check is live again", thread_key=task_id, now=now)
    return True


def report(conn, board, *, now=None):
    """Per-card active/expired/refused overrides plus board totals, for `capsule report`
    (row 454 item 4)."""
    now = int(now if now is not None else time.time())
    by_task = {}
    granted = expired = refused = 0
    for r in conn.execute("SELECT task_id, kind, payload, created_at FROM task_events"
                          " WHERE kind IN ('override','override_refused') ORDER BY id"):
        payload = json.loads(r["payload"] or "{}")
        entry = by_task.setdefault(r["task_id"], [])
        if r["kind"] == "override":
            state = "active" if payload.get("expires_at", 0) > now else "expired"
            entry.append({"state": state, "check": payload.get("check"), "reason": payload.get("reason"),
                          "expires_at": payload.get("expires_at"), "fix_card": payload.get("fix_card"),
                          "issuer": payload.get("issuer"), "at": r["created_at"]})
            granted += 1
            expired += state == "expired"
        else:
            entry.append({"state": "refused", "check": payload.get("check"), "reason": payload.get("reason"),
                          "why": payload.get("why"), "at": r["created_at"]})
            refused += 1
    applied = conn.execute("SELECT COUNT(*) FROM task_events WHERE kind='checks_overridden'").fetchone()[0]
    return {
        "board": board, "by_task": by_task,
        "totals": {"granted": granted, "active": granted - expired, "expired": expired, "refused": refused,
                   "applied": applied},
    }
