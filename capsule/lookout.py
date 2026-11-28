"""The Lookout producer (spec Specs/capsule-corp-lookout-design.md in the notes vault
5.1-5.10, rows 476/478/480): tick step (8a) reads the board read-only and appends
JSON lines to $CAPSULE_HOME/lookout/spool.jsonl; (8b)/(8c) launch the
shipper/publisher and never wait (G3)."""
import datetime
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from capsule import blocks, config, db, legacy_names, paths, review_scope
from capsule.answer_base import ACCEPT_CLOSE_KIND, BLOCK_CLOSE, BLOCK_OPEN  # noqa: F401

SPOOL_V = 1
EVENTS_WATERMARK = "lookout:events"
SPOOL_MAX_BYTES = 64 * 1024 * 1024


def ship_on(cfg):
    """Shipping is opt-out. An absent `lookout.ship` means on; only an explicit non-True
    value (strict, so a truthy "yes" is not on) turns the shipper and its heartbeat hand-off off."""
    return (cfg.get("lookout") or {}).get("ship", True) is True
ROTATE_BYTES = 8 * 1024 * 1024
# spool.rejected cap: past it the file rotates to spool.rejected.1
REJECTED_MAX_BYTES = 1024 * 1024
HEALTH_EVERY = 10
RESYNC_EVERY = 120
RUN_OVERLAP_S = 120
TZ = datetime.timezone(datetime.timedelta(hours=7))   # Asia/Bangkok, no DST (A11)
# blocks.block puts the block kind there
KIND_IN_PAYLOAD = ("blocked", "block_loop_detected", "dependency_wait")
STATE = {"completed": "closed", "unblocked": "closed", "failure_reset": "closed", "first_screen_ok": "closed",
         "first_screen_fix": "closed", "blocked": "open", "decision_requested": "open",
         "first_screen_hold": "open", "parked_by_human": "open", "parked": "open",
         "failure_limit": "open"}
OWNER_WAIT = "owner"     # the stored waiting_on value
OWNER_WORDS = {"needs_input": "needs your answer", "decision_requested": "needs your answer",
               "first_screen_hold": "screen to approve", "contradiction_loop": "rules disagree",
               "capability": "missing tool or access", "review_infra": "review could not run twice",
               "spec_stale": "spec citation is stale", "parked": "failed too often, reset it",
               "failure_limit": "failed too often, reset it"}
PHASE_BY_ROLE = {"implementer": "implementing", "validator": "validating", "reviewer": "reviewing",
                 "guide": "guide_ruling", "korin": "korin"}

sys.path.insert(0, str(paths.REPO / "maintenance"))
import scrub  # noqa: E402  (G7: free text leaving the board is scrubbed first)


def lookout_dir(home, board=None):
    d = Path(home) / "lookout"
    return d / board if board else d


def spool_path(home, board=None):
    return lookout_dir(home, board) / "spool.jsonl"


def offset_path(home, board=None):
    return lookout_dir(home, board) / "spool.offset"


def rejected_path(home, board=None):
    """A spool row the shipper parsed fine but Postgres rejected lands here, one JSON line per
    row, instead of blocking every later row behind it forever."""
    return lookout_dir(home, board) / "spool.rejected"


def rotate_rejected(home, incoming=0, board=None):
    """Mirror of maintain_spool for the quarantine sidecar: when appending `incoming`
    bytes would take spool.rejected past REJECTED_MAX_BYTES, the live file becomes
    spool.rejected.1 (replacing the older generation), so the pair never exceeds
    about twice the cap."""
    p = rejected_path(home, board)
    try:
        size = p.stat().st_size
    except OSError:
        return False
    if size == 0 or size + incoming <= REJECTED_MAX_BYTES:
        return False
    os.replace(p, p.with_name(p.name + ".1"))
    return True


def event_id(board, *parts):
    return hashlib.sha1("\x1f".join([board, *map(str, parts)]).encode()).hexdigest()


def repo_slug(repo_url):
    if not repo_url:
        return None
    m = re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$", repo_url)
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    parts = [p for p in re.sub(r"\.git/?$", "", repo_url).split("/") if p]
    return "/".join(parts[-2:]) if len(parts) >= 2 else None


