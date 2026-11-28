"""R28 (B1b): a real tick with `redesign.blind_tests` = report. The strong runner is a stand-in
(no docker, no model); the blind run line runs on the host through `test_blind_run.Container`, so
the real harness runner produces the real results. The failing blind test must reach the validator
as a `mode: report only` row and the card must still end `done`; a validator that fails the card
only on a blind row is told its document is invalid and retries.

Z1: contracts/intent/blind_run.jsonl rows (source row 601) are replayed through the real tick once
the file ships; the built-in EXPECT holds until then. The container row reads the kwargs the tick
really passed to the container, never a hand-written list. Needs `not-run` in `blind_result.v1`
(branch tmp/blind-run-patch)."""
import unittest
from pathlib import Path

from capsule import bundles, intent
from capsule.runners import docker
from capsule.tests.test_blind_run import TESTS, Container, blind_repo, record
from capsule.tests.test_check_gate import CheckGateCase
from capsule.tests.test_tick import IMPL_DONE, VAL_PASS


def observe(started):
    """The container case, from what the tick really asked docker for: `started` is the
    (run_id, workspace, kwargs) of the blind container's `start` call."""
    run_id, ws, kw = started
    argv = docker.run_args(run_id, Path(ws), "img", **{k: kw[k] for k in
                                                       ("git_dir", "tmpdir", "ro", "mounts")})
    vols = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
    mode = {v.split(":")[1]: "ro" if v.endswith(":ro") else "rw" for v in vols}
    return {"workspace_mount": mode["/workspace"], "blind_mount": mode["/blind"],
            "writable_mounts": [dst for dst, how in mode.items() if how == "rw"],
            "network": "none" if "--network=none" in argv else "open"}


EXPECT = {
    "off-runs-nothing": {"containers_started": 0, "rows": []},
    "report-rows-never-block": {"rows": ["blind:<card>.test_fails exit 1",
                                         "blind:<card>.test_greets exit 0"],
                                "card_status": "done", "snapshot_left": False},
    "container-is-read-only-and-sealed": {"workspace_mount": "ro", "blind_mount": "ro",
                                          "writable_mounts": ["/out"], "network": "none"},
    "validator-fail-on-blind-row-is-retried": {"document": "invalid", "retried": True,
                                               "card_sent_to_rework": False},
}


