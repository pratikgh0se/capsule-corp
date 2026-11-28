"""docker_idle (D195): the idle reaper. Fakes only: docker, osascript and ps are shell scripts in a
throwaway dir (AGENT_OPS_DOCKER_BIN) that record their argv; never the real Docker or osascript."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from capsule import db, paths  # noqa: E402
from capsule.tests import tmpclean  # noqa: E402

tmpclean.install()
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import docker_idle  # noqa: E402

DOCKER = """#!/bin/sh
echo "docker $*" >> "$FAKE_LOG"
case "$1" in
  info) [ -f "$FAKE_DIR/down" ] && exit 1; exit 0;;
  ps) cat "$FAKE_DIR/containers"; exit 0;;
  events) [ -f "$FAKE_DIR/eventsfail" ] && exit 1
    [ -f "$FAKE_DIR/events" ] && cat "$FAKE_DIR/events"; exit 0;;
  compose) [ -f "$FAKE_DIR/stopfail" ] && exit 1
    [ -f "$FAKE_DIR/spawn" ] && printf 'capsule-run-late\t\n' >> "$FAKE_DIR/containers"
    exit 0;;
esac
"""
OSA = '#!/bin/sh\necho "osascript $*" >> "$FAKE_LOG"\n'
PS = '#!/bin/sh\ncat "$FAKE_DIR/procs"\n'
STACK = "capsule-corp-postgres\tcapsule-corp\ncapsule-corp-grafana\tcapsule-corp\n"
T0 = 1_000_000.0
MIN = 60
HOUR = 3600


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="docker-idle-test-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin, self.fd, self.home = self.tmp / "bin", self.tmp / "fd", self.tmp / "home"
        self.bin.mkdir()
        self.fd.mkdir()
        self.log = self.tmp / "fake.log"
        self.log.write_text("")
        for name, body in (("docker", DOCKER), ("osascript", OSA), ("ps", PS)):
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)
        self.containers(STACK)
        self.procs("/sbin/launchd\n")
        env = {"PATH": f"{self.bin}:/usr/bin:/bin", "AGENT_OPS_DOCKER_BIN": str(self.bin),
               "AGENT_OPS_STATE_DIR": str(self.tmp / "state"), "CAPSULE_HOME": str(self.home),
               "FAKE_LOG": str(self.log), "FAKE_DIR": str(self.fd)}
        self.enterContext(mock.patch.dict(os.environ, env))
        os.environ.pop("OPS_DOCKER_IDLE_MIN", None)
        self.now, self.at = T0, 0
        self.enterContext(mock.patch.object(docker_idle, "_now", lambda: self.now))

    def containers(self, text):
        (self.fd / "containers").write_text(text)

    def procs(self, text):
        (self.fd / "procs").write_text(text)

    def state(self):
        return self.tmp / "state" / "docker_idle.state"

    def calls(self):
        return self.log.read_text().splitlines()

    def acted(self):
        return [c for c in self.calls() if c.startswith(("docker compose", "osascript"))]

    def go(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = docker_idle.main(list(args))
        return rc, out.getvalue()

    def tick(self, minutes, *args):
        """Run at `minutes`; with --apply, every 5 minutes since the last run first (launchd's
        cadence: a longer gap restarts the idle clock)."""
        apply = not args
        for m in range(self.at + 5, minutes, 5) if apply else ():
            self.now = T0 + m * MIN
            self.go("--apply")
        self.at, self.now = minutes, T0 + minutes * MIN
        return self.go(*(args or ("--apply",)))

    def queue(self, name, doc, mtime=None):
        d = self.home / "queue" / "capsule-corp"
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(json.dumps(doc))
        os.utime(d / name, (mtime or self.now, mtime or self.now))

    def board(self):
        path = paths.board_db("t", self.home)
        path.parent.mkdir(parents=True, exist_ok=True)
        db.create_board(path)
        conn = db.connect(path)
        self.addCleanup(conn.close)
        return conn


class Busy(Base):
    def assert_busy(self, text):
        self.tick(0)
        rc, out = self.tick(60)
        self.assertEqual(rc, 0)
        self.assertIn("busy", out)
        self.assertIn(text, out)
        self.assertEqual(self.acted(), [])
        self.assertFalse(self.state().exists())

    def test_non_compose_container(self):
        self.containers(STACK + "capsule-run-abc\t\n")
        self.assert_busy("capsule-run-abc")

    def test_container_of_another_compose_project(self):
        self.containers("other-db\tsomeone-else\n")
        self.assert_busy("other-db")

    def test_short_container_between_polls(self):
        (self.fd / "events").write_text("\n")      # a `docker run --rm` that came and went
        self.assert_busy("a container ran in the last 10 min")

    def test_stack_only_events_are_not_busy(self):
        (self.fd / "events").write_text("capsule-corp\ncapsule-corp\n")
        self.assertNotIn("busy", self.tick(0)[1])
        self.assertIn("--since", " ".join(c for c in self.calls() if "events" in c))

    def test_events_failure_stays_up(self):
        (self.fd / "eventsfail").write_text("")
        self.assert_busy("docker events failed")

    def test_events_timeout_stays_up(self):
        boom = subprocess.TimeoutExpired("docker", 1)
        with mock.patch.object(docker_idle, "docker", side_effect=lambda *a: (
                (_ for _ in ()).throw(boom) if a[0] == "events" else docker_idle.events.docker_run(
                    list(a), capture_output=True, text=True))):
            self.assert_busy("docker events failed")

    def test_keeper_claim(self):
        self.queue("owners.json", {"build/x": {"owner": "merge-keeper", "heartbeat": T0}})
        self.assert_busy("merge-keeper holds build/x")

    def test_landing_marker(self):
        self.queue("t_1.json", {"state": "landing"})
        self.assert_busy("state landing")

    def test_other_owner_is_not_busy(self):
        self.queue("owners.json", {"build/x": {"owner": "builder", "heartbeat": T0}})
        self.assertNotIn("busy", self.tick(0)[1])

    def test_docker_build_process(self):
        self.procs("/usr/local/bin/docker buildx build --label capsule=1 .\n")
        self.assert_busy("build running")

    def test_compose_build_process(self):
        self.procs("docker compose -p capsule-corp build\n")
        self.assert_busy("build running")

    def test_stale_claim_still_holds_and_is_named(self):
        self.queue("owners.json", {"build/x": {"owner": "merge-keeper",
                                           "heartbeat": T0 - 10 * HOUR}})
        self.assert_busy("merge-keeper holds build/x")
        self.assertIn("stale claim pins Docker (1", self.tick(65)[1])

    def test_claim_without_a_heartbeat_holds(self):
        self.queue("owners.json", {"build/x": {"owner": "merge-keeper"}})
        self.assert_busy("merge-keeper holds build/x")

    def test_old_marker_holds_and_is_named(self):
        self.queue("t_1.json", {"state": "landing"}, mtime=T0 - 10 * HOUR)
        self.assert_busy("state landing")
        self.assertIn("stale claim pins Docker", self.tick(65)[1])

    def test_card_waiting_on_a_container_holds(self):
        conn = self.board()
        db.new_task(conn, title="review card", status="review", branch="epic/x", now=T0)
        self.assert_busy("board t: cards are waiting on a container")

    def test_paused_board_with_waiting_card_does_not_hold(self):
        conn = self.board()
        db.new_task(conn, title="review card", status="review", branch="epic/x", now=T0)
        db.guard_set(conn, "pause:board", {"until": T0 + 10**6}, now=T0)
        self.assertNotIn("busy", self.tick(0)[1])

    def test_board_with_nothing_waiting_does_not_hold(self):
        self.board()
        self.assertNotIn("busy", self.tick(0)[1])

    def test_pending_slack_row_holds_until_delivered_or_given_up(self):
        conn = self.board()
        conn.execute("INSERT INTO outbox (channel, thread_key, body, created_at) "
                     "VALUES ('slack', 'k', 'page', ?)", (T0,))
        self.assert_busy("a Slack message is waiting")
        conn.execute("UPDATE outbox SET attempts=12 WHERE channel='slack'")
        self.assertNotIn("busy", self.tick(70)[1])
        conn.execute("UPDATE outbox SET attempts=0, status='delivered' WHERE channel='slack'")
        self.assertNotIn("busy", self.tick(75)[1])

    def test_unreadable_board_holds(self):
        path = paths.board_db("t", self.home)
        path.parent.mkdir(parents=True)
        path.write_text("not a database")
        self.assert_busy("board t: unreadable")

    def test_busy_restarts_the_idle_clock(self):
        self.tick(0)
        self.containers(STACK + "capsule-run-abc\t\n")
        self.tick(10)
        self.containers(STACK)
        self.tick(15)                            # idle again since 15, not since 0
        rc, out = self.tick(25)
        self.assertIn("waiting", out)
        self.assertEqual(self.acted(), [])
        self.tick(30)
        self.assertEqual(len(self.acted()), 2)


class Idle(Base):
    def test_idle_not_long_enough_does_nothing(self):
        self.tick(0)
        rc, out = self.tick(14)
        self.assertEqual(rc, 0)
        self.assertIn("waiting", out)
        self.assertEqual(self.acted(), [])

    def test_idle_long_enough_stops_compose_then_quits(self):
        self.tick(0)
        rc, out = self.tick(15)
        self.assertEqual(rc, 0)
        self.assertEqual(self.acted(), ["docker compose -p capsule-corp stop",
                                        'osascript -e quit app "OrbStack"'])
        self.assertFalse(self.state().exists())
        lines = (self.tmp / "state" / "docker_idle.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 2)

    def test_quit_writes_the_stamp_heal_reads(self):
        self.tick(0)
        self.tick(15)
        self.assertTrue((self.tmp / "state" / "docker-idle-quit").exists())

    def test_something_started_while_the_stack_stopped_keeps_docker_up(self):
        (self.fd / "spawn").touch()
        self.tick(0)
        rc, out = self.tick(15)
        self.assertEqual(rc, 0)
        self.assertEqual(self.acted(), ["docker compose -p capsule-corp stop"])
        self.assertIn("skipped, busy", out)
        self.assertFalse(self.state().exists())
        self.assertFalse((self.tmp / "state" / "docker-idle-quit").exists())

    def test_a_gap_in_the_runs_restarts_the_clock(self):
        self.tick(0)
        self.now = T0 + 20 * MIN                  # slept through four runs
        rc, out = self.go("--apply")
        self.assertIn("idle 0 of 15", out)
        self.assertEqual(self.acted(), [])

    def test_env_overrides_the_wait(self):
        os.environ["OPS_DOCKER_IDLE_MIN"] = "2"
        self.tick(0)
        self.tick(2)
        self.assertEqual(len(self.acted()), 2)

    def test_failed_stop_does_not_quit(self):
        (self.fd / "stopfail").touch()
        self.tick(0)
        rc, _ = self.tick(15)
        self.assertEqual(rc, 1)
        self.assertEqual(self.acted(), ["docker compose -p capsule-corp stop"])

    def test_never_destructive(self):
        self.tick(0)
        self.tick(15)
        for c in self.calls():
            self.assertNotRegex(c, r"\b(rm|rmi|prune|down|kill)\b")

    def test_daemon_down_is_a_quiet_noop(self):
        (self.fd / "down").touch()
        rc, out = self.tick(0)
        self.assertEqual((rc, out), (0, ""))
        self.tick(60)
        self.assertEqual(self.acted(), [])
        self.assertFalse(self.state().exists())


class Builds(unittest.TestCase):
    def test_build_command_lines(self):
        for line in ("docker build .", "/usr/local/bin/docker buildx build -t x .",
                     "docker --context colima build .", "docker -H tcp://h:2375 build .",
                     "docker image build .", "docker builder build .",
                     "docker compose -p capsule-corp build", "docker compose up -d --build",
                     "/x/cli-plugins/docker-compose compose build",
                     "/x/cli-plugins/docker-buildx buildx build --load .",
                     "docker-buildx bake"):
            with self.subTest(line):
                self.assertTrue(docker_idle._is_build(line), line)

    def test_non_build_command_lines(self):
        for line in ("docker ps", "docker compose up -d", "docker run --rm img build",
                     "/x/cli-plugins/docker-compose docker-cli-plugin-metadata",
                     "/x/cli-plugins/docker-buildx docker-cli-plugin-metadata",
                     "tail -f docker-build.log", "/sbin/launchd"):
            with self.subTest(line):
                self.assertFalse(docker_idle._is_build(line), line)


class DryRun(Base):
    def test_dry_run_never_acts_or_starts_the_clock(self):
        rc, out = self.tick(0, "--dry-run")
        rc, out = self.tick(60, "--dry-run")
        self.assertEqual(rc, 0)
        self.assertEqual(self.acted(), [])
        self.assertFalse(self.state().exists())
        self.assertFalse((self.tmp / "state").exists())

    def test_dry_run_names_the_action_when_the_clock_has_run_out(self):
        self.tick(0)
        self.tick(10)
        rc, out = self.tick(15, "--dry-run")
        self.assertIn("would run docker compose -p capsule-corp stop", out)
        self.assertEqual(self.acted(), [])

    def test_no_flag_is_a_dry_run(self):
        self.tick(0)
        self.tick(10)
        self.now = T0 + 15 * MIN
        self.go()
        self.assertEqual(self.acted(), [])


if __name__ == "__main__":
    unittest.main()