def iso(epoch):
    return None if epoch is None else datetime.datetime.fromtimestamp(float(epoch), datetime.timezone.utc).isoformat()


def clip(text, n):
    return None if text is None else scrub.scrub(str(text))[:n]


def _payload(raw):
    """A `task_events.payload` that is not valid JSON, or that
    parses to something other than an object, becomes `({}, False)` rather than
    raising `json.JSONDecodeError` or `AttributeError` on the first `.get` - one
    poisoned row must never stop the whole Lookout pass."""
    try:
        p = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}, False
    return (p, True) if isinstance(p, dict) else ({}, False)


def _kind_fields(kind, p):
    """Extra shipped payload fields per event kind. denied_tools (tool names) and expires_at (an
    epoch) feed the weekly Security panels; neither is free text."""
    if kind == "completed":
        return {"commit": p.get("commit"), "gates": p.get("gates")}
    if kind == "crashed":
        return {"denied_tools": p.get("denied_tools")}
    if kind == "override":
        return {"expires_at": p.get("expires_at")}
    return {}


def effective_kind(block_kind, open_kind, status):
    """The open block from the event history is the authority (ruling 2). tasks.block_kind only
    speaks while the card is still blocked/triage (or a todo dependency wait): unblock never clears it."""
    if open_kind:
        return open_kind
    if status in ("blocked", "triage") or (block_kind == "dependency" and status == "todo"):
        return block_kind
    return None


def derive_waiting(block_kind, open_kind, status, has_live_run, parent_id, pause_until,
                   reason=None, human=False, cause=None):
    kind = effective_kind(block_kind, open_kind, status)
    cap = kind == "spec_stale" and blocks.blind_cap_label(cause)
    if cap:
        return OWNER_WAIT, cap
    if kind == "guide_ruling":
        # tick.py parks the card in triage while the guide rules
        return "tick", "guide is ruling"
    if kind in ("parked", "parked_by_human") and human:
        # row 506: a human park is the owner's own act: his trigger, not the failure wording
        return OWNER_WAIT, f"you parked it: {reason}" if reason else "you parked it"
    if kind in OWNER_WORDS:
        return OWNER_WAIT, OWNER_WORDS[kind]
    if kind == "dependency":
        return (parent_id, f"waiting for card {parent_id}") if parent_id else ("tick", "queued")
    if kind == "transient":
        # rq_07ad0af5: pause_until is the board-wide pause, not this card's retry; nothing unblocks
        # a transient block when it lapses
        return OWNER_WAIT, "blocked, no retry scheduled: unblock|reset"
    if pause_until and not has_live_run and status in ("ready", "todo"):
        at = datetime.datetime.fromtimestamp(pause_until, TZ).strftime("%H:%M")
        return "tick", f"retrying automatically at {at}"
    if kind:
        return OWNER_WAIT, f"blocked: {kind}"
    if has_live_run:
        return None, None
    if status == "review":
        return "tick", "review queued"
    if status in ("ready", "todo", "triage"):
        return "tick", "queued"
    return None, None


def settle_done(t, ob, waiting_on, words):
    """(eff_kind, open block, waiting_on, words) for a card row. W361: a done card is nobody's to
    answer and its last block event may never have been closed, so it keeps no waiting_on; only
    a first-screen hold really sits on a done card."""
    kind = effective_kind(t["block_kind"], ob.get("kind"), t["status"])
    if t["status"] == "done" and kind != "first_screen_hold":
        return None, {}, None, None
    return kind, ob, waiting_on, words


def phase_of(status, live_role, waiting_on, kind=None):
    if kind == "guide_ruling" and live_role is None and status == "triage":
        return "guide_ruling"                # waiting for the guide's automatic ruling
    if waiting_on == OWNER_WAIT and status != "archived":
        return "waiting"                     # a first-screen hold sits on a done card (ruling 3)
    if status in ("done", "archived"):
        return status
    if live_role in PHASE_BY_ROLE:
        return PHASE_BY_ROLE[live_role]
    if waiting_on not in (None, "tick"):
        return "waiting"
    return "queued"


def era_of(runner):
    return "capsule" if runner else legacy_names.ERA_VALUE


