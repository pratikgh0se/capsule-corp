"""Card t_4f2e12fc (C10): review seats and heavy seats as caps shared across every board under
$CAPSULE_HOME/boards (`caps.max_review` default 2, `caps.max_heavy` default 3). A board whose
DB cannot be read counts as full (fail closed) and says so once an hour on the outbox."""
import copy
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from capsule import config, db, dispatch, incidents, intent, lease, paths, seats
from capsule.tests import tmpclean
from capsule.tests import test_korin
from capsule.tests.test_tick import IMPL_DONE, REV_CONTRADICTS, TickCase, ruling

CFG = config.load(paths.REPO / "capsule" / "config.capsule-corp.example.json")
CFG["caps"]["external_heavy"] = 0   # the shipped template reserves 1; these tests count all seats
EPIC = CFG["epics"][0]
ELSEWHERE = "epic/elsewhere"   # seat-holding cards sit off the ready card's branch (serial rule)


class SeatCase(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(tmpclean.sweep, self.home)
        self.own = self.mkboard(CFG["board"])

    def mkboard(self, name):
        path = paths.board_db(name, self.home)
        path.parent.mkdir(parents=True, exist_ok=True)
        db.create_board(path)
        conn = db.connect(path)
        self.addCleanup(conn.close)
        return conn

    @staticmethod
    def card(conn, status="ready", branch=EPIC):
        tid = db.new_task(conn, title="c", assignee="dev-backend", branch=branch)
        if status != "ready":
            conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, tid))
        return tid

    def live(self, conn, role, *, ended=False):
        """A live (or, with `ended`, finished) review-class run on a fresh card in `review`."""
        rid = uuid.uuid4().hex
        card = self.card(conn, "review", ELSEWHERE)
        pk = lease.start_run(conn, card, role=role, claim_lock=f"h:{rid}", expires=10**9,
                             run_id=rid, now=1000)
        if ended:
            lease.end_run(conn, pk, status="completed", outcome="completed", now=1001)

    def run_row(self, conn, *, pid=None, started=1900, host=None, role="reviewer"):
        """A local (or `host`) review run recorded with `worker_pid=pid`."""
        rid = uuid.uuid4().hex
        card = self.card(conn, "review", ELSEWHERE)
        lock = f"{host or socket.gethostname()}:{rid}"
        pk = lease.start_run(conn, card, role=role, claim_lock=lock, expires=10**9,
                             run_id=rid, now=started)
        conn.execute("UPDATE task_runs SET worker_pid=? WHERE id=?", (pid, pk))

    @staticmethod
    def dead_pid():
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        return proc.pid

    def running_impl(self, conn, expires=10**9):
        tid = self.card(conn, "running", ELSEWHERE)
        conn.execute("UPDATE tasks SET claim_expires=? WHERE id=?", (expires, tid))

    def corrupt_board(self, name="broken"):
        path = paths.board_db(name, self.home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"this is not a sqlite database at all" * 40)


class Counts(SeatCase):
    def test_fable_is_not_a_review_profile(self):
        self.assertNotIn("fable", seats.REVIEW_PROFILES)

    def test_review_roles_count_live_runs_only(self):
        for role in ("reviewer", "validator", "guide", "korin"):
            self.live(self.own, role)
        self.live(self.own, "reviewer", ended=True)
        self.live(self.own, "implementer")          # an implementer run is not a review seat
        got = seats.counts(self.own, self.home, CFG["board"], now=2000)
        self.assertEqual((got["review"], got["unreadable"]), (4, []))

    def test_heavy_is_implementers_plus_reviews_across_boards(self):
        other = self.mkboard("other-app")
        self.running_impl(self.own)
        self.running_impl(other)
        self.running_impl(other, expires=500)        # an expired claim is not running
        self.live(other, "reviewer")
        got = seats.counts(self.own, self.home, CFG["board"], now=2000)
        self.assertEqual((got["implementers"], got["review"], got["heavy"]), (2, 1, 3))

    def test_a_dir_without_a_kanban_db_is_not_a_board(self):
        (self.home / "boards" / "scratch").mkdir()
        got = seats.counts(self.own, self.home, CFG["board"], now=2000)
        self.assertEqual(got["unreadable"], [])

    def test_unreadable_boards_are_named(self):
        self.corrupt_board("broken")
        empty = paths.board_db("empty", self.home)
        empty.parent.mkdir(parents=True)
        empty.write_bytes(b"")                       # a zero-byte DB has no tables: unreadable too
        got = seats.counts(self.own, self.home, CFG["board"], now=2000)
        self.assertEqual(got["unreadable"], ["broken", "empty"])

    def test_home_none_counts_only_this_board(self):
        self.live(self.own, "reviewer")
        self.assertEqual(seats.counts(self.own, None, CFG["board"], now=2000)["review"], 1)


