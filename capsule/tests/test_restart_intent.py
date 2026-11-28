"""M155 Z1: the three rows of contracts/intent/restart_ladder.jsonl, each replayed on the real tick
(the scripted Ladder world of test_restart) and compared with what the tick did, not with a
unit's answer."""
import unittest
from unittest import mock

from capsule import config, intent, restart, runaway, tick
from capsule.tests import test_restart as tr


class IntentExamples(tr.Ladder):
    @classmethod
    def row(cls, row_id):
        rows = {r["id"]: r for r in intent.load("restart_ladder")}
        return rows[row_id]       # a missing file or row is a KeyError: the test fails

    def replay_hand_off(self, doc):
        """Escalation per the row; drive until the run after `failed_rounds` failed rounds is
        launched, noting the card's status and the next implementer route at its claim."""
        self.switch(on=doc["escalation"])
        claims, real = [], tick.Tick._claim_and_launch

        def spy(inst, conn, cfg, row, now):
            route = restart.route(conn, cfg, row["id"], "implementer")
            claims.append((row["status"], route["model"]))
            return real(inst, conn, cfg, row, now)
        with mock.patch.object(tick.Tick, "_claim_and_launch", spy):
            self.drive(lambda: len(self.jobs) == doc["failed_rounds"] + 1)
        status, model = claims[-1]
        job = self.jobs[doc["failed_rounds"]]
        return {"route": "implementer_restart" if model == tr.OPUS["model"] else "implementer",
                "status": status, "restart_notes": len(job["inputs"].get("restart_notes", [])),
                "prior_runs": len(job["inputs"]["prior_runs"])}

    def test_there_are_exactly_three_rows(self):
        self.assertEqual(len(intent.load("restart_ladder")), 3)

    def test_three_sonnet_fails_hand_off_to_the_restart(self):
        ex = self.row("three-sonnet-fails-hand-off-to-restart")
        self.assertEqual(self.replay_hand_off(ex["given"]["document"]), ex["expect"])

    def test_the_sixth_failed_round_goes_to_triage_and_nothing_runs_until_reset(self):
        ex = self.row("sixth-failed-round-goes-to-triage")
        doc = ex["given"]["document"]
        self.switch(on=doc["escalation"])
        self.drive(self.triaged)
        self.assertEqual(restart._failed_rounds(self.conn, self.tid), doc["failed_rounds"])
        failing = "implementer_restart" if self.models()[-1] == tr.OPUS["model"] else "implementer"
        self.assertEqual(failing, doc["failing_route"])
        runs = len(self.runs("implementer"))
        for _ in range(6):
            self.tick.once()
        got = {"status": self.task()["status"], "block_kind": self.task()["block_kind"],
               "next_runs": len(self.runs("implementer")) - runs}
        self.assertEqual(got, ex["expect"])

    def test_switch_off_changes_nothing(self):
        ex = self.row("switch-off-changes-nothing")
        doc = ex["given"]["document"]
        self.switch(on=doc["escalation"])
        self.drive(self.triaged)   # today's own same-class stop ends the third failed round
        self.assertEqual(len(self.runs("implementer")), doc["failed_rounds"])
        self.assertEqual(self.models(), [self.sonnet] * doc["failed_rounds"])
        cfg = config.load(self.home / "config.json")
        route = restart.route(self.conn, cfg, self.tid, "implementer")
        got = {"route": "implementer" if route == config.route(cfg, "implementer")
               else "implementer_restart",
               "restart_notes": sum("restart_notes" in j["inputs"] for j in self.jobs),
               "failure_limit": restart.failure_cap(cfg),
               "max_rounds": runaway.settings(cfg)["max_rounds"]}
        self.assertEqual(got, ex["expect"])


if __name__ == "__main__":
    unittest.main()
