"""Board wiring on top of the landed tick_guard lock (row 517): `Tick(board=...)` reads its own
board's config and writes its own outbox log, refuses a config naming another board, and the
heartbeat carries `config`/`lock`/`watch`.
Oracle 1: the events the tick logs (FakeEvents, in-process). Oracle 2: the files on disk (the
per-board outbox log, the legacy one left untouched) - two temp boards, ticked independently."""
import json
import time
from pathlib import Path

from capsule import config, db, outbox, paths, tick_guard
from capsule.tests.test_korin import KorinCase, postmortem
from capsule.tests.test_tick import BRANCH, FakeEvents, TickCase


class BoardWiring(TickCase):
    def setUp(self):
        super().setUp()
        self.events = FakeEvents()
        d = paths.board_dir("other", self.home)
        d.mkdir(parents=True, exist_ok=True)
        (d / "config.json").write_text(json.dumps(dict(self.cfg, board="other")))
        db.create_board(paths.board_db("other", self.home))

    def test_a_board_tick_serves_its_own_board_and_writes_its_own_outbox_log(self):
        self.runner.up = False
        oconn = db.connect(paths.board_db("other", self.home))
        outbox.add(oconn, "file", "capsule: hello from other")
        oconn.close()
        t = self.make_tick(board="other", events=self.events)
        t.once()
        self.assertEqual([e["board"] for e in self.events.logged], ["other"])
        self.assertIn("hello from other", paths.outbox_log(self.home, "other").read_text())
        legacy = self.home / "outbox.log"
        self.assertFalse(legacy.exists() and "hello from other" in legacy.read_text())

    def test_the_legacy_tick_still_writes_the_shared_outbox_log(self):
        """board=None (the legacy, single-board tick) keeps today's behaviour untouched."""
        self.runner.up = False
        conn = db.connect(self.board)
        outbox.add(conn, "file", "capsule: hello from legacy")
        conn.close()
        t = self.make_tick(events=self.events)
        t.once()
        self.assertIn("hello from legacy", (self.home / "outbox.log").read_text())

    def test_a_config_naming_another_board_is_refused_before_any_db_is_opened(self):
        (paths.board_dir("other", self.home) / "config.json").write_text(
            json.dumps(dict(self.cfg, board="test")))       # "other"'s own config names "test"
        t = self.make_tick(board="other", events=self.events)
        counts = t.once()
        self.assertEqual(counts["config_error"], 1)
        self.assertEqual(self.events.logged, [])             # no pass, no heartbeat: refused early

    def test_the_heartbeat_payload_carries_this_ticks_config_and_lock_state(self):
        self.runner.up = False
        t = self.make_tick(board="other", events=self.events)
        t.once()
        payload = self.events.logged[-1]["payload"]
        self.assertEqual(payload["config"], str(paths.config_path(self.home, "other")))
        self.assertIs(payload["lock"], False)                # no tick_guard.guard_start ran here
        f = tick_guard.guard_start(self.home, "other")
        self.addCleanup(f.close)
        t.once()
        self.assertIs(self.events.logged[-1]["payload"]["lock"], True)

    def test_legacy_tick_refuses_a_hot_reloaded_config_naming_another_board(self):
        """I15: the legacy tick pins itself to the board its config named on its first valid
        load; a later live edit that points the config at another board must not make it serve
        that board's db."""
        self.runner.up = False
        t = self.make_tick(events=self.events)                # no board, no pin_board: self-pins
        self.assertEqual(t.once()["config_error"], 0)
        self.cfg["board"] = "other"
        self.write_cfg()
        self.assertEqual(t.once()["config_error"], 1)
        self.assertEqual([e["board"] for e in self.events.logged], ["test"])

    def test_the_heartbeat_carries_the_deadman_flag(self):
        self.runner.up = False
        t = self.make_tick(board="other", events=self.events)
        t.once()
        self.assertIs(self.events.logged[-1]["payload"]["watch"], True)      # default: watched
        (paths.board_dir("other", self.home) / "config.json").write_text(
            json.dumps(dict(self.cfg, board="other", deadman=False)))
        t.once()
        self.assertIs(self.events.logged[-1]["payload"]["watch"], False)
        with self.assertRaises(ValueError):
            config.validate(dict(self.cfg, deadman="no"))

    def test_the_heartbeat_reports_a_pause(self):
        """The Grafana unowned-work-idle rule reads `paused` from the tick_health payload."""
        self.runner.up = False
        t = self.make_tick(board="other", events=self.events)
        t.once()
        self.assertIs(self.events.logged[-1]["payload"]["paused"], False)
        conn = db.connect(paths.board_db("other", self.home))
        db.guard_set(conn, "pause:account:default", {"until": time.time() + 600})
        conn.close()
        t.once()
        self.assertIs(self.events.logged[-1]["payload"]["paused"], True)

    def test_two_boards_tick_independently_in_one_process(self):
        """Z2 second oracle for the whole file: two temp boards, ticked through two Tick
        instances, land in two disjoint outbox logs and two disjoint event streams."""
        self.runner.up = False
        oconn = db.connect(paths.board_db("other", self.home))
        outbox.add(oconn, "file", "capsule: only for other")
        oconn.close()
        conn = db.connect(self.board)
        outbox.add(conn, "file", "capsule: only for test")
        conn.close()
        legacy_events, other_events = FakeEvents(), FakeEvents()
        legacy = self.make_tick(events=legacy_events)
        other = self.make_tick(board="other", events=other_events)
        legacy.once()
        other.once()
        self.assertEqual([e["board"] for e in legacy_events.logged], ["test"])
        self.assertEqual([e["board"] for e in other_events.logged], ["other"])
        self.assertIn("only for test", (self.home / "outbox.log").read_text())
        self.assertIn("only for other", paths.outbox_log(self.home, "other").read_text())
        self.assertNotIn("only for other", (self.home / "outbox.log").read_text())


class BoardKorin(KorinCase):
    """F2: Korin's evidence tail on a `--board` tick reads that board's own outbox log."""

    def setUp(self):
        super().setUp()
        d = paths.board_dir("other", self.home)
        d.mkdir(parents=True, exist_ok=True)
        korin = {"enabled": True, "since": "2026-01-01", "vault": str(self.vault),
                 "main_checkout": str(self.main)}
        cfg = dict(self.cfg, board="other", korin=korin)
        (d / "config.json").write_text(json.dumps(cfg))
        board_db = paths.board_db("other", self.home)
        db.create_board(board_db)
        oconn = db.connect(board_db)
        db.new_task(oconn, title="timer screen", body="x", assignee="dev-frontend", branch=BRANCH)
        oconn.commit()
        oconn.close()
        paths.outbox_log(self.home, "other").write_text("only for other\n")
        (self.home / "outbox.log").write_text("only for legacy\n")
        self.tick = self.make_tick(board="other")

    def test_korin_reads_the_boards_own_outbox_log(self):
        self.korin_docs.append(lambda inc: postmortem(inc))
        self.crash_once(passes=5)
        seen = [log["path"] for log in self.korin_jobs[0]["inputs"]["logs"]]
        self.assertIn(str(paths.outbox_log(self.home, "other")), seen)
        self.assertNotIn(str(self.home / "outbox.log"), seen)