class Admit(SeatCase):
    def review_card(self):
        tid = self.card(self.own, "review")
        return self.own.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()

    def admit(self, missing, now=2000):
        task = self.review_card()
        return task, dispatch.admit_reviews(self.own, CFG, self.home, task, missing, now)

    def deferred(self, task):
        return self.own.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='review_deferred'",
            (task["id"],)).fetchone()[0]

    def test_two_live_reviews_defer_the_third(self):
        self.live(self.own, "reviewer")
        self.live(self.own, "validator")
        task, got = self.admit(["guide"])
        self.assertEqual(got, [])
        self.assertEqual(self.deferred(task), 1)
        row = self.own.execute("SELECT status FROM tasks WHERE id=?", (task["id"],)).fetchone()
        self.assertEqual(row[0], "review")

    def test_one_free_seat_admits_the_first_missing_role_only(self):
        self.live(self.own, "reviewer")
        task, got = self.admit(["validator", "guide"])
        self.assertEqual(got, ["validator"])
        self.assertEqual(self.deferred(task), 1)       # guide waits

    def test_a_review_live_on_another_board_takes_a_seat(self):
        other = self.mkboard("other-app")
        self.live(other, "reviewer")
        self.live(self.own, "reviewer")
        self.assertEqual(self.admit(["guide"])[1], [])

    def test_a_full_heavy_cap_defers_a_review_though_a_review_seat_is_free(self):
        other = self.mkboard("other-app")
        self.running_impl(other)
        self.running_impl(other)
        self.live(self.own, "reviewer")                      # heavy 3 of 3, review 1 of 2
        task, got = self.admit(["validator"])
        self.assertEqual(got, [])
        payload = self.own.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='review_deferred'",
            (task["id"],)).fetchone()[0]
        self.assertEqual(json.loads(payload)["reason"], "heavy_cap")

    def test_free_seats_admit_everything_and_record_nothing(self):
        task, got = self.admit(["validator", "reviewer"])
        self.assertEqual(got, ["validator", "reviewer"])
        self.assertEqual(self.deferred(task), 0)

    def test_deferral_live_counts_this_calls_admissions(self):
        task, got = self.admit(["a", "b", "c"])
        self.assertEqual(got, ["a", "b"])
        payload = json.loads(self.own.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='review_deferred'",
            (task["id"],)).fetchone()[0])
        self.assertEqual((payload["live"], payload["cap"], payload["roles"]), (2, 2, ["c"]))

    def test_a_standing_deferral_is_recorded_once_not_every_tick(self):
        self.live(self.own, "reviewer")
        self.live(self.own, "validator")
        task = self.review_card()
        for now in (2000, 2030, 2060):
            got = dispatch.admit_reviews(self.own, CFG, self.home, task, ["guide"], now)
            self.assertEqual(got, [])
        self.assertEqual(self.deferred(task), 1)

    def test_an_unreadable_board_defers_every_review(self):
        self.corrupt_board()
        self.assertEqual(self.admit(["guide"])[1], [])

    def test_the_caps_come_from_config_with_defaults_two_and_three(self):
        bare = {k: v for k, v in CFG["caps"].items() if k not in ("max_review", "max_heavy")}
        self.assertEqual(seats.limits(dict(CFG, caps=bare)), (2, 3))
        tuned = dict(CFG, caps=dict(bare, max_review=1, max_heavy=4))
        self.assertEqual(seats.limits(tuned), (1, 4))


