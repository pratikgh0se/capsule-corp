"""inc_f94ec5d3: a rerun after a landing that did not pass names the failed check in its findings.
Real tick over the stub runner; the landing verdict is recorded by lander.record."""
import json
import unittest

from capsule import lander
from capsule.tests import test_tick as tt


def result(verdict, status, detail="3 failed"):
    return {"verdict": verdict, "hold": "", "commit": None, "base": "b", "checks": [
        {"name": "suites", "status": status, "detail": detail},
        {"name": "size", "status": "pass", "detail": "ok"}]}


class LandingReason(tt.TickCase):
    def passed_gates(self):
        self.script["implementer"] = [[tt.IMPL_DONE], [tt.IMPL_DONE]]
        self.script["reviewer"] = [[tt.REV_PASS], [tt.REV_PASS]]
        self.script["validator"] = [[tt.VAL_PASS], [tt.VAL_PASS]]
        self.script["guide"] = [[tt.GUIDE_OK], [tt.GUIDE_OK]]
        for _ in range(8):
            self.tick.once()
            if self.task()["status"] == "done":
                return
        self.fail(f"card never passed its gates: {self.task()['status']}")

    def rerun_inputs(self, verdict, status):
        self.passed_gates()
        lander.record(self.conn, self.tid, result(verdict, status), limit=3)
        for _ in range(4):
            self.tick.once()
            if len(self.runs("implementer")) == 2:
                break
        second = self.runs("implementer")[1]
        job = json.loads((self.spool_dir(second["run_id"]) / "job.json").read_text())
        bundle = self.spool_dir(second["run_id"]) / "bundle.json"
        prompt = json.loads(bundle.read_text())["prompt"]
        return job["inputs"], prompt

    def test_a_rerun_after_a_red_landing_names_the_failed_check_in_its_findings(self):
        inputs, prompt = self.rerun_inputs("red", "fail")
        self.assertEqual(inputs.get("review_findings"),
                         ["landing check suites (fail), after review: 3 failed"])
        section = prompt.split("## Review findings to fix", 1)[1]
        self.assertIn("landing check suites (fail), after review: 3 failed", section)

    def test_a_wait_verdict_names_the_check_in_its_findings(self):
        self.passed_gates()
        lander.record(self.conn, self.tid, result("wait", "wait", "main moved"), limit=3)
        lines = self.landing_lines()
        self.assertEqual(lines, ["landing check suites (wait), after review: main moved"])

    def test_temp_paths_in_a_check_detail_are_collapsed_and_the_cap_holds(self):
        self.passed_gates()
        detail = ("FAIL /private/tmp/lander-ab12/c/capsule/foo.py line 3; cache"
                  " /var/folders/zz/yy9/T/lander-ab12/t/x " + "z" * 2000)
        lander.record(self.conn, self.tid, result("red", "fail", detail), limit=3)
        line = self.landing_lines()[0]
        self.assertIn("FAIL <clone>/capsule/foo.py line 3", line)
        self.assertIn("cache <tmp>/lander-ab12/t/x", line)
        self.assertNotIn("/private/tmp", line)
        self.assertNotIn("/var/folders", line)
        self.assertLessEqual(len(line.split("after review: ", 1)[1]), 1500)

    def test_collapse_only_touches_real_temp_roots_and_the_lander_clone(self):
        from capsule.review_scope import _collapse_temp as c
        self.assertEqual(c("capsule/tmp/a.py"), "capsule/tmp/a.py")
        self.assertEqual(c("see https://example.com/tmp/x"), "see https://example.com/tmp/x")
        self.assertEqual(c("/private/tmp/lander-x/c/tmp/foo.py"), "<clone>/tmp/foo.py")
        self.assertEqual(c("at /tmp/lander-x/c/capsule/a.py:3"), "at <clone>/capsule/a.py:3")
        self.assertEqual(c("/tmp/pytest-1/c/x"), "<tmp>/pytest-1/c/x")
        self.assertEqual(c("root /private/tmp/lander-ab/c"), "root <clone>")

    def test_a_checked_verdict_adds_no_finding(self):
        self.passed_gates()
        lander.record(self.conn, self.tid, result("checked", "pass", "ok"), limit=3)
        self.assertEqual(self.landing_lines(), [])

    def test_a_completed_rerun_after_the_event_drops_the_line(self):
        self.passed_gates()
        lander.record(self.conn, self.tid, result("red", "fail"), limit=3)
        self.assertNotEqual(self.landing_lines(), [])
        self.conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at,"
            " run_id, metadata) VALUES (?,?,?,?,?,?,?,?)",
            (self.tid, "implementer", "done", "completed", 2 ** 31, 2 ** 31 + 1, "rlate", "{}"))
        self.conn.commit()
        self.assertEqual(self.landing_lines(), [])

    def landing_lines(self):
        from capsule import review_scope
        return review_scope.landing_lines(self.conn, self.tid)


if __name__ == "__main__":
    unittest.main()
