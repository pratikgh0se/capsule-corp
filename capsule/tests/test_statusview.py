# mutation-check: targets-predate-range b957739c9 -- owner-word rename, no behaviour pinned ::
#   OwnerAssigneeIsNeverDispatched.test_ready_card_assigned_to_owner_is_not_dispatchable,
#   Sections.test_a_task_is_in_one_section_by_precedence,
#   Sections.test_human_file_waiting_on_owner_lands_in_needs_you,
#   Sections.test_owner_assignee_lands_in_needs_you
"""`capsule/statusview.py`: the read-only data layer behind `capsule status` (visibility spec
section 3 and 8). Every board here is a temp DB built with the repo's own schema helper."""
import fcntl
import hashlib
import json
import re
import tempfile
import time
import unittest
from pathlib import Path

from capsule import config, db, dispatch, paths, statusview

NOW = 1_800_000_000
OLD = NOW - 10 * 3600
BOARD = "bx"
ID_RE = re.compile(r"t_[0-9a-f]{8}|inc_|python3 -m|/Users/")


def without_refs(value):
    if isinstance(value, dict):
        return {k: without_refs(v) for k, v in value.items() if k != "ref"}
    return [without_refs(v) for v in value] if isinstance(value, list) else value


class Fixture(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.events = self.home / "events.jsonl"
        self.conn = self.make_board(BOARD)
        self.addCleanup(self.conn.close)

    def make_board(self, board):
        d = self.home / "boards" / board
        d.mkdir(parents=True)
        db.create_board(d / "kanban.db")
        return db.connect(d / "kanban.db")

    def task(self, title="Some card", status="ready", kind=None, assignee="dev-backend", at=OLD,
             **extra):
        tid = db.new_task(self.conn, title=title, assignee=assignee, status=status, now=at)
        sets = {"block_kind": kind, **extra}
        for col, val in sets.items():
            self.conn.execute(f"UPDATE tasks SET {col}=? WHERE id=?", (val, tid))
        return tid

    def run_task(self, title="Run card", profile="implementer", model="claude-sonnet-5-5",
                 beat=NOW - 60):
        tid = self.task(title, status="running")
        cur = self.conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, started_at, model, last_heartbeat_at)"
            " VALUES (?,?,'running',?,?,?)", (tid, profile, NOW - 12 * 60, model, beat))
        self.conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (cur.lastrowid, tid))
        return tid

    def seat(self, key, owner="implementer", beat=NOW - 60, repo="repo"):
        d = self.home / "queue" / repo
        d.mkdir(parents=True, exist_ok=True)
        p = d / "owners.json"
        doc = json.loads(p.read_text()) if p.exists() else {}
        doc[key] = {"owner": owner, "heartbeat": beat}
        p.write_text(json.dumps(doc))

    def snap(self):
        return statusview.snapshot(self.home, now=NOW, boards=[BOARD], events_path=self.events)

    def names(self, section, snap=None):
        return {i["name"] for i in (snap or self.snap())[section]}


