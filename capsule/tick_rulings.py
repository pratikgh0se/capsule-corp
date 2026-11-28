"""The ruling stage of the tick, as a mixin: `Tick(TickRuns, TickReview, TickRulings)`
supplies the host (`self._run`, `self._spool_root`, `self.schemas`, ...). The implementer-report
router, the git-lock notes, the contradictions an intent example raises, and the guide's ruling
packet and history; this module never imports `capsule.tick`, and never calls a model."""
import json

from capsule import blocks, contradiction, intent, lease, runaway, stall_ruling
from capsule.blind import exempt as blind_exempt
from capsule.db import event, txn
from capsule.tick_review import REVIEW_ROLES


class TickRulings:
    # The container's read-only git dir is correct by design, so a worker naming it is not a
    # blocker (detection: `blocks.is_git_lock_note`). `commands` is model-written prose with no
    # second oracle: `Result.raw` persists only on the fatal crash path, never the handoff path.
    @staticmethod
    def _is_git_lock_note(text):
        return blocks.is_git_lock_note(text)

    @classmethod
    def _drop_git_lock_notes(cls, report):
        """A copy of `report` with any git-index-lock note stripped from `left_undone`/`issues`;
        every other field, and the stored handoff, keep the model's own text."""
        out = dict(report)
        for key in ("left_undone", "issues"):
            if key in out:
                out[key] = [x for x in out[key] if not cls._is_git_lock_note(x)]
        return out

    def _note_git_lock(self, conn, task_id, report, run, now):
        """One informational `implementer_note` event when the report names the read-only
        container git dir - never a block, never a notice, never a Korin incident
        (`implementer_note` is not in `incidents.EVENT_KINDS`)."""
        hits = [x for x in (report.get("issues") or []) + (report.get("left_undone") or [])
               if self._is_git_lock_note(x)]
        if hits:
            with txn(conn):
                event(conn, task_id, "implementer_note",
                      {"kind": "git_dir_read_only", "count": len(hits)},
                      run_pk=run["id"], run_id=run["run_id"], now=now)

    @staticmethod
    def _route(report):
        """Where an implementer report goes: `checkpoint` (restart), `hold` (left undone/issues,
        always blocking - informational goes in `notes` instead) or `review`. The one router
        `_close` branches on and passes to `intent.check` as `$outcome`."""
        if report["status"] == "checkpoint":
            return "checkpoint"
        return "hold" if report["left_undone"] or report["issues"] else "review"

    @staticmethod
    def _ruling_of(run):
        """The `contradiction` event id a guide run rules on, or None for any other run."""
        try:
            return json.loads(run["metadata"] or "{}").get("ruling_of")
        except (ValueError, AttributeError):
            return None

    def _guide_contradiction(self, conn, run, found, now):
        """The guide's own alignment verdict contradicts an intent example: a failed review run,
        not a verdict on the work. No handoff is stored, so the round retries the guide once
        (`review_retry`) and a second miss is `review_infra` for the owner. It never goes to a
        ruling: a reset sends the implementer to fix a verdict it cannot edit. Kept in report/."""
        what = "; ".join(f"{f['id']} ({f['source']}): {', '.join(f['differs'])}" for f in found)
        with txn(conn):
            conn.execute("UPDATE task_runs SET error_class='invalid_output'"
                         " WHERE id=? AND error_class IS NULL", (run["id"],))
            lease.end_run(conn, run["id"], status="failed", outcome="contradiction",
                          error=f"intent contradiction: {what}"[:2000], now=now)
            event(conn, run["task_id"], "contradiction",
                  {"role": run["profile"], "examples": found, "precedence": intent.PRECEDENCE,
                   "ruling_card": None, "routed": "review_retry"},
                  run_pk=run["id"], run_id=run["run_id"], now=now)

    def _review_live(self, conn, cfg, task_id, *, exclude_pk=None):
        """Whether a validator/reviewer/guide run on this task has genuinely not finished yet: a
        run row with no `ended_at` whose spool has no `run.json` on disk. A run closed earlier in
        the same pass does not count: a reviewer contradiction waits for a live sibling, never
        for bookkeeping order within one pass."""
        root = self._spool_root(cfg)
        rows = conn.execute("SELECT id, run_id FROM task_runs WHERE task_id=?"
                            " AND profile IN (?,?,?,?) AND ended_at IS NULL",
                            (task_id,) + REVIEW_ROLES).fetchall()
        return any(r["id"] != exclude_pk and not (root / r["run_id"] / "run.json").exists()
                   for r in rows)

    def _contradiction(self, conn, cfg, run, sp, found, now):
        """A schema-valid document an intent example calls wrong: a `guide_ruling` request in
        the run's spool, one `contradiction` event and one outbox line, and the card goes to
        triage (block_kind `guide_ruling`) instead of a hold for the owner; step 5b then starts the
        guide's ruling run. A card no longer running or in review records the event only.

        A contradiction while a sibling review run of the same round is still live only records
        the event as `routed: "pending"` and leaves the card in `review`. `_review_one` finishes
        the routing (`contradiction.route_contradiction`) once every review run of the round has
        ended, so `_open_findings`' rerun bundle sees every finished role's handoff.

        Ending this run (`lease.end_run`) shares the routing attempt's transaction: a failed
        `route_contradiction` leaves this run un-ended too, or `_review_one` would score an
        ordinary fail instead of retrying the routing."""
        role, task_id = run["profile"], run["task_id"]
        live = self._review_live(conn, cfg, task_id, exclude_pk=run["id"])
        with txn(conn):
            lease.end_run(conn, run["id"], status="done" if role in REVIEW_ROLES else "blocked",
                          outcome="contradiction", summary=", ".join(f["id"] for f in found),
                          now=now)
            if live:
                event(conn, task_id, "contradiction",
                      {"role": role, "examples": found, "precedence": intent.PRECEDENCE,
                       "ruling_card": None, "routed": "pending"},
                      run_pk=run["id"], run_id=run["run_id"], now=now)
                return
            contradiction.route_contradiction(conn, task_id, role, found, run, sp, self.schemas,
                                              now)

    def _ruling_packet(self, conn, ev):
        """What the guide rules on: the contradicted document, each example, the precedence."""
        if ev["kind"] in runaway.KINDS:
            return runaway.packet(conn, ev)
        if ev["kind"] == stall_ruling.KIND:
            return stall_ruling.packet(conn, ev)
        payload = json.loads(ev["payload"])
        run = self._run(conn, ev["run_id"]) if ev["run_id"] is not None else None
        document = blind_exempt.report(conn, ev["id"], run)     # scrubbed of blind rows' texts
        lines = {ex["id"]: ex for ex in intent.load(payload["role"], self.intent_dir)}
        examples = [dict(f, example=lines.get(f["id"])) for f in payload["examples"]]
        return {"role": payload["role"], "document": document, "examples": examples,
                "precedence": payload["precedence"], "contradiction": ev["id"]}

    @staticmethod
    def _document_wrong_resets(conn, task_id, example_id):
        """Applied document-wrong rulings on `task_id` whose contradiction named `example_id`."""
        n = 0
        for (raw,) in conn.execute("SELECT payload FROM task_events"
                                   " WHERE task_id=? AND kind='guide_ruling'", (task_id,)):
            p = json.loads(raw or "{}")
            ids = p.get("fingerprint") or [i.strip() for i in (p.get("examples") or "").split(",")
                                           if i.strip()]
            n += (p.get("choice") == "document-wrong" and bool(p.get("applied"))
                  and example_id in ids)
        return n

    def _guide_rulings(self, conn, task_id):
        """The guide's rulings on this card since its latest body amendment, oldest first, for the
        implementer rerun. A ruling on the guide's or the reviewer's own verdict is not the
        implementer's to act on."""
        return [json.loads(r[0])["reason"] for r in conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='guide_ruling' AND id>?"
            " ORDER BY id", (task_id, runaway.rerun_amendment(conn, task_id)[0]))
            if json.loads(r[0]).get("choice") in ("document-wrong", "continue", *stall_ruling.FAULT)
            and json.loads(r[0]).get("role") not in ("guide", "reviewer")]