class EndToEnd(CheckGateCase):
    """A real tick: mode `report` with a failing blind test, and the card still lands."""

    def setUp(self):
        super().setUp()
        self.tick.contracts.grace = 60      # the blind run takes real time: a pass waits for it
        readme = TESTS.replace("cat hello.txt", "cat README.md").replace('"hi\\n"', '"my-app\\n"')
        self.repo = blind_repo(self.home / "blind", self.tid, readme, board=self.cfg["board"])
        record(self.conn, self.tid, self.repo)
        self.boxes = []
        start, exec_ = self.runner.start, self.runner.exec

        def begin(run_id, ws, **kw):
            name = start(run_id, ws, **kw)
            if "mounts" in kw:
                box = Container(ws)
                box.start(run_id, ws, **kw)
                self.boxes.append(box)
            return name

        def run(name, cmd, timeout):
            if self.boxes and "harness.unittest_runner" in cmd:
                return self.boxes[-1].exec(name, cmd, timeout)
            return exec_(name, cmd, timeout)
        self.runner.start, self.runner.exec = begin, run
        self.script["validator"] = [[VAL_PASS]]
        self.script["implementer"] = [[IMPL_DONE]]

    def switch(self, mode):
        self.cfg["redesign"] = {"blind_tests": mode}
        self.cfg["blind_tests"] = {"repo": str(self.repo), "adapter": "unittest"}
        self.write_cfg()

    def replay(self, mode):
        self.switch(mode)
        self.settle()
        blind = [o for o in self.outputs() if o["cmd"].startswith("blind:")]
        sp = self.spool_dir(self.conn.execute(
            "SELECT run_id FROM task_runs WHERE task_id=? AND profile='validator'",
            (self.tid,)).fetchone()["run_id"])
        seen = {"rows": [f"{o['cmd']} exit {o['exit']}".replace(self.tid, "<card>")
                         for o in sorted(blind, key=lambda o: o["cmd"])],
                "card_status": self.task()["status"], "snapshot_left": (sp / "blind").exists(),
                "containers_started": len(self.boxes)}
        return {**seen, **(observe(self.boxes[0].started[0]) if self.boxes else {})}

    def replay_blind_fail(self):
        """A validator that fails the card only on the failing blind row, then (retried) passes."""
        fail = dict(VAL_PASS, verdict="fail", assertions=[
            {"cmd": f"blind:{self.tid}.test_fails", "exit": 1, "result": "fail", "excerpt": "no"}])
        self.script["validator"] = [[fail], [VAL_PASS]]
        self.switch("report")
        self.settle()
        runs = self.runs("validator")
        return {"document": "invalid" if runs[0]["error_class"] == "invalid_output" else "valid",
                "retried": len(runs) > 1,
                "card_sent_to_rework": bool(self.task()["failure_count"]
                                            or self.task()["status"] == "ready")}

    def test_report_mode_shows_the_rows_to_the_validator_and_never_blocks_the_card(self):
        seen = self.replay("report")
        self.assertEqual(seen["card_status"], "done")
        failing = [o["cmd"] for o in self.outputs() if o["cmd"].startswith("blind:") and o["exit"]]
        self.assertEqual(failing, [f"blind:{self.tid}.test_fails"])     # a failing blind test ...
        prompt = self.validator_files()[1]
        self.assertIn(f"- cmd: {failing[0]}\n  exit: 1\n  mode: report only\n", prompt)  # shown
        self.assertEqual(self.task()["failure_count"], 0)                # ... and fails nothing
        self.assertEqual(seen["rows"], EXPECT["report-rows-never-block"]["rows"])
        self.assertIn(bundles.BLIND_REPORT_NOTE, prompt)
        self.assertEqual(seen["containers_started"], 1)
        self.assertFalse(seen["snapshot_left"])

    def test_a_validator_that_fails_only_on_a_blind_row_is_retried_and_the_card_is_not_failed(self):
        seen = self.replay_blind_fail()
        self.assertEqual(seen, EXPECT["validator-fail-on-blind-row-is-retried"])
        first = self.runs("validator")[0]
        self.assertIn("report-only blind row", first["error"])
        self.assertEqual(self.task()["status"], "done")

    def test_the_shipped_intent_rows_match_what_the_cases_observe(self):
        rows = intent.load("blind_run")
        if not rows:
            self.skipTest("contracts/intent/blind_run.jsonl is not shipped yet")
        pending = {"validator-fail-on-blind-row-is-retried"}   # ships next round (3-row cap)
        self.assertEqual({r["id"] for r in rows}, set(EXPECT) - pending)
        report = self.replay("report")
        self.tearDown()
        self.setUp()                        # a fresh board for each further case
        off = self.replay("off")
        self.tearDown()
        self.setUp()
        retried = self.replay_blind_fail()
        by_id = {"off-runs-nothing": off, "report-rows-never-block": report,
                 "container-is-read-only-and-sealed": report,
                 "validator-fail-on-blind-row-is-retried": retried}
        for ex in rows:
            self.assertEqual(ex["source"], "row 601")
            for key, value in ex["expect"].items():
                self.assertEqual(by_id[ex["id"]][key], value, f"{ex['id']}: {key}")

    def test_the_switch_off_adds_no_row_and_no_container(self):
        seen = self.replay("off")
        self.assertEqual((seen["rows"], seen["containers_started"], seen["card_status"]),
                         ([], 0, "done"))

    def test_the_built_in_expectation_of_the_container_case(self):
        seen = self.replay("report")
        want = EXPECT["container-is-read-only-and-sealed"]
        self.assertEqual({k: seen[k] for k in want}, want)


if __name__ == "__main__":
    unittest.main()