class WeeklyPause(Fixture):
    RESET = NOW + 3 * 86400

    def hold(self, held, reset_at=RESET, payload=None):
        db.guard_set(self.conn, "weekly_pause", {"held": held}, now=NOW)
        db.event(self.conn, "weekly", "weekly_pause", {"pct": 93, "reset_at": reset_at}, now=NOW)
        if payload is not None:
            self.conn.execute("UPDATE task_events SET payload=? WHERE kind='weekly_pause'",
                              (payload,))
            self.conn.commit()

    def line(self):
        from capsule import status_cli
        snap = self.snap()
        self.assertEqual(status_cli.render(snap).splitlines()[1], snap["weekly_pause"])
        return snap["weekly_pause"]

    def test_held_shows_pct_and_local_reset_in_json_and_screen(self):
        from capsule import status_cli
        self.hold(True)
        want = (f"paused: weekly usage 93% (resets "
                f"{time.strftime('%a %H:%M', time.localtime(self.RESET))})")
        snap = self.snap()
        self.assertEqual(snap["weekly_pause"], want)
        self.assertEqual(status_cli.render(snap).splitlines()[1], want)

    def test_reset_none_or_already_past_omits_the_resets_part(self):
        self.hold(True, reset_at=None)
        self.assertEqual(self.line(), "paused: weekly usage 93%")
        self.hold(True, reset_at=NOW - 60)
        self.assertEqual(self.line(), "paused: weekly usage 93%")

    def test_a_bad_row_never_raises_and_falls_back_to_a_question_mark(self):
        for payload in ('{"pct": 93, "reset_at": "soon"}', '{"pct": 93, "reset_at": 1e30}',
                        '[1, 2]', 'not json'):
            with self.subTest(payload=payload):
                self.hold(True, payload=payload)
                self.assertEqual(self.line(), "paused: weekly usage ?%")
        self.hold(True, payload="null")
        self.assertEqual(self.snap()["weekly_pause"], "paused: weekly usage ?%")

    def test_resumed_or_never_held_shows_no_line(self):
        from capsule import status_cli
        self.assertNotIn("weekly_pause", self.snap())
        self.hold(True)
        db.guard_set(self.conn, "weekly_pause", {"held": False}, now=NOW)
        snap = self.snap()
        self.assertNotIn("weekly_pause", snap)
        self.assertNotIn("paused:", status_cli.render(snap))