def _live(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (ValueError, OSError):
        return False


def kick(home, verb, pidfile, every=0):
    """Launch `python3 -m capsule <verb...>` detached; never wait (G3). Single-flight on `pidfile`."""
    d = lookout_dir(home)
    d.mkdir(parents=True, exist_ok=True)
    pf = d / pidfile
    try:
        recent = pf.exists() and time.time() - pf.stat().st_mtime < every
        if pf.exists() and (_live(pf.read_text().strip()) or recent):
            return False        # single-flight, and `every` s between launches across boards
    except OSError:
        pass
    log = open(d / f"{verb[-1]}.log", "ab")
    try:
        argv = [sys.executable, "-m", "capsule", *verb, "--capsule-home", str(home)]
        proc = subprocess.Popen(argv, cwd=str(paths.REPO), stdin=subprocess.DEVNULL,
                                stdout=log, stderr=log, start_new_session=True)
        pf.write_text(str(proc.pid))
    finally:
        log.close()
    return True


def _key(line):
    row = line.get("row") or {}
    if line.get("t") == "snap":
        keys = {"cap_cards": ("task_id",), "cap_runs": ("run_id",), "cap_blocks": ("task_id", "opened_at"),
                "cap_epics": ("epic",), "cap_prs": ("repo", "pr_number"), "cap_commits": ("repo", "sha"),
                "cap_incidents": ("id",)}.get(line.get("table"), ())
        return ("snap", line.get("table"), line.get("board"), *[row.get(k) for k in keys])
    if line.get("t") == "tickmark":
        return ("tickmark", line.get("board"))
    return None


def _compacted(tail):
    """The unshipped `tail` bytes with every superseded snap/tickmark line dropped."""
    lines = [json.loads(l) for l in tail.decode("utf-8", "replace").splitlines() if l.strip()]
    newest = {}
    for i, l in enumerate(lines):
        k = _key(l)
        if k is not None:
            newest[k] = i
    kept = [l for i, l in enumerate(lines) if _key(l) is None or newest[_key(l)] == i]
    return "".join(json.dumps(l, ensure_ascii=False) + "\n" for l in kept)


def maintain_spool(home, board=None):
    """Rotate a fully shipped spool; compact one above SPOOL_MAX_BYTES (spec 5.10). Only while
    holding ship.lock, so the shipper never reads a half-rewritten file."""
    sp, op = spool_path(home, board), offset_path(home, board)
    if not sp.exists():
        return "ok"
    size = sp.stat().st_size
    try:
        offset = int(op.read_text())
    except (OSError, ValueError):
        offset = 0
    if size <= SPOOL_MAX_BYTES and not (offset >= size and size > ROTATE_BYTES):
        return "ok"
    with open(lookout_dir(home, board) / "ship.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return "busy"
        data = sp.read_bytes()
        try:
            offset = int(op.read_text())
        except (OSError, ValueError):
            offset = 0
        tmp = sp.with_name(".spool.jsonl.tmp")
        if offset >= len(data):
            tmp.write_bytes(b"")
            what = "rotated"
        else:
            tmp.write_text(_compacted(data[offset:]), encoding="utf-8")
            what = "compacted"
        # Reset the offset BEFORE the rename: a crash right after `os.replace` could
        # otherwise leave the old (larger) offset pointing into the new, shorter file's
        # layout - starting the shipper mid-line or skipping bytes.
        op.write_text("0")
        os.replace(tmp, sp)
        return what


class Collector:
    def __init__(self, home, board, *, host, pid, per_board=False):
        self.home, self.board, self.host, self.pid = Path(home), board, host, pid
        self.spool_board = board if per_board else None   # a --board tick owns lookout/<board>/
        self.passes = 0
        self.sent = {}                 # (table, key) -> content hash; empty = full resync
        self.last_pass_ts = None
        self.harness_sha = None
        self.snaps_paused = False
        # event ids already counted as malformed, so a poisoned event is not recounted each pass
        self.bad_payload_ids = set()

    # -- helpers ------------------------------------------------------------------------
    def _sha(self):
        """Once per process: the harness checkout's HEAD (the trusted repo, never a worker clone).
        `git rev-parse` also works in a worktree, where `.git` is a file."""
        if self.harness_sha is None:
            try:
                r = subprocess.run(["git", "-C", str(paths.REPO), "rev-parse", "--short=12", "HEAD"],
                                   capture_output=True, text=True, timeout=5)
                self.harness_sha = r.stdout.strip() if r.returncode == 0 else "unknown"
            except (OSError, subprocess.TimeoutExpired):
                self.harness_sha = "unknown"
        return self.harness_sha

    def _snap(self, out, pending, table, key, row, envelope):
        """A row is marked sent in `pending`, never directly in
        `self.sent` - `collect` only merges `pending` into `self.sent` once `_append`
        has returned, so a failed spool write leaves every row of this pass unsent
        and the next pass resends it, instead of losing it silently until the next
        hourly resync."""
        h = hashlib.sha1(json.dumps(row, sort_keys=True, default=str).encode()).hexdigest()
        if self.sent.get((table, key)) == h:
            return
        pending[(table, key)] = h
        out.append(dict(envelope, t="snap", table=table, row=row))

    # -- the step ------------------------------------------------------------------------
    def _open_block(self, e, cur, board, tid, counts):
        """The BLOCK_OPEN step of `collect`. A `parked_by_human` event (row 506) closes any open
        hold first: the park replaces it, else the card keeps the old words."""
        p, ok = _payload(e["payload"])
        if not ok and e["id"] not in self.bad_payload_ids:
            counts["lookout_errors"] += 1
            self.bad_payload_ids.add(e["id"])
        human_park = e["kind"] == "parked_by_human" or (
            e["kind"] == "parked" and p.get("by") == "human")
        closed = None
        if cur is not None and human_park:
            cur.update(closed_at=iso(e["created_at"]), closed_by="reset")
            closed, cur = cur, None
        if cur is None:
            kind = (p.get("kind") if e["kind"] in KIND_IN_PAYLOAD else None) or e["kind"]
            reason = p.get("trigger") if human_park else p.get("reason")
            cur = {"task_id": tid, "opened_at": iso(e["created_at"]), "kind": kind,
                   "reason": clip(reason, 500), "opened_event_id": event_id(board, e["id"]),
                   "closed_at": None, "closed_by": None, "human": human_park,
                   "cause": p.get("cause")}
        return closed, cur

    def collect(self, conn, cfg, now, counts, pass_ms, lookout_ms=0, lookout_errors=0):
        self.passes += 1
        if self.passes % RESYNC_EVERY == 0:
            self.sent = {}
        full = not self.sent
        board, repo = self.board, repo_slug(cfg.get("repo_url"))
        env = {"v": SPOOL_V, "board": board, "snap_ts": iso(now), "pass_id": f"{self.host}:{self.pid}:{self.passes}"}
        out = []
        # (table, key) -> hash; merged into self.sent only after a successful write
        pending = {}
        mark, _ = db.guard_get(conn, EVENTS_WATERMARK)
        since = (mark or {}).get("event_id", 0)
        tasks = {r["id"]: r for r in conn.execute("SELECT * FROM tasks")}
        live = {r["task_id"]: r for r in conn.execute(
            "SELECT * FROM task_runs WHERE ended_at IS NULL AND status='running'")}
        parents = {r[0]: r[1] for r in conn.execute(
            "SELECT l.child_id, l.parent_id FROM task_links l JOIN tasks p ON p.id=l.parent_id"
            " WHERE p.status NOT IN ('done','archived')")}
        pauses = [json.loads(r[0]).get("until", 0) for r in conn.execute(
            "SELECT value FROM guard_state WHERE key LIKE 'pause:%'")]
        pause_until = max([u for u in pauses if u > now], default=None)
        paused_accounts = sorted(r[0][len("pause:account:"):] for r in conn.execute(
            "SELECT key, value FROM guard_state WHERE key LIKE 'pause:account:%'")
            if json.loads(r[1]).get("until", 0) > now)
        commits = {r[0]: r[1] for r in conn.execute(
            "SELECT task_id, COUNT(*) FROM task_events WHERE kind='completed'"
            " AND json_extract(payload, '$.commit') IS NOT NULL GROUP BY task_id")}
        new_events = conn.execute("SELECT * FROM task_events WHERE id > ? ORDER BY id", (since,)).fetchall()
        touched = {e["task_id"] for e in new_events}
        # blocks: walk the block events of every touched task (all tasks on a full sync)
        open_kind, block_rows = {}, []
        # Live cards too - a decision_requested applied from a live run's spool must keep its open
        # block on the passes after the event, while the run stays live and nothing else touches it.
        scope = tasks.keys() if full else (touched | set(live))
        for tid in scope:
            cur = None
            kinds = (*BLOCK_OPEN, *BLOCK_CLOSE, ACCEPT_CLOSE_KIND)
            for e in conn.execute("SELECT id, kind, payload, created_at FROM task_events WHERE task_id=? AND kind IN"
                                  f" ({','.join('?' * len(kinds))}) ORDER BY id",
                                  (tid, *kinds)):
                if e["kind"] in BLOCK_OPEN:
                    closed, cur = self._open_block(e, cur, board, tid, counts)
                    if closed is not None:
                        block_rows.append(closed)
                elif e["kind"] in BLOCK_CLOSE and cur is not None:
                    cur.update(closed_at=iso(e["created_at"]), closed_by=BLOCK_CLOSE[e["kind"]])
                    block_rows.append(cur)
                    cur = None
                elif e["kind"] == ACCEPT_CLOSE_KIND and cur is not None:
                    p, ok = _payload(e["payload"])
                    if not ok and e["id"] not in self.bad_payload_ids:
                        counts["lookout_errors"] += 1
                        self.bad_payload_ids.add(e["id"])
                    if p.get("by") == "accept":               # accept closes the open block too
                        cur.update(closed_at=iso(e["created_at"]), closed_by="unblocked")
                        block_rows.append(cur)
                        cur = None
            if cur is not None:
                block_rows.append(cur)
                open_kind[tid] = cur
        for b in block_rows:
            t = tasks.get(b["task_id"])
            b["waiting_on"] = derive_waiting(t["block_kind"] if t else None, b["kind"], t["status"] if t else "",
                                             False, parents.get(b["task_id"]), pause_until,
                                             b.get("reason"), b.get("human"),
                                             b.get("cause"))[0] or "tick"
            snap_row = dict(b)
            # `human`/`cause` only steer waiting_on/words; not cap_blocks columns
            snap_row.pop("human", None)
            snap_row.pop("cause", None)
            self._snap(out, pending, "cap_blocks", (b["task_id"], b["opened_at"]), snap_row, env)
        # cards
        for tid, t in tasks.items():
            if not full and tid not in touched and tid not in live and (("cap_cards", tid) in self.sent):
                continue
            run = live.get(tid)
            # the still-open block from the event history, if any
            ob = open_kind.get(tid) or {}
            waiting_on, words = derive_waiting(t["block_kind"], ob.get("kind"), t["status"],
                                               run is not None, parents.get(tid), pause_until,
                                               ob.get("reason"), ob.get("human"), ob.get("cause"))
            eff_kind, ob, waiting_on, words = settle_done(t, ob, waiting_on, words)
            row = {"task_id": tid, "title": clip(t["title"], 300), "status": t["status"], "product": board,
                   "epic": t["branch_name"], "repo": repo,
                   "phase": phase_of(t["status"], run["profile"] if run else None, waiting_on, eff_kind),
                   "role_now": run["profile"] if run else None, "model_now": run["model"] if run else None,
                   "run_id_now": run["run_id"] if run else None, "block_kind": eff_kind,
                   "block_reason": ob.get("reason"), "waiting_on": waiting_on, "waiting_words": words,
                   "waiting_since": ob.get("opened_at"),
                   "failure_count": t["failure_count"] or 0, "consecutive_failures": t["consecutive_failures"] or 0,
                   "risk_class": t["risk_class"], "tier": t["tier"], "created_at": iso(t["created_at"]),
                   "started_at": iso(t["started_at"]), "completed_at": iso(t["completed_at"]),
                   "commits": commits.get(tid, 0), "review_rounds": review_scope.reviewer_round(conn, tid)}
            self._snap(out, pending, "cap_cards", tid, row, env)
        # runs
        floor = (self.last_pass_ts or now) - RUN_OVERLAP_S
        runs_sql = ("SELECT * FROM task_runs" if full else
                    "SELECT * FROM task_runs WHERE ended_at IS NULL OR ended_at >= ?")
        for r in conn.execute(runs_sql, () if full else (floor,)):
            # Legacy old-system rows predate the run_id column (A3)
            run_id = r["run_id"] or f"{legacy_names.RUN_ID_PREFIX}{r['id']}"
            row = {"run_id": run_id, "task_id": r["task_id"], "role": r["profile"], "model": r["model"],
                   "provider": r["provider"], "account": r["account"], "runner": r["runner"], "status": r["status"],
                   "outcome": r["outcome"], "error_class": r["error_class"], "attempts": r["attempts"],
                   "started_at": iso(r["started_at"]), "ended_at": iso(r["ended_at"]),
                   "last_heartbeat_at": iso(r["last_heartbeat_at"]), "tokens_in": r["tokens_in"],
                   "tokens_out": r["tokens_out"], "tokens_cache_read": r["tokens_cache_read"],
                   "tokens_cache_write": r["tokens_cache_write"], "era": era_of(r["runner"])}
            self._snap(out, pending, "cap_runs", run_id, row, env)
        # epics
        branches = {t["branch_name"] for t in tasks.values() if t["branch_name"]} | set(config.epic_branches(cfg))
        for epic in sorted(branches):
            mine = [t for t in tasks.values() if t["branch_name"] == epic]
            entry = config.epic_for_branch(cfg, epic) or {}
            row = {"epic": epic, "product": board, "repo": repo, "spec": entry.get("spec"),
                   "decision_rows": [str(r) for r in entry.get("rows", [])] or None,
                   "cards_total": len(mine), "cards_done": sum(t["status"] == "done" for t in mine),
                   "cards_blocked": sum(t["status"] in ("blocked", "triage") for t in mine),
                   "head_sha": None, "ahead_of_main": None, "pr_number": None, "pr_url": None,
                   "publish_state": "none", "publish_reason": None, "publish_state_since": None}
            self._snap(out, pending, "cap_epics", epic, row, env)
        # commits
        for e in conn.execute("SELECT * FROM task_events WHERE kind='completed' AND id > ?", (0 if full else since,)):
            p, ok = _payload(e["payload"])
            if not ok and e["id"] not in self.bad_payload_ids:
                counts["lookout_errors"] += 1
                self.bad_payload_ids.add(e["id"])
            if not p.get("commit") or not repo:
                continue
            g = p.get("gates") or {}
            t = tasks.get(e["task_id"])
            row = {"repo": repo, "sha": p["commit"], "epic": t["branch_name"] if t else None, "task_id": e["task_id"],
                   "run_id": p.get("run_id"), "committed_at": iso(e["created_at"]), "validator": g.get("validator"),
                   "reviewer": g.get("reviewer"), "guide": g.get("guide"), "review_rounds": g.get("rounds"),
                   "overrides": g.get("overrides"), "pushed_at": None, "pr_number": None, "pr_comment_url": None}
            self._snap(out, pending, "cap_commits", (repo, p["commit"]), row, env)
        # incidents
        for i in conn.execute("SELECT * FROM incidents"):
            row = {"id": i["id"], "task_id": i["task_id"], "run_id": i["run_id"], "kind": i["kind"],
                   "status": i["status"], "role": i["role"], "opened_at": iso(i["opened_at"]),
                   "closed_at": iso(i["closed_at"]), "root_cause": clip(i["root_cause"], 300),
                   "fix_card": i["fix_card"], "repeat_of": i["repeat_of"]}
            self._snap(out, pending, "cap_incidents", i["id"], row, env)
        # events
        top = since
        for e in new_events:
            top = e["id"]
            if e["kind"] == "heartbeat":
                continue
            p, ok = _payload(e["payload"])
            # a poisoned row is skipped, counted once, never re-seen (top moved)
            if not ok:
                if e["id"] not in self.bad_payload_ids:
                    counts["lookout_errors"] += 1
                    self.bad_payload_ids.add(e["id"])
                continue
            t = tasks.get(e["task_id"])
            run = conn.execute("SELECT profile, model FROM task_runs WHERE run_id=?", (p.get("run_id"),)).fetchone() \
                if p.get("run_id") else None
            payload = {"event": e["kind"], "run_id": p.get("run_id"), "role": run["profile"] if run else None,
                       "model": run["model"] if run else None, "epic": t["branch_name"] if t else None,
                       "product": board, "repo": repo}
            if e["kind"] in STATE:
                payload["state"] = STATE[e["kind"]]
            payload.update(_kind_fields(e["kind"], p))
            out.append({"v": SPOOL_V, "t": "event", "row": {
                "v": 1, "event_id": event_id(board, e["id"]), "ts": iso(e["created_at"]), "kind": "card",
                "source": "capsule", "board": board, "task_id": e["task_id"], "slug": None,
                "title": clip(t["title"], 300) if t else None, "text": clip(p.get("reason") or p.get("error"), 500),
                "link": None, "severity": "warn" if e["kind"] in STATE and STATE[e["kind"]] == "open" else "info",
                "payload": payload}})
        # tickmark + health
        running = len(live)
        ready = sum(t["status"] == "ready" for t in tasks.values())
        blocked = sum(t["status"] in ("blocked", "triage") for t in tasks.values())
        # lookout_ms: the PREVIOUS pass's Lookout step (ruling 7); this pass's is
        # measured by the tick around this very call, so it cannot be known here.
        counts_out = dict(counts, lookout_ms=lookout_ms, lookout_errors=lookout_errors)
        unshipped = 0
        sp = spool_path(self.home, self.spool_board)
        if sp.exists():
            try:
                unshipped = max(0, sp.stat().st_size - int(self._offset_file().read_text()))
            except (OSError, ValueError):
                unshipped = sp.stat().st_size
        out.append(dict(env, t="tickmark", row={
            "host": self.host, "pid": self.pid, "harness_sha": self._sha(), "last_pass_at": iso(now),
            "pass_ms": pass_ms, "lookout_ms": lookout_ms, "counts": counts_out, "running": running,
            "ready": ready, "blocked": blocked, "paused_accounts": paused_accounts,
            "spool_bytes_unshipped": unshipped}))
        if self.passes % HEALTH_EVERY == 0:
            out.append({"v": SPOOL_V, "t": "event", "row": {
                "v": 1, "event_id": event_id(board, "health", env["pass_id"]), "ts": iso(now), "kind": "health",
                "source": "capsule", "board": board, "task_id": None, "slug": None, "title": None, "text": None,
                "link": None, "severity": "info",
                "payload": {"tick": True, "board": board, "counts": counts_out, "pass_ms": pass_ms,
                            "lookout_ms": lookout_ms, "running": running, "ready": ready, "blocked": blocked}}})
        sp = spool_path(self.home, self.spool_board)
        self.snaps_paused = sp.exists() and sp.stat().st_size > SPOOL_MAX_BYTES
        if self.snaps_paused:                               # spec 5.10: still over after compaction
            out = [l for l in out if l["t"] != "snap"]
            pending = {}         # none of this pass's snap rows were written; nothing to mark sent
            # Forget sent-row hashes (publish keys aside) so every row is resent once
            # shipping catches up.
            self.sent = {k: v for k, v in self.sent.items() if k[0] == "publish"}
        # raises on a full disk: watermark untouched
        self._append(out)
        self.sent.update(pending)          # only now, once the write has actually succeeded
        if top != since:
            db.guard_set(conn, EVENTS_WATERMARK, {"event_id": top}, now=int(now))
        self.last_pass_ts = now
        maintain_spool(self.home, self.spool_board)
        self.snaps_paused = sp.exists() and sp.stat().st_size > SPOOL_MAX_BYTES
        return len(out)

    def _offset_file(self):
        return offset_path(self.home, self.spool_board)

    def _append(self, lines):
        """`os.write` can return a short count without raising (a
        plain ENOSPC on a regular file often does), which would otherwise fsync a
        torn JSON line into the spool and still advance the watermark."""
        d = lookout_dir(self.home, self.spool_board)
        d.mkdir(parents=True, exist_ok=True)
        blob = "".join(json.dumps(l, ensure_ascii=False, default=str) + "\n" for l in lines).encode("utf-8")
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        fd = os.open(spool_path(self.home, self.spool_board), flags, 0o600)
        try:
            start = os.fstat(fd).st_size
            try:
                written = 0
                while written < len(blob):
                    n = os.write(fd, blob[written:])
                    if n <= 0:
                        raise OSError("short write: os.write wrote 0 bytes")
                    written += n
                os.fsync(fd)
            except Exception:
                try:
                    os.ftruncate(fd, start)
                except OSError:
                    pass
                raise
        finally:
            os.close(fd)
