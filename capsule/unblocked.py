"""`capsule unblocked`: a deterministic, read-only detector of unblocked, unowned work.

It reads every source of ready-but-unclaimed work, applies one shared blocker vocabulary,
and prints a table plus a `UNBLOCKED n UNOWNED m` summary line. Every DB opens `mode=ro`,
every file is a plain read, and every git call is read-only plumbing (`for-each-ref`,
`rev-parse --verify`). Markers live as plain files under `$CAPSULE_HOME`, never in git
(a marker committed in a repo would carry through a merge onto later branches).

Sources: (a) board `kanban.db` files (Capsule boards with a config only); (b) the unmerged-branch
queue per repo, with markers in `$CAPSULE_HOME/queue/<repo>/`; (c) pending human verbs under
`boards/<board>/human/`;
(d) run layers whose `progress.json` shows a done layer with no `triage-<layer>.md`,
either next to the `progress.json` or in `$CAPSULE_HOME/queue/runs/<run-dir>/triage-<layer>.md`.

Blockers come from a record's `waits:` line: `report-commit <sha>` (a run with that commit is
still running), `owner` (only a human-verb file or
config `unblocked.owner_holds` may declare it). Any other `waits` text is unblocked: an
unrecognised wait is a bug in its writer, not a licence to sit silent. (A
retired kind was removed with its tool: it now reads as unblocked like any unknown text.)

Derived blockers never come from `waits:`: `dependency` (a parent card is not done), then,
for a ready unassigned card on the tick's own board, `board-paused`, `no-epic`, `capacity`
and `dispatcher-hold`. Each mirrors a gate in `dispatch.ready`, and each shadows the next.
"""
import fnmatch
import glob
import json
import re
import sqlite3
import subprocess
import time
from pathlib import Path

from capsule import blocks, cardmeta, config, db, design_briefs, dispatch, dispatch_gate, idle_lane
from capsule import human, paths, spool

DEFAULT_BOARDS = ("capsule-corp",)
DEFAULT_REPOS = ("~/CodeBase/capsule-corp",)
BRANCH_NAME_PATTERNS = ("fix/*", "capsule/*", "htc/*", "zenkai/*")
# ALIAS(remove): old config key `unblocked.<old name>_holds`; drop when config.json says
# owner_holds (D270)
LEGACY_HOLDS_KEY = human.LEGACY_OWNER + "_holds"
OWNER_HEARTBEAT_MAX_S = 40 * 60          # a heartbeat younger than 40 minutes owns the item
PARK_OR_DELETE_AGE_S = 30 * 24 * 3600    # branches older than 30 days
WAITS_RE = re.compile(r"(?im)^\s*waits:\s*(.+?)\s*$")
REPORT_COMMIT_RE = re.compile(r"^report-commit\s+([0-9a-fA-F]{7,40})$")
CANDIDATE_CARD_STATUSES = ("ready", "built", "review")
DONE_STATES = ("landed", "superseded")    # marker states: cleanup, listed but never counted as work


def configured_boards(capsule_home=None):
    """Boards from config `unblocked.boards`, else DEFAULT_BOARDS. An unreadable config
    falls back to the defaults (callers are views, which must not die on a bad file). An empty
    `boards: []` also shows the defaults here, while `detect` then reads no board."""
    try:
        return load_config(paths.home(capsule_home))["boards"] or list(DEFAULT_BOARDS)
    except EnvironmentError_:
        return list(DEFAULT_BOARDS)


class EnvironmentError_(Exception):
    """A source could not be read at all (not merely empty) - `main` turns this into exit 2."""


class Item:
    __slots__ = ("item", "source", "state", "blocker", "owner", "age_s")

    def __init__(self, item, source, state, blocker="", owner="", age_s=None):
        self.item = item
        self.source = source
        # unblocked | blocked | unclassified (unmerged branch, no marker, no known name)
        # | notice (board not taken over; never counted)
        self.state = state
        self.blocker = blocker      # "" or the waits: text that blocked it
        self.owner = owner          # "" or who/what owns it
        self.age_s = age_s

    def unowned(self):
        return self.state == "unblocked" and not self.owner

    def as_dict(self):
        return {"item": self.item, "source": self.source, "state": self.state,
                "blocker": self.blocker, "owner": self.owner, "age_s": self.age_s}


# --------------------------------------------------------------------------- config