class Sections(Fixture):
    def test_running_task_has_role_model_minutes(self):
        self.run_task("Retry on rate limit")
        (r,) = self.snap()["running"]
        self.assertEqual((r["name"], r["role"], r["role_label"], r["model"], r["minutes"]),
                         ("Retry on rate limit", "implementer", "builder", "Sonnet 5.5", 12))
        self.assertIsNone(r["quiet_minutes"])

    def test_quiet_running_task_is_both_running_and_attention_when_unowned(self):
        tid = self.run_task("Silent one", beat=NOW - 25 * 60)
        snap = self.snap()
        self.assertEqual(snap["running"][0]["quiet_minutes"], 25)
        self.assertTrue(any("Silent one" in a["what"] for a in snap["attention"]["open"]))
        self.seat(tid)
        snap = self.snap()
        self.assertFalse(any("Silent one" in a["what"] for a in snap["attention"]["open"]))
        self.assertEqual(snap["attention"]["handled_count"], 1)

    def test_every_needs_input_kind_lands_in_needs_you(self):
        kinds = statusview.HIS_KINDS
        for kind in kinds:
            self.task(f"hold {kind}", status="blocked", kind=kind)
        self.assertEqual(self.names("needs_you"), {f"hold {k}" for k in kinds})
        self.assertEqual(self.snap()["blocked"], [])

    def test_triage_needs_input_lands_in_needs_you(self):
        self.task("Looped", status="triage", kind="needs_input")
        self.assertEqual(self.names("needs_you"), {"Looped"})

    def test_decision_request_lands_in_needs_you_with_its_question(self):
        tid = self.task("Needs a ruling", status="triage")
        self.conn.execute(
            "INSERT INTO requests (run_id, schema, body, status, created_at)"
            " VALUES ('r','decision.v1',?,'applied',1)",
            (json.dumps({"task_id": tid, "question": "Keep the line? See contracts/x.md"}),))
        (n,) = self.snap()["needs_you"]
        self.assertEqual(n["ask"], "Keep the line? See a file")

    def test_owner_assignee_lands_in_needs_you(self):
        self.task("Owner only", status="todo", assignee="owner")
        self.assertEqual(self.names("needs_you"), {"Owner only"})

    def test_human_file_waiting_on_owner_lands_in_needs_you(self):
        d = self.home / "boards" / BOARD / "human"
        d.mkdir()
        (d / "a.json").write_text(json.dumps({"waits": "owner", "kind": "close", "at": 5}))
        (d / "b.json").write_text(json.dumps({"waits": "sample-app", "kind": "park"}))
        self.assertEqual(self.names("needs_you"), {"close"})

    def test_parked_and_dependency_and_unclear_land_in_blocked(self):
        self.task("Parked one", status="parked")
        self.task("Held by parent", status="todo", kind="dependency")
        self.task("Blocked parked", status="blocked", kind="parked")
        self.task("No reason", status="blocked")
        reasons = {b["name"]: b["reason"] for b in self.snap()["blocked"]}
        self.assertEqual(reasons, {"Parked one": "parked on purpose",
                                   "Held by parent": "waiting on another task",
                                   "Blocked parked": "parked on purpose",
                                   "No reason": "stuck, reason unclear"})

    def test_stale_unowned_ready_card_is_attention_owned_one_is_handled(self):
        self.task("Forgotten", status="ready", at=NOW - 4 * 3600)
        owned = self.task("Looked after", status="ready", at=NOW - 4 * 3600)
        self.task("Fresh", status="ready", at=NOW - 600)
        self.seat(owned)
        snap = self.snap()
        text = " ".join(a["what"] for a in snap["attention"]["open"])
        self.assertIn("Forgotten", text)
        self.assertNotIn("Looked after", text)
        self.assertNotIn("Fresh", text)
        self.assertEqual(snap["attention"]["handled_count"], 1)

    def test_stale_seat_does_not_own_and_is_not_counted_outside(self):
        tid = self.task("Forgotten", status="ready", at=NOW - 4 * 3600)
        self.seat(tid, beat=NOW - 41 * 60)
        snap = self.snap()
        self.assertTrue(any("Forgotten" in a["what"] for a in snap["attention"]["open"]))
        self.assertEqual(snap["outside_factory"]["seats"], 0)

    def test_incident_without_fix_card_is_attention_with_fix_card_is_handled(self):
        fix = self.task("Fix it", status="todo")
        for kind, fc in (("crash", None), ("hang", fix)):
            self.conn.execute("INSERT INTO incidents"
                              " (id, board, kind, opened_at, fingerprint, fix_card)"
                              " VALUES (?,?,?,?,?,?)", (f"inc_{kind}", BOARD, kind, 1, kind, fc))
        snap = self.snap()
        text = " ".join(a["what"] for a in snap["attention"]["open"])
        self.assertIn("1 crash incident", text)
        self.assertNotIn("hang", text)
        self.assertEqual(snap["attention"]["handled_count"], 1)

    def test_incident_whose_fix_card_matches_no_task_is_not_handled(self):
        self.conn.execute("INSERT INTO incidents"
                          " (id, board, kind, opened_at, fingerprint, fix_card)"
                          " VALUES ('inc_x', ?, 'crash', 1, 'c', 't_00000000')", (BOARD,))
        snap = self.snap()
        self.assertEqual(snap["attention"]["handled_count"], 0)
        self.assertTrue(any("1 crash incident" in a["what"] for a in snap["attention"]["open"]))

    def _incident(self, name, kind, fix):
        self.conn.execute("INSERT INTO incidents"
                          " (id, board, kind, opened_at, fingerprint, fix_card)"
                          " VALUES (?,?,?,?,?,?)", (f"inc_{name}", BOARD, kind, 1, name, fix))

    def test_an_incident_whose_fix_card_is_held_names_that_card_in_attention(self):
        blocked = self.task("Held fix", status="blocked")
        triage = self.task("Triage fix", status="triage")
        live = self.task("Live fix", status="todo")
        self._incident("a", "crash", blocked)
        self._incident("b", "crash", triage)
        self._incident("c", "hang", live)
        snap = self.snap()
        items = snap["attention"]["open"]
        (crash,) = [x for x in items if "crash incident" in x["what"]]
        self.assertIn("2 crash incidents", crash["what"])
        self.assertIn("Held fix", crash["what"])
        self.assertIn("Triage fix", crash["what"])
        self.assertNotRegex(crash["what"], r"t_[0-9a-f]{8}")
        self.assertEqual(sorted(crash["ref"]["tasks"]), sorted([blocked, triage]))
        blob = json.dumps(items)
        self.assertNotIn("Live fix", blob)
        self.assertNotIn(live, blob)
        self.assertEqual(snap["attention"]["handled_count"], 1)

    def test_held_fix_card_line_shows_five_titles_then_the_rest_as_a_count(self):
        ids = [self.task(f"Held{i}", status="blocked") for i in range(7)]
        for i, t in enumerate(ids):
            self._incident(str(i), "crash", t)
        (item,) = [x for x in self.snap()["attention"]["open"] if "crash incident" in x["what"]]
        self.assertEqual(sum(f"Held{i}" in item["what"] for i in range(7)), 5)
        self.assertIn("and 2 more", item["what"])
        self.assertEqual(sorted(item["ref"]["tasks"]), sorted(ids))

    def test_two_incidents_sharing_one_held_fix_card_name_it_once(self):
        shared = self.task("Shared fix", status="blocked")
        self._incident("p", "crash", shared)
        self._incident("q", "crash", shared)
        (item,) = [x for x in self.snap()["attention"]["open"] if "crash incident" in x["what"]]
        self.assertIn("2 crash incidents", item["what"])
        self.assertEqual(item["what"].count("Shared fix"), 1)
        self.assertEqual(item["ref"]["tasks"], [shared])

    def test_incident_without_held_fix_card_adds_no_id(self):
        self._incident("n", "crash", None)
        self._incident("m", "crash", "t_00000000")
        (item,) = [x for x in self.snap()["attention"]["open"] if "crash incident" in x["what"]]
        self.assertNotIn("held fix cards", item["what"])
        self.assertNotIn("ref", item)

    def test_invalid_decision_request_does_not_ask_for_a_ruling(self):
        tid = self.task("Not really asked", status="triage")
        self.conn.execute(
            "INSERT INTO requests (run_id, schema, body, status, created_at)"
            " VALUES ('r','decision.v1',?,'invalid',1)",
            (json.dumps({"task_id": tid, "question": "Ignore me?"}),))
        self.assertEqual(self.snap()["needs_you"], [])

    def test_a_task_is_in_one_section_by_precedence(self):
        self.task("Both", status="blocked", kind="needs_input", assignee="owner")
        snap = self.snap()
        self.assertEqual((len(snap["needs_you"]), len(snap["blocked"])), (1, 0))

    def test_outside_factory_counts_fresh_seats(self):
        self.seat("a", "cos-keeper")
        self.seat("b", "cos-keeper")
        self.seat("c", "implementer")
        self.seat("d", "landed")
        out = self.snap()["outside_factory"]
        self.assertEqual((out["seats"], out["summary"]), (3, "2 CoS keepers, 1 builder"))

    def test_done_and_archived_tasks_are_not_shown(self):
        self.task("Finished", status="done")
        self.task("Gone", status="archived")
        snap = self.snap()
        self.assertEqual([snap["running"], snap["needs_you"], snap["blocked"]], [[], [], []])