class Ready(SeatCase):
    def ready(self, now=2000, cfg=CFG):
        return dispatch.ready(self.own, cfg, {"strong"}, now, self.home)

    def test_three_heavy_seats_stop_dispatch(self):
        other = self.mkboard("other-app")
        self.card(self.own)
        self.running_impl(other)
        self.live(self.own, "reviewer")
        self.live(other, "guide")
        self.assertEqual(self.ready(), [])

    def test_two_heavy_seats_leave_room(self):
        other = self.mkboard("other-app")
        tid = self.card(self.own)
        self.running_impl(other)
        self.live(other, "guide")
        self.assertEqual([r["id"] for r in self.ready()], [tid])

    def test_another_boards_implementer_does_not_trip_this_boards_max_running(self):
        other = self.mkboard("other-app")
        tid = self.card(self.own)
        self.running_impl(other)
        self.assertEqual([r["id"] for r in self.ready()], [tid])

    def test_an_unreadable_board_stops_dispatch_and_says_so_once_an_hour(self):
        self.card(self.own)
        self.corrupt_board()
        self.assertEqual(self.ready(now=2000), [])
        self.assertEqual(self.ready(now=2030), [])
        self.assertEqual(self.paused_lines(), 1)
        self.assertEqual(self.ready(now=2000 + 3601), [])
        self.assertEqual(self.paused_lines(), 2)

    def paused_lines(self):
        return self.own.execute(
            "SELECT COUNT(*) FROM outbox WHERE channel='file'"
            " AND body LIKE '%unreadable%'").fetchone()[0]

    def test_without_a_home_only_this_board_counts(self):
        tid = self.card(self.own)
        self.assertEqual([r["id"] for r in dispatch.ready(self.own, CFG, {"strong"}, 2000)], [tid])


class OrphanedSeats(SeatCase):
    """t_fb64942f: a review run whose worker is gone (a crash left `ended_at` NULL) frees its
    seat on every board; a run that is alive never does."""

    def review(self, now=2000):
        return seats.counts(self.own, self.home, CFG["board"], now=now)["review"]

    def test_a_dead_worker_frees_its_seat_on_every_board(self):
        other = self.mkboard("other-app")
        self.run_row(other, pid=self.dead_pid())
        self.run_row(self.own, pid=self.dead_pid())
        self.assertEqual(self.review(), 0)

    def test_a_live_worker_keeps_its_seat_however_old_the_run(self):
        other = self.mkboard("other-app")
        self.run_row(other, pid=os.getpid(), started=1)
        self.assertEqual(self.review(now=10**7), 1)

    def test_a_run_with_no_pid_yet_always_holds_its_seat(self):
        self.run_row(self.own, pid=None, started=1)
        self.assertEqual(self.review(now=10**7), 1)

    def test_a_run_on_another_host_is_never_expired_here(self):
        self.run_row(self.own, pid=self.dead_pid(), host="elsewhere", started=1)
        self.assertEqual(self.review(), 1)

    def test_an_orphan_no_longer_defers_a_review_or_blocks_dispatch(self):
        other = self.mkboard("other-app")
        for _ in range(2):
            self.run_row(other, pid=self.dead_pid())
        tid = self.card(self.own, "review")
        task = self.own.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        self.assertEqual(dispatch.admit_reviews(self.own, CFG, self.home, task, ["guide"], 2000),
                         ["guide"])


