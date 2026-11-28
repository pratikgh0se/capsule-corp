"""Korin's record: open, attach, apply a post-mortem, and the closure gate (row 456;
Zenkai rows 454, 455)."""
import hashlib
import json
import os
import re
import secrets
import sqlite3
import subprocess
import time
from pathlib import Path

from capsule import (blocks, host, human, incident_inputs, incident_watch, outbox, paths,
                     routine_nudges)
from capsule import incident_blind, incident_blind_run, incident_leak, incident_match
from capsule import incident_recurrence, incident_repo, slack_msgs
from capsule.db import INCIDENTS_WATERMARK, connect_ro, event, guard_get, guard_set, new_task, txn
from capsule.incident_recurrence import (
    SHELL_ID_RE, TEST_TIMEOUT, recurrence_problems, recurrence_test_is_new, run_recurrence_test)

# row 470: a second firing of the SAME Grafana alert within 24h is the incident
ALERT_REFIRE_WINDOW = 86400
ALERT_GUARD_PREFIX = "alert_firing:"

KINDS = ("crash", "hang", "silent_failure", "review_infra", "invalid_document", "contradiction",
        "human_intervention", "override", "escaped_bug", "repeat",
        "two_dispatchers", "main_checkout_write", "lost_card_state", "runaway")
STATUSES = ("open", "postmortem", "fix_pending", "closed", incident_watch.SUPERSEDED)
KEEP_IDS = ("human_intervention", "override", "escaped_bug", "runaway")   # ids stay in the key
WATCH_BARRED = incident_watch.BARRED   # a watch closure is never allowed for these kinds
PER_EVENT = ("human_intervention", "override", "escaped_bug")   # one per file/grant/card
ROUTINE_COS_KINDS = routine_nudges.ROUTINE_COS_KINDS     # cos nudges: no incident
ROUTINE_REASON = "routine CoS nudge (policy 2026-10-21)"
REPEAT_WINDOW = 30 * 86400
DAY = incident_watch.DAY
WATCH_DAYS = incident_watch.WATCH_DAYS
RECORD_FIELDS = ("root_cause", "lesson", "mitigation", "enforcer", "recurrence_test")
ID_RE = re.compile(r"^inc_[0-9a-f]{8}$")
WATERMARK = INCIDENTS_WATERMARK
KORIN, NOTE_DIR = "korin", "Knowledge/Incidents"
DEFAULT_DECISIONS_NOTE = host.DEFAULT_DECISION_NOTE


class CloseRefused(Exception):
    """A typed refusal: `missing` lists every closure check that failed (the CLI prints them)."""

    def __init__(self, incident_id, missing):
        self.incident_id, self.missing = incident_id, list(missing)
        super().__init__(f"incident {incident_id} not closed: " + "; ".join(self.missing))


# -- fingerprint ----------------------------------------------------------------------------

# tempfile paths and names
_TMP = re.compile(paths.TEMP_ROOT + r"/\S*|\btmp[a-z0-9_]{6,}\b")
_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
_CARD_ID = re.compile(r"\b(?:t|inc)_[0-9a-f]{8}\b")
_ADDR = re.compile(r"\b0x[0-9a-f]+\b")
# run ids, shas, hashes (8+ hex); 'cafe1' stays
_LONGHEX = re.compile(r"\b[0-9a-f]{8,}\b")
_NUM = re.compile(r"\b\d+\b")
# an exit code discriminates: keep it
_KEEP_NUM = re.compile(r"(?:exit|rc|code|signal|status)[\s=:]*$")


def normalize(key, *, keep_ids=False):
    """Lowercase; mask tempfile paths, uuids, 0x addresses, hex runs of 8+, card/incident ids (kept
    when `keep_ids`) and bare numbers except an exit/rc/signal/status code; collapse whitespace."""
    text = str(key or "").lower()
    text = _TMP.sub("<tmp>", text)
    text = _UUID.sub("#", text)
    if not keep_ids:
        text = _CARD_ID.sub("#", text)
    text = _ADDR.sub("#", text)
    text = _LONGHEX.sub("#", text)
    text = _NUM.sub(lambda m: m.group(0) if _KEEP_NUM.search(text[:m.start()]) else "#", text)
    return " ".join(text.split())[:300]


def fingerprint(kind, key, *, role=None):
    """sha256("<kind>\\n<role>\\n<normalised key>")[:16]; task ids stay in the key for the
    per-event kinds (human_intervention, override), where the card IS the cause."""
    if kind not in KINDS:
        raise ValueError(f"unknown incident kind {kind!r}")
    norm = normalize(key, keep_ids=kind in KEEP_IDS)
    return hashlib.sha256(f"{kind}\n{role or ''}\n{norm}".encode("utf-8")).hexdigest()[:16]


main_head = incident_blind_run.main_head


def _day_start(ts):
    return int(ts) - int(ts) % DAY


# -- the record -----------------------------------------------------------------------------

def get(conn, incident_id):
    row = conn.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
    return dict(row) if row is not None else None


def listing(conn, status=None):
    sql, args = "SELECT * FROM incidents", ()
    if status:
        sql, args = sql + " WHERE status=?", (status,)
    return [dict(r) for r in conn.execute(sql + " ORDER BY opened_at, id", args)]


