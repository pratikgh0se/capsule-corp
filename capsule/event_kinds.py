"""Per-event-kind payload allowlist for `task_events` (row 569 item 1, Research intake
2026-10-04 Part C)."""
import sys

_DECISION = ("schema", "run_id", "task_id", "claim_lock", "question", "options", "recommended",
             "context", "scope", "provisional", "reversible", "searched", "if_no_answer")
_BLOCK = ("reason", "kind", "recurrences", "cause", "incident")
_INTAKE = ("intake_id", "kind", "fingerprint", "source_board", "source_task")

ALLOWED = {kind: frozenset(keys) for kind, keys in {
    "created": ("by",) + _INTAKE,
    "card_fields_set": ("by", "old", "new"),   # R04: check/size/depends/workload verbs
    "assigned": ("by", "assignee", "old_assignee", "backup"),
    "contract_amended": ("issuer", "reason", "old_file_set", "new_file_set", "body_hash_before",
                         "body_hash_after", "backup", "lint_skipped"),
    "claimed": ("lock", "expires"),
    "claim_rejected": ("reason",),
    "claim_extended": ("reason", "worker_pid"),
    "reclaim_deferred": ("reason", "worker_pid"),
    "reclaimed": ("stale_lock", "worker_pid", "heartbeat_stale"),
    "blocked": _BLOCK,
    "block_loop_detected": _BLOCK,
    "dependency_wait": _BLOCK + ("waits_on",),
    "wait_refused": ("branch", "reason"),
    "main_wait_released": ("waits_on", "main"),
    "main_wait_unknown": ("waits_on",),
    "unblocked": ("status", "by"),
    "unblock_refused": ("reason",),
    "accept_refused": ("reason",),
    "request_review": ("by", "after_run"),
    "failed": ("reason", "failure_count", "limit"),
    "parked_by_human": ("by", "trigger", "was", "queued"),
    "park_queued": ("trigger", "by"),
    "stop_refused": ("trigger", "reason"),
    "stop_cancelled": ("reason",),
    "failure_limit": ("reason", "failure_count", "limit"),
    "park_refused": ("trigger", "reason"),
    "failure_reset": ("by",),
    "promoted": ("parents",),
    "lane_leftover_set_aside": ("owner", "backup", "paths"),
    "epic_assigned": ("branch", "assignee"),
    "epic_refused": ("branch", "reason"),
    "reset_refused": ("reason",),
    "epic_synced": ("sha", "main_kept"),
    "landing_checked": ("verdict", "hold", "commit", "card_commit", "base", "checks"),
    "landing_flaky": ("detail",),   # a red suites check that went green on its one retry
    "landing": ("outcome", "commit", "detail"),   # push / refusal / revert outcomes
    "epic_sync_skipped": ("error",),
    "epic_sync_conflict": ("branch", "files"),
    "protected_path_refused": ("path", "task_id", "stage"),   # obs-security-events: row 395
    "needs_input": ("role", "error_class", "error"),
    "epic_published": ("sha",),
    "epic_push_refused": ("error",),
    "first_screen_hold": ("branch", "screens", "commit"),
    "first_screen_ok": ("note", "by"),
    "first_screen_fix": ("note",),
    "screen_refused": ("kind", "reason"),
    "checkpoint": ("chain",),
    "checkpoint_restart": ("resumed_from", "chain"),
    "completed": ("commit", "gates", "acceptance", "outside", "author"),
    "image_request_error": ("reason", "count"),   # M137: a step pass failed on this card
    "image_requested": ("tool", "bin"),   # M137: a worker's missing tool became this card
    "outcome": ("window", "escaped", "signals", "commit", "human_fix"),   # Evals E4
    # E2: a board-level check, so task_id is the sentinel "eval"
    "eval_check": ("metric", "a", "b", "tolerance", "agree", "window", "note", "detail"),
    "eval_ledger_due": ("window", "note", "nudge"),   # inc_6e917366: the ledger row is asked for
    "eval_owner_unset": ("window", "found"),   # inc_1aac8250: owner_acts waits, owner.name unset
    # D234: board-level (task_id "weekly"): the tick held or resumed new card runs at the usage line
    "weekly_pause": ("pct", "reset_at"),
    "weekly_resume": ("pct", "reset_at"),
    "notes": ("entries",),
    "vault_context": ("mode", "switch", "rows", "notes", "withheld", "flags", "bytes"),   # row 626
    "vault_context_verdict": ("verdict", "item", "path", "row", "reason"),   # row 626 (7.4)
    "amendment_no_op_dropped": ("count",),   # inc_10699f92
    # row 584: info-only landing cost, `net` as counted by git
    "cost": ("tokens", "cache_read", "wall_s", "runs", "line", "net", "net_mismatch"),
    "out_of_set_findings": ("findings",),
    "artefact": ("path",),
    "untrack": ("path",),
    "style_warning": ("role", "rules", "count"),   # rows 570, 575: style_lint advisory warnings
    "invalid_document": ("reason",),
    "decision_ids_renamed": ("run_id", "renamed_ids"),   # inc_f6e8a93a: option ids cleaned
    "review_deferred": ("roles", "live", "cap", "reason"),
    "design_brief_missing": ("path",),   # TEAM-23: a risky card waits for its design brief
    "needs_missing": ("path",),          # inc_bb1905e8: a card waits ready for a `needs:` file
    "mac_lane_held": ("reason",),       # D305: a mac card waits ready; the lane cannot run it
    "blind_tester_started": ("card_hash", "start_sha", "snapshot_failed"),   # R30
    "blind_tester_skipped": ("reason", "card_hash", "final"),
    "blind_tester_failed": ("reason",),
    "blind_tests_reported": ("covered", "not_testable", "question"),
    "blind_red_checked": ("verdict", "round", "card_hash", "outcomes", "problems", "tested",
                          "reason", "statements"),   # R31: one per tester round that got checked
    "blind_red_retry": ("reason", "attempt"),   # R31: a runner failure, not a tester round
    "blind_landed": ("blind_commit", "card_hash", "start_sha", "tests", "acceptance", "exempt"),
    "blind_tests_stale": ("card_hash", "landed_hash", "blind_commit", "acceptance_changed"),
    # R32: one per gate-mode blind run; counts and ids only, never a test's content
    "blind_gated": ("counts", "failing", "leaks", "held", "detail"),
    "blind_hold_released": ("reason",),     # R32: its tests landed, the card went back to review
    "decision_requested": _DECISION,
    "crashed": ("role", "error_class", "exit", "tool", "denied_tools", "denial_reason", "error"),
    "review_retry": ("role", "failed_run", "error", "routed"),
    "contract_started": ("run_id",),   # the validator's contract job was submitted
    "contract_failed": ("run_id", "error"),   # a contract job ended by exception (infra failure)
    "contract_flaky": ("cmd", "ids"),   # red on the head, green on one re-run
    "contract_timeout": ("cmd", "seconds"),   # a contract command hit its limit (exit 124)
    "engine_down": ("retry_s",),   # a worker start found the engine down: the card is ready again
    "start_slow": ("retry_s",),   # a worker start timed out on a live engine: backing off
    "engine_restarted": ("restarts_today",),   # the wake hard-restarted a hung engine
    "contract_infra": ("run_id", "why", "retry_s"),   # engine down or start timeout: retry
    "review_started": ("role",),
    "spawn_failed": ("role", "error"),
    "worktree_mismatch": ("role", "error_class", "error"),
    "stale_run": ("role",),
    "timed_out": ("role", "cap", "elapsed", "last_log", "last_step", "worker_pid", "kind"),
    "rate_limited": ("until",),
    "tick_error": ("error", "detail", "where"),
    "intervention": ("kind", "text", "at", "file", "src", "tag", "author"),
    "closed": ("by", "reason", "was"),
    "close_refused": ("reason",),
    "archived": ("by", "reason", "was"),
    "archive_refused": ("reason",),
    "implementer_note": ("kind", "count"),
    "follow_up_filed": ("intake_id", "title", "files"),   # inc_e1d965b7: report.v1 follow_ups
    "checks_overridden": ("checks",),
    "validator_error": ("rows",),
    "aligned_uncited": ("reason",),
    "spec_stale": ("rows", "spec_section"),
    "contradiction": ("role", "examples", "precedence", "ruling_card", "routed"),
    "gate_passed": (),                  # a round that passed every gate; runaway skips it
    "runaway": ("trigger", "detail", "pass", "failures", "verdict_runs"),   # stop-the-line trip
    "runaway_warn": ("trigger", "detail"),
    "claims": ("missing", "pass"),   # pre-flight claim check sent the card to the guide
    "contradiction_loop": ("examples", "resets", "reason", "role"),
    "ruling_started": ("contradiction",),
    "ruling_failed": ("contradiction", "error"),
    "guide_ruling": ("choice", "reason", "contradiction", "role", "examples", "fingerprint",
                     "applied", "text", "card_hash",   # text, card_hash: red-exempt rulings only
                     "fault", "question", "provisional", "reversible"),   # a stall ruling only
    "override": ("check", "reason", "expires_at", "fix_card", "issuer", "board"),
    "override_refused": ("check", "reason", "why"),
    "override_expired": ("check", "expires_at", "fix_card"),
    "incident_opened": ("incident", "kind", "action"),
    "incident_closed": ("incident", "by", "reason", "list_sha"),  # list_sha: row 638 b
    "fix_card_routed": ("incident", "epic", "assignee"),
    "fix_card_attached": ("incident", "card", "board"),
    "incident_reattached": ("incident", "old", "card", "board", "why"),
    "incident_retest": ("incident", "old", "new", "reason", "by"),
    "incident_duplicate": ("incident", "duplicate_of", "by", "basis", "reason"),
    "korin_started": ("incident",),
    "korin_failed": ("incident", "error"),
    "korin_gave_up": ("incident", "runs"),
    "korin_applied": ("incident", "fix_card", "note"),
    "intake_ingested": _INTAKE + ("deduped_into",),
    "preview_asked": ("count",),          # F1: the owner was shown a preview card
    "preview_accepted": ("by", "note"),
    "preview_rejected": ("by", "text", "fix_card", "count"),
    # the stall supervisor (capsule/stall.py): a board-level stall uses the task id "stall"
    "stall_opened": ("class", "subject", "since", "incident"),
    "stall_cleared": ("class", "subject", "duration_s", "cleared_by"),
    "stall_ruling": ("class", "subject", "age_s", "reason", "pass"),   # L1: asks the guide
    # quiet stops (capsule/stall_continue.py): one per implementer run, and one per continue
    "run_closed": ("role", "outcome", "kind", "session_id", "before", "after"),
    # owner-wait timers (capsule/stall_owner.py): a card set aside, a choice Capsule made alone
    "set_aside": ("class", "why", "question", "since"),
    "auto_decision": ("id", "class", "option", "source", "undo", "basis", "label", "dossier",
                      "cites"),
    "auto_decision_vetoed": ("id", "by"),
    "stall_action": ("class", "subject", "action", "result", "pid", "kind", "n", "cause", "path",
                     "from_run"),
}.items()}
RUN_ID = "run_id"      # added by db.event itself; allowed on every kind


def problems(kind, payload):
    """[] or one message: an unknown kind, or payload keys this kind does not allow."""
    allowed = ALLOWED.get(kind)
    if allowed is None:
        return [f"unknown event kind {kind!r} (add it to capsule/event_kinds.py ALLOWED)"]
    extra = sorted(set(payload or {}) - allowed - {RUN_ID})
    return [f"event {kind!r}: unexpected payload key(s) {extra} (allowed: {sorted(allowed)})"] if extra else []


def warn(kind, payload):
    """One stderr line per `problems` entry; never raises, so the caller still writes its row."""
    for line in problems(kind, payload):
        print(f"capsule db: {line}", file=sys.stderr)
