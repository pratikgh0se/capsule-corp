#!/usr/bin/env python3
"""board.py - the one module that speaks to the legacy board CLI.

Why this exists: the old CLI's `kanban_db_path()` gives `LEGACY_KANBAN_DB` precedence over
`--board`,
so any code that inherits a worker's environment silently reads the *worker's* board. One
`Board` per board name now owns the argv and the environment: it pins `LEGACY_KANBAN_DB` to its
own db path and passes `--board <name>` as well, so env and flag agree by construction.

Nothing else under maintenance/ may build `kanban` argv - tests/test_contracts.py
enforces that.
"""
import contextlib
import fcntl
import inspect
import json
import os
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import board_fence
import events
from capsule import legacy_names
from legacy_gate import legacy_present
from board_errors import (BoardError, BoardFrozen, CardNotFound, LegacyCliError,  # noqa
                          LegacyOutputInvalid, Locked, TransitionRefused)

# Resolved through `paths.legacy_home()`, the resolver the `.capsule-owned` marker uses, so the
# marker and the fence cannot drift. That honours LEGACY_KANBAN_HOME; the old CLI also read its
# own home variable (legacy_names.OLD_ENV_HOME), which neither side honours. If it ever enters
# the deployment, `paths.legacy_home` must learn it first, because the pin below *wins* and
# would silently read the wrong DB.
# `boards_dir=` stays the injection point for anything else.
BOARDS_DIR = board_fence.legacy_boards_dir()
# The `default` board's DB is not under boards/ (the old CLI's kanban_db.py). Capsule
# never uses it; db_path still resolves it rather than inventing a path.
DEFAULT_DB = Path.home() / legacy_names.LEGACY_HOME_DIRNAME / "kanban.db"
BOARD_RE = re.compile(r"[a-z0-9][a-z0-9-]*")
# Canonical task-id shape, real legacy ids (e.g. t_94f19b88): lowercase hex only. The one
# pattern every module that validates a task id shares (intake.py's approval-stamp guard,
# ledger.py's path-segment guard).
TASK_ID_RE = re.compile(r"^t_[0-9a-f]+$")
# `spec: Specs/<slug>.md`: the line build_meta_block writes into a dispatched card's body and
# the planner posts as a comment on a triage card. One pattern, two homes: see Card.spec_slug.
SPEC_LINE_RE = re.compile(r"^\s*spec:\s*Specs/([a-z0-9][a-z0-9-]{0,80})\.md\s*$", re.M)

# `bet: <slug>` in a card's machine-readable header is the one line that says WHY the card
# exists; a body otherwise carries `risk:` and `source:`, provenance, never a reason.
#
# It lives here because both writers need it and neither may import the other: intake.py
# writes it for an intake-born card (`intake._body_for`), board_bridge.py for a spec-born one
# (`build_meta_block`).
#
# The flag is default OFF and read on every call rather than at import, so a test can flip it.
# With it off `bet_line` returns "" and every card body is unchanged (asserted in
# test_board_bridge.py and test_intake.py).
#
# NOT in contracts.META_KEYS: that tuple is asserted key-by-key against a rendered block
# (test_contracts.py), and this line is optional.
BET_FLAG = "OPS_BET_FIELD"
# The same charset as merge_gate.SLUG_RE and board_bridge._SLUG_RE - a bet slug
# names a Pitches/<slug>.md file the way a spec slug names Specs/<slug>.md.
BET_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")
# Reads the line back out of a body. Anchored per-line (re.M) with nothing but the slug charset
# after the colon; only horizontal whitespace (`[ \t]`, never `\s`), so the match cannot cross
# a newline: a bare `bet:` line followed by a slug-shaped line must not read as a bet on it.
BET_LINE_RE = re.compile(r"^bet:[ \t]*([a-z0-9][a-z0-9-]{0,80})[ \t]*$", re.M)


def bet_enabled():
    """True only for an explicit `1` (budgets.enabled's flag rule)."""
    return os.environ.get(BET_FLAG) == "1"


def bet_line(slug):
    """`bet: <slug>\\n` for a card body, or `""`. Empty when the flag is off, the slug is empty,
    or the slug is malformed. Both callers validate the slug first, so the last branch should be
    unreachable; it exists so an unreachable path can never render a line a reader would trust.
    """
    if not bet_enabled() or not slug:
        return ""
    slug = str(slug)
    if not BET_SLUG_RE.fullmatch(slug):
        return ""
    return f"bet: {slug}\n"


# The one GitHub PR-url pattern: board_bridge's merge-note scan and board_view's Archive PR
# field both need it. It lives here so board_view (which board_bridge imports) can use it
# without importing board_bridge back.
PR_URL_RE = re.compile(r"https://github\.com/\S+/pull/\d+")

