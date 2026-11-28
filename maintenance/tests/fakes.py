"""Scripted, in-memory fakes for the Task 4 end-to-end pipeline test.

FakeLegacyCli implements just enough of the legacy kanban CLI subset that
board_bridge / merge_gate / quality_gate / decisions actually call, plus
helper methods that play the human/reviewer/qa/lead roles so the test can
drive a whole dispatch -> review -> qa -> gate -> merge -> sync flow with no
model and no subprocess. It is deliberately narrow: only the subcommands the
real code issues are implemented, and every state transition is guarded the
way the legacy CLI itself guards it (no request-review on a card that isn't
running/ready, no unblock on a card that isn't blocked, ...) so a test built
on this fake cannot pass on a sequence the legacy CLI would have refused.

Blocks come in two shapes here because the legacy CLI has two block paths and they
leave different records behind: `_block` is the `kanban_block` MCP tool (a
`blocked` task_event and a run summary, NO comment) and is what every worker
role helper uses, because that is what every SOUL calls; `cli_block` is
legacy `kanban block` (the `BLOCKED:` comment first, then the same event) and
is what argv reaching `_cli_block` produces, i.e. what merge_gate
(and the retired repair) leave behind. Modelling only the CLI is what hid C2.

FakeSh / FakeGh are small dict-driven callables for the `sh=` / `gh=`
injection points in merge_gate.py.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import board as _board
from capsule import legacy_names  # noqa: E402


def blocked_event(reason, at=1, kind="blocked", block_kind="needs_input"):
    """One `task_events` row as `show --json` renders it (kanban.py:1822-1830).
    `block_task` writes exactly this on every successful block, whichever path
    called it (kanban_db.py:6452-6472)."""
    return {"kind": kind, "run_id": None, "created_at": at,
            "payload": {"reason": reason, "kind": block_kind,
                        "recurrences": 1, "source_status": "ready"}}


def blocked_comment(reason, at=1):
    """The comment only `_cmd_block` posts (legacy kanban.py:2443-2444)."""
    return {"author": legacy_names.NAME, "body": f"BLOCKED: {reason}", "created_at": at}


def block_shapes(reason, at=1):
    """`(label, comments, events)` for each way a blocked card can read back,
    so a test that classifies a block reason can be run against all of them.

    - `tool`: the `kanban_block` MCP tool - the event row and nothing else.
      Every worker SOUL blocks this way, so this is the shape production sees.
    - `cli_comment`: the `BLOCKED:` comment alone, isolating the trail source.
    - `cli`: what the CLI really leaves behind - comment AND event, same reason.
    """
    ev = [blocked_event(reason, at)]
    cm = [blocked_comment(reason, at)]
    return (("tool", [], ev), ("cli_comment", cm, []), ("cli", cm, ev))


def _strip_dash_c(argv):
    """Drop a `-C <path>` pair anywhere in argv so `git -C <wt> fetch ...` and
    `git fetch ...` (run with cwd=<wt> instead) key the same way."""
    out = []
    i = 0
    a = list(argv)
    while i < len(a):
        if a[i] == "-C" and i + 1 < len(a):
            i += 2
            continue
        out.append(a[i])
        i += 1
    return out


class FakeSh:
    """`(argv, cwd=None, timeout=None) -> (rc, out)`.

    `script` maps a tuple prefix of the (``-C``-stripped) argv to a canned
    `(rc, out)` pair (or a `f(argv, cwd) -> (rc, out)` callable). The longest
    matching prefix wins, so a specific `("git", "branch", "--show-current")`
    entry does not collide with a generic `("git", "fetch")` one. Any call
    with no matching prefix returns `(0, "")` - most read-only git calls in
    the gate tolerate an empty answer (see `required=False` reads).
    """

    def __init__(self, script=None):
        self.script = {tuple(k): v for k, v in (script or {}).items()}
        self.calls = []

    def __call__(self, argv, cwd=None, timeout=None):
        self.calls.append(list(argv))
        toks = tuple(_strip_dash_c(argv))
        best = None
        for key in self.script:
            if toks[: len(key)] == key and (best is None or len(key) > len(best)):
                best = key
        if best is None:
            return (0, "")
        val = self.script[best]
        return val(argv, cwd) if callable(val) else val


class FakeGh:
    """`(argv, cwd=None, timeout=None) -> (rc, out)` for `gh pr/repo ...`.

    Starts with no open PR (`gh pr list` answers empty, so the gate creates
    one via `gh pr create` -> `pr_url`). `gh pr view --json state` answers
    OPEN until `gh pr merge` is called, then MERGED.
    """

    def __init__(self, pr_url, checks=None, owner="example-owner", default_branch="main"):
        self.pr_url = pr_url
        self.checks = checks if checks is not None else [{"name": "tests", "bucket": "pass", "state": "SUCCESS"}]
        self.owner = owner
        self.default_branch = default_branch
        self.pr_comments = []
        self._merged = False
        self.calls = []

    def __call__(self, argv, cwd=None, timeout=None):
        self.calls.append(list(argv))
        if argv[:3] == ["gh", "pr", "create"]:
            return (0, self.pr_url + "\n")
        if argv[:3] == ["gh", "pr", "list"]:
            return (0, "" if not self._merged else self.pr_url + "\n")
        if argv[:3] == ["gh", "pr", "view"]:
            joined = " ".join(argv)
            if "comments" in joined:
                return (0, json.dumps({"comments": self.pr_comments}))
            if "headRefName" in joined:
                return (0, "sprint/soft-tick\n")
            return (0, ("MERGED" if self._merged else "OPEN") + "\n")
        if argv[:3] == ["gh", "pr", "checks"]:
            # Real `gh pr checks` does not exit 0 unless every check is
            # green (C1): 1 on a fail/cancel bucket or zero checks, 8 while
            # pending. Match that so the e2e replays reality.
            buckets = {c.get("bucket") for c in self.checks}
            if buckets & {"fail", "cancel"} or not self.checks:
                rc = 1
            elif buckets - {"pass", "skipping"}:
                rc = 8
            else:
                rc = 0
            return (rc, json.dumps(self.checks))
        if argv[:3] == ["gh", "pr", "merge"]:
            self._merged = True
            return (0, "")
        if argv[:3] == ["gh", "repo", "view"]:
            if "defaultBranchRef" in " ".join(argv):
                return (0, self.default_branch + "\n")
            return (0, self.owner + "\n")
        return (1, "unexpected gh " + " ".join(argv))


def _split_argv(args):
    """argv tail (after the subcommand) -> (positional, {flag: value|True})."""
    pos, kv = [], {}
    i = 0
    while i < len(args):
        a = args[i]
        if isinstance(a, str) and a.startswith("--"):
            key = a[2:]
            if i + 1 < len(args) and not (isinstance(args[i + 1], str) and args[i + 1].startswith("--")):
                kv[key] = args[i + 1]
                i += 2
            else:
                kv[key] = True
                i += 1
        else:
            pos.append(a)
            i += 1
    return pos, kv


class FakeLegacyCli:
    """In-memory legacy `kanban` board. Callable as the bridge/gate/hook
    runner: `__call__(argv, env=None) -> str` (the two-arg form is what
    `board_bridge.run_legacy_cli` uses when a caller passes a non-None `env`,
    e.g. the quality hook and decisions.py; merge_gate and board_bridge's
    own callers pass one arg)."""

    def __init__(self, boards_dir=None):
        self.boards_dir = boards_dir
        self.tasks = {}
        self.comments = {}
        self.runs = {}
        self.events = {}
        self.parents = {}
        self.children = {}
        self.created = []  # [{"id", "assignee", "parent"}] in creation order
        self.create_kv = []  # the `kv` dict of each `create` call, in order (recorder only)
        self._n = 0
        self._clock = 0
        self._idem = {}
        self._ledger_board = None  # board name of the last `create` (all creates in a
                                   # dispatch share one board), for ledger_entries()
        self._implementer = {}  # tid -> assignee at request-review time (for request-changes)
        self._current_claim = {}  # tid -> "review"/"ready" source_status of the active claimed run, or None
        self.calls = []

    # -- plumbing ---------------------------------------------------------
    def _tick(self):
        self._clock += 1
        return self._clock

    def _new_id(self):
        self._n += 1
        return f"t_{self._n:08x}"

    def __call__(self, argv, env=None):
        self.calls.append(list(argv))
        i = argv.index("kanban")
        rest = list(argv[i + 1 :])
        board = None
        if rest and rest[0] == "--board":
            board = rest[1]
            rest = rest[2:]
        if not rest:
            return ""
        sub = rest[0]
        pos, kv = _split_argv(rest[1:])
        fn = getattr(self, "_cli_" + sub.replace("-", "_"), None)
        if fn is None:
            return ""  # unimplemented/no-op subcommand (e.g. "boards new <repo>")
        return fn(pos, kv, board) or ""

    def board(self, name="my-app", **kw):
        """A real board.Board wired to this fake as its runner. Preferred over a
        separate FakeBoard: every transition guard lives here once, so a test on
        a Board cannot pass on a sequence the legacy CLI would have refused."""
        kw.setdefault("boards_dir", self.boards_dir)
        return _board.open_board(name, runner=self, **kw)

    def _show_json(self, tid):
        t = self.tasks[tid]
        task = dict(t)
        # Duplicated onto the task dict too (not just the payload's top-level
        # "parents") - a lineage walk is sometimes handed just the task
        # object (e.g. from a caller that already unwrapped `show(id)["task"]`),
        # and the real legacy CLI's `show --json` task record carries its own parents.
        task["parents"] = list(self.parents.get(tid, []))
        return {
            "task": task,
            "latest_summary": t.get("latest_summary"),
            "comments": list(self.comments.get(tid, [])),
            "parents": list(self.parents.get(tid, [])),
            "children": list(self.children.get(tid, [])),
            "runs": list(self.runs.get(tid, [])),
            "events": list(self.events.get(tid, [])),
        }

    def show(self, tid):
        return self._show_json(tid)

    def cards_by_assignee(self):
        """Last-created-wins map of assignee -> task id, for tests that dispatched
        one card per role (impl/verify/finalize)."""
        return {c["assignee"]: c["id"] for c in self.created}

    def ledger_entries(self):
        """The entries `Board.append_ledger` wrote for every card this fake has
        created, in creation order. `append_ledger` goes straight to the real
        ledger hook (this fake is not in that path), so this just reads them
        back the way `ledger.read` callers elsewhere in the suite do."""
        import ledger
        board = self._ledger_board
        if not board:
            return []
        out = []
        for c in self.created:
            out.extend(ledger.read(board, c["id"]))
        return out

    # -- CLI subcommands ----------------------------------------------------
    def _new_card(self, tid, title, kv, assignee, workspace):
        # `_landing_status_after_parents`, not a bare "todo if parent else
        # ready": a card whose parent is created already-done (the epic
        # lane's verify/finalize and round-2 review cards, created after
        # their parent completed) must land `ready`/`todo` off the PARENT'S
        # actual status, exactly as the real legacy CLI's create does, not off mere
        # presence of a parent id.
        return {
            "id": tid,
            "title": title,
            "body": kv.get("body", "") or "",
            "assignee": assignee,
            "status": self._landing_status_after_parents(tid),
            "workspace_path": workspace,
            "branch": kv.get("branch", "") or "",
            "model": kv.get("model", "") or "",
            "block_kind": None,
        }

    def _cli_create(self, pos, kv, board):
        title = pos[0] if pos else ""
        idem = kv.get("idempotency-key")
        if idem and idem in self._idem:
            return json.dumps({"id": self._idem[idem]})
        tid = self._new_id()
        parent = kv.get("parent")
        assignee = kv.get("assignee", "") or ""
        workspace = kv.get("workspace", "") or ""
        if workspace.startswith("worktree:"):
            workspace = workspace[len("worktree:") :]
        elif workspace.startswith("dir:"):
            workspace = workspace[len("dir:") :]
        self.parents[tid] = [parent] if parent else []
        self.tasks[tid] = self._new_card(tid, title, kv, assignee, workspace)
        self.create_kv.append(dict(kv))
        if board:
            self._ledger_board = board
        self.children.setdefault(tid, [])
        if parent:
            self.children.setdefault(parent, []).append(tid)
        self.comments.setdefault(tid, [])
        self.runs.setdefault(tid, [])
        self.events.setdefault(tid, [])
        self._implementer[tid] = assignee
        if idem:
            self._idem[idem] = tid
        self.created.append({"id": tid, "assignee": assignee, "parent": parent})
        return json.dumps({"id": tid})

    def _cli_show(self, pos, kv, board):
        if pos[0] not in self.tasks:
            return "{}"      # card absent -> board.Board.show raises CardNotFound,
                             # which is what a real `kanban show <missing>` means
        return json.dumps(self._show_json(pos[0]))

    def _cli_list(self, pos, kv, board):
        return json.dumps([dict(t) for t in self.tasks.values()])

    def _cli_comment(self, pos, kv, board):
        self._comment(pos[0], pos[1] if len(pos) > 1 else "")
        return ""

    def _cli_block(self, pos, kv, board):
        tid = pos[0]
        reason = " ".join(str(x) for x in pos[1:])
        # argv reached us, so this is the CLI: comment first, then the guard.
        self.cli_block(tid, reason, kv.get("kind"))
        return ""

    def _cli_unblock(self, pos, kv, board):
        self._unblock(pos[0])
        return ""

    def _cli_complete(self, pos, kv, board):
        metadata = json.loads(kv["metadata"]) if kv.get("metadata") else None
        self._complete(pos[0], kv.get("summary", "") or "", metadata=metadata)
        return ""

    def _cli_request_changes(self, pos, kv, board):
        reason = pos[1] if len(pos) > 1 else ""
        self._request_changes(pos[0], reason)
        return ""

    def _cli_archive(self, pos, kv, board):
        self.tasks[pos[0]]["status"] = "archived"
        return ""

    def _cli_set_model(self, pos, kv, board):
        return ""

    # -- state mutators (shared by the CLI parser and the role helpers) -----
    def _comment(self, tid, text):
        self.comments.setdefault(tid, []).append({"author": "bridge", "body": text, "created_at": self._tick()})

    def _block(self, tid, reason, kind=None):
        """The TOOL path: `kanban_block` (tools/kanban_tools.py:866-871) calls
        kb.block_task directly, which writes a run summary and one `blocked`
        `task_events` row (kanban_db.py:6452-6472) and posts NO comment. This
        is how every worker SOUL blocks, so it is the default shape here -
        `cli_block` below is the other one."""
        t = self.tasks[tid]
        if t["status"] not in ("running", "ready"):
            raise ValueError(f"cannot block {tid}: not running/ready (status {t['status']!r})")
        t["status"] = "blocked"
        t["latest_summary"] = reason
        t["block_kind"] = kind
        self.events.setdefault(tid, []).append(
            {"kind": "blocked", "run_id": None, "created_at": self._tick(),
             "payload": {"reason": reason, "kind": kind, "recurrences": 1,
                         "source_status": "ready"}}
        )

    def cli_block(self, tid, reason, kind=None):
        """The CLI path: `_cmd_block` (legacy kanban.py:2443-2450) posts
        `BLOCKED: <reason>` and THEN calls block_task - so a block the legacy CLI
        refuses still leaves the comment behind. merge_gate (and the retired
        repair) block through the CLI; workers do not."""
        if reason:
            self._comment(tid, f"BLOCKED: {reason}")
        self._block(tid, reason, kind)

    def _unblock(self, tid):
        """The CLI (`_cli_unblock`) and every unblock caller share this: real
        `unblock_task` (kanban_db.py:6901) always appends an `unblocked`
        task_event (kanban_db.py:6943-6958) alongside the status write, no
        matter which path parked the card - board.block_records relies on it
        to retire a foreign block once it has been lifted."""
        t = self.tasks[tid]
        if t["status"] != "blocked":
            raise ValueError(f"cannot unblock {tid}: not blocked (status {t['status']!r})")
        t["status"] = "ready"
        t["block_kind"] = None
        self.events.setdefault(tid, []).append(
            {"kind": "unblocked", "run_id": None, "created_at": self._tick(), "payload": None}
        )

    def _landing_status_after_parents(self, tid):
        """Mirrors legacy kanban_db.py `_landing_status_after_parents` (~line
        6880): 'todo' if any parent isn't done/archived yet, else 'ready'."""
        parents = self.parents.get(tid, [])
        if parents and not all(self.tasks[p]["status"] in ("done", "archived") for p in parents):
            return "todo"
        return "ready"

    def _complete(self, tid, summary, metadata=None):
        """Mirrors kanban_db.py `complete_task` (~line 5363): accepts
        running|ready|blocked|review -> done."""
        t = self.tasks[tid]
        if t["status"] not in ("running", "ready", "blocked", "review"):
            raise ValueError(f"cannot complete {tid}: invalid status {t['status']!r}")
        t["status"] = "done"
        t["latest_summary"] = summary
        self._current_claim[tid] = None
        for child_id in self.children.get(tid, []):
            parents = self.parents.get(child_id, [])
            if parents and all(self.tasks[p]["status"] == "done" for p in parents):
                if self.tasks[child_id]["status"] not in ("done", "archived"):
                    self.tasks[child_id]["status"] = "ready"

    def _request_review(self, tid, reviewer, summary=""):
        """Mirrors kanban_db.py `request_review` (~line 6501): requires status
        in ('running', 'ready'), lands in 'review'."""
        t = self.tasks[tid]
        if t["status"] not in ("running", "ready"):
            raise ValueError(f"cannot request-review on {tid}: not running/ready (status {t['status']!r})")
        self._implementer[tid] = t["assignee"]
        t["status"] = "review"
        t["assignee"] = reviewer
        self._current_claim[tid] = None
        if summary:
            t["latest_summary"] = summary
        self.events.setdefault(tid, []).append(
            {"kind": "review_requested", "run_id": None, "created_at": self._tick(),
             "payload": {"reviewer": reviewer, "summary": summary}}
        )

    def _claim_review(self, tid):
        """Mirrors kanban_db.py `claim_review_task` (~line 4750): review ->
        running, and records that the active run was claimed from 'review'
        (kanban_db stores this as `source_status: "review"` on the run's
        `claimed` event, ~line 4785/4835) - `request_changes` (~line 6711)
        refuses to act on a run that wasn't."""
        t = self.tasks[tid]
        if t["status"] != "review":
            raise ValueError(f"cannot claim {tid} for review: not in review (status {t['status']!r})")
        t["status"] = "running"
        self._current_claim[tid] = "review"

    def _claim_ready(self, tid):
        """Mirrors kanban_db.py `claim_task` (~line 4628): ready -> running,
        the dispatcher's own claim before a dev/qa/lead worker starts."""
        t = self.tasks[tid]
        if t["status"] != "ready":
            raise ValueError(f"cannot claim {tid}: not ready (status {t['status']!r})")
        t["status"] = "running"
        self._current_claim[tid] = "ready"

    def _request_changes(self, tid, reason):
        """Mirrors kanban_db.py `request_changes` (~line 6663-6779): valid only
        for a run claimed from 'review' (checked via `_current_claim`, which
        stands in for the real `claimed` event's `source_status` field,
        ~line 6711); lands at 'ready'/'todo' via `_landing_status_after_parents`
        (~line 6880), never straight back to 'running'."""
        t = self.tasks[tid]
        if t["status"] != "running" or self._current_claim.get(tid) != "review":
            raise ValueError(f"cannot request-changes on {tid}: not an active review-claimed run (status {t['status']!r})")
        landing = self._landing_status_after_parents(tid)
        t["status"] = landing
        t["assignee"] = self._implementer.get(tid, t["assignee"])
        t["latest_summary"] = reason
        self._current_claim[tid] = None
        self.events.setdefault(tid, []).append(
            {"kind": "changes_requested", "payload": {"reason": reason}, "created_at": self._tick(), "run_id": None}
        )

    # -- role helpers ---------------------------------------------------------
    def run_for(self, tid, profile, metadata=None, outcome="completed", summary=None):
        self.runs.setdefault(tid, []).append(
            {
                "profile": profile,
                "status": "done",
                "outcome": outcome,
                "summary": summary or "",
                "metadata": metadata or {},
                "ended_at": self._tick(),
            }
        )

    def dev_finishes(self, tid):
        """dev claims its own ready card (kanban_db.claim_task, ready ->
        running - the dispatcher does this before the worker touches the
        card) and then calls kanban_request_review(reviewer="reviewer")."""
        if self.tasks[tid]["status"] == "ready":
            self._claim_ready(tid)
        self._request_review(tid, "reviewer", "ready for review")

    def reviewer_passes(self, tid, round=1):
        """reviewer claims the review lane (kanban_db.claim_review_task,
        review -> running with source_status="review") and then completes."""
        self._claim_review(tid)
        summary = f"review: PASS round {round} - clean"
        self.run_for(tid, "reviewer", metadata={"review": {"round": round, "findings": 0, "verdict": "pass"}}, outcome="completed", summary=summary)
        self._complete(tid, summary)

    def reviewer_requests_changes(self, tid, reason):
        """reviewer claims the review lane, then hands the card back
        (kanban_db.request_changes: valid only for that claimed run)."""
        self._claim_review(tid)
        self._request_changes(tid, reason)

    def qa_passes(self, tid, suite, ui=None):
        t = self.tasks[tid]
        if t["status"] not in ("running", "ready"):
            raise ValueError(f"cannot qa_passes {tid}: not running/ready (status {t['status']!r})")
        metadata = {"suite": suite}
        if ui is not None:
            metadata["ui"] = ui
        self.run_for(tid, "qa", metadata=metadata, outcome="completed", summary="approved: qa passed")
        self._complete(tid, "approved: qa passed")

    def lead_blocks_ready(self, tid, title, what, tests):
        self._block(tid, f"READY: {title}\nWhat: {what}\nTests: {tests}", kind="ready")


def fake_board(name="my-app", legacy=None, **kw):
    """(Board, FakeLegacyCli) in one line, for a test that only needs a board."""
    h = legacy or FakeLegacyCli(kw.get("boards_dir"))
    return h.board(name, **kw), h
