"""Docker-down detector (D159: a stopped factory with no fix in hand tells the owner). Docker
unavailable while a card waits on a container for DOWN_S (15 min, board key `docker_down_s`)
opens one incident and one outbox alert; the next pass while still down opens nothing; docker
back clears the record; `pause:board` or no waiting card opens nothing; `capsule status` says
how long and how many while it lasts.

Oracle 1: a fixture board with one ready strong card and docker down 16 minutes yields one
incident and one alert. Oracle 2: docker up, a paused board, no waiting card, or a young outage
yields none."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from capsule import blocks, config, db, docker_down, incidents, paths, statusview
from capsule.landing_reverts import LANDED
from capsule.tests.test_tick import TickCase

BOARD, EPIC, NOW = "t", "epic/x", 1_800_000_000
MIN = 60


def board(*, ready=1, review=0, lander_wait=0):
    """(home, conn, cfg): a temp board with the waiting cards asked for."""
    home = Path(tempfile.mkdtemp())
    p = paths.board_db(BOARD, home)
    p.parent.mkdir(parents=True)
    db.create_board(p)
    conn = db.connect(p)
    for _ in range(ready):
        db.new_task(conn, title="ready card", status="ready", branch=EPIC, now=NOW - 7200)
    for _ in range(review):
        db.new_task(conn, title="review card", status="review", branch=EPIC, now=NOW - 7200)
    for _ in range(lander_wait):
        tid = db.new_task(conn, title="done card", status="ready", branch=EPIC, now=NOW - 7200)
        db.transition(conn, tid, "done", frm=("ready",))
        db.transition(conn, tid, "ready", frm=("done",))
        blocks.block(conn, tid, kind="transient", reason="lander_wait_docker: suites",
                     now=NOW - 3600,
                     offer="unblock|reset", cause="lander_wait_docker")
    return home, conn, {"board": BOARD}


def alerts(conn):
    rows = conn.execute("SELECT body FROM outbox WHERE channel != 'local'").fetchall()
    return [r["body"] for r in rows
            if "Docker down for" in r["body"]]


def docker_incidents(conn):
    return [i for i in incidents.listing(conn) if "docker" in (i["root_key"] or "").lower()]


class DockerDownStep(unittest.TestCase):
    def pass_at(self, conn, cfg, up, minutes):
        docker_down.step(conn, cfg, up, NOW + minutes * MIN)

    def test_down_16_minutes_with_a_waiting_card_opens_one_incident_and_one_alert(self):
        _, conn, cfg = board()
        self.pass_at(conn, cfg, False, 0)
        self.assertEqual(docker_incidents(conn), [])
        self.pass_at(conn, cfg, False, 16)
        (inc,) = docker_incidents(conn)
        self.assertIn("16", " ".join(inc["evidence"]) if isinstance(inc["evidence"], list)
                      else inc["evidence"])
        (alert,) = alerts(conn)
        self.assertIn("16", alert)
        self.assertIn("1 card", alert)

    def test_a_later_pass_while_still_down_opens_nothing_new(self):
        _, conn, cfg = board(ready=2)
        for minute in (0, 16, 20, 40, 90):
            self.pass_at(conn, cfg, False, minute)
        self.assertEqual(len(docker_incidents(conn)), 1)
        self.assertEqual(len(alerts(conn)), 1)

    def test_under_15_minutes_opens_nothing(self):
        _, conn, cfg = board()
        for minute in (0, 5, 14):
            self.pass_at(conn, cfg, False, minute)
        self.assertEqual((docker_incidents(conn), alerts(conn)), ([], []))

    def test_threshold_is_a_board_config_key(self):
        _, conn, cfg = board()
        cfg["docker_down_s"] = 5 * MIN
        self.pass_at(conn, cfg, False, 0)
        self.pass_at(conn, cfg, False, 6)
        self.assertEqual(len(docker_incidents(conn)), 1)

    def test_threshold_must_be_a_positive_integer_not_a_bool(self):
        base = json.loads((paths.REPO / "capsule" / "config.example.json").read_text())
        config.validate(dict(base, docker_down_s=300))
        for bad in (True, False, 0, -5, 1.5, "900"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                config.validate(dict(base, docker_down_s=bad))

    def test_docker_back_clears_the_record_and_a_new_outage_restarts_the_clock(self):
        _, conn, cfg = board()
        self.pass_at(conn, cfg, False, 0)
        self.pass_at(conn, cfg, True, 10)
        self.pass_at(conn, cfg, True, 11)        # two passes back: the outage is over
        self.pass_at(conn, cfg, False, 12)       # down again: the clock restarts here
        self.pass_at(conn, cfg, False, 20)       # only 8 minutes into the new outage
        self.assertEqual(docker_incidents(conn), [])
        self.pass_at(conn, cfg, False, 28)
        self.assertEqual(len(docker_incidents(conn)), 1)

    def test_a_one_pass_flap_keeps_the_clock_so_a_flapping_docker_still_alerts(self):
        _, conn, cfg = board()
        for minute, up in ((0, False), (5, True), (6, False), (11, True), (12, False),
                           (14, False)):
            self.pass_at(conn, cfg, up, minute)
        self.assertEqual(docker_incidents(conn), [])
        self.pass_at(conn, cfg, False, 16)
        self.assertEqual(len(docker_incidents(conn)), 1)

    def test_a_second_episode_that_dedupes_into_the_open_incident_does_not_page_again(self):
        _, conn, cfg = board()
        self.pass_at(conn, cfg, False, 0)
        self.pass_at(conn, cfg, False, 16)
        self.pass_at(conn, cfg, True, 20)
        self.pass_at(conn, cfg, True, 21)
        self.pass_at(conn, cfg, False, 22)
        self.pass_at(conn, cfg, False, 40)
        self.assertEqual((len(docker_incidents(conn)), len(alerts(conn))), (1, 1))

    def test_a_second_outage_after_close_inside_30_days_is_a_repeat(self):
        _, conn, cfg = board()
        self.pass_at(conn, cfg, False, 0)
        self.pass_at(conn, cfg, False, 16)
        (inc,) = docker_incidents(conn)
        conn.execute("UPDATE incidents SET status='closed', closed_at=? WHERE id=?",
                     (NOW + 20 * MIN, inc["id"]))
        self.pass_at(conn, cfg, True, 30)
        self.pass_at(conn, cfg, True, 31)
        self.pass_at(conn, cfg, False, 40)
        self.pass_at(conn, cfg, False, 60)
        self.assertEqual(len(docker_incidents(conn)), 2)
        self.assertEqual(incidents.summary(conn)["repeat"], 1)

    def test_a_paused_board_opens_nothing(self):
        _, conn, cfg = board()
        db.guard_set(conn, "pause:board", {"until": NOW + 86400}, now=NOW)
        self.pass_at(conn, cfg, False, 0)
        self.pass_at(conn, cfg, False, 60)
        self.assertEqual((docker_incidents(conn), alerts(conn)), ([], []))

    def test_no_waiting_card_opens_nothing_even_with_docker_down(self):
        _, conn, cfg = board(ready=0)
        self.pass_at(conn, cfg, False, 0)
        self.pass_at(conn, cfg, False, 60)
        self.assertEqual((docker_incidents(conn), alerts(conn)), ([], []))

    def test_docker_up_with_waiting_cards_opens_nothing(self):
        _, conn, cfg = board()
        self.pass_at(conn, cfg, True, 0)
        self.pass_at(conn, cfg, True, 60)
        self.assertEqual((docker_incidents(conn), alerts(conn)), ([], []))

    def test_each_kind_of_waiting_card_counts(self):
        _, conn, cfg = board(ready=1, review=1, lander_wait=1)
        self.pass_at(conn, cfg, False, 0)
        self.pass_at(conn, cfg, False, 16)
        (alert,) = alerts(conn)
        self.assertIn("3 cards", alert)

    def test_a_card_held_for_an_exec_outage_waits_on_docker(self):
        _, conn, cfg = board(ready=0)
        tid = db.new_task(conn, title="held card", status="ready", branch=EPIC, now=NOW - 7200)
        blocks.block(conn, tid, kind="transient", reason="exec outage", now=NOW - 3600,
                     cause="exec_outage")
        self.assertEqual(docker_down.waiting(conn), [tid])
        blocks.unblock(conn, tid, by="worker", now=NOW - 90)
        blocks.block(conn, tid, kind="transient", reason="other", now=NOW - 60, cause="x")
        self.assertEqual(docker_down.waiting(conn), [])

    def test_a_card_on_the_light_tier_does_not_wait_on_docker(self):
        _, conn, cfg = board(ready=1)
        conn.execute("UPDATE tasks SET tier='light'")
        self.pass_at(conn, cfg, False, 0)
        self.pass_at(conn, cfg, False, 60)
        self.assertEqual((docker_incidents(conn), alerts(conn)), ([], []))

    def test_a_ready_card_behind_an_open_parent_does_not_wait_but_one_in_review_does(self):
        _, conn, cfg = board(ready=1)
        parent = db.new_task(conn, title="parent", status="running", branch=EPIC, now=NOW)
        for status in ("ready", "review"):
            db.new_task(conn, title=f"child {status}", status=status, branch=EPIC,
                        parents=[parent], now=NOW)
        self.assertEqual(len(docker_down.waiting(conn)), 2)       # the free ready + the review

    def test_a_landed_card_counts_only_while_its_record_is_a_live_held_push(self):
        _, conn, cfg = board(ready=0)
        for name, status, rec in (
                ("live", "done", {"state": "pushed", "held_since": NOW}),
                ("verified", "done", {"state": "verified", "held_since": NOW}),
                ("reopened", "ready", {"state": "pushed", "held_since": NOW}),
                ("gone", None, {"state": "pushed", "held_since": NOW}),
                ("plain", "done", {"state": "pushed"})):
            tid = (db.new_task(conn, title=name, status="ready", branch=EPIC, now=NOW)
                   if status else "t_gone")
            if status == "done":
                db.transition(conn, tid, "done", frm=("ready",))
            db.guard_set(conn, LANDED + tid, rec, now=NOW)
            if name == "live":
                live = tid
        conn.execute("UPDATE tasks SET status='running' WHERE title='reopened'")
        self.assertEqual(docker_down.waiting(conn), [live])

    def test_a_lost_version_race_is_read_again_and_the_fire_is_saved(self):
        _, conn, cfg = board()
        self.pass_at(conn, cfg, False, 0)
        real, calls = db.guard_set, []

        def flaky(*args, **kw):
            calls.append(1)
            return False if len(calls) == 1 else real(*args, **kw)
        with mock.patch.object(docker_down, "guard_set", flaky):
            self.pass_at(conn, cfg, False, 16)
        self.assertEqual(db.guard_get(conn, docker_down.GUARD)[0]["fired_at"], NOW + 16 * MIN)
        self.assertEqual((len(docker_incidents(conn)), len(alerts(conn))), (1, 1))

    def test_the_clock_going_backwards_restarts_the_window(self):
        _, conn, cfg = board()
        self.pass_at(conn, cfg, False, 100)
        self.pass_at(conn, cfg, False, 10)       # the wall clock jumped back
        self.assertEqual(db.guard_get(conn, docker_down.GUARD)[0]["since"], NOW + 10 * MIN)
        self.pass_at(conn, cfg, False, 20)
        self.assertEqual(docker_incidents(conn), [])
        self.pass_at(conn, cfg, False, 26)
        self.assertEqual(len(docker_incidents(conn)), 1)

    def test_state_without_a_time_reads_the_clock(self):
        _, conn, cfg = board()
        self.assertIsNone(docker_down.state(conn))
        docker_down.step(conn, cfg, False, int(time.time()))
        self.assertEqual(docker_down.state(conn)["waiting"], 1)

    def test_a_corrupt_record_is_no_record_and_is_replaced(self):
        _, conn, cfg = board()
        for junk in ("{not json", "[1, 2]", "null"):
            conn.execute("DELETE FROM guard_state WHERE key=?", (docker_down.GUARD,))
            conn.execute("INSERT INTO guard_state (key, value, version, updated_at)"
                         " VALUES (?,?,3,?)", (docker_down.GUARD, junk, NOW))
            with self.subTest(junk=junk):
                self.assertIsNone(docker_down.state(conn, NOW))
                self.pass_at(conn, cfg, False, 0)
                self.assertEqual(db.guard_get(conn, docker_down.GUARD)[0]["since"], NOW)

    def test_a_light_card_in_review_waits_on_docker_for_the_validator(self):
        _, conn, cfg = board(ready=0, review=2)
        conn.execute("UPDATE tasks SET tier='light'")
        self.pass_at(conn, cfg, False, 0)
        self.pass_at(conn, cfg, False, 16)
        (alert,) = alerts(conn)
        self.assertIn("2 cards", alert)
        self.assertEqual(len(docker_incidents(conn)), 1)


class DockerDownStatus(unittest.TestCase):
    def problems(self, home):
        snap = statusview.snapshot(home, now=NOW + 30 * MIN, boards=[BOARD],
                                   events_path=home / "none.jsonl")
        return [p["what"] for p in snap["attention"]["open"] if "Docker" in p["what"]]

    def test_status_says_how_long_and_how_many_while_it_lasts(self):
        home, conn, cfg = board(ready=2)
        docker_down.step(conn, cfg, False, NOW)
        docker_down.step(conn, cfg, False, NOW + 30 * MIN)       # the pass the snapshot follows
        conn.close()
        (what,) = self.problems(home)
        self.assertIn("Docker down for 30 min, 2 cards waiting", what)

    def test_status_shows_nothing_once_no_pass_has_checked_docker_recently(self):
        home, conn, cfg = board(ready=2)
        docker_down.step(conn, cfg, False, NOW)                  # then the tick stops
        conn.close()
        self.assertEqual(self.problems(home), [])

    def test_status_shows_nothing_while_the_board_is_paused(self):
        home, conn, cfg = board(ready=2)
        docker_down.step(conn, cfg, False, NOW + 30 * MIN)
        db.guard_set(conn, "pause:board", {"until": NOW + 86400}, now=NOW)
        conn.close()
        self.assertEqual(self.problems(home), [])

    def test_status_is_quiet_once_docker_is_back(self):
        home, conn, cfg = board()
        docker_down.step(conn, cfg, False, NOW)
        docker_down.step(conn, cfg, True, NOW + 10 * MIN)
        docker_down.step(conn, cfg, True, NOW + 11 * MIN)
        conn.close()
        self.assertEqual(self.problems(home), [])


class DockerDownInTick(TickCase):
    def test_the_tick_pass_opens_one_incident_when_docker_stays_down(self):
        self.runner.up = False
        for minute in (0, 16, 30):
            self.tick.once(now=NOW + minute * MIN)
        self.assertEqual(len(docker_incidents(self.conn)), 1)
        self.assertEqual(len(alerts(self.conn)), 1)

    def test_the_tick_pass_opens_nothing_when_docker_is_up(self):
        self.runner.up = True
        for minute in (0, 16, 30):
            self.tick.once(now=NOW + minute * MIN)
        self.assertEqual(docker_incidents(self.conn), [])


if __name__ == "__main__":
    unittest.main()