# The BODY form above is written only by build_meta_block, but a COMMENT can be posted by any
# worker with the kanban CLI, who could point a backlog card at an already-approved slug and
# have the bridge dispatch it. So a spec comment counts only with the bridge's own trailing
# marker, which board_bridge.link_spec writes. A prompt-injected worker that knows the string
# can still type it (an accepted residual); the marker removes the accident and the one-line
# forgery.
SPEC_LINK_MARK = "<!-- by: bridge -->"
# The gap before and inside the marker is [ \t]*, not \s*: \s matches a newline, which would let
# a marker on the FOLLOWING line splice onto the slug line (the marker must be on the same line).
SPEC_COMMENT_RE = re.compile(
    r"^[ \t]*spec:[ \t]*Specs/([a-z0-9][a-z0-9-]{0,80})\.md"
    r"[ \t]+<!--[ \t]*by:[ \t]*bridge[ \t]*-->[ \t]*$", re.M)

# --- the vocabulary legacy transitions are written in -----------------------
# `block_task` fires only WHERE status IN ('running','ready') and `_cmd_block` appends the
# `BLOCKED: <reason>` comment BEFORE it calls block_task, so a block on any other status litters
# the trail and then fails. Board.block checks this set before shelling out.
BLOCKABLE_STATUSES = {"running", "ready"}
# `unblock_task` fires only WHERE status IN ('blocked', 'scheduled'). It excludes `triage`, the
# block-loop-breaker's landing column, on purpose: legacy has no CLI verb that resumes a `triage`
# card. Unlike Board.block, Board.unblock does NOT refuse before argv: unblock is the hot path,
# so it reacts to the CLI's refusal and re-raises with a clear reason only when the card turns
# out to be `triage` (see its docstring).
UNBLOCKABLE_STATUSES = {"blocked", "scheduled"}
# The statuses whose `BLOCKED:` comment trail still applies. `triage` is where
# legacy unblock-loop breaker parks a card (kanban_db.py:6376-6390).
PARKED_STATUSES = {"blocked", "triage"}
LIVE_STATUSES = {"todo", "ready", "running", "blocked", "review", "triage"}
FAILURE_OUTCOMES = {"timed_out", "gave_up", "crashed", "protocol_violation", "spawn_failed"}

# The legacy CLI's `kanban block` posts its reason as a comment with this prefix, and only the
# CLI does. See BLOCK_EVENT_KINDS.
BLOCKED_COMMENT_PREFIX = "BLOCKED: "
# The `task_events` kinds whose `payload.reason` records why a card is parked. legacy has TWO
# block paths and only one writes a comment:
#   - CLI `_cmd_block` posts `BLOCKED: <reason>` and then calls block_task (the path repair
#     and merge_gate use).
#   - the `kanban_block` MCP tool calls kb.block_task directly and posts NO comment (the path
#     every worker SOUL uses, so where every real DECISION:/READY: reason arrives).
# `block_task` writes one of these rows on every successful block, from both paths: `blocked`,
# `block_loop_detected` when the unblock-loop breaker routes to triage, `dependency_wait` when
# the card is routed to `todo` instead (never a PARKED_STATUS, so it carries a reason but never
# parks a card). `created_at` is `int(time.time())`, the unit comments use.
BLOCK_EVENT_KINDS = ("blocked", "block_loop_detected", "dependency_wait")
# Capsule's own two block-reason prefixes: `block_reason` has to tell ours from
# a foreign one, so they live here rather than in repair.
HOLD_PREFIX = "repair: "
NEEDS_REPAIR_PREFIX = "NEEDS REPAIR: "
# The reclassify rung's identity on the card; the quality gate's route note posts it.
RECLASSIFY_PREFIX = "repair: reclassify"
RECLASSIFY_COMMENT = (
    "repair: reclassify - this card has failed 3x / gone over budget. Options:\n"
    "  split (cut it into cards of <=5 files each) \u00b7 epic \u00b7 drop")
ACT_COMMENT = "human: repair retry"
REASSIGN_COMMENT_PREFIX = "human: repair reassign "

# A helper card is legacy plumbing (e.g. the changes card the merge gate files when the owner asks
# for changes), not something a human plans around: board_view.collect
# excludes it.
#
# A SUBSTRING test for the marker anywhere in the body let a worker that edits its own card body
# waive checks by writing seven characters. So helper status is a structured fact, and `is_helper`
# below is the only implementation:
#
#  1. STRUCTURE. The marker must be the exact line the retired merge gate wrote, on a
#     line of its own, within the first HELPER_MARKER_MAX_LINES lines (the window the writer
#     controls). A mention in prose, in a code fence, after other text, or further down is not
#     a marker.
#  (A second, created_by provenance check served only the retired quality gate.)
HELPER_MARKER = "helper-of:"
# The `--created-by` the merge gate stamps on the helper cards it files. Not a
# legacy profile name, so no role can be it.
HELPER_CREATED_BY = "capsule-merge-gate"
# How far into the body the marker may sit. merge_gate writes it as line 1; the
# slack is for a future writer that puts a `<!-- sprint-meta -->` block first.
HELPER_MARKER_MAX_LINES = 3
# The whole line, anchored both ends. Two spellings: the HTML-comment form merge_gate writes
# (`<!-- helper-of: t_ab12cd34 -->`) and the bare form (`helper-of: t_ab12cd34`). The id must be
# a real legacy task id (`"t_" + secrets.token_hex(4)`), so a path, a word or an empty value is
# not a marker.
_HELPER_LINE_RE = re.compile(r"^(?:<!--[ \t]*)?helper-of:[ \t]+t_[0-9a-f]{1,32}[ \t]*(?:-->[ \t]*)?$")


