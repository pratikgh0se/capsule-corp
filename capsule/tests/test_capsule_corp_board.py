"""Factory autonomy Task 13 (spec 4.6): the capsule-corp tick's config and its routing verb.
Oracle 1: config.load. Oracle 2: a real Tick.once() and preflight's own config line."""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from capsule import blocks, config, db, human, paths, preflight, report, spool

TEMPLATE = paths.REPO / "capsule" / "config.capsule-corp.example.json"


def _board_home(test):
    home = Path(tempfile.mkdtemp())
    test.addCleanup(shutil.rmtree, home, True)
    d = paths.board_dir("capsule-corp", home)
    d.mkdir(parents=True)
    shutil.copy(TEMPLATE, d / "config.json")
    db.create_board(d / "kanban.db")
    return home


def _ready_card(home):
    conn = db.connect(paths.board_dir("capsule-corp", home) / "kanban.db")
    tid = db.new_task(conn, title="c", status="ready")
    conn.commit()
    conn.close()
    return tid


def _row_and_events(home, tid):
    conn = db.connect(paths.board_dir("capsule-corp", home) / "kanban.db")
    try:
        sql = "SELECT branch_name, assignee FROM tasks WHERE id=?"
        row = conn.execute(sql, (tid,)).fetchone()
        sql = "SELECT kind, payload FROM task_events WHERE task_id=?"
        evs = conn.execute(sql, (tid,)).fetchall()
        return (row["branch_name"], row["assignee"]), {k: p for k, p in evs}
    finally:
        conn.close()


def _once(home):
    from capsule import tick
    from capsule.tests.test_tick import FakeEvents, FakeRunner
    runner = FakeRunner()
    runner.up = False
    return tick.Tick(home, board="capsule-corp", runners=lambda tier, role: runner,
                     events=FakeEvents(), alive=lambda pid: False).once()


class Template(unittest.TestCase):
    def test_loads_and_says_one_at_a_time(self):
        cfg = config.load(TEMPLATE)
        self.assertEqual((cfg["board"], cfg["caps"]["max_running"], cfg["epics"]),
                         ("capsule-corp", 1, ["epic/capsule-harness"]))   # Q2, 496(2)
        self.assertTrue(cfg["slack"]["board_prefix"])                               # ruling Q5
        self.assertTrue(all(v.startswith("C_SET_ME_") for v in cfg["slack"]["channels"].values()))
        self.assertIs(cfg.get("deadman"), True)
        a = config.autonomy(cfg)
        self.assertEqual((a["touch_epic"], a["touch_assignee"], a["touch_cards"]),
                         ("epic/capsule-harness", "dev-backend", True))            # Q7
        self.assertEqual(cfg["assignees"][a["touch_assignee"]], "implementer")
        runner = "bash tests/run_shell_suites.sh"
        self.assertIn(f"{runner} --shard fast", cfg["contract_commands"])           # Q8, t_029d1b53
        self.assertIn(f"SHELL_SUITE_TIMEOUT=720 {runner} --shard slow", cfg["contract_commands"])

    def test_carries_the_2026_10_08_amendments(self):
        cfg = config.load(TEMPLATE)
        self.assertEqual((cfg["caps"]["max_review"], cfg["caps"]["max_heavy"]), (2, 3))
        self.assertEqual(config.route(cfg, "implementer")["model"], "claude-sonnet-5")
        self.assertNotIn("fable", cfg["routing"])   # dead profile dropped (Z8)
        self.assertEqual(cfg["contract_commands"][0],
                         "python3 -m unittest discover -s capsule/tests -t .")
        self.assertTrue(any("maintenance/tests -q" in c and "AGENT_OPS_NO_SEND=1" in c
                            for c in cfg["contract_commands"]))

    def test_unknown_cap_keys_are_accepted_and_ignored(self):
        """C10 reads max_review/max_heavy; until then load neither raises nor changes a cap."""
        raw = json.loads(TEMPLATE.read_text())
        raw["caps"]["max_someday"] = 7
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        p = d / "c.json"
        p.write_text(json.dumps(raw))
        cfg = config.load(p)
        self.assertEqual((cfg["caps"]["max_someday"], cfg["caps"]["max_running"]), (7, 1))
        self.assertEqual(config.cap(cfg, "max_runtime_seconds"), 3600)

    def test_preflight_reads_it_as_the_board_config(self):
        home = _board_home(self)
        def no_cmd(*a, **k):   # no docker, no claude
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        lines = preflight.run(home, board="capsule-corp", run_cmd=no_cmd, which=lambda name: None)
        cfg_line = next(row for row in lines if row[1] == "config")
        self.assertEqual(cfg_line[0], "ok", cfg_line)
        self.assertIn("capsule-corp", cfg_line[2])

    def test_a_board_tick_runs_a_pass_on_it(self):
        """Oracle 2: the tick's own loader, board guard and heartbeat, not config.load again."""
        from capsule import tick
        from capsule.tests.test_tick import FakeEvents, FakeRunner
        home = _board_home(self)
        runner, events = FakeRunner(), FakeEvents()
        runner.up = False
        counts = tick.Tick(home, board="capsule-corp", runners=lambda tier, role: runner,
                           events=events, alive=lambda pid: False).once()
        self.assertEqual(counts["config_error"], 0)
        last = events.logged[-1]
        self.assertEqual((last["board"], last["payload"]["watch"]), ("capsule-corp", True))


