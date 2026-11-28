# mutation-check: targets-predate-range b957739c9 -- owner-word rename in a fixture string ::
#   HelperMarker.test_the_line_the_merge_gate_writes_is_a_helper
import inspect, json, os, subprocess, sys, tempfile, unittest, unittest.mock
from pathlib import Path
sys.path[:0] = [str(Path(__file__).resolve().parents[n]) for n in (1, 2)]
import capsule.tests  # noqa: E402,F401 - its __init__ installs the swept TMPDIR (tmpclean)
os.environ["AGENT_OPS_NO_SEND"] = "1"
os.environ.setdefault("EVENTS_SPOOL", str(Path(tempfile.mkdtemp(prefix="events-board-")) / "events.jsonl"))
import board
from capsule import legacy_names as _LN  # noqa: E402


def _boards(*names):
    root = Path(tempfile.mkdtemp(prefix="boards-"))
    for n in names:
        (root / n).mkdir(parents=True)
        (root / n / "kanban.db").write_text("")
    return root


class Env(unittest.TestCase):
    def setUp(self):
        self.boards = _boards("my-app", "capsule-corp")

    def test_pins_its_own_db_and_board_and_drops_worker_pins(self):
        b = board.open_board("my-app", runner=lambda argv: "", boards_dir=self.boards)
        poison = {"LEGACY_KANBAN_DB": "/somewhere/else/kanban.db",
                  "LEGACY_KANBAN_BOARD": "capsule-corp",
                  "LEGACY_KANBAN_TASK": "t_deadbeef",
                  "LEGACY_KANBAN_RUN_ID": "77",
                  "LEGACY_KANBAN_CLAIM_LOCK": "/tmp/claim.lock",
                  # Minor 1: the legacy CLI honours this at highest precedence too
                  # (kanban_db.py:737-745), so it belongs in the same sweep.
                  "LEGACY_KANBAN_WORKSPACES_ROOT": "/tmp/someone-elses-scratch"}
        with unittest.mock.patch.dict(os.environ, poison, clear=False):
            env = b._env()
        self.assertEqual(env["LEGACY_KANBAN_DB"], str(self.boards / "my-app" / "kanban.db"))
        self.assertEqual(env["LEGACY_KANBAN_BOARD"], "my-app")
        for k in ("LEGACY_KANBAN_TASK", "LEGACY_KANBAN_RUN_ID", "LEGACY_KANBAN_CLAIM_LOCK",
                  "LEGACY_KANBAN_WORKSPACES_ROOT"):
            self.assertNotIn(k, env)
        self.assertEqual(env["PATH"], os.environ["PATH"])   # the rest of the env is inherited

    def test_env_extra_is_applied_but_never_beats_the_pins(self):
        b = board.open_board("my-app", runner=lambda argv: "", boards_dir=self.boards,
                             env_extra={"LEGACY_HOME": "/h/automation",
                                        "LEGACY_KANBAN_BOARD": "nope"})
        env = b._env()
        self.assertEqual(env["LEGACY_HOME"], "/h/automation")
        self.assertEqual(env["LEGACY_KANBAN_BOARD"], "my-app")

    def test_db_path_of_the_default_board_is_not_under_boards_dir(self):
        b = board.open_board("default", runner=lambda argv: "", boards_dir=self.boards)
        self.assertEqual(b.db_path, Path.home() / f".{_LN.NAME}" / "kanban.db")

    def test_invalid_board_name_is_refused_before_argv(self):
        with self.assertRaises(ValueError):
            board.open_board("../etc", runner=lambda argv: "", boards_dir=self.boards)

    def test_the_ledger_hook_signature_is_a_string_not_a_name_bound_to_none(self):
        # Minor 11: `LedgerHook = None` read like a type alias and was not one.
        self.assertIsInstance(board.LEDGER_HOOK_SIG, str)
        self.assertFalse(hasattr(board, "LedgerHook"))


class Argv(unittest.TestCase):
    def setUp(self):
        self.boards = _boards("my-app")

    def test_every_argv_starts_with_legacy_base_kanban_board(self):
        seen = []
        b = board.open_board("my-app", runner=lambda argv, env=None: seen.append((argv, env)) or "",
                             boards_dir=self.boards)
        b._run(["comment", "t_1", "hi"])
        argv, env = seen[0]
        self.assertEqual(argv, ["-m", "legacy_cli.main", "kanban", "--board", "my-app",
                                "comment", "t_1", "hi"])
        self.assertEqual(env["LEGACY_KANBAN_BOARD"], "my-app")

    def test_a_one_argument_runner_is_never_handed_an_env(self):
        seen = []
        b = board.open_board("my-app", runner=lambda argv: seen.append(argv) or "",
                             boards_dir=self.boards)
        b._run(["unblock", "t_1"])
        self.assertEqual(seen[0][-2:], ["unblock", "t_1"])

    def test_a_star_args_runner_is_handed_an_env(self):
        seen = []

        class R:
            def __call__(self, argv, *a, **kw):
                seen.append((argv, kw.get("env") or (a[0] if a else None)))
                return ""

        board.open_board("my-app", runner=R(), boards_dir=self.boards)._run(["unblock", "t_1"])
        self.assertIsNotNone(seen[0][1])

    def test_cli_failure_becomes_legacy_cli_error_with_argv_and_stderr(self):
        import subprocess

        def boom(argv, env=None):
            raise subprocess.CalledProcessError(1, argv, output="", stderr="cannot block t_1\n" + "x" * 500)

        b = board.open_board("my-app", runner=boom, boards_dir=self.boards)
        with self.assertRaises(board.LegacyCliError) as ctx:
            b._run(["block", "t_1", "--kind", "needs_input", "--", "NEEDS REPAIR: a\nb"])
        exc = ctx.exception
        self.assertEqual(exc.returncode, 1)
        self.assertLessEqual(len(exc.stderr), 200)
        self.assertIn("NEEDS REPAIR: a", exc.argv)          # only the first line of the reason
        self.assertNotIn("NEEDS REPAIR: a\nb", exc.argv)

    def test_board_error_hierarchy(self):
        for cls in (board.BoardNotFound, board.CardNotFound, board.LegacyCliError,
                    board.TransitionRefused, board.Locked):
            self.assertTrue(issubclass(cls, board.BoardError))
        self.assertTrue(issubclass(board.LegacyOutputInvalid, board.LegacyCliError))


class Discovery(unittest.TestCase):
    def test_board_names_lists_only_dirs_holding_a_kanban_db(self):
        root = _boards("my-app", "capsule-corp")
        (root / "empty").mkdir()
        self.assertEqual(board.board_names(root), ["capsule-corp", "my-app"])

    def test_ensure_board_swallows_already_exists(self):
        calls = []

        envs = []

        def runner(argv, env=None):
            calls.append(argv)
            envs.append(env)
            raise RuntimeError("board already exists")

        b = board.ensure_board("my-app", runner=runner, boards_dir=_boards())
        self.assertEqual(b.name, "my-app")
        self.assertEqual(calls[0], ["-m", "legacy_cli.main", "kanban", "boards", "new", "my-app"])
        self.assertEqual(envs[0]["LEGACY_KANBAN_BOARD"], "my-app")
        self.assertTrue(envs[0]["LEGACY_KANBAN_DB"].endswith("/my-app/kanban.db"))


