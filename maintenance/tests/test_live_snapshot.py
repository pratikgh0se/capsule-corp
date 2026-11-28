"""W347: live_snapshot.py parsing and row building, from fixture docker JSON.
No Docker, no Postgres."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "maintenance"))
sys.path.insert(0, str(REPO))
import live_snapshot as ls  # noqa: E402


def _box(name, image, state, status, created, labels=""):
    return {"Names": name, "Image": image, "State": state, "Status": status,
            "RunningFor": "3 hours ago", "CreatedAt": created, "Labels": labels}


DOCKER = "\n".join(json.dumps(o) for o in [
    _box("capsule-corp-postgres", "postgres:16-alpine", "running", "Up 3 hours (healthy)",
         "2026-10-24 10:11:12 +0700 +07"),
    _box("capsule-corp-loki", "grafana/loki:3.7.9", "running", "Up 3 hours", "garbage", "a=b"),
    _box("capsule-abc123def456", "worker:1", "running", "Up 5 minutes",
         "2026-10-24 12:00:00 +0700 +07",
         "capsule=1,capsule-run-id=abc123def456xyz,capsule-board=capsule-corp"),
    _box("capsule-corp-grafana", "grafana/grafana:13.2.4", "exited", "Exited (0)", "")])


class Containers(unittest.TestCase):
    def test_parses_json_lines_and_kinds(self):
        rows = {r["name"]: r for r in ls._parse_containers(DOCKER + "\nnot json\n")}
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows["capsule-corp-postgres"]["kind"], "service")
        w = rows["capsule-abc123def456"]
        self.assertEqual((w["kind"], w["board"]), ("worker", "capsule-corp"))
        self.assertEqual(w["run_id"], "abc123def456xyz")
        created = rows["capsule-corp-postgres"]["created_at"]
        self.assertTrue(created.startswith("2026-10-24T10:11:12"))
        self.assertIsNone(rows["capsule-corp-loki"]["created_at"])

    def test_a_capsule_container_without_a_run_id_is_not_a_worker(self):
        gate = _box("capsule-gate", "worker:1", "running", "Up", "", "capsule=1,capsule.kind=run")
        self.assertEqual(ls._parse_containers(json.dumps([gate]))[0]["kind"], "service")

    def test_parses_a_json_array_and_empty_output(self):
        self.assertEqual(len(ls._parse_containers(json.dumps([{"Names": "x"}, 5]))), 1)
        self.assertEqual(ls._parse_containers(""), [])


class Health(unittest.TestCase):
    def test_tick_boards_reads_board_flag_and_default(self):
        out = ("1 python -u -m capsule tick --replace\n"
               "2 python -m capsule tick --replace --board capsule-corp\n"
               "3 pgrep -fl capsule tick\n")
        self.assertEqual(ls._tick_boards(out, "sample-board"), {"sample-board", "capsule-corp"})
        self.assertEqual(ls._tick_boards("", "sample-board"), set())

    def test_observe_uses_the_host_default_board_for_a_bare_tick(self):
        ps = mock.Mock(returncode=0, stdout="")
        pg = mock.Mock(stdout="1 python -m capsule tick --replace\n")
        seen = {}
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.dict(os.environ, {"CAPSULE_HOME": home}), \
                mock.patch.object(ls, "_docker", return_value=ps), \
                mock.patch.object(ls, "_run", return_value=pg), \
                mock.patch.object(ls, "_disk_free_gb", return_value=100), \
                mock.patch.object(ls, "_dark_result", return_value=None), \
                mock.patch.object(ls, "_read_seats", return_value=[]), \
                mock.patch.object(ls, "_build_health",
                                  side_effect=lambda c, t, *a: seen.setdefault("t", set(t)) or []):
            ls._observe(home)
            Path(home, "host.json").write_text('{"boards": {"default": "sample-board"}}')
            seen.clear()
            ls._observe(home)
        self.assertEqual(seen["t"], {"sample-board"})

    def test_dark_status(self):
        line = "12.5 h of 48 elapsed; 40 results, 38 PASS, 1 INFO, 1 FAIL"
        self.assertEqual(ls._parse_dark_status(line, 1), (False, "12.5 h of 48; 38 pass, 1 fail"))
        self.assertTrue(ls._parse_dark_status(line.replace("1 FAIL", "0 FAIL"), 0)[0])
        self.assertEqual(ls._parse_dark_status("dark_test: no start marker", 2),
                         (True, "no offline test running"))
        self.assertFalse(ls._parse_dark_status("boom", 1)[0])

    def test_health_rows(self):
        cs = ls._parse_containers(DOCKER)
        built = ls._build_health(cs, {"capsule-corp"}, ["capsule-corp", "sample-board"], 20.0,
                                 (True, "x"))
        rows = {r[0]: r for r in built}
        self.assertTrue(rows["loop:capsule-corp"][2])
        self.assertFalse(rows["loop:sample-board"][2])
        self.assertFalse(rows["disk"][2])                       # under 25 GB
        self.assertTrue(rows["svc:postgres"][2])
        self.assertFalse(rows["svc:grafana"][2])                # exited
        self.assertFalse(rows["svc:infisical"][2])              # absent
        self.assertEqual(rows["svc:infisical"][4], "not found")
        self.assertTrue(rows["offline_test"][2])
        self.assertTrue(ls._build_health([], set(), [], 80.0, (True, ""))[0][2])


class Seats(unittest.TestCase):
    COUNTS = {"heavy": 3, "review": 1, "landers": 0}

    def test_external_seat_is_taken_off_the_cap(self):
        caps = {"max_heavy": 3, "max_review": 2, "external_heavy": 1}
        rows = {r[0]: r for r in ls._build_seats(self.COUNTS, caps)}
        self.assertEqual(rows["building"][1:3], (3, 2))
        self.assertEqual(rows["review"][1:3], (1, 2))
        self.assertIsNone(rows["keeper"][3])

    def test_no_external_key_and_a_keeper(self):
        counts = {**self.COUNTS, "landers": 1}
        rows = {r[0]: r for r in ls._build_seats(counts, {"max_heavy": 3, "max_review": 2})}
        self.assertEqual(rows["building"][2], 3)
        self.assertEqual(rows["keeper"][3], "a merge keeper")


class Sql(unittest.TestCase):
    def test_literals_quote_safely(self):
        self.assertEqual(ls._lit("it's\0"), "'it''s'")
        got = (ls._lit(None), ls._lit(True), ls._lit(3), ls._lit(1.5))
        self.assertEqual(got, ("NULL", "true", "3", "1.5"))

    def test_one_transaction_replaces_every_table(self):
        cs = ls._parse_containers(DOCKER)
        health = ls._build_health(cs, set(), ["b"], 50.0, (True, "d"))
        zero = {"heavy": 0, "review": 0, "landers": 0}
        seats = ls._build_seats(zero, {"max_heavy": 3, "max_review": 2})
        sql = ls._build_sql(cs, health, seats)
        self.assertTrue(sql.startswith("begin;") and sql.rstrip().endswith("commit;"))
        self.assertEqual(sql.count("begin;"), 1)
        for t in ("live_containers", "live_health", "live_seats"):
            self.assertIn(f"from {t} where true;", sql)
            self.assertIn(f"insert into {t} ", sql)
        self.assertLess(sql.index("from live_seats where true;"),
                        sql.index("insert into live_containers"))

    def test_empty_snapshot_still_clears_the_tables(self):
        sql = ls._build_sql([], [], [])
        self.assertNotIn("insert", sql)
        self.assertIn("from live_health where true;", sql)


class Main(unittest.TestCase):
    """The collector always exits 0 and says what happened on stderr."""

    def _run_main(self, fake_docker):
        calls = []

        def docker(args, **kw):
            calls.append(args[0])
            return fake_docker(args, **kw)

        err = io.StringIO()
        with tempfile.TemporaryDirectory() as home:
            env = {k: v for k, v in os.environ.items() if k != "AGENT_OPS_NO_SEND"}
            env["CAPSULE_HOME"] = home
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(ls.events, "docker_run", docker), \
                    mock.patch.object(ls, "_run", return_value=None), \
                    mock.patch.object(ls, "_read_seats", return_value=[]), \
                    mock.patch.object(ls, "_dark_result", return_value=(True, "x")), \
                    contextlib.redirect_stderr(err):
                rc = ls.main()
        return rc, err.getvalue(), calls

    def test_docker_missing_exits_zero_and_writes_nothing(self):
        def gone(args, **kw):
            raise FileNotFoundError("docker")
        rc, err, calls = self._run_main(gone)
        self.assertEqual(rc, 0)
        self.assertEqual(calls, ["ps"])                  # no exec: nothing was written
        self.assertIn("snapshot skipped", err)

    def test_docker_hung_exits_zero(self):
        def hung(args, **kw):
            raise subprocess.TimeoutExpired("docker", 30)
        rc, err, calls = self._run_main(hung)
        self.assertEqual((rc, calls), (0, ["ps"]))
        self.assertIn("TimeoutExpired", err)

    def test_psql_failure_exits_zero_with_a_note(self):
        def fake(args, **kw):
            if args[0] == "ps":
                return subprocess.CompletedProcess(args, 0, DOCKER, "")
            return subprocess.CompletedProcess(args, 1, "", "connection refused")
        rc, err, calls = self._run_main(fake)
        self.assertEqual((rc, calls), (0, ["ps", "exec"]))
        self.assertIn("psql: connection refused", err)
        self.assertIn("snapshot not written", err)

    def _dark(self, text, rc):
        import dark_test

        def status(_args):
            print(text)
            return rc
        with mock.patch.object(dark_test, "_cmd_status", status), \
                mock.patch.object(subprocess, "run", side_effect=AssertionError("no child")):
            return ls._dark_result()

    def test_offline_test_is_read_in_process(self):
        line = "3.0 h of 48 elapsed; 9 results, 9 PASS, 0 INFO, 0 FAIL"
        self.assertEqual(self._dark(line, 0), (True, "3.0 h of 48; 9 pass, 0 fail"))
        self.assertFalse(self._dark(line.replace("0 FAIL", "2 FAIL"), 1)[0])

    def test_offline_test_exit_two_is_green(self):
        self.assertEqual(self._dark("dark_test: no start marker", 2),
                         (True, "no offline test running"))

    def test_log_is_trimmed_in_place_when_big(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "live-snapshot.log"
            log.write_text("".join(f"line {i}\n" for i in range(100)))
            ls._trim_log(log, max_bytes=100, keep_lines=3)
            self.assertEqual(log.read_text(), "line 97\nline 98\nline 99\n")
            ls._trim_log(log, max_bytes=100, keep_lines=1)
            self.assertEqual(log.read_text(), "line 97\nline 98\nline 99\n")   # small: untouched
            ls._trim_log(Path(d) / "missing.log")


if __name__ == "__main__":
    unittest.main()