def attach(conn, incident_id, evidence):
    """Merge `evidence` (paths or `event:<id>` strings) into the record's list, keeping order, no duplicates."""
    with txn(conn):
        row = conn.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
        if row is None:
            raise LookupError(f"no incident {incident_id!r}")
        have = json.loads(row["evidence"] or "[]")
        _, fresh = incident_inputs.blind_safe(row["role"], "", evidence)
        have += [e for e in fresh if e and e not in have]
        conn.execute("UPDATE incidents SET evidence=? WHERE id=?", (json.dumps(have, ensure_ascii=False), incident_id))
    return have


def open_incident(conn, *, board, kind, key, evidence=(), task_id=None, run_id=None, role=None, main_sha=None,
                  now=None, description=None):
    """(id, "opened" | "deduped" | "repeat" | "recurred"). See the module docstring for the rule."""
    if kind not in KINDS or kind == "repeat":
        raise ValueError(f"cannot open kind {kind!r} (repeat is decided here, not by the caller)")
    now = int(now if now is not None else time.time())
    key, evidence = incident_inputs.blind_safe(role, key, evidence)     # D161: no tester text
    fp = fingerprint(kind, key, role=role)
    with txn(conn):
        same_day = conn.execute(     # per-event: empty day window, only fix_pending non-watch folds
            "SELECT id, status, fix_card FROM incidents WHERE fingerprint=?"
            " AND COALESCE(closure,'') != 'watch' AND (status='fix_pending' OR (opened_at>=?"
            " AND opened_at<? AND status NOT IN ('closed','superseded')))"
            " ORDER BY status='fix_pending' DESC, opened_at LIMIT 1",
            (fp, _day_start(now), _day_start(now) + (0 if kind in PER_EVENT else DAY))).fetchone()
        if same_day is not None:     # fix_pending any day, per-event too: attach, page
            attach(conn, same_day["id"], list(evidence))
            if same_day["status"] != "fix_pending":
                return same_day["id"], "deduped"
            _opened(conn, same_day["id"], kind, "recurred", task_id, run_id, now)
            incident_recurrence.recurred(conn, fp, same_day, task_id, now, key, normalize(key))
            return same_day["id"], "recurred"
        original = conn.execute(
            "SELECT id FROM incidents WHERE fingerprint=? AND status='closed' AND closed_at>=?"
            " ORDER BY closed_at DESC LIMIT 1", (fp, now - REPEAT_WINDOW)).fetchone()
        watched = incident_watch.watched(conn, fp)      # a recurrence in a watch is a repeat
        original = original or watched
        action = "repeat" if original is not None else "opened"
        incident_id = "inc_" + secrets.token_hex(4)
        conn.execute(
            "INSERT INTO incidents (id, board, task_id, run_id, kind, opened_at, evidence, root_key, fingerprint,"
            " status, repeat_of, role, main_sha, description)"
            " VALUES (?,?,?,?,?,?,?,?,?,'open',?,?,?,?)",
            (incident_id, board, task_id, run_id, "repeat" if original else kind, now,
             json.dumps(list(dict.fromkeys(e for e in evidence if e)), ensure_ascii=False),
             f"{kind}: {role + ': ' if role else ''}{normalize(key, keep_ids=kind in KEEP_IDS)}",
             fp, original["id"] if original else None, role, main_sha, description))
        if watched is not None and original is watched:     # the watch failed: not closed
            conn.execute("UPDATE incidents SET status=?, closure=? WHERE id=?",
                         (incident_watch.SUPERSEDED, incident_watch.FAILED, watched["id"]))
        _opened(conn, incident_id, kind, action, task_id, run_id, now)
        if original is not None:           # the headline number (row 456): always one line
            incident_recurrence.page(
                conn, fp, task_id, now,
                f"REPEAT incident {incident_id}: {kind} again within 30 days",
                fields=[("original", original["id"]), ("root key", normalize(key)[:200])],
                file_body=f"capsule: REPEAT incident {incident_id} ({kind}) of {original['id']}:"
                          f" {normalize(key)[:300]}")
    return incident_id, action


def _opened(conn, incident_id, kind, action, task_id, run_id, now):
    if task_id and conn.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone():
        event(conn, task_id, "incident_opened",
              {"incident": incident_id, "kind": kind, "action": action}, run_id=run_id, now=now)


def set_decision_row(conn, incident_id, row):
    if not isinstance(row, int) or isinstance(row, bool) or row <= 0:
        raise ValueError("decision row must be a positive integer")
    with txn(conn):
        if conn.execute("UPDATE incidents SET decision_row=? WHERE id=?", (row, incident_id)).rowcount != 1:
            raise LookupError(f"no incident {incident_id!r}")