T0 = 1_800_000_000.0


def _run(outcome, ended, summary="boom: traceback line"):
    return {"profile": "dev", "status": "done" if outcome == "completed" else outcome,
            "outcome": outcome, "summary": summary, "ended_at": ended,
            "id": int(ended) if ended is not None else 0}


def _raw(runs=(), comments=(), status="running", tid="t_1", latest_summary="", events=()):
    return {"task": {"id": tid, "title": "A card", "assignee": "dev", "status": status,
                     "body": "b", "workspace_path": "/w"},
            "latest_summary": latest_summary, "comments": list(comments), "runs": list(runs),
            "parents": [], "children": [], "events": list(events)}


def _blocked(reason, at):
    return {"author": "legacy-cli", "body": board.BLOCKED_COMMENT_PREFIX + reason, "created_at": at}


def _block_event(reason, at, kind="blocked", block_kind="needs_input"):
    """The row `kb.block_task` writes on EVERY block, CLI or tool
    (kanban_db.py:6464). The `kanban_block` MCP tool - how every worker blocks
    - writes only this; no comment (tools/kanban_tools.py:867-873)."""
    return {"kind": kind, "created_at": at, "run_id": 7,
            "payload": {"reason": reason, "kind": block_kind, "recurrences": 1,
                        "source_status": "running"}}


def _unblock_event(at, payload=None):
    """The row `unblock_task` appends on every successful unblock
    (kanban_db.py:6943-6958), whichever path parked the card and whichever
    path lifted it."""
    return {"kind": "unblocked", "created_at": at, "run_id": None, "payload": payload}