class LoopAndBoards(Fixture):
    def test_loop_down_when_no_lock_and_other_sections_still_render(self):
        self.task("Parked one", status="parked")
        snap = self.snap()
        self.assertEqual(snap["boards"][0]["loop"], "down")
        self.assertEqual(len(snap["blocked"]), 1)
        self.assertIn("loop is not running", snap["attention"]["open"][0]["what"])

    def _hold_lock(self):
        f = open(self.home / f"tick-{BOARD}.lock", "w")
        fcntl.flock(f, fcntl.LOCK_EX)
        self.addCleanup(f.close)

    def _heartbeat(self, board, ts):
        line = json.dumps({"kind": "tick_health", "board": board, "ts": ts})
        self.events.write_text(line + "\n")

    def test_loop_alive_when_lock_held_and_heartbeat_fresh(self):
        self._hold_lock()
        self._heartbeat(BOARD, NOW - 30)
        self.assertEqual(self.snap()["boards"][0]["loop"], "alive")

    def test_loop_stalled_when_lock_held_and_heartbeat_old(self):
        self._hold_lock()
        self._heartbeat(BOARD, NOW - 600)
        self.assertEqual(self.snap()["boards"][0]["loop"], "stalled")
        self.assertIn("stalled", self.snap()["attention"]["open"][0]["what"])

    def test_heartbeat_of_another_board_does_not_count(self):
        self._hold_lock()
        self._heartbeat("other", NOW - 600)
        self.assertEqual(self.snap()["boards"][0]["loop"], "alive")

    def test_unreadable_board_is_down_with_attention_not_a_crash(self):
        snap = statusview.snapshot(self.home, now=NOW, boards=[BOARD, "missing"],
                                   events_path=self.events)
        self.assertEqual([b["loop"] for b in snap["boards"]][1], "down")
        text = " ".join(a["what"] for a in snap["attention"]["open"])
        self.assertIn("missing: the board cannot be opened", text)

    def test_corrupt_board_file_is_down_with_attention(self):
        bad = self.home / "boards" / "bad"
        bad.mkdir()
        (bad / "kanban.db").write_text("not a database")
        snap = statusview.snapshot(self.home, now=NOW, boards=["bad"], events_path=self.events)
        self.assertTrue(any("bad:" in a["what"] for a in snap["attention"]["open"]))

    def test_auto_landing_reads_the_live_config(self):
        cfg = self.home / "boards" / BOARD / "config.json"
        for mode, want in (("land", True), ("dry-run", False), ("off", False)):
            cfg.write_text(json.dumps({"lander": {"mode": mode}}))
            self.assertEqual(self.snap()["boards"][0]["auto_landing"], want, mode)
        cfg.unlink()
        self.assertFalse(self.snap()["boards"][0]["auto_landing"])

    def test_snapshot_never_writes_the_board(self):
        self.task("Parked one", status="parked")
        self.run_task()
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        path = self.home / "boards" / BOARD / "kanban.db"
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        self.snap()
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)