class EpicAssignee(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        db.create_board(self.dir / "kanban.db")
        self.conn = db.connect(self.dir / "kanban.db")
        self.addCleanup(self.conn.close)
        self.tid = db.new_task(self.conn, title="c", status="ready")

    def _row(self):
        sql = "SELECT branch_name, assignee FROM tasks WHERE id=?"
        return self.conn.execute(sql, (self.tid,)).fetchone()

    def test_routes_branch_and_assignee_together(self):
        ok = blocks.assign_epic(self.conn, self.tid, "epic/a", declared=["epic/a"],
                                assignee="dev-backend", assignees={"dev-backend": "implementer"})
        row = self._row()
        self.assertEqual((ok, row["branch_name"], row["assignee"]), (True, "epic/a", "dev-backend"))
        sql = "SELECT payload FROM task_events WHERE kind='epic_assigned'"
        payload = self.conn.execute(sql).fetchone()[0]
        self.assertEqual(json.loads(payload), {"branch": "epic/a", "assignee": "dev-backend"})

    def test_refuses_an_assignee_that_is_not_an_implementer(self):
        ok = blocks.assign_epic(self.conn, self.tid, "epic/a", declared=["epic/a"],
                                assignee="korin", assignees={"dev-backend": "implementer"})
        row = self._row()
        self.assertEqual((ok, row["branch_name"], row["assignee"]), (False, None, None))

    def test_without_an_assignee_the_old_behaviour_holds(self):
        ok = blocks.assign_epic(self.conn, self.tid, "epic/a", declared=["epic/a"])
        row = self._row()
        self.assertEqual((ok, row["branch_name"], row["assignee"]), (True, "epic/a", None))

    def test_handover_route_is_a_known_tag(self):
        p = human.write(self.dir, "b", self.tid, "epic", "epic/a", tag="handover-route",
                        assignee="dev-backend")
        doc = json.loads(p.read_text())
        self.assertEqual((doc["tag"], doc["assignee"]), ("handover-route", "dev-backend"))

    def test_a_non_string_assignee_is_refused_not_raised(self):
        ok = blocks.assign_epic(self.conn, self.tid, "epic/a", declared=["epic/a"],
                                assignee=["x"], assignees={"dev-backend": "implementer"})
        kinds = [r[0] for r in self.conn.execute("SELECT kind FROM task_events")]
        self.assertEqual((ok, self._row()["branch_name"], "epic_refused" in kinds),
                         (False, None, True))


class Wiring(unittest.TestCase):
    """The epic verb's assignee reaches the card through the real Tick and the real CLI."""

    def setUp(self):    # drive the CLI as a person's terminal, whoever runs the test
        env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "AI_AGENT")}
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_routed_epic_file_sets_branch_and_assignee_through_the_tick(self):
        home = _board_home(self)
        tid = _ready_card(home)
        human.write(home, "capsule-corp", tid, "epic", "epic/capsule-harness",
                    tag="handover-route", assignee="dev-backend")
        self.assertEqual(_once(home)["config_error"], 0)
        row, evs = _row_and_events(home, tid)
        self.assertEqual(row, ("epic/capsule-harness", "dev-backend"))
        self.assertEqual(json.loads(evs["epic_assigned"]),
                         {"branch": "epic/capsule-harness", "assignee": "dev-backend"})

    def test_a_non_implementer_assignee_is_refused_through_the_tick(self):
        home = _board_home(self)
        tid = _ready_card(home)
        human.write(home, "capsule-corp", tid, "epic", "epic/capsule-harness", assignee="korin")
        _once(home)
        row, evs = _row_and_events(home, tid)
        self.assertEqual((row, "epic_refused" in evs, "epic_assigned" in evs),
                         ((None, None), True, False))

    def test_a_malformed_assignee_in_a_hand_written_file_does_not_wedge_the_tick(self):
        home = _board_home(self)
        tid = _ready_card(home)
        d = spool.human_dir(home, "capsule-corp")
        d.mkdir(parents=True, exist_ok=True)
        doc = {"task_id": tid, "kind": "epic", "at": 1, "text": "epic/capsule-harness",
               "assignee": []}
        (d / "1.json").write_text(json.dumps(doc))
        self.assertEqual(_once(home)["config_error"], 0)
        row, evs = _row_and_events(home, tid)
        self.assertEqual((row, "epic_refused" in evs), ((None, None), True))

    def test_the_cli_carries_assignee_and_tag_into_the_file(self):
        import contextlib
        import io
        from capsule.__main__ import main
        home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, home, True)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = main(["human", "capsule-corp", "t_1", "epic", "epic/a", "--assignee",
                       "dev-backend", "--tag", "handover-route", "--capsule-home", str(home)])
        doc = json.loads(Path(out.getvalue().strip()).read_text())
        self.assertEqual((rc, doc["assignee"], doc["tag"]), (0, "dev-backend", "handover-route"))

    def test_the_cli_refuses_misused_assignee_and_tag(self):
        import contextlib
        import io
        from capsule.__main__ import main
        home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, home, True)
        base = ["human", "capsule-corp", "t_1"]
        tail = ["--capsule-home", str(home)]
        for argv in (base + ["reset", "x", "--assignee", "dev-backend"] + tail,
                     base + ["assign", "dev-backend", "--assignee", "dev-backend"] + tail,
                     base + ["epic", "epic/a", "--tag", "handover-route"] + tail,
                     base + ["reset", "x", "--tag", "handover-route", "--assignee", "a"] + tail):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as cm:
                    main(argv)
                self.assertEqual(cm.exception.code, 2)
        self.assertFalse(spool.human_dir(home, "capsule-corp").exists())

    def test_the_writer_refuses_misused_assignee_and_tag(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        for kind, kw in (("reset", dict(assignee="dev-backend")),
                         ("epic", dict(tag="handover-route")),
                         ("unblock", dict(tag="handover-route", assignee="dev-backend"))):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                human.write(d, "b", "t_1", kind, "x", **kw)
        self.assertFalse(spool.human_dir(d, "b").exists())


class ReportCounting(unittest.TestCase):
    def test_handover_routing_is_not_a_human_touch_in_the_ramp_rate(self):
        from capsule.tests.test_report import setup_board
        home, board, tids = setup_board()
        before = report.interventions(home, board)["interventions_per_card"]
        human.write(home, board, tids[1], "epic", "epic/a", now=19, tag="handover-route",
                    assignee="dev-backend")
        self.assertEqual(report.interventions(home, board)["interventions_per_card"], before)
        human.write(home, board, tids[1], "epic", "epic/a", now=20)
        self.assertGreater(report.interventions(home, board)["interventions_per_card"], before)


if __name__ == "__main__":
    unittest.main()