class Derived(unittest.TestCase):
    def test_block_reason_is_empty_on_a_card_that_is_not_parked(self):
        raw = _raw(status="running", comments=[_blocked("repair: retry 1/3 at 10:00 (crashed)", T0)])
        self.assertEqual(board.block_reason(raw), "")
        self.assertFalse(board.is_parked(raw))

    def test_block_reason_is_the_newest_block_comment_on_a_blocked_card(self):
        raw = _raw(status="blocked",
                   comments=[_blocked("repair: retry 1/3 at 10:00 (crashed)", T0),
                             _blocked("DECISION: which cache?", T0 + 5)])
        self.assertEqual(board.block_reason(raw), "DECISION: which cache?")
        self.assertTrue(board.is_parked(raw))

    def test_a_foreign_block_beats_a_newer_block_of_ours(self):
        raw = _raw(status="blocked",
                   comments=[_blocked("DECISION: which cache?", T0),
                             _blocked("repair: retry 1/3 at 10:00 (crashed)", T0 + 5)])
        self.assertEqual(board.block_reason(raw), "DECISION: which cache?")

    def test_a_comment_older_than_the_newest_stamped_failure_is_spent(self):
        raw = _raw(status="blocked", runs=[_run("crashed", T0 + 100)],
                   comments=[_blocked("repair: retry 1/3 at 10:00 (crashed)", T0)])
        self.assertEqual(board.block_reason(raw), "")

    def test_a_hold_comment_at_the_same_second_as_the_failure_is_live(self):
        # `>=`, not `>`: created_at and ended_at are both unix epoch seconds
        # and the hold is posted in the same second as the failure it reacts to.
        raw = _raw(status="blocked", runs=[_run("crashed", T0)],
                   comments=[_blocked("repair: retry 1/3 at 10:00 (crashed)", T0)])
        self.assertEqual(board.block_reason(raw), "repair: retry 1/3 at 10:00 (crashed)")

    def test_triage_counts_as_parked(self):
        raw = _raw(status="triage", comments=[_blocked("NEEDS REPAIR: A card", T0)])
        self.assertEqual(board.block_reason(raw), "NEEDS REPAIR: A card")

    def test_latest_summary_is_not_a_block_reason(self):
        raw = _raw(status="running", latest_summary="NEEDS REPAIR: stale, legacy never cleared it")
        self.assertEqual(board.block_reason(raw), "")
        self.assertEqual(board.Card("my-app", raw).latest_summary,
                         "NEEDS REPAIR: stale, legacy never cleared it")

    # --- the tool path: a block with no comment at all (C2) -----------------
    # `kanban_block` (tools/kanban_tools.py:867-873) calls kb.block_task
    # directly; block_task writes a run summary and one `task_events` row
    # (kanban_db.py:6464) and NO comment. Only the CLI's `_cmd_block` adds the
    # `BLOCKED:` comment (kanban.py:2443-2444). Every SOUL blocks via the tool,
    # so the comment trail is empty for every real DECISION / READY card.

    def test_a_tool_issued_block_has_no_comment_and_still_yields_its_reason(self):
        raw = _raw(status="blocked", events=[_block_event("DECISION: which cache?", T0)])
        self.assertEqual(raw["comments"], [])
        self.assertEqual(board.block_reason(raw), "DECISION: which cache?")

    def test_a_block_loop_detected_event_is_a_block_reason_too(self):
        # The unblock-loop breaker routes to `triage` and writes this kind
        # instead of `blocked` (kanban_db.py:6405).
        raw = _raw(status="triage",
                   events=[_block_event("DECISION: gate keeps failing", T0,
                                        kind="block_loop_detected")])
        self.assertEqual(board.block_reason(raw), "DECISION: gate keeps failing")

    def test_an_unrelated_event_kind_is_not_a_block_reason(self):
        raw = _raw(status="blocked",
                   events=[{"kind": "changes_requested", "created_at": T0,
                            "payload": {"reason": "please fix the tests"}}])
        self.assertEqual(board.block_reason(raw), "")

    def test_a_block_event_with_no_reason_is_ignored(self):
        # `block_task` allows `reason=None` (a legacy un-typed block).
        raw = _raw(status="blocked",
                   events=[{"kind": "blocked", "created_at": T0,
                            "payload": {"reason": None, "kind": None}}])
        self.assertEqual(board.block_reason(raw), "")

    def test_the_newest_of_the_two_sources_wins(self):
        raw = _raw(status="blocked",
                   comments=[_blocked("repair: retry 1/3 at 10:00 (crashed)", T0)],
                   events=[_block_event("DECISION: which cache?", T0 + 5)])
        self.assertEqual(board.block_reason(raw), "DECISION: which cache?")

    def test_a_newer_comment_beats_an_older_event(self):
        raw = _raw(status="blocked",
                   comments=[_blocked("BLOCKER: waiting on infra", T0 + 5)],
                   events=[_block_event("DECISION: which cache?", T0)])
        self.assertEqual(board.block_reason(raw), "BLOCKER: waiting on infra")

    def test_a_foreign_event_beats_a_newer_hold_comment_of_ours(self):
        # The foreign-wins rule applies across both sources: repair must never
        # release, count against or move a card a worker parked.
        raw = _raw(status="blocked",
                   comments=[_blocked("repair: retry 1/3 at 10:00 (crashed)", T0 + 5)],
                   events=[_block_event("READY: Soft tick", T0)])
        self.assertEqual(board.block_reason(raw), "READY: Soft tick")

    def test_an_event_older_than_the_newest_stamped_failure_is_spent(self):
        # Same freshness rule as the trail: the old store has since let it run again.
        # Ours, because the foreign-wins rule below deliberately outranks
        # freshness - a foreign block is never spent.
        raw = _raw(status="blocked", runs=[_run("crashed", T0 + 100)],
                   events=[_block_event("repair: retry 1/3 at 10:00 (crashed)", T0)])
        self.assertEqual(board.block_reason(raw), "")

    def test_a_foreign_event_is_never_spent_by_a_later_failure(self):
        raw = _raw(status="blocked", runs=[_run("crashed", T0 + 100)],
                   events=[_block_event("DECISION: which cache?", T0)])
        self.assertEqual(board.block_reason(raw), "DECISION: which cache?")

    def test_a_block_event_at_the_same_second_as_the_failure_is_live(self):
        raw = _raw(status="blocked", runs=[_run("crashed", T0)],
                   events=[_block_event("READY: Soft tick", T0)])
        self.assertEqual(board.block_reason(raw), "READY: Soft tick")

    def test_a_cli_block_writes_both_sources_and_reads_back_once(self):
        # `_cmd_block` posts the comment and THEN block_task writes the event,
        # both with the same reason: whichever wins the tie, same answer.
        raw = _raw(status="blocked", comments=[_blocked("BLOCKER: infra", T0)],
                   events=[_block_event("BLOCKER: infra", T0)])
        self.assertEqual(board.block_reason(raw), "BLOCKER: infra")

    def test_a_tool_block_on_a_card_that_is_not_parked_is_still_empty(self):
        raw = _raw(status="running", events=[_block_event("DECISION: which cache?", T0)])
        self.assertEqual(board.block_reason(raw), "")

    # --- NEW-1: retirement by the old store's own sticky-block predicate ------------
    # `_has_sticky_block` (kanban_db.py:4454-4488) looks only at the most
    # recent of ('blocked', 'unblocked') task_events by row id; `unblock_task`
    # (kanban_db.py:6943-6958) appends `unblocked` whenever a block is lifted,
    # whichever path parked the card. `block_records` must drop every comment
    # / event record older than the newest `unblocked` event so a foreign
    # reason that was answered and unblocked cannot stay authoritative forever.

    def test_a_foreign_event_is_retired_by_a_later_unblock_event(self):
        raw = _raw(status="blocked",
                   events=[_block_event("DECISION: which cache?", T0),
                           _unblock_event(T0 + 5)])
        self.assertEqual(board.block_records(raw), [])
        self.assertEqual(board.block_reason(raw), "")

    def test_a_record_after_the_unblock_survives_retirement(self):
        raw = _raw(status="blocked",
                   events=[_block_event("DECISION: which cache?", T0),
                           _unblock_event(T0 + 5)],
                   comments=[_blocked("repair: retry 1/3 at 10:00 (crashed)", T0 + 10)])
        self.assertEqual(board.block_reason(raw), "repair: retry 1/3 at 10:00 (crashed)")

    def test_a_record_older_than_the_unblock_never_resurfaces_behind_a_live_hold(self):
        # NEW-1 repro (repair): the old foreign event must not out-rank a
        # live hold just because the foreign-wins rule would otherwise favour
        # it - it is retired before foreign-wins is ever applied.
        raw = _raw(status="blocked",
                   events=[_block_event("waiting on t_9", 1000), _unblock_event(1100)],
                   comments=[_blocked("repair: retry 1/3 at 10:00 (crashed)", 6000)])
        self.assertEqual(board.block_reason(raw), "repair: retry 1/3 at 10:00 (crashed)")

    def test_a_record_older_than_the_unblock_never_resurfaces_as_a_decision(self):
        # NEW-1 repro (decisions): a foreign DECISION answered and unblocked
        # must not keep reading back as an open decision after a later,
        # unrelated block.
        raw = _raw(status="blocked",
                   events=[_block_event("DECISION: which cache?", 1000), _unblock_event(1100)],
                   comments=[_blocked("repair: retry 1/3 at 10:00 (crashed)", 6000)])
        self.assertFalse(board.block_reason(raw).startswith("DECISION:"))

    def test_a_comment_tied_with_the_unblock_second_is_retired(self):
        # Tie rule: a comment (source_rank 0) at the same second as the
        # unblock is retired - it sorts before the unblock's event position in
        # block_records' own (created_at, source_rank, position) order, the
        # same convention that makes a CLI block's comment precede its own
        # block_task event at a shared timestamp.
        raw = _raw(status="blocked",
                   comments=[_blocked("DECISION: which cache?", T0)],
                   events=[_unblock_event(T0)])
        self.assertEqual(board.block_records(raw), [])

    def test_an_event_tied_with_the_unblock_second_but_earlier_in_the_trail_is_retired(self):
        raw = _raw(status="blocked",
                   events=[_block_event("DECISION: which cache?", T0), _unblock_event(T0)])
        self.assertEqual(board.block_records(raw), [])

    def test_an_event_tied_with_the_unblock_second_but_later_in_the_trail_survives(self):
        raw = _raw(status="blocked",
                   events=[_unblock_event(T0), _block_event("DECISION: which cache?", T0)])
        self.assertEqual(board.block_reason(raw), "DECISION: which cache?")

    def test_no_unblock_event_leaves_block_records_untouched(self):
        raw = _raw(status="blocked", events=[_block_event("DECISION: which cache?", T0)])
        self.assertEqual(board.block_reason(raw), "DECISION: which cache?")

    def test_only_the_newest_unblock_event_retires(self):
        # An older `unblocked` event must not retire a record that is newer
        # than it but older than the true newest unblock.
        raw = _raw(status="blocked",
                   events=[_unblock_event(T0), _block_event("DECISION: which cache?", T0 + 5),
                           _unblock_event(T0 + 10)])
        self.assertEqual(board.block_records(raw), [])

    def test_card_derives_a_tool_issued_block_reason(self):
        c = board.Card("my-app", _raw(status="blocked",
                                        events=[_block_event("READY: Soft tick", T0)]))
        self.assertEqual(c.current_block_reason, "READY: Soft tick")
        self.assertTrue(c.is_parked)

    def test_a_dependency_wait_carries_a_reason_but_never_parks_the_card(self):
        # `kind="dependency"` routes to `todo` (kanban_db.py:6320-6347), which
        # is not a PARKED_STATUS - listed in BLOCK_EVENT_KINDS for completeness.
        self.assertIn("dependency_wait", board.BLOCK_EVENT_KINDS)
        raw = _raw(status="todo", events=[_block_event("waiting on t_2", T0,
                                                       kind="dependency_wait")])
        self.assertEqual(board.block_reason(raw), "")

    def test_failures_drop_runs_that_ended_at_or_before_the_human_retry(self):
        raw = _raw(status="ready", runs=[_run("crashed", T0), _run("crashed", T0 + 100)],
                   comments=[{"author": "human", "body": board.ACT_COMMENT, "created_at": T0}])
        self.assertEqual([r["ended_at"] for r in board.failures(raw)], [T0 + 100])

    def test_a_reassign_comment_also_resets_the_cycle(self):
        raw = _raw(status="ready", runs=[_run("crashed", T0)],
                   comments=[{"author": "human", "body": board.REASSIGN_COMMENT_PREFIX + "qa",
                              "created_at": T0 + 1}])
        self.assertEqual(board.failures(raw), [])

    def test_an_unstamped_failure_sorts_last_and_survives_the_cutoff(self):
        crashed = _run("crashed", None)
        raw = _raw(status="ready", runs=[_run("crashed", T0), crashed],
                   comments=[{"author": "human", "body": board.ACT_COMMENT, "created_at": T0 + 10}])
        self.assertEqual(board.failures(raw), [crashed])

    def test_non_failure_outcomes_are_not_failures(self):
        self.assertEqual(board.failures(_raw(runs=[_run("completed", T0), _run("cancelled", T0)])), [])

    def test_card_exposes_the_derivations_and_the_raw_columns(self):
        raw = _raw(status="blocked", runs=[_run("crashed", T0)],
                   comments=[_blocked("NEEDS REPAIR: A card", T0 + 1)])
        c = board.Card("my-app", raw)
        self.assertEqual(c.board, "my-app")
        self.assertEqual((c.id, c.title, c.assignee, c.body, c.workspace_path),
                         ("t_1", "A card", "dev", "b", "/w"))
        self.assertEqual(c.legacy_status, "blocked")
        self.assertEqual(c.current_block_reason, "NEEDS REPAIR: A card")
        self.assertTrue(c.is_parked)
        self.assertEqual(len(c.failures_since_human_retry), 1)
        self.assertIsInstance(c.failures_since_human_retry, tuple)