def helper_marker_line(body):
    """The 1-based line number of the merge gate's own helper marker in `body`,
    or 0 when the body carries no marker in the writer's window. Never raises."""
    for i, line in enumerate(str(body or "").splitlines()[:HELPER_MARKER_MAX_LINES], 1):
        if _HELPER_LINE_RE.match(line):
            return i
    return 0


def is_helper_body(body):
    """True when `body` carries the structured marker (part 1 only)."""
    return helper_marker_line(body) > 0


def _task_dict(obj):
    """The `task` mapping of a `Card`, a raw `show --json` dict, or a task dict
    already unwrapped. `{}` for anything else, so callers never guard."""
    if isinstance(obj, Card):
        return obj.task
    if isinstance(obj, dict):
        inner = obj.get("task")
        return inner if isinstance(inner, dict) else obj
    return getattr(obj, "task", None) or {}


def is_helper(card):
    """Is `card` a helper card? The one implementation of the marker rule."""
    return is_helper_body(_task_dict(card).get("body"))

# --- the column mapping ------------------------------------------------------
# The one place a legacy status becomes a board column. First match wins, in this order;
# `board_view.py` renders it and nothing else maps a status to a heading. `archived` matches
# nothing: archived cards are not rendered.
#
#  - `Needs repair`/`Blocked` come FIRST. `is_parked` covers `triage` as well as `blocked`, so
#    with Backlog first a card the unblock-loop breaker parked in `triage` would render as
#    backlog and hide its reason.
#  - `Blocked` is not bare `is_parked`: a fresh intake card is `triage` with no `BLOCKED:`
#    comment (reason ""), and a `blocked` card whose trail is spent also reports "". The first
#    must fall through to Backlog, the second must still render. Status OR a live reason covers
#    both.
#  - `Backlog`/`Spec Ready`/`Approved` also claim a `ready` card that is an `is_source_card`:
#    legacy `kanban create` stamps `status: ready` on every intake card, so a never-dispatched
#    source card must be treated like `triage`/`todo` or the approval step never
#    queues it. `Sprint` keeps `ready` cards that are NOT source cards: the impl/verify/lead
#    cards a dispatch creates (they carry an assignee), plus a source card mid-archive that
#    still shows a `bridge: dispatched` comment.
#  - `is_source_card` is the ONE gate for every status this predicate touches, not just `ready`:
#    a dispatch-created chain card in `todo` (verify, lead, an epic task/review card) that
#    carries a `spec: Specs/<slug>.md` line and a stamped approval would otherwise render as
#    Approved, and the approval step would archive it out from under its own children. A card
#    with an assignee or a `bridge: dispatched` comment can never be pending.
#
#    A positive identity check (`created_by == "intake"`) is deliberately NOT required: a card
#    filed by a human running the legacy board `kanban create` CLI directly, or an older pre-intake
#    card, is a legitimate source card with no such stamp. The absence rule (no assignee, no
#    dispatched marker) is the one that dispatch-created cards can never satisfy.
def _is_pending(c):
    """True for a card the Backlog/Spec Ready/Approved trio should still consider: an
    undispatched source card (see `Card.is_source_card`) in any classic pre-dispatch status
    (`triage`, `todo`, `ready`). A dispatch-created chain card (an assignee, or a
    `bridge: dispatched` comment) is never pending, whatever its status."""
    return (c.legacy_status in ("triage", "todo", "ready")
            and c.is_source_card)


COLUMNS = (
    ("## \U0001f6e0 Needs repair", lambda c: c.is_parked and c.current_block_reason.startswith(NEEDS_REPAIR_PREFIX)),
    ("## ⏸ Blocked",          lambda c: c.legacy_status == "blocked"
     or (c.is_parked and bool(c.current_block_reason))),
    ("## \U0001f4e5 Backlog",      lambda c: _is_pending(c) and not c.spec_slug),
    ("## \U0001f52c Spec Ready",   lambda c: _is_pending(c) and bool(c.spec_slug) and not c.spec_approved),
    ("## ✅ Approved",         lambda c: _is_pending(c) and bool(c.spec_slug) and c.spec_approved),
    ("## \U0001f3c3 Sprint",       lambda c: c.legacy_status == "ready" and not c.is_source_card),
    ("## \U0001f528 In Progress",  lambda c: c.legacy_status == "running"),
    ("## \U0001f440 Review",       lambda c: c.legacy_status == "review"),
    ("## ✅ Done",             lambda c: c.legacy_status == "done"),
)
COLUMN_NAMES = tuple(h for h, _ in COLUMNS)


def column_of(card):
    """The column heading `card` renders under, or None when no column claims it (an archived
    card). A predicate that raises is treated as "no match": the view must never die because
    one card is shaped oddly."""
    for heading, pred in COLUMNS:
        try:
            if pred(card):
                return heading
        except Exception:
            continue
    return None


class BoardNotFound(BoardError):
    """No boards/<name>/kanban.db."""


# The ledger hook's signature, as a docstring constant rather than a name bound to None
# pretending to be a type alias.
LEDGER_HOOK_SIG = "Callable[[board: str, task_id: str, entry: dict], None]"