class Feed(Fixture):
    """The Grafana feed alarm reads plain files and never takes a lock (spec section 3)."""

    def _spool(self, size=100, offset=0, last=None):
        d = self.home / "lookout"
        d.mkdir(exist_ok=True)
        (d / "spool.jsonl").write_bytes(b"x" * size)
        (d / "spool.offset").write_text(str(offset))
        if last is not None:
            (d / "ship.last.json").write_text(json.dumps(last))

    def _feed(self):
        return statusview._feed_problem(self.home, NOW)

    def test_fresh_good_ship_is_fine_even_with_bytes_waiting(self):
        self._spool(last={"ts": NOW - 60, "rc": 0})
        self.assertIsNone(self._feed())

    def test_stale_ship_with_bytes_waiting_alarms(self):
        self._spool(last={"ts": NOW - 3600, "rc": 0})
        self.assertIn("Grafana", self._feed()["what"])

    def test_never_shipped_with_bytes_waiting_alarms(self):
        self._spool()
        self.assertIn("Grafana", self._feed()["what"])

    def test_failed_last_ship_alarms(self):
        self._spool(last={"ts": NOW - 60, "rc": 1})
        self.assertIsNotNone(self._feed())

    def test_nothing_waiting_is_fine_and_missing_spool_is_fine(self):
        self._spool(size=100, offset=100)
        self.assertIsNone(self._feed())
        self.assertIsNone(statusview._feed_problem(Path(tempfile.mkdtemp()), NOW))