class Reads(unittest.TestCase):
    def setUp(self):
        self.boards = _boards("my-app", "capsule-corp")

    def _runner(self, payload):
        self.seen = []

        def runner(argv, env=None):
            self.seen.append(argv)
            return json.dumps(payload)

        return runner

    def test_show_argv_and_card(self):
        b = board.open_board("my-app", runner=self._runner(_raw()), boards_dir=self.boards)
        c = b.show("t_1")
        self.assertEqual(self.seen[0][-3:], ["show", "t_1", "--json"])
        self.assertEqual(c.id, "t_1")

    def test_show_raises_card_not_found_when_the_payload_has_no_task(self):
        b = board.open_board("my-app", runner=self._runner({}), boards_dir=self.boards)
        with self.assertRaises(board.CardNotFound):
            b.show("t_nope")

    def test_try_show_returns_none_on_any_failure(self):
        def boom(argv, env=None):
            raise RuntimeError("no such board")

        opened = board.open_board("my-app", runner=boom, boards_dir=self.boards)
        self.assertIsNone(opened.try_show("t_1"))
        b = board.open_board("my-app", runner=lambda argv, env=None: "not json",
                             boards_dir=self.boards)
        self.assertIsNone(b.try_show("t_1"))

    def test_list_argv_and_refs(self):
        rows = [{"id": "t_1", "status": "ready", "assignee": "dev", "title": "A"},
                {"id": "t_2", "status": "done", "assignee": None, "title": "B"}]
        b = board.open_board("my-app", runner=self._runner(rows), boards_dir=self.boards)
        refs = b.list()
        self.assertEqual(self.seen[0][-2:], ["list", "--json"])
        self.assertEqual([r.id for r in refs], ["t_1", "t_2"])
        self.assertEqual([r.id for r in b.list(statuses=board.LIVE_STATUSES)], ["t_1"])

    def test_find_card_scans_every_board_and_returns_none_when_absent(self):
        def runner(argv, env=None):
            name = argv[argv.index("--board") + 1]
            return json.dumps(_raw()) if name == "capsule-corp" else "{}"

        found = board.find_card("t_1", boards_dir=self.boards, runner=runner)
        self.assertIsNotNone(found)
        self.assertEqual((found[0].name, found[1].id), ("capsule-corp", "t_1"))
        self.assertIsNone(board.find_card("t_zz", boards_dir=self.boards,
                                          runner=lambda argv, env=None: "{}"))