class UnreadableHold(SeatCase):
    """t_95d1cb40: a board held unreadable for an hour opens one incident, not just a notice."""

    def snap(self, now):
        return seats.snapshot(self.own, CFG, self.home, now)

    def incidents(self):
        return self.own.execute("SELECT kind, status FROM incidents").fetchall()

    def sample(self, start, end, step=300):
        """Snapshots every `step` seconds from `start` through `end` (a tick that keeps looking)."""
        for now in range(start, end + 1, step):
            self.snap(now)

    def test_under_an_hour_opens_nothing(self):
        self.corrupt_board()
        self.sample(2000, 2000 + seats.HOLD_INCIDENT_AFTER - 1)
        self.assertEqual(self.incidents(), [])

    def test_an_hour_opens_one_incident_and_only_one(self):
        self.corrupt_board()
        self.sample(2000, 2000 + seats.HOLD_INCIDENT_AFTER + 600)
        self.assertEqual([tuple(r) for r in self.incidents()], [("silent_failure", "open")])

    def test_a_hold_over_several_days_still_has_one_incident(self):
        """open_incident dedupes within a day; the per-hold guard is what holds across days."""
        self.corrupt_board()
        self.sample(2000, 2000 + 3 * 86400, step=600)
        self.assertEqual([tuple(r) for r in self.incidents()], [("silent_failure", "open")])

    def test_a_recovered_board_restarts_the_clock(self):
        self.corrupt_board()
        self.snap(2000)
        paths.board_db("broken", self.home).unlink()
        self.snap(3000)
        self.corrupt_board()
        self.sample(3500, 2000 + seats.HOLD_INCIDENT_AFTER)
        self.assertEqual(self.incidents(), [])

    def test_two_blips_an_hour_apart_with_no_sample_between_open_nothing(self):
        self.corrupt_board()
        self.snap(2000)
        self.snap(2000 + seats.HOLD_INCIDENT_AFTER)
        self.assertEqual(self.incidents(), [])


class ConfigCaps(unittest.TestCase):
    def cfg(self, **caps):
        return dict(CFG, caps=dict(CFG["caps"], **caps))

    def test_valid_caps_load(self):
        got = config.validate(self.cfg(max_review=2, max_heavy=3))
        self.assertEqual((got["caps"]["max_review"], got["caps"]["max_heavy"]), (2, 3))

    def test_zero_negative_bool_and_text_are_refused(self):
        for key in ("max_review", "max_heavy"):
            for bad in (0, -1, True, "2", 1.5):
                with self.assertRaises(ValueError, msg=f"{key}={bad!r}"):
                    config.validate(self.cfg(**{key: bad}))


class WiredIntoTheTick(TickCase):
    def other_board_with_two_live_reviews(self):
        path = paths.board_db("elsewhere", self.home)
        path.parent.mkdir(parents=True)
        db.create_board(path)
        other = db.connect(path)
        self.addCleanup(other.close)
        for role in ("reviewer", "guide"):
            rid = uuid.uuid4().hex
            tid = db.new_task(other, title="x", assignee="dev-frontend", branch="b")
            lease.start_run(other, tid, role=role, claim_lock=f"h:{rid}", expires=10**9,
                            run_id=rid, now=1)

    def kinds(self):
        return [r[0] for r in self.conn.execute(
            "SELECT kind FROM task_events WHERE task_id=?", (self.tid,))]

    def status(self):
        return self.conn.execute("SELECT status FROM tasks WHERE id=?", (self.tid,)).fetchone()[0]

    def test_a_third_review_run_waits_when_two_are_live_on_another_board(self):
        self.other_board_with_two_live_reviews()
        self.cfg["caps"] = dict(self.cfg["caps"], max_review=2)
        self.write_cfg()
        self.script["implementer"] = [[IMPL_DONE]]
        for _ in range(4):
            self.tick.once()
        self.assertEqual(self.status(), "review")
        self.assertIn("review_deferred", self.kinds())
        self.assertNotIn("review_started", self.kinds())

    def test_a_deferred_round_neither_clones_nor_fetches(self):
        self.other_board_with_two_live_reviews()
        self.script["implementer"] = [[IMPL_DONE]]
        self.cfg["caps"] = dict(self.cfg["caps"], max_review=2)
        self.write_cfg()
        for _ in range(4):
            self.tick.once()
        self.assertEqual(self.status(), "review")
        (self.home / "origin").rename(self.home / "origin-moved")   # any fetch now fails loudly
        self.tick.once()
        self.assertNotIn("tick_error", self.kinds())
        self.assertEqual(self.status(), "review")

    def test_at_the_default_two_seats_a_round_runs_two_then_one_to_a_verdict(self):
        self.cfg["caps"] = dict(self.cfg["caps"], max_review=2, max_heavy=3)
        self.write_cfg()
        self.script["implementer"] = [[IMPL_DONE]]
        for _ in range(10):
            self.tick.once()
            if self.status() == "done":
                break
        self.assertEqual(self.status(), "done")
        self.assertEqual(sorted(r["profile"] for r in self.runs()),
                         ["guide", "implementer", "reviewer", "validator"])
        roles = [json.loads(r[0])["roles"] for r in self.conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='review_deferred'",
            (self.tid,))]
        self.assertEqual(roles[0], ["guide"])