def load_config(capsule_home=None):
    """The `unblocked` block of `<capsule_home>/config.json`, merged over defaults.

    A missing config file (no board set up yet) is not an environment error - it just
    means every default applies. An unreadable *present* file (bad JSON, bad permissions)
    is: something is broken, not merely unconfigured.
    """
    cfg = {"boards": list(DEFAULT_BOARDS), "repos": list(DEFAULT_REPOS), "runs": [],
           "review_notes": None, "owner_holds": []}
    path = paths.config_path(capsule_home)
    try:
        text = Path(path).read_text()
    except FileNotFoundError:
        return cfg
    except OSError as exc:
        raise EnvironmentError_(f"cannot read {path}: {exc}") from exc
    try:
        whole = json.loads(text)
    except ValueError as exc:
        raise EnvironmentError_(f"{path}: invalid JSON ({exc})") from exc
    block = whole.get("unblocked", {}) if isinstance(whole, dict) else {}
    if not isinstance(block, dict):
        raise EnvironmentError_(f"{path}: 'unblocked' must be an object")
    if LEGACY_HOLDS_KEY in block and "owner_holds" not in block:
        block = {**block, "owner_holds": block[LEGACY_HOLDS_KEY]}
    for key in ("boards", "repos", "runs", "owner_holds"):
        if key in block:
            if not isinstance(block[key], list) or not all(isinstance(x, str) for x in block[key]):
                raise EnvironmentError_(f"{path}: unblocked.{key} must be a list of strings")
            cfg[key] = block[key]
    if "review_notes" in block:
        if block["review_notes"] is not None and not isinstance(block["review_notes"], str):
            raise EnvironmentError_(f"{path}: unblocked.review_notes must be a string or null")
        cfg["review_notes"] = block["review_notes"]
    return cfg


def load_caps(capsule_home=None):
    """`dispatch_gate.caps_from` applied to the top level of ``<capsule_home>/config.json``:
    the board, cap, epic list and assignee map this installation's tick governs. ``None``
    when the file is absent, not an object, or lacks a valid ``board`` and ``caps.max_running``.
    Only a config.json that fails to parse, or whose assignees map has a
    non-string entry, raises."""
    path = paths.config_path(capsule_home)
    try:
        text = Path(path).read_text()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise EnvironmentError_(f"cannot read {path}: {exc}") from exc
    try:
        whole = json.loads(text)
    except ValueError as exc:
        raise EnvironmentError_(f"{path}: invalid JSON ({exc})") from exc
    if not isinstance(whole, dict):
        return None
    try:
        return dispatch_gate.caps_from(whole)
    except ValueError as exc:
        raise EnvironmentError_(f"{path}: {exc}") from exc


def load_caps_all(capsule_home=None):
    """Every configured board: the legacy board plus `boards/*/config.json`."""
    try:
        return dispatch_gate.load_caps_all(capsule_home, load_caps(capsule_home))
    except ValueError as exc:
        raise EnvironmentError_(str(exc)) from exc


dispatcher_state = dispatch_gate.state
dispatcher_blocker = dispatch_gate.blocker


# --------------------------------------------------------------------------- blockers

def parse_waits(text):
    """The `waits:` value in `text` (its last match wins - the freshest line), or None."""
    if not text:
        return None
    matches = WAITS_RE.findall(text)
    return matches[-1] if matches else None


def _newest_of_body_and_comment(body_val, comment_val, comment, card):
    """The tie-break shared by `waits:` and `builds:`: `tasks` has no body-edit time, so
    `created_at` stands in for the body's age. A strictly newer comment wins; otherwise the body."""
    if comment and comment["created_at"] is not None and card["created_at"] is not None \
            and comment["created_at"] > card["created_at"]:
        return comment_val or body_val
    return body_val or comment_val


def _report_commit_running(sha_prefix, run_status_by_commit):
    """True while ANY run layer file whose commit matches the case-insensitive `sha_prefix`
    is `status == "running"`. `run_status_by_commit` is {lowercased sha: [status, ...]};
    a list, because a re-run can reuse a commit."""
    sha_prefix = sha_prefix.lower()
    return any(status == "running"
               for full_sha, statuses in run_status_by_commit.items() if full_sha.startswith(sha_prefix)
               for status in statuses)


def evaluate_blocker(waits, *, run_status_by_commit, allow_owner=False):
    """(blocked: bool, blocker: str) for a `waits` spec under the two allowed rules.

    `run_status_by_commit` is {lowercased sha: [status, ...]} from `run_status_registry`.
    `waits: owner` blocks only when
    `allow_owner` is set, which only human-verb files and config `unblocked.owner_holds`
    may do; board cards and queue markers may declare `report-commit` only.
    """
    if not waits:
        return False, ""
    m = REPORT_COMMIT_RE.match(waits.strip())
    if m:
        sha = m.group(1)
        blocked = _report_commit_running(sha, run_status_by_commit)
        return blocked, f"waits: report-commit {sha}" if blocked else ""
    if human.canonical_author(waits.strip()) == "owner" and allow_owner:
        return True, "waits: owner"
    return False, ""    # not one of the two allowed rules: unrecognised waits never blocks


