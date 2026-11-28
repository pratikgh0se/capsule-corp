"""A lost Claude login pauses the board instead of crashing the card (t_040dbbe2, inc_e25fd25e).
Z1 intent: the CLI refuses a run with its own `Not logged in` line, the run is released, the
board waits 5 minutes, and no crash, failure charge, review try or incident follows. The claude
binary is never run: `claude.parse` reads the CLI's own output shapes from a fixture."""
import json
import time
from pathlib import Path

from capsule import db, incidents, invalid_doc, worker
from capsule.models import claude
from capsule.tests.test_korin import KorinCase
from capsule.tests.test_tick import GUIDE_OK, IMPL_DONE

FX = Path(__file__).parent / "fixtures" / "claude" / "not_logged_in.json"
LINE = claude.AUTH_LINES[0]


def envelope(result, is_error=True):
    return json.dumps({"type": "result", "subtype": "success", "is_error": is_error,
                       "session_id": "s", "result": result, "usage": {}})


def lost(now):
    return claude.parse(FX.read_text(), "", 1, now=now)


class LoginLost(KorinCase):
    def kinds(self):
        sql = "SELECT kind FROM task_events WHERE task_id=? ORDER BY id"
        return [r[0] for r in self.conn.execute(sql, (self.tid,))]

    def test_a_not_logged_in_envelope_pauses_the_board_and_opens_no_crash(self):
        now = int(time.time())
        res = lost(now)
        self.assertEqual((res.error_class, res.retry_after), ("auth_required", now + 300))
        self.script["implementer"] = [[res], [IMPL_DONE]]
        self.assertEqual(self.tick.once(now)["dispatched"], 1)
        self.tick.once(now + 1)                                          # the run closes
        run = self.runs("implementer")[0]
        self.assertEqual((run["status"], run["error_class"]), ("released", "auth_required"))
        self.assertGreater(db.guard_get(self.conn, "pause:board")[0]["until"], now)
        self.assertNotIn("crashed", self.kinds())
        self.assertEqual(self.task()["failure_count"], 0)
        self.assertEqual(incidents.listing(self.conn), [])
        sql = "SELECT body FROM outbox WHERE body LIKE '%paused%'"
        lines = [r[0] for r in self.conn.execute(sql)]
        self.assertEqual(len(lines), 1)
        self.assertIn("Claude login lost on default", lines[0])
        self.assertEqual(self.tick.once(now + 2)["dispatched"], 0)      # the pause holds

    def test_a_guide_run_with_a_lost_login_uses_no_review_try(self):
        now = int(time.time())
        self.script["guide"] = [[lost(now)], [GUIDE_OK]]
        self.script["implementer"] = [[IMPL_DONE]]
        self.tick.once(now)
        self.tick.once(now + 1)                    # the guide run closes: released, board paused
        self.tick.once(now + 301)                  # the pause is over: the guide runs again
        self.tick.once(now + 302)
        guides = self.runs("guide")
        self.assertEqual(len(guides), 2)
        self.assertEqual((guides[0]["status"], guides[0]["outcome"]), ("released", "rate_limited"))
        self.assertNotEqual(guides[1]["outcome"], "rate_limited")
        self.assertNotEqual(self.task()["status"], "blocked")
        task = self.conn.execute("SELECT block_kind FROM tasks WHERE id=?", (self.tid,)).fetchone()
        self.assertNotEqual(task["block_kind"], "review_infra")
        rounds = self.tick._review_round(self.conn, self.tid, guides[0])
        self.assertNotIn(guides[0]["id"], [r["id"] for rs in rounds.values() for r in rs])

    def test_a_korin_run_with_a_lost_login_puts_the_incident_back_to_open(self):
        self.enable()
        now = int(time.time())
        self.korin_docs.append(lambda inc: lost(now))
        self.crash_once(passes=3, now=now)
        rec = self.incidents()[0]
        korin = self.conn.execute(
            "SELECT * FROM task_runs WHERE profile='korin' ORDER BY id").fetchall()
        self.assertEqual(len(korin), 1)
        self.assertEqual((korin[0]["status"], korin[0]["outcome"]), ("released", "rate_limited"))
        self.assertEqual(rec["status"], "open")
        counted = [k for k in korin if k["outcome"] not in ("completed", "rate_limited")]
        self.assertEqual(counted, [])                          # the try count did not grow

    def test_other_is_error_text_stays_fatal(self):
        cases = [(envelope("Invalid tool config"), ""),
                 (envelope(f"Note: {LINE} was printed earlier"), ""),
                 (envelope("Invalid API key · Please run /login"), ""),
                 (envelope(LINE, is_error=False), ""),          # exit 1 without is_error
                 ("", f"warning: {LINE} (quoted)\n"),
                 ("", "Invalid API key · Please run /login\n")]
        for out, err in cases:
            res = claude.parse(out, err, 1, now=1)
            self.assertEqual((res.error_class, res.retry_after), ("fatal", None), (out, err))

    def test_a_success_report_quoting_the_line_stays_ok(self):
        res = claude.parse(envelope(LINE, is_error=False), "", 0, now=1)
        self.assertIsNone(res.error_class)

    def test_the_line_alone_on_stderr_without_an_envelope_is_a_lost_login(self):
        res = claude.parse("", f"{LINE}\n", 1, now=10)
        self.assertEqual((res.error_class, res.retry_after), ("auth_required", 310))

    def test_an_is_error_envelope_gives_its_result_as_the_error_line(self):
        a = worker._error_line(envelope(LINE))
        b = worker._error_line(envelope("Invalid tool config"))
        self.assertEqual((a, b), (LINE, "Invalid tool config"))
        self.assertEqual(worker._error_line(envelope("x" * 400)), "x" * 300)
        # not an error envelope, or an empty result: the old path (the raw JSON, cut)
        self.assertTrue(worker._error_line(envelope("ok line", is_error=False)).startswith("{"))
        self.assertTrue(worker._error_line(envelope("")).startswith("{"))
        self.assertEqual(worker._error_line("x\nValueError: boom\n"), "ValueError: boom")

    def test_a_refusal_text_longer_than_300_characters_is_cut_to_300(self):
        doc = {"error_class": "account", "error": "y" * 400}
        self.assertEqual(invalid_doc.refusal(doc), "y" * 300)
        self.assertEqual(invalid_doc.refusal({"error_class": "rate_limit", "error": "y"}), "")

    def test_an_account_run_with_an_empty_error_is_still_labelled_account_refused(self):
        run = {"account": "acct", "task_id": "t", "run_id": "r", "profile": "p"}
        keys, why = invalid_doc.pause_scope(None, "b", run, {"error_class": "account"}, 0)
        self.assertEqual(keys, ["pause:board", "pause:account:acct"])
        self.assertEqual(why, "account refused on acct")