class Names(Fixture):
    def test_task_without_short_name_shows_trimmed_title(self):
        self.task("Report: Retry the rate limited calls with backoff and jitter please",
                  status="parked")
        self.assertEqual(self.names("blocked"), {"Retry the rate limited calls with"})

    def test_short_name_and_number_are_used_when_the_columns_exist(self):
        self.task("A long original title nobody wants", status="parked",
                  short_name="Slack routing", number=27)
        (b,) = self.snap()["blocked"]
        self.assertEqual((b["name"], b["number"]), ("Slack routing", 27))

    def test_fallback_strips_prefix_and_cuts_to_six_words(self):
        self.assertEqual(statusview._trimmed_title("Report: a b c d e f g h"), "a b c d e f")

    def test_json_carries_name_and_number_from_a_created_task(self):
        db.new_task(self.conn, title="Long title here", assignee="dev-backend", status="parked",
                    short_name="Retry on rate limit", now=OLD)
        (b,) = self.snap()["blocked"]
        self.assertEqual((b["name"], b["number"]), ("Retry on rate limit", 1))

    def test_ids_never_reach_a_name_or_an_ask(self):
        self.task("Redo t_9069a758 now", status="blocked", kind="needs_input")
        text = json.dumps(without_refs(self.snap()))
        self.assertFalse(ID_RE.search(text), text)

    def test_items_carry_their_ref_for_the_json_flag(self):
        self.task("Parked one", status="parked")
        snap = self.snap()
        self.assertEqual(snap["blocked"][0]["ref"]["task"][:2], "t_")


class Helpers(unittest.TestCase):
    def test_model_labels(self):
        self.assertEqual(statusview._model_label("claude-sonnet-5-5"), "Sonnet 5.5")
        self.assertEqual(statusview._model_label("claude-haiku-4-5-20251001"), "Haiku 4.5")
        self.assertEqual(statusview._model_label("claude-opus-5"), "Opus 5")
        self.assertEqual(statusview._model_label(None), "")

    def test_plain_strips_ids_paths_commands_and_cuts(self):
        self.assertEqual(statusview.plain("run python3 -m capsule on t_0123abcd in /a/b.py"),
                         "run capsule on that item in a file")
        self.assertEqual(len(statusview.plain("word " * 100)), statusview.PLAIN_LIMIT)

    def test_trimmed_title_strips_prefix_and_cuts_to_six_words(self):
        self.assertEqual(statusview._trimmed_title("Report: one two three four five six seven"),
                         "one two three four five six")
        self.assertEqual(statusview._trimmed_title(""), "untitled task")


class OwnerAssigneeIsNeverDispatched(unittest.TestCase):
    """Spec section 3 rule 4 / card 1: a card assigned `owner` must never be dispatched."""

    def test_ready_card_assigned_to_owner_is_not_dispatchable(self):
        cfg = config.load(paths.REPO / "capsule" / "config.example.json")
        d = Path(tempfile.mkdtemp())
        db.create_board(d / "k.db")
        conn = db.connect(d / "k.db")
        self.addCleanup(conn.close)
        db.new_task(conn, title="mine", assignee="owner", branch=cfg["epics"][0])
        self.assertNotIn("owner", cfg["assignees"])
        self.assertEqual(dispatch.ready(conn, cfg, host_tiers={"strong"}), [])


class DefaultBoards(unittest.TestCase):
    def names(self, home):
        snap = statusview.snapshot(home, now=NOW)
        return [b["name"] if "name" in b else b.get("board") for b in snap["boards"]]

    def test_configured_boards_are_the_default(self):
        home = Path(tempfile.mkdtemp())
        (home / "config.json").write_text('{"unblocked": {"boards": ["aa", "bb"]}}')
        self.assertEqual(self.names(home), ["aa", "bb"])

    def test_no_config_uses_only_the_shipped_default(self):
        self.assertEqual(self.names(Path(tempfile.mkdtemp())), ["capsule-corp"])


if __name__ == "__main__":
    unittest.main()