class Writes(unittest.TestCase):
    def setUp(self):
        self.boards = _boards("my-app")
        self.seen = []

    def _board(self, out="", card=None):
        def runner(argv, env=None):
            self.seen.append(argv)
            if "show" in argv:
                return json.dumps(card if card is not None else _raw())
            return out

        return board.open_board("my-app", runner=runner, boards_dir=self.boards)

    def _tail(self, i=0):
        argv = self.seen[i]
        return argv[argv.index("--board") + 2:]

    def test_new_card_argv_is_the_canonical_order(self):
        c = board.NewCard(title="T", body="B", assignee="dev", workspace="worktree:/r/.worktrees/x",
                          branch="sprint/x", max_retries=3, model="claude-opus-5",
                          idempotency_key="sprint:x", created_by="sprint-bridge")
        self.assertEqual(c.argv(), ["create", "T", "--body", "B", "--assignee", "dev",
                                    "--workspace", "worktree:/r/.worktrees/x",
                                    "--branch", "sprint/x", "--max-retries", "3",
                                    "--model", "claude-opus-5",
                                    "--idempotency-key", "sprint:x",
                                    "--created-by", "sprint-bridge", "--json"])

    def test_new_card_omits_every_unset_flag_but_never_json(self):
        self.assertEqual(board.NewCard(title="T").argv(), ["create", "T", "--json"])

    def test_create_returns_the_task_id(self):
        b = self._board(out=json.dumps({"id": "t_abc"}))
        self.assertEqual(b.create(board.NewCard(title="T", assignee="qa", parent="t_p")), "t_abc")
        self.assertEqual(self._tail(), ["create", "T", "--assignee", "qa", "--parent", "t_p", "--json"])

    def test_create_without_an_id_is_a_cli_error(self):
        with self.assertRaises(board.LegacyCliError):
            self._board(out=json.dumps({"ok": True})).create(board.NewCard(title="T"))

    def test_comment_argv(self):
        self._board().comment("t_1", "gate: PR ready")
        self.assertEqual(self._tail(), ["comment", "t_1", "gate: PR ready"])

    def test_block_argv_on_a_blockable_card(self):
        # the kanban `block` verb is `task_id [reason ...] [--kind KIND]` - a `--`
        # separator or a `--kind` placed before the reason both make the outer
        # CLI parser choke with "unrecognized arguments" (2026-09-11
        # hotfix; reproduced against a live legacy CLI). Reason is one
        # positional argv element, `--kind` comes after it.
        b = self._board(card=_raw(status="ready"))
        b.block("t_1", "repair: retry 1/3 at 10:00 (crashed)", kind="transient")
        self.assertEqual(self._tail(1), ["block", "t_1",
                                         "repair: retry 1/3 at 10:00 (crashed)",
                                         "--kind", "transient"])

    def test_block_argv_survives_a_runner_that_rejects_the_old_broken_shape(self):
        """Runner-level regression guard: a fake CLI that reproduces the
        real CLI's argparse behavior (2026-09-11 repro against a live legacy
        CLI on a scratch DB) - a bare `--` anywhere, or `--kind` appearing
        before the reason token, is an outer-parser usage error (exit 2).
        `Board.block` must never build an argv this runner rejects."""
        import subprocess

        def strict_runner(argv, env=None):
            self.seen.append(argv)
            tail = argv[argv.index("--board") + 2:]
            if tail and tail[0] == "show":
                return json.dumps(_raw(status="ready"))
            if tail and tail[0] == "block":
                rest = tail[1:]
                if "--" in rest:
                    raise subprocess.CalledProcessError(
                        2, argv, stderr="cli: error: unrecognized arguments: -- ...")
                if "--kind" in rest and rest.index("--kind") < 2:
                    raise subprocess.CalledProcessError(
                        2, argv, stderr="cli: error: unrecognized arguments: ...")
                return "Blocked " + rest[0]
            return ""

        b = board.open_board("my-app", runner=strict_runner, boards_dir=self.boards)
        b.block("t_1", "repair: retry 1/3 (crashed)", kind="transient")  # must not raise
        self.assertIn("block", [a[a.index("--board") + 2] for a in self.seen])

    def test_block_on_a_non_blockable_status_makes_zero_subprocess_calls_after_the_read(self):
        b = self._board(card=_raw(status="triage"))
        with self.assertRaises(board.TransitionRefused):
            b.block("t_1", "NEEDS REPAIR: A card", kind="needs_input")
        self.assertEqual([a[a.index("--board") + 2] for a in self.seen], ["show"])

    def test_block_accepts_a_prefetched_card_and_does_not_read_again(self):
        b = self._board(card=_raw(status="ready"))
        b.block("t_1", "r", kind="transient", card=board.Card("my-app", _raw(status="ready")))
        self.assertEqual([a[a.index("--board") + 2] for a in self.seen], ["block"])

    def test_unblock_on_a_triage_card_reports_a_clear_reason_after_the_cli_refuses(self):
        # Live bug (2026-09-11): the legacy block-loop detector (kanban_db.py
        # block_task, ~6406) parks a card in `triage`, and `unblock_task`
        # only accepts ('blocked', 'scheduled') - shelling out produced a
        # bare "cannot unblock (not blocked/scheduled?)". unblock() does not
        # pre-check status (it is the hot path - see its own docstring), but
        # reacts to the CLI's refusal by re-reading the card and, when it is
        # `triage`, re-raising with a message that says WHY (no legacy verb
        # resumes a triage card) so decisions.answer/merge_gate can tell a
        # human apart from an ordinary infra hiccup.
        def runner(argv, env=None):
            self.seen.append(argv)
            if "unblock" in argv:
                raise subprocess.CalledProcessError(1, argv, stderr="not blocked/scheduled?")
            if "show" in argv:
                return json.dumps(_raw(status="triage"))
            return ""

        b = board.open_board("my-app", runner=runner, boards_dir=self.boards)
        with self.assertRaises(board.TransitionRefused) as cm:
            b.unblock("t_1")
        self.assertIn("triage", str(cm.exception))
        self.assertIn("no CLI verb", str(cm.exception))
        self.assertEqual([a[a.index("--board") + 2] for a in self.seen], ["unblock", "show"])

    def test_unblock_on_a_non_triage_failure_raises_the_ordinary_cli_error(self):
        # A ready/running/etc. card that fails to unblock for some other
        # reason (already unblocked by a racing tick, transient infra) must
        # still surface as a plain LegacyCliError, not our triage-specific one.
        def runner(argv, env=None):
            self.seen.append(argv)
            if "unblock" in argv:
                raise subprocess.CalledProcessError(1, argv, stderr="boom")
            if "show" in argv:
                return json.dumps(_raw(status="ready"))
            return ""

        b = board.open_board("my-app", runner=runner, boards_dir=self.boards)
        with self.assertRaises(board.LegacyCliError) as cm:
            b.unblock("t_1")
        self.assertNotIsInstance(cm.exception, board.TransitionRefused)

    def test_unblock_on_the_happy_path_makes_exactly_one_subprocess_call(self):
        b = self._board()
        b.unblock("t_1")
        self.assertEqual(len(self.seen), 1)
        self.assertEqual(self._tail(0), ["unblock", "t_1"])

    def test_the_remaining_verbs(self):
        b = self._board()
        b.unblock("t_1")
        b.complete("t_1", "merged x", {"pr": "u"})
        b.set_model("t_1", "none")
        b.set_model("t_2", "claude-opus-5", provider="anthropic")
        b.archive("t_1")
        self.assertEqual(self._tail(0), ["unblock", "t_1"])
        self.assertEqual(self._tail(1), ["complete", "t_1", "--summary", "merged x",
                                         "--metadata", '{"pr": "u"}'])
        self.assertEqual(self._tail(2), ["set-model", "t_1", "none"])
        self.assertEqual(self._tail(3), ["set-model", "t_2", "claude-opus-5",
                                         "--provider", "anthropic"])
        self.assertEqual(self._tail(4), ["archive", "t_1"])

    def test_the_dead_legacy_only_verbs_are_gone(self):
        for name in ("reassign", "link", "request_review", "notify_subscribe"):
            self.assertFalse(hasattr(board.Board, name), name)
        self.assertFalse(hasattr(board, "subscribe_argv"))


class Seams(unittest.TestCase):
    def setUp(self):
        self.boards = _boards("my-app")
        self.state = tempfile.mkdtemp(prefix="gate-state-board-")
        self._old_state = os.environ.get("MERGE_GATE_STATE")
        os.environ["MERGE_GATE_STATE"] = self.state
        self.b = board.open_board("my-app", runner=lambda argv: "", boards_dir=self.boards)

    def tearDown(self):
        # Restore rather than pop: test_merge_gate.py sets MERGE_GATE_STATE once at
        # import time for the whole process, and other test modules running later
        # in the same `unittest discover` process depend on it staying set.
        if self._old_state is None:
            os.environ.pop("MERGE_GATE_STATE", None)
        else:
            os.environ["MERGE_GATE_STATE"] = self._old_state

    def test_lock_path_is_the_documented_one(self):
        self.assertEqual(board.lock_path("my-app", "t_1"),
                         Path(self.state) / "locks" / "my-app-t_1.lock")

    def test_a_second_holder_raises_locked(self):
        import fcntl
        with self.b.lock("t_1"):
            fh = open(board.lock_path("my-app", "t_1"), "a+")
            with self.assertRaises(OSError):
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fh.close()

    def test_lock_raises_locked_when_another_handle_holds_it(self):
        import fcntl
        p = board.lock_path("my-app", "t_2")
        p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        holder = open(p, "a+")
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with self.assertRaises(board.Locked):
                with self.b.lock("t_2"):
                    pass
        finally:
            fcntl.flock(holder.fileno(), fcntl.LOCK_UN); holder.close()

    def test_lock_is_released_even_when_the_body_raises(self):
        import fcntl
        with self.assertRaises(RuntimeError):
            with self.b.lock("t_3"):
                raise RuntimeError("boom")
        fh = open(board.lock_path("my-app", "t_3"), "a+")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)   # must not raise
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN); fh.close()

    def test_emit_writes_one_event_through_events_log(self):
        spool = Path(tempfile.mkdtemp()) / "e.jsonl"
        row = self.b.emit({"kind": "card", "source": "board", "text": "hi",
                           "task_id": "t_1", "payload": {"event": "test"}}, spool=spool)
        self.assertEqual(row["board"], "my-app")
        self.assertEqual(json.loads(spool.read_text())["task_id"], "t_1")

    def test_append_ledger_calls_the_hook_and_swallows_its_failure(self):
        seen = []
        b = board.open_board("my-app", runner=lambda argv: "", boards_dir=self.boards,
                             ledger=lambda bd, tid, entry: seen.append((bd, tid, entry)))
        b.append_ledger("t_1", {"v": 1})
        self.assertEqual(seen, [("my-app", "t_1", {"v": 1})])

        def boom(bd, tid, entry):
            raise RuntimeError("disk full")

        board.open_board("my-app", runner=lambda argv: "", boards_dir=self.boards,
                         ledger=boom).append_ledger("t_1", {"v": 1})   # must not raise

    def test_the_default_ledger_hook_does_nothing(self):
        self.assertIsNone(board.no_ledger("my-app", "t_1", {"v": 1}))