def dependency_blocked(conn, card_id):
    """``(blocked, blocker)`` for `card_id`'s parent links (`task_links`, never card text).
    Blocked while any parent is not `done`/`archived`. A schema without `task_links` is
    never blocked."""
    try:
        parent = db.open_parent(conn, card_id)
    except sqlite3.OperationalError:
        return False, ""
    return (True, f"dependency: {parent}") if parent is not None else (False, "")


def capacity_blocked(running_count, max_running):
    """True once the board's live ``running_count`` (`dispatch.running_count`, the query
    `dispatch.ready` gates on) has reached ``caps.max_running``. Plain arithmetic on purpose:
    `board_items`' end-to-end read of a real `tasks` table is the second oracle (Z2)."""
    return running_count >= max_running


def epic_branch_ok(branch_name, epic_branches):
    """True while `branch_name` is one of the board's configured epic branches.
    `dispatch.ready` only selects rows on those, so any other card is `no-epic`, not
    `capacity`."""
    return branch_name in epic_branches


def dispatcher_hold_reason(conn, card, busy_branches, caps=None, home=None, work_root=None):
    """Why `dispatch.ready`'s per-card gates would hold `card` back this tick, or `None`.

    It reuses `dispatch`'s own predicates so it cannot drift from `ready`, in `ready`'s order:
    `branch-busy`, `clone-dirty`, `screen-hold`. It does not mirror the routing
    and tier gates: they need a routed role and live host-tier state this read-only
    detector never has."""
    if dispatch.branch_busy(card["branch_name"], busy_branches):
        return "branch-busy"
    if caps and design_briefs.missing(caps, card, home):
        return "design-brief"
    if caps and (need := dispatch.needs_missing(caps, card, home)):
        return f"needs-file {need}"
    holder = dispatch.clone_dirty(conn, card["branch_name"], card["id"], work_root)
    if holder:
        return f"clone-dirty (held by {holder})"
    if dispatch.screen_held(conn, card["branch_name"], card["id"]):
        return "screen-hold"
    return None


# --------------------------------------------------------------------------- (a) boards

def _board_db_candidates(board, capsule_home):
    """A configured board (its own `boards/<b>/config.json`, or the legacy top-level config's
    board) reads only its Capsule db (C25). Any other board is not read (the old boards
    `default` and `ollama-dashboard` are dropped, D244)."""
    legacy = load_caps(capsule_home)
    if paths.config_path(capsule_home, board).is_file() or (legacy and legacy["board"] == board):
        return [paths.board_db(board, capsule_home)]
    return []


_FALLBACK_CARD_COLUMNS = ("id", "title", "body", "assignee", "status", "created_at",
                          "current_run_id", "branch_name")