def no_ledger(board: str, task_id: str, entry: dict) -> None:
    """The explicit opt-out hook. Does nothing, returns None. Pass it as
    `open_board(..., ledger=no_ledger)` in a test that must not write."""


def _default_ledger(board: str, task_id: str, entry: dict):
    """The hook every Board gets unless a caller overrides it. Imported lazily so board.py keeps
    no import-time dependency on the vault, and so `ledger` may import `events` and `vault_git`
    without a cycle.

    A `task_id` that does not match `TASK_ID_RE` is a silent no-op HERE: a readable non-legacy
    id (a fixture, a synthetic card) is legitimate from this hook's point of view.
    `ledger.append`'s own stderr line stays for a caller that reaches it directly, the
    path-safety guard."""
    if not TASK_ID_RE.fullmatch(str(task_id or "")):
        return None
    import ledger
    return ledger.append(board, task_id, entry)


def _bridge():
    """board_bridge lazily: it imports this module, and it owns
    LEGACY_BASE / run_legacy_cli - the subprocess primitive stays in one place."""
    import board_bridge as bb
    return bb


def _gate():
    import merge_gate as mg   # lazy: merge_gate imports this module
    return mg


def lock_path(board_name, task_id, state_dir=None):
    """`<state>/locks/<board>-<task_id>.lock`: the path the sync tick and intake already
    exclude each other on (RUNBOOK "Merge gate"). It must not move. The approve scripts do not
    take it."""
    root = Path(state_dir) if state_dir else _gate().state_dir(create=True)
    return root / "locks" / f"{board_name}-{task_id}.lock"


def _accepts_env(runner):
    """Whether `runner` takes the (argv, env) form. Decided once, by signature: many tests inject
    `lambda argv: ...`, and catching TypeError instead would swallow a TypeError raised inside
    the runner itself."""
    try:
        params = list(inspect.signature(runner).parameters.values())
    except (TypeError, ValueError):
        return True   # builtins/C callables: assume the real two-arg shape
    if any(p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD) for p in params):
        return True
    named = [p for p in params
             if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)]
    return len(named) >= 2


def board_names(boards_dir=None):
    """Board names holding a kanban.db, sorted. A directory without one is
    skipped - what repair and decisions.blocked_decisions:151 do."""
    root = Path(boards_dir or BOARDS_DIR)
    try:
        return sorted(p.parent.name for p in root.glob("*/kanban.db"))
    except OSError:
        return []


def intake_board_names():
    """The board names the Intake grammar accepts, sorted: Capsule boards holding a kanban.db,
    plus the legacy ones only when LEGACY_PRESENT=1. Read-only; the board
    write paths stay on board_names."""
    names = set(board_names(board_fence.capsule_boards_dir()))
    if legacy_present():
        names.update(board_names())
    return sorted(names)


def open_board(name, *, runner=None, boards_dir=None, ledger=None, env_extra=None):
    return Board(name, runner=runner, boards_dir=boards_dir, ledger=ledger, env_extra=env_extra)


def ensure_board(name, *, runner=None, boards_dir=None, ledger=None, env_extra=None):
    """`kanban boards new <name>`, idempotent: "already exists" is swallowed,
    exactly as board_bridge._ensure_board:1305-1310 does."""
    b = open_board(name, runner=runner, boards_dir=boards_dir, ledger=ledger, env_extra=env_extra)
    try:
        b._raw_run(["boards", "new", b.name], with_board_flag=False)
    except Exception:
        pass
    return b


@dataclass(frozen=True)
class CardRef:
    """One row of `list --json`."""
    id: str
    status: str
    assignee: str
    title: str


def outcome_of(run):
    return run.get("outcome") or run.get("status")


def ended(run, now):
    """`run["ended_at"]`, or `now` when the run crashed hard enough never to
    stamp one - the old system's crash path closes a run with an outcome only."""
    stamp = run.get("ended_at")
    return now if stamp is None else stamp


def is_repair_reset_comment(body):
    """True for the ONE comment shape that resets the derived failure counter: `ACT_COMMENT`
    (`"human: repair retry"`) or `REASSIGN_COMMENT_PREFIX<role>` (`"human: repair reassign
    <role>"`), compared after `.strip()` and case-folding. An exact `==` once ignored `Human:
    repair retry` and a trailing space, so a human who typed it slightly differently got no
    reset. Normalising fixes both without widening what counts: `human: repair retry please`
    does NOT match, and `reassign` keeps its prefix rule (any role resets, since only the CLI
    controls the role).

    THE ONLY APPROVED RESET is a hand-written comment matching this predicate (the old
    repair-card.sh is gone; incidents and the failure-limit park replace it). A plain status
    reset (SQL `UPDATE tasks SET status='ready', block_recurrences=0`), a cleared
    `current_run_id`, or a later `completed` run does NOT reset the counter, by design."""
    norm = str(body or "").strip().casefold()
    return norm == ACT_COMMENT.casefold() or norm.startswith(REASSIGN_COMMENT_PREFIX.casefold())


def last_human_retry_at(raw):
    """`created_at` of the newest reset-shaped comment (`is_repair_reset_
    comment`), or None. A human reply gives the card a fresh failure cycle."""
    times = [c.get("created_at") for c in (raw.get("comments") or [])
             if is_repair_reset_comment(c.get("body")) and c.get("created_at") is not None]
    return max(times) if times else None


