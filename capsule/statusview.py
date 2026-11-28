"""The read-only data layer behind `capsule status` (visibility spec 2026-10-21, section
3)."""
import json
import os
import re
import shutil
import time
from pathlib import Path

from capsule import (blocks, db, dispatch, docker_down, epic_clean, human, idle_lane, lane_behind,
                     paths, preview, stall, tick_guard, unblocked)

STALL_S = 3 * 3600           # an unmoving ready/review card with no owner (spec section 3)
QUIET_S = 20 * 60            # a running task silent this long is "(quiet 20m)"
LOOP_STALL_S = 5 * 60        # newest tick_health older than this while the lock is held: stalled
DISK_MIN_BYTES = 25 * 2**30   # the floor: image builds refuse under it (R11)
FEED_STALE_S = 15 * 60       # last good ship older than this, with bytes waiting: feed is blind
EVENTS_TAIL_BYTES = 512 * 1024
PLAIN_LIMIT = 140
NAME_WORDS = 6

ROLES = {"implementer": ("🔨", "builder"), "reviewer": ("🔍", "reviewer"),
         "validator": ("✅", "validator"), "guide": ("🧭", "guide"),
         "keeper": ("🗝️", "keeper")}
PREVIEW_ASK = "A preview is ready for you to look at; accept it or say what is wrong."
HIS_KINDS = blocks.NEEDS_INPUT_CLASS_KINDS + ("contradiction_loop", "guide_ruling")
ASKS = {
    "needs_input": "A question is waiting for your answer.",
    "review_infra": "The reviewers gave no usable verdict twice; it needs your call.",
    "spec_stale": "The spec it cites has changed; it needs your call.",
    "invalid_document": "The builder's notes came back malformed twice; it needs your call.",
    "epic_sync_conflict": "Its branch conflicts with main; it needs your hands.",
    "epic_sync_error": "Syncing its branch with main failed; it needs your hands.",
    "contradiction_loop": "Two reviewers keep contradicting each other; it needs your ruling.",
    "guide_ruling": "The guide gave up on a ruling; it needs your call.",
}
REASONS = {"dependency": "waiting on another task", "parked": "parked on purpose",
           "claims": "the guide is checking the files its text names",
           "transient": "hit a temporary snag", "capability": "missing a capability it needs",
           "owner_look": "waits for its slice; your preview look comes after"}
FALLBACK_REASON = "stuck, reason unclear"
DONE_STATES = ("done", "archived")

_ID_RE = re.compile(r"\b(?:(?:t|inc)_[0-9a-f]{6,}|[0-9a-f]{16,})\b")
_PREFIX_RE = re.compile(r"^[\w/.-]{1,16}:\s+")
_MODEL_RE = re.compile(r"^claude-([a-z]+)-(\d+(?:-\d{1,2})?)(?:-\d{8})?$")


def plain(text, limit=PLAIN_LIMIT):
    """`text` with ids, paths and command prefixes removed, whitespace collapsed, cut to `limit`."""
    text = _ID_RE.sub("that item", str(text or ""))
    text = re.sub(r"\S*/\S*", "a file", text).replace("python3 -m", "")
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _trimmed_title(title):
    """The spec's no-short-name fallback: drop a leading "Report:"-style prefix, keep 6 words."""
    words = _PREFIX_RE.sub("", str(title or "").strip(), count=1).split()
    return plain(" ".join(words[:NAME_WORDS])) or "untitled task"


def _model_label(model):
    """claude-sonnet-5-5 -> "Sonnet 5.5"; anything unrecognised is shown as given."""
    m = _MODEL_RE.match(model or "")
    return f"{m.group(1).capitalize()} {m.group(2).replace('-', '.')}" if m else (model or "")


def _name(row):
    keys = row.keys()
    short = row["short_name"] if "short_name" in keys else None
    return plain(short) if short else _trimmed_title(row["title"])


def _number(row):
    return row["number"] if "number" in row.keys() else None


def _item(board, row, **extra):
    return {"board": board, "number": _number(row), "name": _name(row), **extra,
            "ref": {"task": row["id"]}}


# --------------------------------------------------------------------------- outside the boards

def _events_path():
    spool = os.environ.get("EVENTS_SPOOL")
    if spool:
        return Path(spool)
    default = Path.home() / ".local" / "state" / "capsule-corp" / "maint"
    root = os.environ.get("AGENT_OPS_STATE_DIR") or default
    return Path(root) / "events.jsonl"