class BoardHook(unittest.TestCase):
    """The DEFAULT ledger hook (`board._default_ledger`), not `ledger.append`
    directly. Fix round 2, controller ruling: a readable non-legacy id like
    `t_impl` is a legitimate id from THIS hook's point of view, so it is a
    silent no-op here - no stderr. `ledger.append`'s own stderr line is
    unchanged for a caller that reaches it directly (the path-safety guard);
    ValidTaskId (test_ledger.py) still covers that."""

    def setUp(self):
        self.boards = _boards("my-app")
        self._old_vault = os.environ.get("OBSIDIAN_VAULT_PATH")
        self.vault = Path(tempfile.mkdtemp(prefix="board-hook-"))
        os.environ["OBSIDIAN_VAULT_PATH"] = str(self.vault)
        self.b = board.open_board("my-app", runner=lambda argv: "", boards_dir=self.boards)

    def tearDown(self):
        if self._old_vault is None:
            os.environ.pop("OBSIDIAN_VAULT_PATH", None)
        else:
            os.environ["OBSIDIAN_VAULT_PATH"] = self._old_vault

    def test_a_non_hex_task_id_writes_nothing_and_prints_nothing(self):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            got = self.b.append_ledger("t_impl", {"v": 1})
        self.assertIsNone(got)
        self.assertEqual(buf.getvalue(), "")
        self.assertFalse((self.vault / "Ledger" / "my-app" / "t_impl.jsonl").exists())


class BridgeIntegration(unittest.TestCase):
    def setUp(self):
        self.boards = _boards("my-app")

    def test_the_bridge_builders_are_new_cards_rendered_through_board(self):
        import board_bridge as bb
        patcher = unittest.mock.patch.dict(
            bb.REPO_PATHS, {"my-app": "/tmp/my-app-sample-checkout"})
        patcher.start()  # sample project
        self.addCleanup(patcher.stop)
        spec = {"title": "T", "repo": "my-app", "risk": "high", "body": "## Goal",
                "area": "frontend"}
        argv = bb.build_impl_argv(spec, "soft-tick", "hash", "sha")
        self.assertEqual(argv[:5], ["-m", "legacy_cli.main", "kanban", "--board", "my-app"])
        self.assertEqual(argv[-1], "--json")
        self.assertEqual(argv[argv.index("--assignee") + 1], "dev-frontend")
        self.assertLess(argv.index("--workspace"), argv.index("--model"))   # canonical order


def _card(status="ready", body="", comments=(), board_name="my-app", assignee=None):
    task = {"id": "t_1", "title": "T", "status": status, "body": body}
    if assignee is not None:
        task["assignee"] = assignee
    return board.Card(board_name, {"task": task, "comments": list(comments)})


class Columns(unittest.TestCase):
    def test_one_column_per_live_status(self):
        want = {"triage": "## \U0001f4e5 Backlog", "todo": "## \U0001f4e5 Backlog",
                "ready": "## \U0001f3c3 Sprint", "running": "## \U0001f528 In Progress",
                "review": "## \U0001f440 Review", "blocked": "## ⏸ Blocked",
                "done": "## ✅ Done"}
        for status, heading in want.items():
            # `ready` here means a dispatched (non-source) card - the impl/
            # verify/lead shape, which always carries an assignee.
            assignee = "alice" if status == "ready" else None
            self.assertEqual(board.column_of(_card(status=status, assignee=assignee)), heading, status)

    def test_every_live_status_plus_done_is_claimed(self):
        for status in board.LIVE_STATUSES | {"done"}:
            self.assertIsNotNone(board.column_of(_card(status=status)), status)

    def test_archived_matches_no_column(self):
        self.assertIsNone(board.column_of(_card(status="archived")))

    def test_new_triage_card_is_backlog_not_blocked(self):
        """An intake card is `triage` with no BLOCKED: trail: is_parked is true
        and the reason is "" - it belongs in Backlog."""
        self.assertEqual(board.column_of(_card(status="triage")), "## \U0001f4e5 Backlog")

    def test_needs_repair_shadows_blocked(self):
        c = _card(status="blocked", comments=[{"body": "BLOCKED: NEEDS REPAIR: it died", "created_at": 5}])
        self.assertEqual(board.column_of(c), "## \U0001f6e0 Needs repair")

    def test_foreign_block_on_triage_is_blocked_not_backlog(self):
        c = _card(status="triage", comments=[{"body": "BLOCKED: unblock loop breaker", "created_at": 5}])
        self.assertEqual(board.column_of(c), "## ⏸ Blocked")

    def test_blocked_with_a_foreign_block_older_than_a_failure_still_renders_with_reason(self):
        """Ruling 2026-09-07: a FOREIGN block (human/tool/another actor) is
        never spent by our own failures - it stays authoritative until a
        newer `unblocked` event retires it, however old it is relative to a
        later run. Only OUR OWN `repair:`/`NEEDS REPAIR:` holds are subject
        to the freshness cutoff (see the next test)."""
        c = board.Card("my-app", {"task": {"id": "t_1", "status": "blocked"},
                                    "comments": [{"body": "BLOCKED: old", "created_at": 1}],
                                    "runs": [{"outcome": "gave_up", "ended_at": 9}]})
        self.assertEqual(c.current_block_reason, "old")
        self.assertEqual(board.column_of(c), "## ⏸ Blocked")

    def test_blocked_with_only_a_spent_repair_hold_still_renders_with_empty_reason(self):
        """A `repair:`-prefixed hold IS ours, so it is spent once it is older
        than the newest stamped failure and `current_block_reason` reads "".
        With no other record, the Blocked predicate falls to
        `legacy_status == "blocked"` alone - the card still renders, just
        with no reason to show."""
        c = board.Card("my-app", {"task": {"id": "t_1", "status": "blocked"},
                                    "comments": [{"body": "BLOCKED: repair: retry after fix", "created_at": 1}],
                                    "runs": [{"outcome": "gave_up", "ended_at": 9}]})
        self.assertEqual(c.current_block_reason, "")
        self.assertEqual(board.column_of(c), "## ⏸ Blocked")

    def test_ready_source_card_with_no_spec_is_backlog(self):
        """A `ready` source card with no spec yet is planner-eligible
        Backlog, exactly like a `triage`/`todo` card would be."""
        c = _card(status="ready")
        self.assertTrue(c.is_source_card)
        self.assertEqual(board.column_of(c), "## \U0001f4e5 Backlog")

    def test_ready_card_with_an_assignee_is_sprint(self):
        """A dispatch's impl/verify/lead cards carry an assignee - those stay
        Sprint, not Backlog/Approved."""
        c = _card(status="ready", assignee="alice")
        self.assertFalse(c.is_source_card)
        self.assertEqual(board.column_of(c), "## \U0001f3c3 Sprint")

    def test_ready_card_with_a_dispatched_comment_is_sprint(self):
        """sync_approved posts `bridge: dispatched <impl_id> (...)` on the
        source card right before archiving it - belt-and-suspenders: if the
        archive step fails and the card outlives it, it must not re-render
        as Backlog/Approved and get re-dispatched."""
        c = _card(status="ready", body="spec: Specs/timer-page.md",
                  comments=[{"body": "bridge: dispatched t_9 (Specs/timer-page.md)", "created_at": 1}])
        self.assertFalse(c.is_source_card)
        self.assertEqual(board.column_of(c), "## \U0001f3c3 Sprint")

    def test_nine_columns_and_names_agree(self):
        self.assertEqual(len(board.COLUMNS), 9)
        self.assertEqual(board.COLUMN_NAMES, tuple(h for h, _ in board.COLUMNS))
        self.assertEqual(len(set(board.COLUMN_NAMES)), 9)