def failures(raw):
    """Failed runs, oldest first, restricted to those that ended strictly after the newest human
    retry/reassign comment. Unstamped runs (no `ended_at`) sort LAST: they are the newest thing
    that happened to the card, and `fails[-1]` must be the run a park reports on. The cutoff is
    strict (`>`), so a run that ended in the same second as the human comment counts as
    pre-retry."""
    fails = [r for r in (raw.get("runs") or []) if outcome_of(r) in FAILURE_OUTCOMES]
    fails = sorted(fails, key=lambda r: (r.get("ended_at") is None, r.get("ended_at") or 0))
    cutoff = last_human_retry_at(raw)
    if cutoff is not None:
        fails = [r for r in fails if r.get("ended_at") is None or r["ended_at"] > cutoff]
    return fails


def newest_stamped_failure(raw):
    """`ended_at` of the newest failed run that actually stamped one, or None. Only stamped
    failures can retire a block comment: coalescing an unstamped one to `now` made every hold
    look older than "the newest failure" on every tick."""
    stamps = [r["ended_at"] for r in failures(raw) if r.get("ended_at") is not None]
    return max(stamps) if stamps else None


def block_comments(raw):
    """`(created_at, i, reason)` for each `BLOCKED: <reason>` comment, oldest first. `i` is the
    trail position, which breaks a `created_at` tie (later = newer): `show --json` gives comments
    no id and no sub-second stamp."""
    out = []
    for i, c in enumerate(raw.get("comments") or []):
        body = str(c.get("body") or "")
        ts = c.get("created_at")
        if ts is not None and body.startswith(BLOCKED_COMMENT_PREFIX):
            out.append((ts, i, body[len(BLOCKED_COMMENT_PREFIX):]))
    return sorted(out)


def block_events(raw):
    """`(created_at, i, reason)` for each block `task_events` row that carries a reason, oldest
    first. `i` is the position in `events`, which breaks a `created_at` tie as the trail position
    does for comments (`list_events` orders by `created_at, id`)."""
    out = []
    for i, e in enumerate(raw.get("events") or []):
        if e.get("kind") not in BLOCK_EVENT_KINDS:
            continue
        ts = e.get("created_at")
        reason = str((e.get("payload") or {}).get("reason") or "")
        if ts is not None and reason:
            out.append((ts, i, reason))
    return sorted(out)


def _newest_unblock(raw):
    """`(created_at, 1, position)` of the newest `unblocked` task_event, or None. The old CLI's own
    sticky-block predicate, `_has_sticky_block`, looks only at the most recent of ('blocked',
    'unblocked') `task_events` to decide whether a block is still sticky; `unblock_task` appends
    the `unblocked` row on every successful unblock, whichever path parked the card. The tuple
    shares block_events' `(created_at, source_rank=1, position)` shape, so it compares directly
    against a `block_records` row."""
    best = None
    for i, e in enumerate(raw.get("events") or []):
        if e.get("kind") != "unblocked":
            continue
        ts = e.get("created_at")
        if ts is None:
            continue
        cand = (ts, 1, i)
        if best is None or cand > best:
            best = cand
    return best


def block_records(raw):
    """Every LIVE record of "why this card is parked", from BOTH block paths, oldest first, as
    `(created_at, source_rank, position, reason)`. `source_rank` puts an event after a comment at
    the same second, the order `_cmd_block` writes them in (comment, then block_task's event). A
    CLI block therefore contributes the same reason twice and reads back the same either way; a
    tool block contributes only the event.

    Retirement: a record older than the newest `unblocked` task_event (`_newest_unblock`) is
    dropped before anything else sees it, since the old system would no longer call the card sticky-
    blocked on it. The comparison reuses this function's `(created_at, source_rank, position)`
    sort key against the unblock's `(created_at, 1, position)`: a strictly lesser record is
    retired. At a tied `created_at` a comment (rank 0) is always retired; a block EVENT tied with
    the unblock is retired only if it sits earlier in `raw["events"]`."""
    rows = [(ts, 0, i, reason) for ts, i, reason in block_comments(raw)]
    rows += [(ts, 1, i, reason) for ts, i, reason in block_events(raw)]
    rows.sort()
    marker = _newest_unblock(raw)
    if marker is not None:
        rows = [r for r in rows if r[:3] >= marker]
    return rows


def is_parked(raw):
    return (raw.get("task") or {}).get("status") in PARKED_STATUSES