def _last_tick_health(board, events_path=None):
    """Epoch seconds of the newest `tick_health` event for `board` in the spool's tail, or None."""
    try:
        with open(events_path or _events_path(), "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - EVENTS_TAIL_BYTES))
            tail = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    for line in reversed(tail):
        if '"tick_health"' not in line:
            continue
        try:
            doc = json.loads(line)
        except ValueError:
            continue
        if doc.get("kind") == "tick_health" and doc.get("board") == board:
            return doc.get("ts")
    return None


def seats(home, now):
    """Fresh owner seats from every `queue/<repo>/owners.json`: [{key, owner, age_s}] (spec 3)."""
    out = []
    for f in sorted((Path(home) / "queue").glob("*/owners.json")):
        for key, entry in unblocked._owners_json(f.parent.name, home).items():
            beat = entry.get("heartbeat") if isinstance(entry, dict) else None
            age = now - beat if isinstance(beat, (int, float)) else None
            owner = str(entry.get("owner", "")) if isinstance(entry, dict) else ""
            if age is not None and age < unblocked.OWNER_HEARTBEAT_MAX_S and owner != "landed":
                out.append({"key": key, "owner": owner, "age_s": max(0, int(age))})
    return out


def _seat_kind(owner):
    low = owner.lower()
    return "keeper" if ("cos" in low or "keeper" in low) else (
        "builder" if low in ("implementer", "builder") else (owner or "agent"))


def _outside_factory(seat_list):
    merged = {}
    for s in seat_list:
        kind = _seat_kind(s["owner"])
        merged[kind] = merged.get(kind, 0) + 1
    summary = ", ".join(f"{n} {'CoS keeper' if k == 'keeper' else k}{'s' if n > 1 else ''}"
                        for k, n in merged.items())
    return {"seats": len(seat_list), "summary": summary,
            "ages_min": [s["age_s"] // 60 for s in seat_list]}


def _lander_on(home, board):
    try:
        cfg = json.loads(paths.resolve_config(home, board).read_text())
        return (cfg.get("lander") or {}).get("mode") not in (None, "off", "dry-run")
    except (OSError, ValueError, AttributeError):
        return False


def _loop_state(home, board, now, events_path=None):
    """"alive" | "stalled" | "down" for one board's tick (lock probe plus newest heartbeat age)."""
    if not tick_guard.held(home, board):
        return "down"
    beat = _last_tick_health(board, events_path)
    return "stalled" if beat is not None and now - beat > LOOP_STALL_S else "alive"


def _feed_problem(home, now):
    """The Grafana feed is blind when bytes wait unshipped and the last good ship is over
    FEED_STALE_S old (or never). Plain file reads only: no lock is ever taken (spec section 3)."""
    lookout = Path(home) / "lookout"
    try:
        size = (lookout / "spool.jsonl").stat().st_size
    except OSError:
        return None
    try:
        offset = int((lookout / "spool.offset").read_text())
    except (OSError, ValueError):
        offset = 0
    if size - offset <= 0:
        return None
    try:
        last = json.loads((lookout / "ship.last.json").read_text())
        ts = last["ts"] if last.get("rc") == 0 else None
    except (OSError, ValueError, KeyError, AttributeError):
        ts = None
    if isinstance(ts, (int, float)) and now - ts <= FEED_STALE_S:
        return None
    return {"what": "The Grafana feed is not shipping, so alerts are blind."}


def _world_problems(home, now):
    out = []
    probe = Path(home) if Path(home).exists() else Path.home()
    try:
        free = shutil.disk_usage(probe).free
    except OSError:
        free = DISK_MIN_BYTES       # cannot tell: say nothing rather than kill --watch
    if free < DISK_MIN_BYTES:
        out.append({"what": f"The disk is nearly full (under {DISK_MIN_BYTES // 2**30} GB free)."})
    feed = _feed_problem(home, now)
    return out + ([feed] if feed else [])


# --------------------------------------------------------------------------- one board

def _decisions(conn):
    """{task_id: question} for decision.v1 requests (rulings asked of the owner)."""
    out = {}
    for r in conn.execute("SELECT body FROM requests"
                          " WHERE schema='decision.v1' AND status='applied' ORDER BY id"):
        try:
            doc = json.loads(r["body"])
        except ValueError:
            continue
        if isinstance(doc, dict) and doc.get("task_id"):
            out[doc["task_id"]] = plain(doc.get("question") or "A ruling is waiting for you.")
    return out


def _human_waits(home, board):
    """Pending human-verb files that wait on the owner (rule-file sign-offs, settings commands)."""
    out = []
    for p in sorted(Path(home, "boards", board, "human").glob("*.json")):
        try:
            doc = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(doc, dict) and human.canonical_author(doc.get("waits")) == "owner":
            out.append({"board": board, "number": None,
                        "name": plain(doc.get("kind") or "sign-off"),
                        "ask": "A change is waiting for your sign-off.", "since": doc.get("at"),
                        "ref": {"file": p.name}})
    return out


def _cap_ask(conn, row):
    """The ask of a `spec_stale` hold that is really the verifier cap, else None (M110)."""
    if row["block_kind"] == "owner_look":      # F1: only once the owner was shown the preview
        return PREVIEW_ASK if preview.asked(conn, row["id"]) else None
    if row["block_kind"] != "spec_stale":
        return None
    label = blocks.cap_hold_label(conn, row["id"])
    return f"Held by the {label}; it needs your call." if label else None


def _needs_ask(row, decisions, cap_ask=None):
    """What the owner must do here, or None (spec section 3, NEEDS YOU rules 1, 2 and 4)."""
    status, kind = row["status"], row["block_kind"]
    if status in ("blocked", "triage") and (kind in HIS_KINDS or row["id"] in decisions
                                           or (kind == "owner_look" and cap_ask)):
        return decisions.get(row["id"]) or cap_ask or ASKS[kind]
    if human.canonical_author(row["assignee"]) == "owner":
        return "This one is yours to do."
    return None


def _incident_problems(conn, board, owned, fix_status):
    """(open problems, handled count) from open incidents: handled = a live fix card or a seat."""
    try:
        rows = conn.execute(
            "SELECT kind, task_id, fix_card FROM incidents"
            " WHERE status NOT IN ('closed','superseded')").fetchall()
    except Exception:
        return [], 0
    handled, open_by_kind, held_by_kind = 0, {}, {}
    for r in rows:
        fix = fix_status.get(r["fix_card"])
        live_fix = fix is not None and fix not in ("blocked", "triage")
        if live_fix or r["task_id"] in owned:
            handled += 1
            continue
        open_by_kind[r["kind"]] = open_by_kind.get(r["kind"], 0) + 1
        held = held_by_kind.setdefault(r["kind"], [])
        if fix is not None and r["fix_card"] not in held:
            held.append(r["fix_card"])
    problems = []
    for kind, n in sorted(open_by_kind.items()):
        many = n > 1
        what = (f"{board}: {n} {kind.replace('_', ' ')} incident{'s' * many} "
                f"with nobody on {'them' if many else 'it'}")
        held = held_by_kind[kind]
        problem = {"what": what}
        if held:
            shown = [conn.execute("SELECT * FROM tasks WHERE id=?", (t,)).fetchone()
                     for t in held[:5]]
            names = [_name(x) + (f" #{_number(x)}" if _number(x) else "") for x in shown]
            problem["what"] += "; held fix cards: " + ", ".join(names)
            if len(held) > 5:
                problem["what"] += f" and {len(held) - 5} more"
            problem["ref"] = {"tasks": held}
        problems.append(problem)
    return problems, handled


def _running(conn):
    return conn.execute(
        "SELECT t.*, r.profile AS run_profile, r.model AS run_model, r.started_at AS run_started,"
        " COALESCE(r.last_heartbeat_at, t.last_heartbeat_at, r.started_at) AS run_beat"
        " FROM tasks t JOIN task_runs r ON r.id=t.current_run_id"
        " WHERE t.status='running' AND t.current_run_id IS NOT NULL").fetchall()


def _running_entry(board, row, now):
    emoji, label = ROLES.get(row["run_profile"], ("🤖", row["run_profile"] or "agent"))
    started = row["run_started"] or now
    quiet = now - (row["run_beat"] or started)
    return _item(board, row, role=row["run_profile"], role_label=label, role_emoji=emoji,
                 model=_model_label(row["run_model"]), minutes=max(0, (now - started) // 60),
                 quiet_minutes=quiet // 60 if quiet >= QUIET_S else None)


def _classify_board(conn, home, board, now, owned):
    """Every open task of one board into the sections; returns the board's contribution."""
    decisions = _decisions(conn)
    changed = dict(conn.execute(
        "SELECT task_id, MAX(created_at) FROM task_events GROUP BY task_id").fetchall())
    run_ids = {r["id"] for r in _running(conn)}
    asked = set()
    out = {"running": [], "needs_you": _human_waits(home, board), "blocked": [], "problems": [],
           "handled": 0}
    fix_status = dict(conn.execute("SELECT id, status FROM tasks").fetchall())
    for row in conn.execute("SELECT * FROM tasks WHERE status NOT IN ('done','archived')"):
        ask = _needs_ask(row, decisions, _cap_ask(conn, row))
        if ask:
            since = changed.get(row["id"]) or row["created_at"]
            out["needs_you"].append(_item(board, row, ask=ask, since=since))
            asked.add(row["id"])
        elif row["status"] in ("ready", "review") and row["id"] not in run_ids:
            last = max(changed.get(row["id"]) or 0, row["created_at"] or 0,
                       row["last_heartbeat_at"] or 0)
            if now - last > STALL_S:
                _stalled(out, board, row, owned, now - last)
        elif row["status"] in ("blocked", "triage", "parked") or row["block_kind"] == "dependency":
            reason = REASONS.get(row["block_kind"] or row["status"], FALLBACK_REASON)
            out["blocked"].append(_item(board, row, reason=reason))
    for row in (r for r in _running(conn) if r["id"] not in asked):
        entry = _running_entry(board, row, now)
        out["running"].append(entry)
        if entry["quiet_minutes"] is not None:
            _stalled(out, board, row, owned, entry["quiet_minutes"] * 60, quiet=True)
    probs, handled = _incident_problems(conn, board, owned, fix_status)
    out["problems"] += probs + _idle_lane_problems(conn, board, now, home)
    out["problems"] += _docker_down_problems(conn, board, now) + _stall_problems(conn, board, now)
    out["problems"] += _lane_behind_problems(conn, board, now)
    out["handled"] += handled
    out["weekly_pause"] = _weekly_pause(conn, now)
    out["lanes"] = _lanes(conn, board, now)
    out["problems"] += [{"what": f"{board}: lane {x['branch']} could not be read ({x['error']})."}
                        for x in out["lanes"] if x.get("unreadable")]
    return out


def _lanes(conn, board, now):
    """Each held lane with ready cards behind its holder, or held by a card nobody is running:
    the holder's name and how many ready cards wait (`epic_clean.lane_holds`, read-only)."""
    return [_lane_item(board, lane) for lane in epic_clean.lane_holds(conn, now)
            if lane.get("unreadable") or lane["ready"] or not lane["live"]]


def _lane_item(board, lane):
    if lane.get("unreadable"):      # that lane only: the rest of the board still shows
        return {"board": board, "branch": lane["branch"], "unreadable": True,
                "error": lane["error"]}
    return _item(board, lane["holder"], branch=lane["branch"], waiting=lane["ready"],
                 live=lane["live"], held_minutes=lane["held_s"] // 60,
                 holders=[{"name": _name(r), "number": _number(r), "ready": r["status"] == "ready"}
                          for r in lane["holders"]])


def _idle_lane_problems(conn, board, now, home=None):
    """The Z5 idle-lane detector as one ATTENTION line: who holds the workspace, and what waits."""
    try:
        got = idle_lane.find(conn, now, dispatch.root(board, home))
    except epic_clean.LANE_ERRORS as exc:   # bad data in a run row: say so, keep the board open
        what = f"{board}: the idle-lane check could not read the runs ({type(exc).__name__})."
        return [{"what": what}]
    if got is None:
        return []
    holder = got["holder"]
    verb = ("is in review on the same branch" if got["hold"] == "branch-busy"
            else "holds the shared workspace")
    why = plain((holder["block_kind"] or holder["status"]).replace("_", " "))
    what = (f"{board}: nothing is running for {got['idle_s'] // 60}m while"
            f" {_name(got['ready'])} waits; {_name(holder)} ({why}) {verb}")
    return [{"what": what, "ref": {"task": holder["id"]}}]


def _lane_behind_problems(conn, board, now):
    """The Z5 lane-behind detector's records: one ATTENTION line per lane whose base lags main."""
    return [{"what": f"{board}: lane {x['branch']} is {x['behind']} commits behind main"
                     f" (oldest missing change {x['hours']}h old);"
                     " its card is checked against old code"}
            for x in lane_behind.find(conn, now)]


def _docker_down_problems(conn, board, now):
    """The Z5 docker-down detector's record as one ATTENTION line, from its first sighting."""
    rec = docker_down.state(conn, now)
    if not rec:
        return []
    mins = max(0, now - rec["since"]) // 60
    return [{"what": f"{board}: Docker down for {mins} min, {rec['waiting']} cards waiting"}]


def _stall_word(rec):
    return rec["level"] if rec.get("level") in ("aside", "decided") else "stalled"


def _stall_problems(conn, board, now):
    """The stall supervisor's records as ATTENTION lines: class, subject and the stall clock."""
    return [{"what": f"{board}: {_stall_word(rec)}"
                     f" {stall.clock(now - rec.get('since', now))}:"
                     f" {key[len(stall.PREFIX):].replace(':', ' ')}"}
            for key, rec in stall.records(conn).items()]


def _weekly_pause(conn, now):
    """The one line while the tick's weekly-usage hold is on (guard `weekly_pause`; pct and reset
    time from its last `weekly_pause` event), else None. Reads what the tick wrote, no meter.
    Never raises: a bad row degrades the line to `weekly usage ?%`."""
    try:
        if not (db.guard_get(conn, "weekly_pause")[0] or {}).get("held"):
            return None
    except (ValueError, TypeError, AttributeError):
        return None
    row = conn.execute("SELECT payload FROM task_events WHERE task_id='weekly'"
                       " AND kind='weekly_pause' ORDER BY id DESC LIMIT 1").fetchone()
    try:
        doc = json.loads(row[0]) if row and row[0] else {}
        return f"paused: weekly usage {doc.get('pct', '?')}%{_resets(doc.get('reset_at'), now)}"
    except (ValueError, TypeError, AttributeError, OverflowError, OSError):
        return "paused: weekly usage ?%"


def _resets(at, now):
    """` (resets Mon 04:00)` for a future reset time, else nothing."""
    return f" (resets {time.strftime('%a %H:%M', time.localtime(at))})" if at and at > now else ""


def _stalled(out, board, row, owned, age_s, quiet=False):
    if row["id"] in owned or row["branch_name"] in owned:
        out["handled"] += 1
        return
    what = f"{board}: {_name(row)} has gone quiet ({age_s // 60}m, no heartbeat)" if quiet else (
        f"{board}: {_name(row)} has not moved in {age_s // 3600}h and nobody is on it")
    out["problems"].append({"what": what, "ref": {"task": row["id"]}})


def board_view(home, board, now, owned, events_path=None):
    """(board entry, contribution) for one board; an unreadable board is down plus a problem."""
    entry = {"board": board, "loop": _loop_state(home, board, now, events_path),
             "auto_landing": _lander_on(home, board)}
    try:
        conn = db.connect_ro_tolerant(paths.board_db(board, home))
        try:
            got = _classify_board(conn, home, board, now, owned)
        finally:
            conn.close()
    except Exception:       # unopenable, corrupt or unmigrated: show it red, never crash the view
        entry["loop"] = "down"
        return entry, {"running": [], "needs_you": [], "blocked": [], "handled": 0, "lanes": [],
                       "problems": [{"what": f"{board}: the board cannot be opened."}]}
    if entry["loop"] != "alive":
        word = "is not running" if entry["loop"] == "down" else "looks stalled"
        got["problems"].insert(0, {"what": f"{board}: the loop {word}."})
    return entry, got


def snapshot(home=None, *, now=None, boards=None, events_path=None):
    """The whole status as one dict (spec section 4). Never raises on a bad board or source."""
    home, now = paths.home(home), int(now or time.time())
    seat_list = seats(home, now)
    owned = {s["key"] for s in seat_list}
    snap = {"generated_at": now, "boards": [], "running": [], "needs_you": [], "blocked": [],
            "attention": {"open": [], "handled_count": 0}, "lanes": [],
            "outside_factory": _outside_factory(seat_list)}
    for board in boards or unblocked.configured_boards(home):
        entry, got = board_view(home, board, now, owned, events_path)
        snap["boards"].append(entry)
        for section in ("running", "needs_you", "blocked", "lanes"):
            snap[section] += got[section]
        snap["attention"]["open"] += got["problems"]
        snap["attention"]["handled_count"] += got["handled"]
        if got.get("weekly_pause"):
            snap.setdefault("weekly_pause", got["weekly_pause"])    # the first held board wins
    snap["attention"]["open"] += _world_problems(home, now)
    return snap