class SpecFields(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="board-spec-"))
        self._old = os.environ.get("MERGE_GATE_STATE")
        os.environ["MERGE_GATE_STATE"] = str(self.root)

    def tearDown(self):
        if self._old is None:
            os.environ.pop("MERGE_GATE_STATE", None)
        else:
            os.environ["MERGE_GATE_STATE"] = self._old

    def test_slug_from_the_body_meta_block(self):
        c = _card(body="<!-- sprint-meta\nrisk: med\nslug: timer\nspec: Specs/timer-page.md\n-->\n\nGoal")
        self.assertEqual(c.spec_slug, "timer-page")

    def test_slug_from_the_newest_comment_when_the_body_has_none(self):
        c = _card(comments=[{"body": f"spec: Specs/first.md {board.SPEC_LINK_MARK}", "created_at": 1},
                            {"body": f"spec: Specs/second.md {board.SPEC_LINK_MARK}", "created_at": 2}])
        self.assertEqual(c.spec_slug, "second")

    def test_no_spec_is_empty_string(self):
        self.assertEqual(_card(body="just a title").spec_slug, "")

    def test_spec_approved_follows_the_stamp(self):
        c = _card(body="spec: Specs/timer-page.md")
        self.assertFalse(c.spec_approved)
        import merge_gate
        merge_gate.write_spec_approval("timer-page", now=1.0)
        self.assertTrue(c.spec_approved)

    def test_no_slug_is_never_approved(self):
        import merge_gate
        merge_gate.write_spec_approval("timer-page", now=1.0)
        self.assertFalse(_card(body="no spec here").spec_approved)

    def test_columns_move_with_the_stamp(self):
        c = _card(status="triage", body="spec: Specs/timer-page.md")
        self.assertEqual(board.column_of(c), "## \U0001f52c Spec Ready")
        import merge_gate
        merge_gate.write_spec_approval("timer-page", now=1.0)
        self.assertEqual(board.column_of(c), "## ✅ Approved")

    def test_ready_source_card_with_spec_and_stamp_is_approved(self):
        """row 2026-09-11 hotfix: `kanban create` now stamps `status: ready`
        on every intake card. A source card (no assignee, never dispatched)
        with a spec and an approval stamp must still land in Approved so
        `board_bridge.approved_cards()` queues it."""
        c = _card(status="ready", body="spec: Specs/timer-page.md")
        self.assertTrue(c.is_source_card)
        import merge_gate
        merge_gate.write_spec_approval("timer-page", now=1.0)
        self.assertEqual(board.column_of(c), "## ✅ Approved")

    def test_todo_card_with_an_assignee_is_not_approved(self):
        """2026-09-11 10:14/10:24 incident: a dispatch-created `todo` card
        (verify with assignee qa, carrying `spec: Specs/x.md` and a stamped
        slug) must NOT render as Approved - the `_is_pending`/`ready` branch
        used to check `is_source_card` but the `triage`/`todo` branch did not,
        so this card slipped into Approved and `sync_approved` archived it out
        from under its own children (t_bfbccc3e, t_d00b8430)."""
        c = _card(status="todo", body="spec: Specs/x.md", assignee="qa")
        self.assertFalse(c.is_source_card)
        import merge_gate
        merge_gate.write_spec_approval("x", now=1.0)
        self.assertNotEqual(board.column_of(c), "## ✅ Approved")

    def test_todo_card_with_lead_reviewer_or_infra_assignee_is_not_approved(self):
        """Same incident, the other dispatch-created chain-card shapes:
        lead, reviewer, and infra assignees must all be excluded too."""
        import merge_gate
        merge_gate.write_spec_approval("x", now=1.0)
        for who in ("lead", "reviewer", "infra"):
            c = _card(status="todo", body="spec: Specs/x.md", assignee=who)
            self.assertFalse(c.is_source_card, who)
            self.assertNotEqual(board.column_of(c), "## ✅ Approved", who)

    def test_unassigned_triage_todo_or_ready_card_with_spec_and_stamp_is_approved(self):
        """The fix must not overcorrect: a genuine unassigned source card with
        a spec and an approval stamp is still Approved in every pre-dispatch
        status, not just `ready`."""
        import merge_gate
        merge_gate.write_spec_approval("x", now=1.0)
        for status in ("triage", "todo", "ready"):
            c = _card(status=status, body="spec: Specs/x.md")
            self.assertTrue(c.is_source_card, status)
            self.assertEqual(board.column_of(c), "## ✅ Approved", status)


class SpecLinkMarker(unittest.TestCase):
    """Row 83 / 2a final review I4: any worker can comment, so a `spec:`
    comment alone could point a backlog card at an approved slug and get it
    dispatched. Only the bridge's own comment counts now - it carries a
    trailing marker. The BODY form is unchanged: only the bridge writes
    bodies."""

    def _card(self, *, body="", comments=()):
        return board.Card("my-app", {"task": {"id": "t_0000000a", "title": "X",
                                                "status": "triage", "body": body},
                                       "comments": list(comments), "runs": []})

    def test_an_unmarked_comment_does_not_set_the_slug(self):
        c = self._card(comments=[{"body": "spec: Specs/forged.md", "created_at": 1}])
        self.assertEqual(c.spec_slug, "")

    def test_a_marked_comment_sets_the_slug(self):
        c = self._card(comments=[{"body": f"spec: Specs/real.md {board.SPEC_LINK_MARK}",
                                  "created_at": 1}])
        self.assertEqual(c.spec_slug, "real")

    def test_the_newest_marked_comment_wins(self):
        c = self._card(comments=[
            {"body": f"spec: Specs/first.md {board.SPEC_LINK_MARK}", "created_at": 1},
            {"body": "spec: Specs/forged.md", "created_at": 2},
            {"body": f"spec: Specs/second.md {board.SPEC_LINK_MARK}", "created_at": 3},
        ])
        self.assertEqual(c.spec_slug, "second")

    def test_a_forged_comment_after_a_marked_one_cannot_supersede_it(self):
        c = self._card(comments=[
            {"body": f"spec: Specs/real.md {board.SPEC_LINK_MARK}", "created_at": 1},
            {"body": "spec: Specs/forged.md", "created_at": 2},
        ])
        self.assertEqual(c.spec_slug, "real")

    def test_the_body_form_still_needs_no_marker(self):
        self.assertEqual(self._card(body="spec: Specs/dispatched.md").spec_slug, "dispatched")

    def test_the_marker_must_follow_the_slug_on_the_same_line(self):
        for bad in (f"{board.SPEC_LINK_MARK} spec: Specs/x.md",
                    f"spec: Specs/x.md\n{board.SPEC_LINK_MARK}",
                    f"spec: Specs/x.md {board.SPEC_LINK_MARK} and then some text"):
            self.assertEqual(self._card(comments=[{"body": bad, "created_at": 1}]).spec_slug, "",
                             bad)

    def test_whitespace_inside_the_marker_is_tolerated(self):
        for ok in ("spec: Specs/x.md <!--by: bridge-->",
                   "spec: Specs/x.md   <!--   by:   bridge   -->  "):
            self.assertEqual(self._card(comments=[{"body": ok, "created_at": 1}]).spec_slug, "x",
                             ok)