def block_reason(raw):
    """The reason the card is parked with right now, or "" if it is free to act on. NOT
    `latest_summary`: the old CLI defines that as the newest non-null run summary, `unblock` never
    clears it and a crashed run never overwrites it, so it outlives the block it described.

    Two sources, because the old CLI has two block paths: the `BLOCKED:` comments it posts and
    the `blocked`/`block_loop_detected` `task_events` rows `block_task` writes on both (see
    BLOCK_EVENT_KINDS). A worker's block comes through the MCP tool and leaves no comment, so the
    trail alone is empty for every real DECISION: and READY: card.

    `block_records` has already dropped every record a later `unblocked` task_event retired, so
    two rules apply to what is left, in order:
    1. A record older than the newest *stamped* failure is spent: the old CLI has since let the card
       run again. (`>=`: a block is written in the same epoch second as the failure it reacts to.)
    2. Of what is left, a foreign record wins over ours even when ours is newer; otherwise the
       newest wins. `_cmd_block` posts the comment before block_task, so a block the old CLI refused
       leaves a comment of ours newer than the foreign block it failed against.
    """
    if not is_parked(raw):
        return ""
    stamped = newest_stamped_failure(raw)
    blocks = block_records(raw)
    foreign = [b for b in blocks if not b[3].startswith((HOLD_PREFIX, NEEDS_REPAIR_PREFIX))]
    live = [b for b in blocks if stamped is None or b[0] >= stamped]
    if not (foreign or live):
        return ""
    return (foreign or live)[-1][3]


@dataclass(frozen=True)
class NewCard:
    """Everything `kanban create` takes. `argv()` renders the canonical flag
    order; every current builder in board_bridge / merge_gate is a NewCard."""
    title: str
    body: str = ""
    assignee: str = None
    parent: str = None
    workspace: str = None
    branch: str = None
    max_retries: int = None
    model: str = None
    provider: str = None
    idempotency_key: str = None
    created_by: str = None

    def argv(self):
        out = ["create", self.title]
        for flag, value in (("--body", self.body or None),
                            ("--assignee", self.assignee),
                            ("--parent", self.parent),
                            ("--workspace", self.workspace),
                            ("--branch", self.branch),
                            ("--max-retries", self.max_retries),
                            ("--model", self.model),
                            ("--provider", self.provider),
                            ("--idempotency-key", self.idempotency_key),
                            ("--created-by", self.created_by)):
            if value is not None:
                out += [flag, str(value)]
        return out + ["--json"]


@dataclass(frozen=True)
class Card:
    """One `show --json` payload. `raw` is kept verbatim so the derivations above stay the single
    implementation and callers that want the dict (board_bridge's flattened task, repair's
    planners) can have it."""
    board: str
    raw: dict

    @property
    def task(self):
        return self.raw.get("task") or {}

    @property
    def latest_summary(self):
        """The old system's newest-run-summary alias - a sibling of `task`, NOT
        tasks.result, and NOT a block reason. See `block_reason`."""
        return str(self.raw.get("latest_summary") or "")

    @property
    def parents(self):
        return tuple(self.raw.get("parents") or [])

    @property
    def children(self):
        return tuple(self.raw.get("children") or [])

    @property
    def comments(self):
        return tuple(self.raw.get("comments") or [])

    @property
    def events(self):
        return tuple(self.raw.get("events") or [])

    @property
    def runs(self):
        return tuple(self.raw.get("runs") or [])

    @property
    def id(self):
        return str(self.task.get("id") or "")

    @property
    def title(self):
        return str(self.task.get("title") or "")

    @property
    def assignee(self):
        return self.task.get("assignee")

    @property
    def body(self):
        return str(self.task.get("body") or "")

    @property
    def workspace_path(self):
        return self.task.get("workspace_path")

    @property
    def legacy_status(self):
        """The raw legacy vocabulary (triage|todo|ready|running|blocked|review|done|archived),
        never a Capsule column."""
        return str(self.task.get("status") or "")

    @property
    def current_block_reason(self):
        return block_reason(self.raw)

    @property
    def is_parked(self):
        return is_parked(self.raw)

    @property
    def spec_slug(self):
        """The slug of the spec this card is built from, or "". Body first: a dispatched card
        carries `spec: Specs/<slug>.md` in its sprint-meta block (only build_meta_block writes a
        body, so no marker is needed), and a `triage` card the planner has specced carries it as a
        comment. A COMMENT counts only with `SPEC_LINK_MARK`: any worker can comment. The newest
        MARKED comment wins, so a re-spec supersedes and a later unmarked comment supersedes
        nothing."""
        m = SPEC_LINE_RE.search(self.body)
        if m:
            return m.group(1)
        for c in reversed(self.comments):
            m = SPEC_COMMENT_RE.search(str(c.get("body") or ""))
            if m:
                return m.group(1)
        return ""

    @property
    def spec_approved(self):
        """True when a host-side spec-approval stamp exists for `spec_slug`
        (merge_gate.read_spec_approval). No slug -> False; an unreadable state root -> False, the
        fail-closed direction for an approval."""
        slug = self.spec_slug
        if not slug:
            return False
        try:
            return _gate().read_spec_approval(slug) is not None
        except Exception:
            return False

    @property
    def failures_since_human_retry(self):
        return tuple(failures(self.raw))

    @property
    def is_source_card(self):
        """Is this an intake-created source card? Legacy `kanban create` stamps `status: ready` on
        creation, so a fresh intake card (no assignee, never dispatched) arrives as `ready`, not
        `triage`/`todo`. A source card has no assignee (dispatch's impl/verify/lead cards always
        get one) and no `bridge: dispatched <id>` comment (the approval step's marker, posted right
        before the source card is archived)."""
        if self.assignee:
            return False
        for c in self.comments:
            if str(c.get("body") or "").startswith("bridge: dispatched"):
                return False
        return True


