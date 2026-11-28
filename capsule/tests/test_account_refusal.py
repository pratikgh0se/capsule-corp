"""An account refusal (401/403 error envelope) pauses the account, whatever role hit it
(t_a2ac42b8, inc_ee46d054, inc_10872a1c). Z1 intent: a validator gets the billing 403, the run is
released, the account waits 30 minutes, one outbox line quotes the CLI's text, and no failure,
crash or incident follows. `claude.parse` reads the CLI's own output shape from a fixture."""
import json
import time
from pathlib import Path

from capsule import db, incidents, worker
from capsule.models import claude
from capsule.tests.test_korin import KorinCase
from capsule.tests.test_tick import GUIDE_OK, IMPL_DONE

FX = Path(__file__).parent / "fixtures" / "cli_result_403_billing.json"
TEXT = json.loads(FX.read_text())["result"]


def refused(now, status=403):
    return claude.parse(json.dumps({**json.loads(FX.read_text()), "api_error_status": status}),
                        "", 1, now=now)


class AccountRefusal(KorinCase):
    def kinds(self):
        sql = "SELECT kind FROM task_events WHERE task_id=? ORDER BY id"
        return [r[0] for r in self.conn.execute(sql, (self.tid,))]

    def lines(self):
        sql = "SELECT body FROM outbox WHERE body LIKE '%paused%'"
        return [r[0] for r in self.conn.execute(sql)]

    def test_a_validator_403_billing_envelope_pauses_the_account_and_opens_no_incident(self):
        now = int(time.time())
        res = claude.parse(FX.read_text(), "", 1, now=now)
        self.assertEqual((res.error_class, res.retry_after), ("account", now + 1800))
        self.script["implementer"] = [[IMPL_DONE]]
        self.script["validator"] = [[res]]
        for step in range(6):
            self.tick.once(now + step)
        run = self.runs("validator")[0]
        self.assertEqual((run["status"], run["error_class"]), ("released", "account"))
        self.assertGreater(db.guard_get(self.conn, "pause:account:default")[0]["until"], now + 1700)
        self.assertNotIn("crashed", self.kinds())
        self.assertEqual(self.task()["failure_count"], 0)
        self.assertEqual(incidents.listing(self.conn), [])
        self.assertEqual(len(self.lines()), 1)
        self.assertIn(TEXT[:300], self.lines()[0])

    def test_a_401_envelope_is_the_account_class(self):
        self.assertEqual(refused(5, 401).error_class, "account")
        body = {**json.loads(FX.read_text()), "is_error": False}
        res = claude.parse(json.dumps(body), "", 1, now=5)
        self.assertEqual(res.error_class, "fatal")                # no is_error: current class
        body = {**json.loads(FX.read_text()), "api_error_status": 500}
        self.assertEqual(claude.parse(json.dumps(body), "", 1, now=5).error_class, "fatal")

    def test_a_second_role_refusal_during_the_pause_adds_no_line_and_no_incident(self):
        now = int(time.time())
        self.script["guide"] = [[refused(now)], [GUIDE_OK]]
        self.script["implementer"] = [[IMPL_DONE]]
        for step in range(3):                                 # the guide run closes on the third
            self.tick.once(now + step)
        self.assertEqual(len(self.lines()), 1)
        run = self.runs("guide")[0]
        doc = {"error_class": "account", "error": TEXT, "retry_after": now + 1800}
        self.tick._pause(self.conn, run, now + 1800, now + 5, doc)    # the validator, same account
        self.assertEqual(len(self.lines()), 1)
        self.assertEqual(incidents.listing(self.conn), [])

    def test_error_line_keys_a_json_envelope_on_its_result(self):
        env = json.loads(FX.read_text())
        self.assertEqual(worker._error_line(json.dumps(env)), TEXT[:300])
        no_result = {**env, "result": ""}
        self.assertEqual(worker._error_line(json.dumps({**no_result, "errors": ["a", "b"]})),
                         "a; b")
        self.assertEqual(worker._error_line(json.dumps(no_result)), env["api_error_code"])
        self.assertEqual(worker._error_line("x\nValueError: boom\n"), "ValueError: boom")