def summary(conn, since=None):
    """Counts for the report and the status board: repeat first (the headline, row 456). `closed`
    includes `closed_history` (closure 'history', row 638 b: not repairs). `backlog`: open incidents
    (not closed, not superseded) opened before `since` (epoch): reported, never dispatched."""
    rows = conn.execute("SELECT kind, status, closure FROM incidents").fetchall()
    backlog = 0 if since is None else conn.execute(
        "SELECT COUNT(*) FROM incidents WHERE status NOT IN ('closed','superseded')"
        " AND opened_at < ?",
        (since,)).fetchone()[0]
    by_status = {s: 0 for s in STATUSES}
    by_kind = {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + 1
    done = by_status["closed"] + by_status["superseded"]
    return {"repeat": by_kind.get("repeat", 0), "open": len(rows) - done,
            "closed": by_status["closed"], "total": len(rows), "by_status": by_status,
            "by_kind": by_kind, "backlog": backlog, "closed_history": sum(
                r["status"] == "closed" and r["closure"] == "history" for r in rows)}


# -- alert firings (row 470: a Grafana page that repeats within a day is the incident) --------

def record_alert_firing(conn, cfg, *, alert, key, board, kind="silent_failure", role=None, now=None,
                        evidence=(), run_id=None):
    """One Grafana alert (e.g. `UnownedWorkIdle`) pages every time it fires - that is
    Slack's job (contact-points-slack.yaml), not Korin's."""
    now = int(now if now is not None else time.time())
    guard_key = ALERT_GUARD_PREFIX + alert
    with txn(conn):
        state, version = guard_get(conn, guard_key)
        last = int((state or {}).get("last_fired_at") or 0)
        repeat = bool(last) and (now - last) <= ALERT_REFIRE_WINDOW
        guard_set(conn, guard_key, {"last_fired_at": now, "count": int((state or {}).get("count") or 0) + 1},
                  expect_version=version, now=now)
        if not repeat:
            return "recorded"
        if not settings(cfg)["enabled"]:
            return "korin_disabled"
        fp = fingerprint(kind, key, role=role)
        already_open = conn.execute(
            "SELECT id FROM incidents WHERE fingerprint=?"
            " AND status NOT IN ('closed','superseded')"
            " AND COALESCE(closure,'') != 'watch' ORDER BY opened_at LIMIT 1",
            (fp,)).fetchone()
        if already_open is not None:
            if evidence:
                attach(conn, already_open["id"], list(evidence))
            return "deduped"
        _, action = open_incident(conn, board=board, kind=kind, key=key, role=role, now=now,
                                  evidence=evidence, run_id=run_id)
    return action


# -- auto-open from typed events (the tick's pass) --------------------------------------------

def _payload(row):
    try:
        doc = json.loads(row["payload"] or "{}")
    except ValueError:
        return {}
    return doc if isinstance(doc, dict) else {}


def _classify(row, payload, conn=None):
    """(incident kind, role, root-cause key) for one typed event, or None when it opens nothing.
    A crash keys on its error class, exit code and the adapter's error line (run.json `error`); a
    hang on the worker log's last line; a human file or an override on its card."""
    kind = row["kind"]
    if kind == "crashed":
        return "crash", payload.get("role"), (f"{payload.get('error_class')}: exit {payload.get('exit')}:"
                                              f" {payload.get('error', '')}")
    if kind == "timed_out":
        return "hang", payload.get("role"), f"timed out: {payload.get('last_log', '')}"
    if kind in ("blocked", "block_loop_detected") and payload.get("kind") == "invalid_document":
        # an IMPLEMENTER wrote a malformed side-document twice (`_invalid_document_retry`): own kind
        return "invalid_document", "implementer", f"invalid_document: {payload.get('reason', '')}"
    if kind in ("blocked", "block_loop_detected") and payload.get("kind") == "review_infra":
        # A REVIEW role whose own document was a repeated invalid requests/ document (row 507(b))
        # is its own cause; every other review_infra cause keeps the general classification.
        if payload.get("cause") == "invalid_document":
            return "review_infra", "review", f"invalid_document: {payload.get('reason', '')}"
        return "silent_failure", "review", f"review_infra: {payload.get('reason', '')}"
    if kind == "contradiction":
        if payload.get("routed") == "pending":
            # ruling race fix (tick.py `_contradiction`): recorded once deferred, again once routed
            return None
        ids = sorted(e.get("id", "") for e in payload.get("examples") or [] if isinstance(e, dict))
        return "contradiction", payload.get("role"), ",".join(ids)
    if kind == "contradiction_loop":
        # the guide ruled document-wrong on the same example(s) past the reset cap (tick.py)
        ids = ",".join(sorted(payload.get("examples") or []))
        return "contradiction", payload.get("role"), f"loop: {ids}"
    # an override opens as `override` below
    if kind == "intervention" and payload.get("kind") != "override":
        if routine_nudges.is_routine_intervention(conn, row["task_id"], row["id"], payload):
            return None    # a routine cos nudge, or a Korin fix-card hold unblock (inc_b8de7b6c)
        return "human_intervention", None, f"{payload.get('kind')}: {row['task_id']}"
    if kind == "override":
        return "override", None, f"{payload.get('check')}: {row['task_id']}"
    if kind == "runaway":    # one per card and trigger; a same-day second trip dedupes
        return "runaway", None, f"{payload.get('trigger')}: {row['task_id']}"
    if kind == "tick_error":    # a crash keyed by the message, or the blind-mount incident (R33)
        return incident_blind.classify(row, payload)
    if kind == "worktree_mismatch":
        # row 465: the implementer preflight refusal is its own crash class, keyed by `error_class`,
        # so a `worktree_dirty` repeat never shares a fingerprint with a `worktree_mismatch` one.
        return ("crash", payload.get("role"),
               f"{payload.get('error_class', 'worktree_mismatch')}: {payload.get('error', '')}")
    return incident_leak.classify(row, payload)      # R32: the blind leak lint, else None


EVENT_KINDS = ("crashed", "timed_out", "blocked", "block_loop_detected", "contradiction", "contradiction_loop",
               "intervention", "override", "tick_error", "worktree_mismatch", "runaway",
               incident_leak.KIND)


HANDOFF_WINDOW = 600     # seconds between an answer and the unblock that follows it
HOLD_SQL = ("SELECT 1 FROM task_events WHERE task_id=? AND id>? AND id<? AND kind IN ("
            + ",".join(f"'{k}'" for k in blocks.HOLD_EVENTS) + ") LIMIT 1")
FOLD_SQL = ("SELECT id FROM incidents WHERE kind='human_intervention' AND task_id=?"
            " AND status NOT IN ('closed','superseded') AND evidence LIKE ? ORDER BY opened_at")


def _fold_target(conn, row, payload):
    """One CoS hand-off, one incident (inc_5b420323): the open incident an `unblock` folds into
    (it follows its own `answer`: same author, close in time, no re-block between), else None."""
    if row["kind"] != "intervention" or payload.get("kind") != "unblock":
        return None
    prior = conn.execute("SELECT id, payload FROM task_events WHERE task_id=? AND id<?"
                         " AND kind='intervention' ORDER BY id DESC LIMIT 1",
                         (row["task_id"], row["id"])).fetchone()
    earlier = json.loads(prior["payload"] or "{}") if prior else {}
    author = human.canonical_author(payload.get("author"))
    if earlier.get("kind") != "answer" or not author \
            or human.canonical_author(earlier.get("author")) != author:
        return None
    at, then = payload.get("at"), earlier.get("at")
    if not all(isinstance(t, (int, float)) and not isinstance(t, bool) for t in (at, then)):
        return None
    if abs(at - then) > HANDOFF_WINDOW:
        return None
    if conn.execute(HOLD_SQL, (row["task_id"], prior["id"], row["id"])).fetchone():
        return None     # the card blocked again after the answer: a new decision
    found = conn.execute(FOLD_SQL, (row["task_id"], f'%"event:{prior["id"]}"%')).fetchone()
    return found["id"] if found else None


def from_events(conn, *, board, spool_root=None, human_dir=None, main_sha=None, now=None):
    """Open (or dedupe) one incident per typed failure event past the watermark; returns
    the ids touched."""
    now = int(now if now is not None else time.time())
    mark, version = guard_get(conn, WATERMARK)
    top = conn.execute("SELECT COALESCE(MAX(id), 0) FROM task_events").fetchone()[0]
    if mark is None:
        guard_set(conn, WATERMARK, {"event_id": top}, expect_version=0, now=now)
        return []
    since = int(mark.get("event_id", 0))
    if top <= since:
        return []
    touched = []
    rows = conn.execute(
        f"SELECT e.id, e.task_id, e.kind, e.payload, e.created_at FROM task_events e LEFT JOIN tasks t ON t.id = e.task_id"
        f" WHERE e.id>? AND e.id<=? AND e.kind IN ({','.join('?' * len(EVENT_KINDS))})"
        f" AND COALESCE(t.assignee, '') != ? ORDER BY e.id", (since, top, *EVENT_KINDS, KORIN)).fetchall()
    with txn(conn):
        for row in rows:
            payload = _payload(row)
            found = _classify(row, payload, conn)
            if found is None:
                continue
            kind, role, key = found
            run_id = payload.get("run_id")
            evidence = [f"event:{row['id']}", *incident_leak.evidence(row, payload)]
            ids = dict.fromkeys([run_id, *payload.get("verdict_runs", ())])
            evidence += [str(Path(spool_root) / r) for r in ids if r and spool_root is not None]
            if row["kind"] == "intervention" and human_dir is not None and payload.get("file"):
                evidence.append(str(Path(human_dir) / "done" / payload["file"]))
            folded = _fold_target(conn, row, payload)   # inc_5b420323
            if folded:
                attach(conn, folded, evidence)
                touched.append(folded)
                continue
            incident_id, _ = open_incident(conn, board=board, kind=kind, key=key, evidence=evidence,
                                           task_id=row["task_id"], run_id=run_id, role=role, main_sha=main_sha,
                                           now=now)
            touched.append(incident_id)
        guard_set(conn, WATERMARK, {"event_id": top}, now=now)
    return touched


# -- Korin's document (postmortem.v1) ---------------------------------------------------------

def intent_examples(directory=None):
    path = Path(directory or paths.REPO / "contracts" / "intent") / f"{KORIN}.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


_ENFORCER_MECHANISM_RE = re.compile(r"\b[\w./-]+\.(?:py|sh|json|jsonl)\b|\btest_\w+")
# KORIN_NOTE (bundles.py) gives this wording for "not yet mechanisable": an honest label.
_ENFORCER_REVIEW_RE = re.compile(
    r"^\s*enforced by review until mechani[sz]ed:\s*(.*)$", re.I | re.S)
_ENFORCER_WHERE_RE = re.compile(
    _ENFORCER_MECHANISM_RE.pattern + r"|\bt_[0-9a-f]{8}\b|\bfix card\b", re.I)


def _weak_enforcer(text):
    """True when `enforcer` names no mechanism (Z1/Z2: free text, so rewording "review" dodges
    the literal intent rows): it needs a repo file (.py/.sh/.json/.jsonl) or a test id, or the
    "enforced by review until mechanised: <where>" label with <where> naming a repo file, a test,
    a card id (t_xxxxxxxx) or "fix card"."""
    text = text or ""
    label = _ENFORCER_REVIEW_RE.match(text)
    return not (_ENFORCER_MECHANISM_RE.search(text)
                or (label and _ENFORCER_WHERE_RE.search(label.group(1))))


# "The alert fired two times in one day." and nothing else: the symptom reworded (korin row 5).
_SYMPTOM_ONLY_RE = re.compile(
    r"^\W*(?:the\s+)?alert\s+(?:fired|fires|firing)\b[^.;]{0,40}?\b(?:twice|again|\w+\s+times)\b"
    r"(?:\s+(?:in|within|over|per|on)\s+\w+(?:\s+\w+){0,3})?\W*$", re.I)
_STOP = frozenset("the a an of in on to and was were is it this that for by with at as".split())


def _restates_symptom(root_cause, incident):
    """True when `root_cause` only repeats the symptom (Z2: korin row 5 bans one wording, this
    catches the rewordings): the bare "alert fired twice" family, or three or more content words
    that are all already in the incident's own root key."""
    text = root_cause or ""
    if incident.get("kind") == "silent_failure" and _SYMPTOM_ONLY_RE.match(text.strip()):
        return True
    words = {w for w in re.findall(r"[a-z0-9_]+", text.lower()) if w not in _STOP and len(w) > 2}
    key = set(re.findall(r"[a-z0-9_]+", (incident.get("root_key") or "").lower()))
    return len(words) >= 3 and words <= key


def postmortem_problems(doc, incident, *, directory=None, conn=None):
    """[] or one line per reason the tick must not apply Korin's schema-valid document:
    it names another incident, its recurrence test is not a unittest id, its
    decision-row text does not name the incident, or an intent example
    (contracts/intent/korin.jsonl, the second oracle after the schema) calls it
    wrong."""
    problems = []
    if doc.get("incident_id") != incident["id"]:
        problems.append(f"incident_id {doc.get('incident_id')!r} is not {incident['id']!r}")
    problems += recurrence_problems(doc.get("recurrence_test") or "", paths.REPO)
    if incident["id"] not in (doc.get("decision_row_text") or ""):
        problems.append("decision_row_text does not name the incident id")
    if _weak_enforcer(doc.get("enforcer")):
        problems.append("enforcer names no mechanism: give a repo file, a test id, or the label"
                        " 'enforced by review until mechanised: <where>' (Zenkai Z2)")
    if _restates_symptom(doc.get("root_cause"), incident):
        problems.append("root_cause restates the symptom: name the line or rule that let it happen")
    if doc.get("closure") == "watch" and incident.get("kind") in WATCH_BARRED:
        problems.append(f"closure 'watch' is not allowed for kind {incident.get('kind')!r}")
    if (doc.get("closure") == "watch" and conn is not None
            and incident_watch.recurred(conn, incident)):
        problems.append("closure 'watch' is not allowed: the same fingerprint already reopened"
                        " (a later incident exists), so a watch could never close")
    inputs = {"incident": {k: incident.get(k) for k in ("id", "kind", "task_id", "fingerprint", "repeat_of")}}
    for ex in intent_examples(directory):
        given = ex.get("given") or {}
        if not incident_match.matches(given.get("document", {}), doc):
            continue
        if "inputs" in given and not incident_match.matches(given["inputs"], inputs):
            continue
        bad = [k for k, p in (ex.get("expect") or {}).items()
               if not k.startswith("$")
               and not incident_match.matches(p, doc.get(k, incident_match.MISSING))]
        if bad:
            problems.append(f"contradicts intent example {ex['id']} ({ex['source']}): {', '.join(bad)}")
    return problems


def note_rel(incident, now=None):
    stamp = time.strftime("%Y-%m-%d", time.gmtime(incident["opened_at"] if now is None else now))
    return f"{NOTE_DIR}/{stamp} - {incident['kind']} - {incident['id']}.md"


def _yaml(value):
    return json.dumps(value, ensure_ascii=False)


def render_note(incident, doc, fix_card, *, now, watch_days=WATCH_DAYS):
    """The post-mortem note, AI-first (vault _CLAUDE.md): rich frontmatter, `## For future agent`, wikilinks."""
    date = time.strftime("%Y-%m-%d", time.gmtime(now))
    watch = doc.get("closure") == "watch"
    evidence = json.loads(incident.get("evidence") or "[]")
    front = [
        "---", "type: incident", f"date: {date}", f"incident: {incident['id']}", f"kind: {incident['kind']}",
        f"board: {_yaml(incident['board'])}", f"task: {_yaml(incident.get('task_id'))}",
        f"run: {_yaml(incident.get('run_id'))}", f"fingerprint: {incident['fingerprint']}",
        f"repeat-of: {_yaml(incident.get('repeat_of'))}", f"fix-card: {fix_card}",
        f"recurrence-test: {_yaml(doc['recurrence_test'])}", f"enforcer: {_yaml(doc['enforcer'])}",
        f"status: {'watching' if watch else 'fix_pending'}",
        f"closure: {'watch' if watch else 'fixed'}",
        "decision-row: pending", f"confidence: {doc['confidence']}",
        "tags: [capsule-corp, incident, korin, " + incident["kind"].replace("_", "-") + "]", "ai-first: true",
        'project: "[[Capsule Corp - A Proper Software Factory]]"',
        'related: ["[[Capsule Corp - Zenkai Rules]]", "[[Capsule Corp - System design decisions 2026-09-06]]"]',
        "---", ""]
    repeat = (f" It is a REPEAT of incident `{incident['repeat_of']}`: the same root-cause fingerprint was closed"
              " within 30 days, so the last fix did not hold." if incident.get("repeat_of") else "")
    body = [
        "## For future agent", "",
        f"Post-mortem for Capsule incident `{incident['id']}` ({incident['kind']}, board `{incident['board']}`,"
        f" opened {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(incident['opened_at']))}), written by the tick"
        f" from Korin's `postmortem.v1` document (row 456).{repeat} The incident closes only when fix card"
        f" `{fix_card}` is done, {_test_hint(doc['recurrence_test'])} on main, the decision row"
        " is recorded and this note exists (capsule/incidents.py). Do not edit the record fields here by hand;"
        " the board row is authoritative.", "",
        "## Root cause", "", doc["root_cause"], "",
        "## Lesson", "", doc["lesson"], "",
        "## Mitigation", "", doc["mitigation"], "",
        "## Enforcer", "", doc["enforcer"], "",
        "## Recurrence test", "", _recurrence_text(doc, watch_days), "",
        "## Fix card", "", f"`{fix_card}`: {doc['fix_card']['title']}", "",
        "## Decision row (for the owner / Fable to record)", "", doc["decision_row_text"], "",
        "## Evidence", ""] + [f"- `{e}`" for e in evidence] + ["",
        "## Sources", "", f"- Board `{incident['board']}` incidents row `{incident['id']}`"
        f" (`python3 -m capsule incidents show {incident['id']}`)",
        "- [[Capsule Corp - System design decisions 2026-09-06]] row 456 (Korin)", ""]
    return "\n".join(front + body)


def _recurrence_text(doc, watch_days):
    test = doc["recurrence_test"]
    if doc.get("closure") == "watch":
        return (f"Closure: watch ({watch_days} days, detector `{test}`): the cause is unproven;"
                " the detector must pass on main and the same fingerprint must not reopen.")
    return f"`{test}` (must fail before the fix, pass after, on main)."


def _test_hint(test_id):
    """The checklist command for a recurrence test id (unittest or shell suite)."""
    shell = SHELL_ID_RE.match(test_id)
    if not shell:
        return f"`python3 -m unittest {test_id}` passes"
    label = f" with a `PASS {shell.group(2)}` line" if shell.group(2) else ""
    return f"`bash {shell.group(1)}` passes{label}"


def _card_test_line(doc):
    test = doc["recurrence_test"]
    if doc.get("closure") == "watch":
        return (f"- Detector test: `{test}` - a watch closure (cause unproven): this card improves"
                " the evidence so a repeat is diagnosable; the detector test must pass on main.")
    return (f"- Recurrence test: `{test}` - write it first;"
            " it fails before the fix and passes after.")


def fix_card_body(incident, doc, conn=None, cfg=None):
    from capsule import prior_verdicts     # lazy: prior_verdicts reads this module (row 585)
    body = "\n".join([
        doc["fix_card"]["body"], "",
        f"## Incident {incident['id']} (Korin post-mortem, row 456)",
        f"- Root cause: {doc['root_cause']}",
        f"- Mitigation: {doc['mitigation']}",
        f"- Enforcer: {doc['enforcer']}",
        _card_test_line(doc),
        f"- [ ] {_test_hint(doc['recurrence_test'])} (the incident gate re-runs it on main).",
    ])
    if conn is not None:
        title = doc["fix_card"]["title"]
        body += prior_verdicts.section(conn, cfg, title, body, exclude=(incident["id"],))
    return body


def fix_route(conn, cfg, incident):
    """(branch, assignee) for the fix card: `korin.fix_epic`, else the branch of the
    incident's card when it is one of the config's epics; `korin.fix_assignee`, else the incident
    card's assignee when it routes to the implementer, else the first implementer assignee."""
    ks = settings(cfg)
    epics = list((cfg or {}).get("epics") or [])
    assignees = (cfg or {}).get("assignees") or {}
    task = conn.execute("SELECT branch_name, assignee FROM tasks WHERE id=?",
                        (incident.get("task_id"),)).fetchone() if incident.get("task_id") else None
    branch = ks["fix_epic"] or (task["branch_name"] if task is not None and task["branch_name"] in epics else None)
    implementers = [a for a, role in assignees.items() if role == "implementer"]
    assignee = ks["fix_assignee"] or (task["assignee"] if task is not None and task["assignee"] in implementers
                                      else (implementers[0] if implementers else None))
    return branch, assignee


def file_fix_card(conn, cfg, inc, doc, branch, assignee, now):
    """The incident's fix card: an attached one is kept, else a blocked one is filed."""
    if inc.get("fix_card"):
        return inc["fix_card"]
    fix = new_task(conn, title=doc["fix_card"]["title"], assignee=assignee, branch=branch,
                   body=fix_card_body(inc, doc, conn, cfg), status="blocked", now=now)
    conn.execute("UPDATE tasks SET block_kind='needs_input' WHERE id=?", (fix,))
    iid = inc["id"]
    need = ("unblock it to dispatch" if branch else
            f"no epic: `python3 -m capsule incidents fixcard {iid} --epic <branch>`, then unblock")
    facts = re.findall(r"^[ \t]*(?:- )?(Needs fact: .+?)[ \t]*$", doc["fix_card"]["body"], re.M)
    reason = f"Korin fix card for incident {iid}: {need}" + "".join(f"\n{f}" for f in facts)
    event(conn, fix, "blocked", {"kind": "needs_input", "incident": iid, "reason": reason}, now=now)
    return fix


def route_fix_card(conn, cfg, incident_id, *, epic, assignee=None, now=None):
    """`capsule incidents fixcard <id> --epic <branch>`: put the fix card on a configured epic (and
    an implementer assignee). Refuses an epic or assignee the config does not know."""
    inc = get(conn, incident_id)
    if inc is None or not inc.get("fix_card"):
        raise LookupError(f"incident {incident_id} has no fix card yet")
    if ":" in inc["fix_card"]:
        raise ValueError(f"fix card {inc['fix_card']} is elsewhere: re-route it on its own board")
    if epic not in ((cfg or {}).get("epics") or []):
        raise ValueError(f"{epic!r} is not one of the config's epics")
    implementers = [a for a, role in ((cfg or {}).get("assignees") or {}).items() if role == "implementer"]
    assignee = assignee or fix_route(conn, cfg, inc)[1]
    if assignee not in implementers:
        raise ValueError(f"assignee {assignee!r} does not route to the implementer")
    with txn(conn):
        conn.execute("UPDATE tasks SET branch_name=?, assignee=? WHERE id=?", (epic, assignee, inc["fix_card"]))
        event(conn, inc["fix_card"], "fix_card_routed", {"incident": incident_id, "epic": epic, "assignee": assignee},
              now=now)
    return inc["fix_card"]


def apply_postmortem(conn, incident_id, doc, *, fix_card, note_path, now=None):
    now = int(now if now is not None else time.time())
    with txn(conn):
        cur = conn.execute(
            "UPDATE incidents SET root_cause=?, lesson=?, mitigation=?, enforcer=?,"
            " recurrence_test=?, decision_row_text=?, confidence=?,"
            " fix_card=COALESCE(NULLIF(fix_card, ''), ?), note_path=?, closure=?,"
            " status='fix_pending'"
            " WHERE id=? AND status IN ('open','postmortem')",
            (doc["root_cause"], doc["lesson"], doc["mitigation"], doc["enforcer"], doc["recurrence_test"],
             doc["decision_row_text"], doc["confidence"], fix_card, note_path,
             doc.get("closure") or "fixed", incident_id))
        if cur.rowcount != 1:
            raise LookupError(f"incident {incident_id} is not open for a post-mortem")


# -- the closure gate -------------------------------------------------------------------------

FIX_CARD_RE = re.compile(r"^(?:([A-Za-z0-9][A-Za-z0-9_.-]*):)?(t_[0-9a-f]{8})$")


def split_fix_card(value):
    """`t_xxxxxxxx` or `<board>:t_xxxxxxxx` -> (board or None, task id); ValueError otherwise."""
    m = FIX_CARD_RE.match(value or "")
    if not m:
        raise ValueError(f"fix card {value!r} is not t_xxxxxxxx or <board>:t_xxxxxxxx")
    return m.group(1), m.group(2)


def _card_rows(c, task):
    row = c.execute("SELECT status FROM tasks WHERE id=?", (task,)).fetchone()
    gone = blocks.not_closed_after("t.id", "e.id")      # a person's close voids an old completion
    done = c.execute("SELECT 1 FROM tasks t JOIN task_events e ON e.task_id=t.id AND "
                     f"e.kind='completed' WHERE t.id=? AND {gone} LIMIT 1", (task,)).fetchone()
    return (row["status"] if row else None), done is not None


def fix_card_status(conn, home, fix_card):
    """(status or None, has a `completed` event). A bare id reads the incident's own connection; a
    `board:id` reads that board's db read-only (the tick never writes another board)."""
    try:
        board, task = split_fix_card(fix_card)
    except ValueError:
        return None, False
    if board is None:
        return _card_rows(conn, task)
    db_path = paths.board_db(board, home or paths.home())
    if not db_path.exists():
        return None, False
    other = connect_ro(db_path)
    try:
        return _card_rows(other, task)
    except sqlite3.Error:
        return None, False
    finally:
        other.close()


def recurrence_gaps(incident, checkout, run_test, timeout, check_new, blind_tests):
    """The close gate's recurrence-test proof (new, then green on main); `landed` runs it too."""
    blind = incident_blind_run.blind_kw(incident, blind_tests)
    test, out, proven = incident.get("recurrence_test"), [], False
    checkout, base = incident_repo.gate(incident, checkout or paths.REPO)
    if (test and check_new and incident.get("closure") != "watch"
            and (run_test is not None or not blind)):    # the tick pre-check runs no blind test
        ok, detail = check_new(test, checkout, base, **blind,
                               **({"timeout": timeout} if blind else {}))
        out += [] if ok else [detail]
        proven = bool(blind)    # a blind id: red then green is the green run too
    if run_test is not None and not proven:
        ok, detail = (run_test(test, checkout, timeout=timeout, **blind) if test
                      else (False, "no recurrence test named"))
        out += [] if ok else [detail]
    return out


def missing(conn, incident, *, vault, decisions_note=DEFAULT_DECISIONS_NOTE, checkout=None, run_test=None,
            timeout=TEST_TIMEOUT, check_new=recurrence_test_is_new, watch_days=WATCH_DAYS,
            now=None, home=None, blind_tests=None):
    """Every closure check that fails, as one line each; [] means the incident may close.
    `run_test=None` skips the recurrence test (the tick's cheap pre-check). A `closure = watch`
    incident (an unproven transient) swaps the "new test" check for a quiet window: `watch_days`
    since it opened and no later incident with its fingerprint; its test may pre-date it."""
    out = incident_watch.gaps(conn, incident, watch_days, now)
    if incident["status"] in ("closed", incident_watch.SUPERSEDED):
        return [f"already {incident['status']}"]
    if incident["status"] != "fix_pending":
        out.append(f"status is {incident['status']}, not fix_pending (Korin has not filled the record)")
    empty = [f for f in RECORD_FIELDS if not (incident.get(f) or "").strip()]
    if empty:
        out.append("record fields empty: " + ", ".join(empty))
    card = incident.get("fix_card")
    if not card:
        out.append("no fix card")
    else:
        status, completed = fix_card_status(conn, home, card)
        if status != "done" or not completed:
            out.append(f"fix card {card} is not done (status {status or 'missing'},"
                       f" completed event {'yes' if completed else 'no'})")
    vault = Path(vault or "/nonexistent-vault").expanduser()   # no vault: the note checks refuse
    if not incident.get("decision_row"):
        out.append("decision_row not set (python3 -m capsule incidents row <id> <n>)")
    else:
        note = vault / decisions_note       # both proofs or none: a missing note is a refusal
        if not note.is_file():
            out.append(f"decisions note not found: {decisions_note}")
        else:
            pat = re.compile(rf"^\|\s*{int(incident['decision_row'])}\s*\|.*{re.escape(incident['id'])}", re.M)
            if not pat.search(note.read_text(encoding="utf-8", errors="replace")):
                out.append(f"decision row {incident['decision_row']} naming {incident['id']} not found in {decisions_note}")
    rel = incident.get("note_path")
    path = vault / rel if rel else None
    if path is None or not path.is_file():
        out.append(f"post-mortem note missing ({rel or 'no note path'})")
    elif incident["id"] not in path.read_text(encoding="utf-8", errors="replace"):
        out.append(f"post-mortem note {rel} does not name {incident['id']}")
    return out + recurrence_gaps(incident, checkout, run_test, timeout, check_new, blind_tests)


def close(conn, incident_id, *, vault, decisions_note=DEFAULT_DECISIONS_NOTE, checkout=None,
          run_test=run_recurrence_test, check_new=recurrence_test_is_new, timeout=TEST_TIMEOUT,
          by="capsule", watch_days=WATCH_DAYS, now=None, home=None, reason=None, blind_tests=None,
          quiet=False):
    """Close, or raise CloseRefused listing each failed check; a `reason` or `quiet`: no notice."""
    now = int(now if now is not None else time.time())
    incident = get(conn, incident_id)
    if incident is None:
        raise CloseRefused(incident_id, ["no such incident"])
    gaps = [] if reason and routine_nudges.is_routine_nudge(conn, incident) else missing(
        conn, incident, vault=vault, decisions_note=decisions_note, checkout=checkout,
        run_test=run_test, timeout=timeout, check_new=check_new, watch_days=watch_days,
        now=now, home=home, blind_tests=blind_tests)
    if gaps:
        raise CloseRefused(incident_id, gaps)
    with txn(conn):
        incident_blind_run.record(conn, incident, blind_tests)
        if conn.execute("UPDATE incidents SET status='closed', closed_at=? WHERE id=?"
                        " AND status IN ('open','fix_pending') AND recurrence_test IS ?",
                        (now, incident_id, incident["recurrence_test"])).rowcount != 1:
            raise CloseRefused(incident_id, ["recurrence test changed during the close"])
        if incident.get("task_id") and conn.execute("SELECT 1 FROM tasks WHERE id=?", (incident["task_id"],)).fetchone():
            why = {"reason": reason} if reason else {}
            event(conn, incident["task_id"], "incident_closed",
                  {"incident": incident_id, "by": by, **why}, now=now)
        if reason:
            return get(conn, incident_id)
        watch = incident.get("closure") == "watch"
        if watch:
            how = f"watch, no recurrence in {watch_days} days"
            body = (f"capsule: incident {incident_id} closed ({how}): evidence card"
                    f" {incident['fix_card']} done, detector {incident['recurrence_test']}"
                    f" passes on main, row {incident['decision_row']}")
        else:
            how = incident["kind"]
            body = (f"capsule: incident {incident_id} closed: fix {incident['fix_card']} done, "
                    f"{incident['recurrence_test']} passes on main, row {incident['decision_row']}")
        quiet or outbox.notify(conn, "done", f"incident {incident_id} closed ({how})",
                      fields=[("row", str(incident["decision_row"])), ("test", incident["recurrence_test"])],
                      thread_key=incident.get("task_id"), file_body=body, now=now)
        others = conn.execute("SELECT 1 FROM incidents WHERE fingerprint=? AND status NOT IN"
                              " ('closed','superseded')", (incident["fingerprint"],)).fetchone()
        if others is None:     # edit only when the last incident of this problem closes
            outbox.finish(conn, slack_msgs.broken_key(incident["fingerprint"]),
                          f"{slack_msgs.HANDLED} {'Watched, no recurrence' if watch else 'Fixed'}:"
                          f" {incident['kind'].replace('_', ' ')}.", now)
    return get(conn, incident_id)


def gate_args(ks):
    """The `missing`/`close` keyword arguments the tick and the CLI both take from `settings`."""
    return {"vault": ks["vault"], "decisions_note": ks["decisions_note"],
            "checkout": ks["main_checkout"], "timeout": ks["test_timeout"],
            "watch_days": ks["watch_days"], "blind_tests": ks["blind_tests"]}


def settings(cfg):
    """The `korin` block with defaults (vault: the block, else host.json or env, else None)."""
    block = dict((cfg or {}).get("korin") or {})
    vault = Path(block["vault"]).expanduser() if block.get("vault") else host.vault_path()
    return {"enabled": bool(block.get("enabled", False)), "vault": vault,
            "main_checkout": Path(block.get("main_checkout") or paths.REPO).expanduser(),
            "decisions_note": block.get("decisions_note") or host.decision_note(vault),
            "test_timeout": int(block.get("test_timeout") or TEST_TIMEOUT),
            "max_running": int(block.get("max_running") or 1),
            "fix_epic": block.get("fix_epic"), "fix_assignee": block.get("fix_assignee"),
            "vault_push": bool(block.get("vault_push", False)),
            "watch_days": int(block.get("watch_days") or WATCH_DAYS),
            "since": incident_inputs.since_ts(block.get("since")),
            "blind_tests": Path(block["blind_tests"]).expanduser()
            if block.get("blind_tests") else None}