class ShippedIntentExamples(SeatCase):
    """Z1/Z2: the shipped examples run through the production `dispatch` functions."""

    def build(self, doc):
        for name, spec in doc["boards"].items():
            if spec == "unreadable":
                self.corrupt_board(name)
                continue
            conn = self.own if name == "own" else self.mkboard(name)
            for _ in range(spec.get("implementers", 0)):
                self.running_impl(conn)
            for role in spec.get("reviews", []):
                self.live(conn, role)
            for role in spec.get("dead_reviews", []):
                self.run_row(conn, pid=self.dead_pid(), role=role)

    def test_each_example_gives_the_expected_decision(self):
        seen = 0
        for ex in intent.load("dispatch_caps"):
            doc = ex["given"]["document"]
            if doc.get("check") != "dispatch_caps":
                continue
            seen += 1
            home = Path(tempfile.mkdtemp())
            self.addCleanup(tmpclean.sweep, home)
            self.home = home
            self.own = self.mkboard(CFG["board"])
            cfg = copy.deepcopy(CFG)
            cfg["caps"].update(doc.get("caps", {}))
            self.build(doc)
            self.card(self.own)
            if doc["action"] == "review":
                task = self.own.execute("SELECT * FROM tasks LIMIT 1").fetchone()
                got = len(dispatch.admit_reviews(self.own, cfg, self.home, task, ["guide"], 2000))
            else:
                got = len(dispatch.ready(self.own, cfg, {"strong"}, 2000, self.home))
            self.assertEqual(got, ex["expect"]["admitted"], ex["id"])
        self.assertEqual(seen, 5)

    def test_the_examples_file_is_valid_json_lines(self):
        lines = (paths.INTENT / "dispatch_caps.jsonl").read_text().splitlines()
        self.assertEqual([json.loads(line)["id"] for line in lines].count(None), 0)


NAMES = ("alpha", "bravo", "charlie")


class FullSeats:
    """Two live review runs on another board: both review seats are taken (max_review 2)."""

    def fill_seats(self):
        path = paths.board_db("elsewhere", self.home)
        path.parent.mkdir(parents=True, exist_ok=True)
        db.create_board(path)
        self.other = db.connect(path)
        self.addCleanup(self.other.close)
        self.seat_runs = []
        for role in ("reviewer", "validator"):
            rid = uuid.uuid4().hex
            tid = db.new_task(self.other, title="x", assignee="dev-frontend", branch="b")
            self.seat_runs.append(lease.start_run(
                self.other, tid, role=role, claim_lock=f"h:{rid}", expires=10**9,
                run_id=rid, now=1))
        self.cfg["caps"] = dict(self.cfg["caps"], max_review=2, max_heavy=3)
        self.write_cfg()

    def free_a_seat(self):
        lease.end_run(self.other, self.seat_runs[0], status="completed", outcome="completed", now=2)