def raw_argv(name, argv):
    """`["kanban", "--board", name] + argv`, so board.py stays the only module that constructs
    `kanban` argv. No production caller renders argv this way: `dispatch` creates through
    `Board.create`, which pins the env. What is left is board_bridge's `build_*_argv` helpers,
    pure builders the tests assert the rendered argv against; they take no runner and touch no
    environment."""
    return ["kanban", "--board", str(name)] + list(argv)


class Board:
    """One board. Construction is cheap and stats nothing: the first call is
    what raises BoardNotFound."""

    def __init__(self, name, *, runner=None, boards_dir=None, ledger=None, env_extra=None):
        if not BOARD_RE.fullmatch(str(name or "")):
            raise ValueError(f"invalid board name {name!r}: must match {BOARD_RE.pattern}")
        self.name = str(name)
        self._boards_dir = Path(boards_dir or BOARDS_DIR)
        self._runner = runner
        self._runner_takes_env = _accepts_env(runner) if runner is not None else True
        self._ledger = ledger if ledger is not None else _default_ledger
        self._env_extra = dict(env_extra or {})

    def __repr__(self):
        return f"Board({self.name!r})"

    @contextlib.contextmanager
    def lock(self, task_id, *, state_dir=None):
        """Non-blocking advisory lock on one card. Raises Locked when another process holds it; the
        caller decides what that means (the sync tick skips the card; intake retries next tick)."""
        path = lock_path(self.name, str(task_id), state_dir)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with open(path, "a+") as fh:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise Locked(f"{self.name} {task_id} is locked elsewhere") from exc
            try:
                yield self
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def emit(self, event, *, spool=None):
        """The one path from a board write to events.log(). `event` is the events.log kwargs minus
        `board`, which is always this board. Existing events.log(...) calls are not moved in here,
        which would change which event a code path emits; only the ledger and this module's own
        tests use it so far."""
        e = dict(event)
        return events.log(e.pop("kind"), e.pop("source"), e.pop("text", ""),
                          board=self.name, spool=spool, **e)

    def append_ledger(self, task_id, entry):
        """Call the ledger hook and return whatever it returns (the vault-relative path, or None
        when nothing was written). Never raises, the events.log discipline: the sync tick must not
        die because of the trail.

        The entry shape: {"v": 1, "id": str (32 hex), "ts": float, "board": str, "task_id": str,
         "kind": str, "source": str, legacy_names.LEDGER_EVENT_KEY: int | None,
         "summary": str, "payload": dict}
        Key: (board, task_id, that event id) when non-null, else
        (board, task_id, id)."""
        try:
            return self._ledger(self.name, str(task_id), entry)
        except Exception as exc:
            print(f"board: ledger not written ({exc})", file=sys.stderr)
            return None

    @property
    def db_path(self):
        if self.name == "default":
            return DEFAULT_DB
        return self._boards_dir / self.name / "kanban.db"

    def _env(self):
        env = dict(os.environ)
        env.update(self._env_extra)
        env["LEGACY_KANBAN_DB"] = str(self.db_path)      # win the precedence race deliberately
        env["LEGACY_KANBAN_BOARD"] = self.name
        # A worker's ownership pins are not ours. WORKSPACES_ROOT is in the list because legacy
        # honours it at highest precedence too; harmless today (Capsule uses `worktree:` / `dir:`
        # workspaces, never `scratch`), but it belongs in the same sweep.
        # (the strip list covers both name generations, capsule/legacy_names.py)
        for k in legacy_names.OLD_KANBAN_ENV_NAMES + legacy_names.NEW_KANBAN_ENV_NAMES[2:]:
            env.pop(k, None)
        return env

    def _raw_run(self, argv, *, with_board_flag=True):
        bb = _bridge()
        head = ["kanban"] + (["--board", self.name] if with_board_flag else [])
        full = bb.LEGACY_BASE + head + [str(a) for a in argv]
        env = self._env()
        try:
            if self._runner_takes_env:
                return bb.run_legacy_cli(full, self._runner, env=env)
            return bb.run_legacy_cli(full, self._runner)
        except BoardError:
            raise
        except subprocess.CalledProcessError as exc:
            raise LegacyCliError(f"kanban {argv[0] if argv else '?'}: exit {exc.returncode}",
                                 argv=full, returncode=exc.returncode,
                                 stderr=(exc.stderr or "")) from exc
        except subprocess.TimeoutExpired as exc:
            raise LegacyCliError(f"kanban {argv[0] if argv else '?'}: timed out",
                                 argv=full, stderr=str(exc)) from exc
        except Exception as exc:
            raise LegacyCliError(f"kanban {argv[0] if argv else '?'}: {exc}",
                                 argv=full, stderr=str(exc)) from exc

    def _run(self, argv):
        board_fence.guard(self._boards_dir, self.name, argv[0], BoardFrozen)    # every write
        return self._raw_run(argv)

    def _run_json_text(self, out, *, what):
        try:
            return json.loads(out or "")
        except ValueError as exc:
            raise LegacyOutputInvalid(f"kanban {what}: unparseable output ({exc})",
                                      stderr=str(out)[:200]) from exc

    def _run_json(self, argv, *, what):
        return self._run_json_text(self._raw_run(argv), what=what)

    # -- reads ---------------------------------------------------------------
    def show(self, task_id):
        data = self._run_json(["show", str(task_id), "--json"], what="show")
        if not isinstance(data, dict) or not (data.get("task") or {}).get("id"):
            raise CardNotFound(f"{self.name}: no card {task_id}")
        return Card(self.name, data)

    def try_show(self, task_id):
        """None on any read failure - the `{}`-or-None contract
        the bridge's and quality_gate's card reads rely on."""
        try:
            return self.show(task_id)
        except Exception:
            return None

    def list(self, *, statuses=None):
        rows = self._run_json(["list", "--json"], what="list")
        if isinstance(rows, dict):
            rows = rows.get("tasks") or []
        out = []
        for r in rows:
            if statuses is not None and r.get("status") not in statuses:
                continue
            out.append(CardRef(id=str(r.get("id") or ""), status=str(r.get("status") or ""),
                               assignee=r.get("assignee"), title=str(r.get("title") or "")))
        return out

    # -- writes: one method per kanban verb Capsule uses today ---------------
    def create(self, card):
        out = self._run(card.argv())
        data = self._run_json_text(out, what="create")
        tid = data.get("id") or data.get("task_id")
        if not tid:
            raise LegacyCliError(f"kanban create: no task id in {out!r}", argv=card.argv())
        return str(tid)

    def comment(self, task_id, body):
        self._run(["comment", str(task_id), body])

    def block(self, task_id, reason, *, kind, card=None):
        """`block_task` fires only from running/ready, and `_cmd_block` appends the `BLOCKED:
        <reason>` comment BEFORE it refuses: one park comment per tick, forever. So the status is
        checked here, before any subprocess. `card=` skips the extra read when the caller has it."""
        board_fence.guard(self._boards_dir, self.name, "block", BoardFrozen)
        c = card if card is not None else self.show(task_id)
        if c.legacy_status not in BLOCKABLE_STATUSES:
            raise TransitionRefused(
                f"{self.name} {task_id}: cannot block from status {c.legacy_status!r} "
                f"(blockable: {sorted(BLOCKABLE_STATUSES)})")
        self._run(["block", str(task_id), reason, "--kind", str(kind)])

    def unblock(self, task_id):
        """`unblock_task` fires only from `blocked`/`scheduled`. Unlike `block()`, this does NOT
        pre-check status with an extra `show`: unblock is the hot path (repair retries, the
        usage-guard's rate-limit victims, decisions.answer) and nearly every call targets a
        genuinely blocked card. Instead, if the legacy `unblock` verb refuses AND the card turns out
        to be `triage`, it re-raises as a distinct, actionable `TransitionRefused` instead of the
        generic 'not blocked/scheduled?' `LegacyCliError`.

        `triage` (the block-loop detector's landing column, event `block_loop_detected`) is
        deliberately NOT unblockable: the old CLI offers no verb that resumes a `triage` card into
        the work pool. `promote_task` gates on ('todo', 'blocked') even with --force;
        `complete_task` on ('running', 'ready', 'blocked', 'review'); `reclaim_task` needs an
        active claim. Only `specify_triage_task` and `decompose_triage_task` leave `triage`, and
        both land on `todo` via an auxiliary-LLM rewrite of title/body: right for an unspecified
        intake card, wrong (and destructive) for a fully specified READY:/DECISION: handoff card.
        So a caller (decisions.answer, merge_gate) can tell a human manual old-side recovery is
        needed, rather than retry forever as if it were an infra hiccup."""
        try:
            self._run(["unblock", str(task_id)])
        except LegacyCliError as exc:
            try:
                status = self.show(task_id).legacy_status
            except Exception:
                status = None
            if status == "triage":
                raise TransitionRefused(
                    f"{self.name} {task_id}: cannot unblock from status 'triage' "
                    f"(unblockable: {sorted(UNBLOCKABLE_STATUSES)}) - the old system has no "
                    "CLI verb that resumes a triage-parked card (see "
                    "block_loop_detected, kanban_db.py:~6406); manual old-system-side "
                    "recovery is required") from exc
            raise

    def complete(self, task_id, summary, metadata=None):
        argv = ["complete", str(task_id), "--summary", summary]
        if metadata is not None:
            argv += ["--metadata", json.dumps(metadata)]
        self._run(argv)

    def set_model(self, task_id, model, *, provider=None):
        """`model="none"` clears the override."""
        argv = ["set-model", str(task_id), str(model)]
        if provider:
            argv += ["--provider", str(provider)]
        self._run(argv)

    def archive(self, task_id):
        self._run(["archive", str(task_id)])


def find_card(task_id, *, boards_dir=None, runner=None, env_extra=None):
    """(Board, Card) for the board holding `task_id`, or None after scanning every board. Task ids
    are unique hex, so one board owns it; a lookup error on one board is treated as 'not here'."""
    for name in board_names(boards_dir):
        b = open_board(name, runner=runner, boards_dir=boards_dir, env_extra=env_extra)
        card = b.try_show(task_id)
        if card is not None and card.id == str(task_id):
            return b, card
    return None