def _fallback_cards(conn):
    """The plain candidate-card read `board_items` falls back to when its caps-only reads
    raise. It builds the select from `PRAGMA table_info(tasks)`, so a board missing a legacy
    base column reads it as `NULL` instead of raising again inside the `except` handler."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    select_list = ", ".join(c if c in have else f"NULL AS {c}" for c in _FALLBACK_CARD_COLUMNS)
    return conn.execute(
        f"SELECT {select_list} FROM tasks WHERE status IN (?,?,?)", CANDIDATE_CARD_STATUSES).fetchall()


def _ready_card_gate(
        caps, card, *, board, conn, paused, running_count, busy_branches, home=None):
    """Why a ready, unblocked/unowned card is blocked, or `None`. A non-live `dispatcher`
    verdict (`none`/`uncertain`) wins alone; otherwise the order is board-paused,
    unassigned, no-epic, capacity, dispatcher-hold. `unassigned` comes first because
    `no-epic` and `capacity` can each clear without anyone touching the card."""
    verdict = dispatcher_blocker(caps["dispatcher"]) if "dispatcher" in caps else None
    if verdict is not None:
        blocked_now, blocker = verdict
        return ("blocked" if blocked_now else "unblocked"), blocker
    if paused:
        return "blocked", "board-paused"
    if _weekly_held(conn):
        return "blocked", "weekly-hold"
    if dispatch.resolve_assignee(caps, card["assignee"]) is None:
        return "blocked", "unassigned"
    if not epic_branch_ok(card["branch_name"], caps["epic_branches"]):
        return "blocked", "no-epic"
    if capacity_blocked(running_count, caps["max_running"]):
        return "blocked", f"capacity: board={board} cap={caps['max_running']} running={running_count}"
    hold = dispatcher_hold_reason(conn, card, busy_branches, caps, home, dispatch.root(board, home))
    return ("blocked", f"dispatcher-hold: {hold}") if hold else None


def _weekly_held(conn):  # guard only, no meter scan; garbled or unreadable = not held (fail open)
    try:
        return bool(db.guard_get(conn, "weekly_pause")[0]["held"])
    except Exception:
        return False


def _factory_owner(card, caps):
    """`factory` for a review card on a board with a live dispatcher: the tick drives it."""
    if card["status"] != "review" or caps is None or "dispatcher" not in caps:
        return ""
    return "factory" if dispatcher_blocker(caps["dispatcher"]) is None else ""


def _card_blocked(conn, card, waits, run_status_by_commit):
    """``(blocked, blocker)``: `waits:` text first, else parent links for a `ready` card
    only (`lease.claim` only claims `ready`). Blocked `waits:` text is never relabelled."""
    blocked, blocker = evaluate_blocker(
        waits, run_status_by_commit=run_status_by_commit,
        allow_owner=False)    # a card body/comment may declare report-commit only
    return (blocked, blocker) if blocked or card["status"] != "ready" else dependency_blocked(conn, card["id"])


def board_items(board, *, capsule_home, now, run_status_by_commit,
                 caps=None):
    """`caps` is `None` (no capacity/epic/pause check) or `{"max_running": N,
    "epic_branches": [...]}` as the board's tick was configured; `detect` passes it only for
    the tick's own board. With `caps`, a `ready`, unassigned card that would be plain
    `unblocked` is reported `blocked` as the first match of: `board-paused`, `no-epic`,
    `capacity`, `dispatcher-hold: <reason>`.

    A board whose schema lacks Capsule's `tasks.file_set` or `guard_state` skips those checks
    and reports one `state="notice"` item instead, only when `caps` was given."""
    db_path = next((p for p in _board_db_candidates(board, capsule_home) if p.is_file()), None)
    if db_path is None:
        return []
    conn = db.connect_ro_tolerant(db_path)
    try:
        running_count, paused, busy_branches = None, False, set()
        out = []
        try:
            if caps is not None:
                # The Capsule-only `file_set` column and caps reads are attempted only when
                # `caps` was given.
                cards = conn.execute(
                    "SELECT id, title, body, assignee, status, created_at, current_run_id,"
                    " branch_name, risk_flags FROM tasks WHERE status IN (?,?,?)",
                    CANDIDATE_CARD_STATUSES).fetchall()
                running_count = dispatch.running_count(conn, now)
                paused = dispatch.board_paused(conn, now)
                busy_branches = dispatch.busy_branches(conn)
            else:
                cards = conn.execute(
                    "SELECT id, title, body, assignee, status, created_at, current_run_id, branch_name"
                    " FROM tasks WHERE status IN (?,?,?)", CANDIDATE_CARD_STATUSES).fetchall()
        except sqlite3.OperationalError as exc:
            # A board the tick has not taken over lacks Capsule's `file_set` column or
            # `guard_state` table; skip the caps checks for it and say so once. Only
            # reachable when `caps is not None`.
            caps = None
            cards = _fallback_cards(conn)
            out.append(Item(
                item=f"board:{board}: capacity/epic/pause/dispatcher-hold checks skipped"
                     f" ({type(exc).__name__}: {exc})",
                source=f"board:{board}", state="notice"))
        for card in cards:
            live = conn.execute(
                "SELECT 1 FROM task_runs WHERE task_id=? AND status='running' LIMIT 1",
                (card["id"],)).fetchone()
            if card["status"] != "ready" and live:
                continue    # built/review with a live run: owned, not a candidate
            comment = conn.execute(
                "SELECT body, created_at FROM task_comments WHERE task_id=? ORDER BY id DESC LIMIT 1",
                (card["id"],)).fetchone()
            body_waits = parse_waits(card["body"])
            comment_waits = parse_waits(comment["body"]) if comment else None
            waits = _newest_of_body_and_comment(body_waits, comment_waits, comment, card)
            blocked, blocker = _card_blocked(conn, card, waits, run_status_by_commit)
            owner = "live-run" if live else ""    # a bare current_run_id pointer owns nothing
            owner = owner or _factory_owner(card, caps)
            if not owner:
                # A card names its branch with a `builds: <repo>:<branch>` line (same
                # newest-body/comment tie-break as `waits:`). A fresh `owners.json` heartbeat
                # for that branch, or for the card id (`bin/capsule-own` accepts either),
                # counts as ownership.
                body_builds = cardmeta.parse_builds(card["body"])
                comment_builds = cardmeta.parse_builds(comment["body"]) if comment else None
                builds = _newest_of_body_and_comment(body_builds, comment_builds, comment, card)
                if builds:
                    repo_name, branch = builds
                    owners = _owners_json(repo_name, capsule_home)
                    branch_hb = (owners.get(branch) or {}).get("heartbeat")
                    card_hb = (owners.get(card["id"]) or {}).get("heartbeat")
                    if isinstance(branch_hb, (int, float)) and (now - branch_hb) < OWNER_HEARTBEAT_MAX_S:
                        owner = f"owners.json:{branch}"
                    elif isinstance(card_hb, (int, float)) and (now - card_hb) < OWNER_HEARTBEAT_MAX_S:
                        owner = "owners.json:card"
            state = "blocked" if blocked else "unblocked"
            if caps is None:
                # No tick governs this board, so a truthy `assignee` is the only ownership
                # signal: an assigned ready card is omitted, whatever `state`/`owner` say.
                if card["status"] == "ready" and card["assignee"]:
                    continue
            else:
                if card["status"] == "ready" and state == "unblocked" and not owner:
                    got = _ready_card_gate(caps, card, board=board, conn=conn, paused=paused,
                                            running_count=running_count,
                                            busy_branches=busy_branches, home=capsule_home)
                    if got is not None:
                        state, blocker = got
                if (card["status"] == "ready" and card["assignee"] and state == "unblocked" and not owner
                        and blocker != "no-dispatcher"):
                    # An assigned ready card is omitted only once every check above found
                    # nothing holding it back: it is about to dispatch. Blocked cards stay
                    # visible, and so does `no-dispatcher`: no tick serves the board, so
                    # an assigned card is as unowned as an unassigned one.
                    continue
            out.append(Item(
                item=f"{board}:{card['id']} {card['title']}"[:120], source=f"board:{board}",
                state=state, blocker=blocker,
                owner=owner, age_s=max(0, now - (card["created_at"] or now))))
        return out
    finally:
        conn.close()


def _idle_lane_items(board, *, capsule_home, now):
    """One unowned item when `idle_lane.find` says the lane has sat idle behind a holder. The
    text carries no clock (the Stop hook re-blocks on a changed `(item, blocker)` pair); the
    age is `age_s`."""
    found = (p for p in _board_db_candidates(board, capsule_home) if p.is_file())
    db_path = next(found, None)
    if db_path is None:
        return []
    conn = db.connect_ro_tolerant(db_path)
    try:
        try:
            got = idle_lane.find(conn, now, dispatch.root(board, capsule_home))
            cap = got and blocks.cap_hold_label(conn, got["holder"]["id"])
        except sqlite3.OperationalError:
            return []       # a board the tick has not taken over: `board_items` already says so
    finally:
        conn.close()
    if got is None:
        return []
    ready, holder = got["ready"], got["holder"]
    why = "/".join(filter(None, (holder["status"], cap or holder["block_kind"])))
    return [Item(item=(f"{board}: idle lane: {ready['id']} {ready['title'][:40]} waits on"
                       f" {holder['id']} {holder['title'][:40]} ({why})"),
                 source=f"board:{board}", state="unblocked", blocker=f"idle-lane: {got['label']}",
                 age_s=got["idle_s"])]


# --------------------------------------------------------------------------- (b) branches

def _git(repo, *args, run=subprocess.run):
    out = run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=30)
    return out.returncode, out.stdout, out.stderr


def _queue_marker(repo_name, branch, capsule_home):
    """`{"state":..., "waits":...}` for `branch`, from
    `$CAPSULE_HOME/queue/<repo_name>/<branch>.json`. The marker lives outside the repo tree:
    a committed marker would inherit through a merge onto later branches. A plain file read."""
    p = paths.queue_dir(repo_name, capsule_home) / f"{branch}.json"
    try:
        doc = json.loads(p.read_text())
        mtime = p.stat().st_mtime    # plain stat: lets `branch_items` spot commits after the marker
    except (OSError, ValueError):
        return None
    return {**doc, "_mtime": mtime} if isinstance(doc, dict) else None


def _owners_json(repo_name, capsule_home):
    p = paths.queue_dir(repo_name, capsule_home) / "owners.json"
    try:
        doc = json.loads(p.read_text())
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _content_on_main(repo, branch, *, run=subprocess.run):
    """True when `branch` adds nothing main lacks: identical trees, or every commit has a
    patch-id match on main (`git cherry` lists no `+`). Commit ancestry cannot tell: a
    cherry-pick lands the content under new shas. A branch whose tree equals its merge-base
    with main (an idle-sync merge of an older main) is also on main, though main moved on.
    Runs only for a done branch with new commits."""
    for args in (("main", branch), (f"main...{branch}",)):    # 2nd: merge-base vs branch
        if _git(repo, "diff", "--quiet", *args, "--", run=run)[0] == 0:
            return True
    rc, out, _ = _git(repo, "cherry", "main", branch, run=run)
    return rc == 0 and not any(ln.startswith("+") for ln in out.splitlines())


def branch_items(repo, *, review_notes, now, run_status_by_commit,
                  run=subprocess.run, capsule_home=None):
    """One for-each-ref per repo, plus up to three git calls (diff, diff main...branch,
    cherry) per done branch with commits newer than its marker. `for-each-ref --no-merged=main`
    filters out branches main reaches. Queue markers and ownership are plain file reads."""
    repo = Path(repo).expanduser()
    if not (repo / ".git").exists():
        return []
    rc, _, _ = _git(repo, "rev-parse", "--verify", "--quiet", "refs/heads/main", run=run)
    if rc != 0:
        return []    # no local `main` at all: nothing to compare branches against
    rc, out, _ = _git(repo, "for-each-ref", "--no-merged=main", "refs/heads",
                       "--format=%(refname:short)\t%(committerdate:unix)", run=run)
    if rc != 0:
        return []
    tips = {}
    for line in filter(None, out.splitlines()):
        name, _, ts = line.partition("\t")
        if name == "main":
            continue
        tips[name] = int(ts) if ts.strip().isdigit() else now
    owners = _owners_json(repo.name, capsule_home)
    out_items = []
    for branch, tip in tips.items():
        marker = _queue_marker(repo.name, branch, capsule_home)
        if marker is not None:
            state_name, waits = marker.get("state", "built-unreviewed"), marker.get("waits")
        elif any(fnmatch.fnmatch(branch, pat) for pat in BRANCH_NAME_PATTERNS):
            state_name, waits = "built-unreviewed", None
        else:
            # An unmerged branch with no marker and no recognised name surfaces under
            # "unclassified" (`render_table`) and is never counted in UNBLOCKED/UNOWNED.
            out_items.append(Item(item=f"{repo.name}:{branch} (unclassified)", source="branch",
                                   state="unclassified", age_s=max(0, now - tip)))
            continue
        if review_notes:
            note = Path(review_notes).expanduser() / f"{repo.name}-{branch.replace('/', '-')}.md"
            if note.is_file():
                continue    # already reviewed
        blocked, blocker = evaluate_blocker(
            waits, run_status_by_commit=run_status_by_commit,
            allow_owner=False)    # a queue marker may declare report-commit only
        heartbeat = (owners.get(branch) or {}).get("heartbeat")
        owned = isinstance(heartbeat, (int, float)) and (now - heartbeat) < OWNER_HEARTBEAT_MAX_S
        newer = marker is not None and tip > marker["_mtime"] and state_name in DONE_STATES
        if newer and _content_on_main(repo, branch, run=run):
            newer = False    # main holds the content (tree, merge-base tree, patch-id)
        done = state_name in DONE_STATES and not newer    # commits after the marker: work again
        out_items.append(Item(
            item=f"{repo.name}:{branch} ({state_name}{', new commits' if newer else ''})",
            source="branch", state="done" if done else "blocked" if blocked else "unblocked",
            blocker=blocker, owner="owners.json" if owned else "", age_s=max(0, now - tip)))
    return out_items


# --------------------------------------------------------------------------- (c) human verbs

def human_items(board, *, capsule_home, now):
    d = spool.human_dir(capsule_home, board)
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.json")):
        try:
            doc = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(doc, dict):
            continue
        waits = doc.get("waits")
        # a human-verb file waits for a human by construction;
        blocked = human.canonical_author(waits) == "owner"
        # with config `unblocked.owner_holds` it is one of two sources that may declare it.
        out.append(Item(
            item=f"{board}:{p.stem} {doc.get('kind', doc.get('task_id', p.name))}",
            source=f"human:{board}", state="blocked" if blocked else "unblocked",
            blocker="waits: owner" if blocked else "", owner="",
            age_s=max(0, now - (doc.get("at") or now))))
    return out


# --------------------------------------------------------------------------- (d) run layers

def _load_progress_files(run_globs):
    paths_ = []
    for pattern in run_globs:
        paths_.extend(Path(p) for p in glob.glob(str(Path(pattern).expanduser())))
    docs = []
    for p in sorted(set(paths_)):
        try:
            doc = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(doc, dict):
            docs.append((p, doc))
    return docs


def run_status_registry(run_globs):
    """{lowercased commit: [status, ...]} across every matched `progress.json` (the
    `report-commit` oracle). A list, because any one run file of a shared commit running
    must block."""
    reg = {}
    for _, doc in _load_progress_files(run_globs):
        commit = doc.get("commit")
        if isinstance(commit, str) and commit:
            reg.setdefault(commit.lower(), []).append(doc.get("status"))
    return reg


ENDED_LAYER_STATUSES = ("done", "blocked", "parked")


def run_layer_items(run_globs, *, now, capsule_home=None):
    """A layer at rest (`done`, `blocked` or `parked`) with no `triage-<layer>.md` marker is
    a candidate. All three read as `state="unblocked"`: this adapter only asks whether a
    human must look at the layer.

    The marker sits either next to the run's `progress.json` or in
    `$CAPSULE_HOME/queue/runs/<run-dir-name>/`, so agents never write inside a run's own
    directory."""
    out = []
    for p, doc in _load_progress_files(run_globs):
        layers = doc.get("layers")
        if not isinstance(layers, dict):
            continue
        for name, layer in layers.items():
            if not isinstance(layer, dict) or layer.get("status") not in ENDED_LAYER_STATUSES:
                continue
            beside = (p.parent / f"triage-{name}.md").exists()
            # Known hazard: this `"runs"` queue namespace is shared with (b), so a repo named
            # `runs` would collide.
            in_queue_dir = (paths.queue_dir("runs", capsule_home) / p.parent.name
                             / f"triage-{name}.md").exists()
            if beside or in_queue_dir:
                continue
            try:
                age = max(0, now - p.stat().st_mtime)
            except OSError:
                age = None
            out.append(Item(item=f"{p}:{name} ({layer['status']})", source="run-layer", state="unblocked",
                             blocker="", owner="", age_s=age))
    return out


# --------------------------------------------------------------------------- detect / render

def _vault_eval_items(capsule_home):
    """One `notice` when `redesign.vault_context` is report/live and the newest dated
    vault-eval result missed (a failed metric, or the two recall counters disagree)."""
    from capsule import vault_eval
    results = sorted(Path(vault_eval.STATE).glob("result-*.json"))
    if not results or vault_eval.mode(capsule_home) not in ("report", "live"):
        return []
    day = results[-1].stem[7:]
    try:
        doc = json.loads(results[-1].read_text())
    except (OSError, ValueError):
        doc = None
    if not isinstance(doc, dict) or not isinstance(doc.get("failed", []), list):
        text = f"vault-eval result unreadable on {day}"
    else:
        failed = [str(x) for x in doc.get("failed", [])]
        if not doc.get("valid", True):
            failed.append("counters disagree")
        if not failed:
            return []
        text = f"vault-eval missed on {day}: {', '.join(failed)}"
    return [Item(text, source="vault-eval", state="notice")]


def detect(*, capsule_home=None, now=None,
           git_run=subprocess.run, dispatcher_check=None):
    """Every item from every source, the shared blocker registry applied once. Raises
    `EnvironmentError_` only when a configured source cannot be read at all; an empty or
    absent source contributes nothing.

    `dispatcher_check` is injected into `dispatcher_state` for every configured board."""
    now = now if now is not None else time.time()
    capsule_home = paths.home(capsule_home)    # no-arg callers (the tick) get the env/default
    cfg = load_config(capsule_home)
    caps_by_board = load_caps_all(capsule_home)
    for b, c in caps_by_board.items():
        c["dispatcher"] = dispatcher_state(b, capsule_home, check=dispatcher_check)
    run_status_by_commit = run_status_registry(cfg["runs"])
    items = []
    for board in cfg["boards"]:
        caps = caps_by_board.get(board)
        items += board_items(board, capsule_home=capsule_home, now=now,
                              run_status_by_commit=run_status_by_commit,
                              caps=caps)
        if caps is not None:
            items += _idle_lane_items(board, capsule_home=capsule_home, now=now)
        items += human_items(board, capsule_home=capsule_home, now=now)
    for repo in cfg["repos"]:
        items += branch_items(repo, review_notes=cfg["review_notes"], now=now,
                               run_status_by_commit=run_status_by_commit,
                               run=git_run,
                               capsule_home=capsule_home)
    items += run_layer_items(cfg["runs"], now=now, capsule_home=capsule_home)
    items += _vault_eval_items(capsule_home)
    # The other source of an owner-only block: an item named by config
    # `unblocked.owner_holds` is forced blocked once, after every adapter has run.
    holds = set(cfg["owner_holds"])
    if holds:
        for item in items:
            if item.item in holds:
                item.state, item.blocker = "blocked", "waits: owner"
    return items


def summarize(items):
    unblocked = [i for i in items if i.state == "unblocked"]
    unowned = [i for i in unblocked if not i.owner]
    return len(unblocked), len(unowned)


def _fmt_age(age_s):
    if age_s is None:
        return "?"
    age_s = int(age_s)
    if age_s < 3600:
        return f"{age_s // 60}m"
    if age_s < 86400:
        return f"{age_s // 3600}h"
    return f"{age_s // 86400}d"


def _heading(lines, title, group_items, *, with_blocker=False):
    """`title` plus one bullet per item, iff `group_items` is non-empty: the grouping every
    blocked-but-visible heading below shares."""
    if not group_items:
        return
    lines.append("")
    lines.append(title)
    for i in group_items:
        lines.append(f"- {i.item}: {i.blocker}" if with_blocker else f"- {i.item}")


def render_table(items):
    """`unclassified` items get their own trailing heading, apart from the main table
    `summarize` counts. The other headings are also `blocked` in the main table."""
    main_items = [i for i in items if i.state not in ("unclassified", "done")]
    unclassified = [i for i in items if i.state == "unclassified"]
    header = ("item", "source", "state", "blocker", "owner", "age")
    rows = [header] + [(i.item, i.source, i.state, i.blocker or "-", i.owner or "-", _fmt_age(i.age_s))
                        for i in main_items]
    widths = [max(len(r[c]) for r in rows) for c in range(len(header))]
    lines = ["  ".join(cell.ljust(w) for cell, w in zip(row, widths)) for row in rows]
    lines.insert(1, "  ".join("-" * w for w in widths))
    n, m = summarize(items)
    lines.append("")
    lines.append(f"UNBLOCKED {n} UNOWNED {m}")
    # board-paused first: `board_items`' own check order.
    _heading(lines, "-- board paused --", [i for i in main_items if i.blocker == "board-paused"])
    _heading(lines, "-- capacity (blocked, not unowned) --",
             [i for i in main_items if i.blocker.startswith("capacity:")], with_blocker=True)
    _heading(lines, "-- not dispatchable (no epic branch) --",
             [i for i in main_items if i.blocker == "no-epic"])
    _heading(lines, "-- dependency (blocked, not unowned) --",
             [i for i in main_items if i.blocker.startswith("dependency:")], with_blocker=True)
    _heading(lines, "-- not dispatchable (unassigned) --",
             [i for i in main_items if i.blocker == "unassigned"])
    _heading(lines, "-- dispatcher hold (blocked, not unowned) --",
             [i for i in main_items if i.blocker.startswith("dispatcher-hold:")], with_blocker=True)
    _heading(lines, "-- done branches (cleanup) --", [i for i in items if i.state == "done"])
    if unclassified:
        lines.append("")
        lines.append("-- unclassified (not counted above) --")
        for i in unclassified:
            due = ", park-or-delete decision due" if (i.age_s or 0) >= PARK_OR_DELETE_AGE_S else ""
            lines.append(f"- {i.item} ({_fmt_age(i.age_s)}){due}")
    return "\n".join(lines)


def render_json(items):
    n, m = summarize(items)
    return json.dumps({"items": [i.as_dict() for i in items], "unblocked": n, "unowned": m})


def main(argv, *, capsule_home=None, now=None, stdout=None):
    import sys
    stdout = stdout or sys.stdout
    as_json = "--json" in argv
    try:
        items = detect(capsule_home=capsule_home, now=now)
    except EnvironmentError_ as exc:
        print(f"capsule unblocked: {exc}", file=stdout)
        return 2
    except Exception as exc:    # every detector error is exit 2, never a raw traceback out
        # of a read-only detector a hook shells out to.
        print(f"capsule unblocked: {type(exc).__name__}: {exc}", file=stdout)
        return 2
    print(render_json(items) if as_json else render_table(items), file=stdout)
    _, m = summarize(items)
    return 1 if m > 0 else 0