class GuideRulingSeats(FullSeats, TickCase):
    """t_2bb0a7b4: a guide ruling counts against max_review and waits like a review (row 602 L3)."""

    def ruling_runs(self):
        return [r for r in self.runs("guide") if json.loads(r["metadata"] or "{}").get("ruling_of")]

    def reach_ruling_triage(self):
        self.script["implementer"] = [[IMPL_DONE]]
        self.script["reviewer"] = [[REV_CONTRADICTS]]
        self.tick.once()
        self.tick.once()
        real = self.tick._rulings
        self.tick._rulings = lambda *a, **k: None       # stop right after routing
        self.tick.once()
        self.tick._rulings = real
        task = self.conn.execute(
            "SELECT status, block_kind FROM tasks WHERE id=?", (self.tid,)).fetchone()
        self.assertEqual((task["status"], task["block_kind"]), ("triage", "guide_ruling"))

    def test_a_guide_ruling_waits_when_the_review_seats_are_full_then_runs_when_one_frees(self):
        self.reach_ruling_triage()
        self.fill_seats()
        self.script["guide"] = [[ruling("ask-owner")]]
        self.tick.once()
        self.assertEqual(self.ruling_runs(), [])
        self.tick.once()
        self.assertEqual(self.ruling_runs(), [])
        self.free_a_seat()
        self.tick.once()
        self.assertEqual(len(self.ruling_runs()), 1)


class KorinSeats(FullSeats, test_korin.KorinCase):
    """t_2bb0a7b4: a Korin run counts against max_review and waits like a review (row 602 L3)."""

    def korin_runs(self):
        return self.conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE profile='korin'").fetchone()[0]

    def test_a_korin_run_waits_when_the_review_seats_are_full_then_runs_when_one_frees(self):
        self.enable()
        self.fill_seats()
        self.crash_once(passes=4)
        self.assertEqual([r["status"] for r in self.incidents()], ["open"])
        self.assertEqual(self.korin_runs(), 0)
        self.free_a_seat()
        self.tick.once(now=int(time.time()) + 10)
        self.assertEqual(self.korin_runs(), 1)

    def open_incidents(self, n):
        for i in range(n):
            incidents.open_incident(self.conn, board=self.cfg["board"], kind="silent_failure",
                                    role="caps", key=f"seat test {NAMES[i]}", now=int(time.time()))

    def other_board(self, reviews, implementers):
        path = paths.board_db("elsewhere", self.home)
        path.parent.mkdir(parents=True, exist_ok=True)
        db.create_board(path)
        other = db.connect(path)
        self.addCleanup(other.close)
        for role in ["reviewer"] * reviews + [None] * implementers:
            tid = db.new_task(other, title="x", assignee="dev-frontend", branch="b")
            if role is None:
                other.execute("UPDATE tasks SET status='running', claim_expires=? WHERE id=?",
                              (10**10, tid))
                continue
            rid = uuid.uuid4().hex
            lease.start_run(other, tid, role=role, claim_lock=f"h:{rid}", expires=10**9,
                            run_id=rid, now=1)

    def test_one_pass_starts_only_as_many_korin_runs_as_seats_are_free(self):
        self.cfg["caps"] = dict(self.cfg["caps"], max_review=2, max_heavy=3)
        self.enable(max_running=2)
        self.other_board(reviews=1, implementers=0)
        self.open_incidents(2)
        self.tick.once(now=int(time.time()))
        self.assertEqual(self.korin_runs(), 1)

    def test_a_full_heavy_cap_holds_korin_though_a_review_seat_is_free(self):
        self.cfg["caps"] = dict(self.cfg["caps"], max_review=2, max_heavy=3)
        self.enable()
        self.other_board(reviews=1, implementers=2)
        self.open_incidents(1)
        self.tick.once(now=int(time.time()))
        self.assertEqual(self.korin_runs(), 0)

    def test_an_unreadable_board_holds_korin(self):
        self.cfg["caps"] = dict(self.cfg["caps"], max_review=2, max_heavy=3)
        self.enable()
        path = paths.board_db("broken", self.home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"this is not a sqlite database at all" * 40)
        self.open_incidents(1)
        self.tick.once(now=int(time.time()))
        self.assertEqual(self.korin_runs(), 0)