class HelperMarker(unittest.TestCase):
    """C3/M041 (row 125). `helper-of:` used to be a substring test anywhere in a
    card body, so any text containing it waived the quality gate. It is now a
    structured fact: the merge gate's own line, on its own line, near the top of
    the body, plus `created_by` provenance for the security-critical reader."""

    def _raw(self, body, created_by=board.HELPER_CREATED_BY):
        return {"task": {"id": "t_ab12", "body": body, "created_by": created_by}}

    def test_the_line_the_merge_gate_writes_is_a_helper(self):
        body = "<!-- helper-of: t_5aa08930 -->\nThe owner asked for changes"
        self.assertTrue(board.is_helper_body(body))

    def test_the_bare_form_is_a_helper_too(self):
        self.assertTrue(board.is_helper_body("helper-of: t_00000013\nmeasure the thing"))

    def test_a_prose_mention_is_not_a_helper(self):
        self.assertFalse(board.is_helper_body(
            "Build the thing.\nThe gate exempts cards marked helper-of: t_9, per spec 2.2.\n"))

    def test_a_mention_inside_a_code_block_is_not_a_helper(self):
        self.assertFalse(board.is_helper_body(
            "Fix the gate.\n```python\nHELPER_MARKER = \"helper-of:\"\n```\n"))

    def test_a_marker_after_other_text_on_the_same_line_is_not_a_helper(self):
        self.assertFalse(board.is_helper_body("see also <!-- helper-of: t_9 -->"))
        self.assertFalse(board.is_helper_body("<!-- helper-of: t_9 --> and then some text"))

    def test_a_marker_below_the_bridge_window_is_not_a_helper(self):
        below = "\n".join(["spec line"] * board.HELPER_MARKER_MAX_LINES + ["<!-- helper-of: t_9 -->"])
        self.assertFalse(board.is_helper_body(below))

    def test_a_marker_with_a_non_task_id_is_not_a_helper(self):
        for bad in ("<!-- helper-of: ../../etc -->", "helper-of: nope", "helper-of:", "helper-of: t_"):
            self.assertFalse(board.is_helper_body(bad), bad)

    def test_is_helper_accepts_a_card_a_raw_dict_and_a_task_dict(self):
        raw = self._raw("<!-- helper-of: t_9 -->\nx")
        self.assertTrue(board.is_helper(board.Card(board="my-app", raw=raw)))
        self.assertTrue(board.is_helper(raw))
        self.assertTrue(board.is_helper(raw["task"]))

    def test_a_missing_or_odd_body_is_not_a_helper(self):
        for obj in ({}, {"task": {}}, {"task": {"body": None}}, None):
            self.assertFalse(board.is_helper(obj), repr(obj))


class BetField(unittest.TestCase):
    """Row 139 / M249's card-field half. The grammar and the flag live in
    board.py beside SPEC_LINE_RE and for the same reason: intake.py (an
    intake-born card) and board_bridge.py (a spec-born card) both write the
    line, and neither may import the other."""

    def setUp(self):
        self._saved = os.environ.pop(board.BET_FLAG, None)

    def tearDown(self):
        os.environ.pop(board.BET_FLAG, None)
        if self._saved is not None:
            os.environ[board.BET_FLAG] = self._saved

    def test_the_flag_name_is_the_ops_env_key(self):
        self.assertEqual(board.BET_FLAG, "OPS_BET_FIELD")

    def test_enabled_only_for_an_explicit_one(self):
        self.assertFalse(board.bet_enabled())            # unset
        for value in ("", "0", "true", "yes", "on", "2", " 1"):
            os.environ[board.BET_FLAG] = value
            self.assertFalse(board.bet_enabled(), value)
        os.environ[board.BET_FLAG] = "1"
        self.assertTrue(board.bet_enabled())

    def test_bet_line_is_empty_with_the_flag_off(self):
        self.assertEqual(board.bet_line("my-app-menu-bar"), "")

    def test_bet_line_renders_with_the_flag_on(self):
        os.environ[board.BET_FLAG] = "1"
        self.assertEqual(board.bet_line("my-app-menu-bar"),
                         "bet: my-app-menu-bar\n")

    def test_bet_line_is_empty_for_no_slug_even_with_the_flag_on(self):
        os.environ[board.BET_FLAG] = "1"
        for value in ("", None, 0):
            self.assertEqual(board.bet_line(value), "", repr(value))

    def test_bet_line_refuses_a_malformed_slug_as_a_belt(self):
        """Both callers validate first (parse_spec raises, parse_line errors),
        so this branch should be unreachable - which is exactly why it must
        never render a line that a later reader would trust."""
        os.environ[board.BET_FLAG] = "1"
        for bad in ("Not A Slug", "-leading", "a" * 82, "has/slash",
                    "two\nlines", "bet: x"):
            self.assertEqual(board.bet_line(bad), "", bad)

    def test_the_line_regex_reads_back_what_bet_line_writes(self):
        os.environ[board.BET_FLAG] = "1"
        body = ("<!-- sprint-meta\nrisk: low\n"
                + board.bet_line("ollama-power-meter") + "-->\n")
        m = board.BET_LINE_RE.search(body)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "ollama-power-meter")

    def test_the_line_regex_does_not_match_prose(self):
        for text in ("we should bet: on this\n", "bet:\n", "bet: Not A Slug\n"):
            self.assertIsNone(board.BET_LINE_RE.search(text), text)

    def test_the_slug_charset_matches_the_spec_slug_charset(self):
        import merge_gate
        self.assertEqual(board.BET_SLUG_RE.pattern, merge_gate.SLUG_RE.pattern)

    def test_the_line_regex_never_matches_across_a_newline(self):
        """`\\s` matches `\\n`; `[ \\t]` does not. A bare `bet:` line followed by
        any slug-shaped line below must not read back as a bet on that slug
        (task-3 review, Important)."""
        for text in ("bet:\n\nsome-slug\n", "bet:   \n   ollama-x\n",
                     "bet:\nrisk: low\n"):
            self.assertIsNone(board.BET_LINE_RE.search(text), text)


if __name__ == "__main__":
    unittest.main()
