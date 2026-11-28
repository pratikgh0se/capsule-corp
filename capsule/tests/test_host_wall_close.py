"""The tick closing a run the host wall refused (D161), and Korin's inputs for a blind incident.
A refused run is infrastructure: a board pause that retries by itself, one incident keyed on the
failure, never the card's failure, and one message to a person per outage (S26, S30); a
light-tier or job-specific refusal holds the card and opens nothing (S27)."""
import json
import time
from unittest import mock

from capsule import (bundles, db, dispatch, idle_lane, incident_inputs, incidents, tick,
                     worker)
from capsule.models import Result
from capsule.runners import host_wall
from capsule.tests.test_korin import FATAL, KorinCase
from capsule.tests.test_tick import IMPL_DONE, REV_PASS, TickCase, git

TOKENS = {"in": 0, "out": 0, "cache_read": 0}
LIGHT = Result(None, 1, TOKENS, 1, "wall_light_tier", None, None, host_wall.LIGHT_REFUSED)
JOB = Result(None, 1, TOKENS, 1, "wall_job_refused", None, None,
             "host wall refused this job: the workspace folder name is too long")


def wall_result(now, why="host wall unavailable: /usr/bin/sandbox-exec: FileNotFoundError: gone"):
    """What `ClaudeAdapter.run` returns for a wall that cannot be applied (retry in 300 s)."""
    return Result(None, 1, TOKENS, 1, "wall_unavailable", now + 300, None, why)


class WallCase(TickCase):
    def guard(self, key):
        return db.guard_get(self.conn, key)[0]

    def messages(self):
        """The file-channel lines that name the wall as a stop (not the incident's own line)."""
        return [r[0] for r in self.conn.execute(
            "SELECT body FROM outbox WHERE channel='file' AND body LIKE '%host wall cannot start%'"
        )]


class Closing(WallCase):
    def incidents(self):
        rows = self.conn.execute("SELECT * FROM incidents WHERE role='host_wall'")
        return [dict(r) for r in rows]

    @staticmethod
    def same_day(span):
        """A `now` whose next `span` seconds stay in one UTC day: an incident dedupes within a
        day (`incidents._day_start`), so ticks that crossed midnight would open a second one."""
        now = int(time.time())
        end = incidents._day_start(now + span)
        return now if incidents._day_start(now) == end else end

    def test_an_implementer_run_is_released_the_board_pauses_and_it_retries_on_its_own(self):
        now = int(time.time())
        self.script["implementer"] = [[wall_result(now)], [IMPL_DONE]]
        self.assertEqual(self.tick.once(now)["dispatched"], 1)
        self.assertEqual(self.tick.once(now + 1)["dispatched"], 0)
        task = self.task()
        self.assertEqual((task["status"], task["block_kind"], task["failure_count"]),
                         ("ready", None, 0))
        self.assertEqual(self.guard("pause:board")["until"], now + 300)
        self.assertIn("wall", self.guard("pause:board"))            # the outage's mark
        self.assertIsNone(self.guard("pause:account:default"))      # not the account's limit
        run = self.runs("implementer")[0]
        self.assertEqual((run["error_class"], run["outcome"]), ("wall_unavailable", "rate_limited"))
        self.assertEqual([(i["kind"], i["role"]) for i in self.incidents()],
                         [("review_infra", "host_wall")])
        self.assertEqual(self.tick.once(now + 301)["dispatched"], 1)     # no person needed

    def test_an_outage_sends_one_message_naming_the_wall_not_one_per_retry(self):
        now = int(time.time())
        self.script["implementer"] = [[wall_result(now + 301 * k)] for k in range(6)]
        self.script["implementer"] += [[IMPL_DONE]]
        for k in range(7):
            self.tick.once(now + 301 * k)
        self.assertEqual(self.task()["failure_count"], 0)
        self.assertEqual(len(self.conn.execute(
            "SELECT 1 FROM task_runs WHERE error_class='wall_unavailable'").fetchall()), 6)
        said = self.messages()
        self.assertEqual(len(said), 1, said)
        self.assertIn("host wall", said[0])
        self.assertIn("🚨", said[0])            # the class that breaks quiet hours, not `paused`
        self.assertIn(self.cfg["board"], said[0])

    def test_a_later_outage_after_a_working_walled_session_sends_another(self):
        now = int(time.time())
        self.script["implementer"] = [[wall_result(now)], [IMPL_DONE]]
        self.script["reviewer"] = [[wall_result(now + 700)], [REV_PASS]]
        for i in range(30):
            self.tick.once(now + i * 100)
        self.assertEqual(len(self.messages()), 2, self.messages())

    def test_a_validators_crash_with_blind_rows_leaves_no_run_text_in_any_record(self):
        marker = "BLINDMARK_e19a given: 7 expect: 8"

        def blind_rows(job, sp, run):
            rows = [{"cmd": "blind:t1.test_x", "given": marker, "observed": marker}]
            (sp / "job.json").write_text(json.dumps(dict(job, inputs=dict(
                job.get("inputs") or {}, outputs=rows))))
        self.hooks["validator"] = blind_rows
        raw = json.dumps({"result": f"ValueError: {marker}",
                          "permission_denials": [{"tool_name": "Read", "tool_use_id": "u1"}]})
        crash = Result(None, 1, TOKENS, 1, "fatal", None, "sess", raw)
        self.script["implementer"] = [[IMPL_DONE]]
        self.script["validator"] = [[crash]]
        now = int(time.time())
        for k in range(6):
            self.tick.once(now + k)
        found = list((self.home / "boards" / "test" / "spool").glob("*/run.json"))
        crashed = [p for p in found if json.loads(p.read_text()).get("error_class") == "fatal"]
        self.assertTrue(crashed)
        seen = " ".join((p.parent / n).read_text()          # what the run left a reader of its logs
                        for p in crashed for n in ("run.json", "worker.log"))
        for table in ("task_events", "incidents", "outbox", "tasks", "task_runs"):
            seen += json.dumps([dict(r) for r in self.conn.execute(f"SELECT * FROM {table}")])
        self.assertNotIn("BLINDMARK", seen)
        self.assertIn(worker.BLIND_ERROR, seen)                 # the fixed phrase and the class

    def test_a_denial_reason_is_kept_for_an_ordinary_run_and_dropped_for_one_with_blind_rows(self):
        text = "BLINDMARK: could not read the expected text"
        raw = json.dumps({"permission_denials": [{"tool_name": "Read", "tool_use_id": "u1"}],
                          "result": text})
        self.assertEqual(worker._tool_denials(raw)[1], text)
        denials, reason = worker._tool_denials(raw, True)
        self.assertEqual((reason, [d["tool_name"] for d in denials]), (None, ["Read"]))

    def test_a_pause_set_by_an_outage_stops_quieting_the_idle_checks_after_15_minutes(self):
        now = int(time.time())
        db.guard_set(self.conn, "pause:board", {"until": now + 5000, "wall": now}, now=now)
        for later, quiet in ((10, True), (dispatch.WALL_QUIET - 1, True),
                             (dispatch.WALL_QUIET + 1, False)):
            self.assertEqual(dispatch.any_pause(self.conn, now + later, checks=True), quiet, later)
            self.assertTrue(dispatch.any_pause(self.conn, now + later))
        db.guard_set(self.conn, "pause:board", {"until": now + 5000, "reason": "person"}, now=now)
        self.assertTrue(dispatch.any_pause(self.conn, now + 4000, checks=True))     # not the wall's
        db.guard_set(self.conn, "pause:board", {"until": now + 5000, "wall": now}, now=now)
        with mock.patch.object(dispatch, "any_pause", return_value=False) as seen:
            idle_lane.find(self.conn, now + 2000)
        self.assertEqual(seen.call_args.kwargs, {"checks": True})

    def test_a_wall_failure_keeps_one_incident_key_whatever_its_message_holds(self):
        now = self.same_day(700)
        whys = ["host wall unavailable: /usr/bin/sandbox-exec: FileNotFoundError: gone",
                "host wall unavailable: /tmp/spool/2/hostwall.sb: sandbox-exec refused the profile",
                "host wall unavailable: the profile at /tmp/spool/3/hostwall.sb was refused"]
        self.script["implementer"] = [[wall_result(now, why)] for why in whys] + [[IMPL_DONE]]
        for k in range(3):
            self.tick.once(now + 301 * k)
        rows = self.incidents()
        self.assertEqual(len(rows), 1, [r["root_key"] for r in rows])
        self.assertEqual(rows[0]["root_key"], "review_infra: host_wall: wall_unavailable")
        self.assertEqual(rows[0]["fingerprint"], incidents.fingerprint(
            "review_infra", "wall_unavailable", role="host_wall"))
        evidence = json.loads(rows[0]["evidence"])
        self.assertIn(whys[0], evidence)                            # the text stays as detail
        self.assertFalse(incident_inputs.blind_incident(evidence, rows[0]["role"]))
        self.assertEqual(incident_inputs.scrub(evidence), {})       # its key is kept for Korin

    def test_the_same_failure_twice_is_one_incident_and_no_failure_count(self):
        now = self.same_day(700)
        self.script["implementer"] = [[wall_result(now)], [wall_result(now + 400)], [IMPL_DONE]]
        self.tick.once(now)
        self.tick.once(now + 301)
        self.assertEqual((self.task()["failure_count"], len(self.incidents())), (0, 1))

    def test_a_reviewer_run_is_released_too_and_costs_no_round(self):
        now = int(time.time())
        self.script["implementer"] = [[IMPL_DONE]]
        self.script["reviewer"] = [[wall_result(now + 200)], [wall_result(now + 700)], [REV_PASS]]
        for i in range(14):
            self.tick.once(now + i * 100)
        task = self.task()
        self.assertEqual(task["failure_count"], 0)
        self.assertNotEqual(task["block_kind"], "review_infra")
        self.assertEqual([r["outcome"] for r in self.runs("reviewer")][:2],
                         ["rate_limited", "rate_limited"])
        self.assertEqual(len({i["fingerprint"] for i in self.incidents()}), 1)

    def test_a_light_tier_refusal_holds_the_card_with_no_incident_and_no_pause(self):
        self.script["implementer"] = [[LIGHT]]
        for i in range(3):
            self.tick.once(int(time.time()) + i)
        task = self.task()
        self.assertEqual((task["status"], task["block_kind"], task["failure_count"]),
                         ("blocked", "capability", 0))
        self.assertIn("cannot nest", self.conn.execute(
            "SELECT payload FROM task_events WHERE kind='blocked'").fetchone()[0])
        self.assertIsNone(self.guard("pause:board"))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0], 0)

    def test_a_job_refusal_holds_that_card_alone_with_a_plain_reason_and_others_go_on(self):
        git(self.home / "origin", "branch", "epic/other")
        self.cfg["epics"].append("epic/other")
        self.write_cfg()
        other = db.new_task(self.conn, title="second", body="Implements §6.9.",
                            assignee="dev-frontend", branch="epic/other")
        self.script["implementer"] = [[JOB], [IMPL_DONE]]
        now = int(time.time())
        for i in range(4):
            self.tick.once(now + i)
        task = self.task()
        self.assertEqual((task["status"], task["block_kind"], task["failure_count"]),
                         ("blocked", "capability", 0))
        self.assertIn("too long", self.conn.execute(
            "SELECT payload FROM task_events WHERE kind='blocked'").fetchone()[0])
        self.assertIsNone(self.guard("pause:board"))                # no board pause
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0], 0)
        self.assertEqual(self.messages(), [])
        ran = self.conn.execute("SELECT 1 FROM task_runs WHERE task_id=?", (other,)).fetchone()
        self.assertTrue(ran)

    def test_a_light_tier_refusal_of_a_review_role_holds_the_card_the_same_way(self):
        self.script["implementer"] = [[IMPL_DONE]]
        self.script["reviewer"] = [[LIGHT]]
        for i in range(8):
            self.tick.once(int(time.time()) + i)
        task = self.task()
        self.assertEqual((task["status"], task["block_kind"]), ("blocked", "capability"))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0], 0)


class KorinWall(KorinCase):
    """Korin: a session the wall refuses burns none of the incident's tries, and a blind
    incident's input holds ids and counts only."""

    def hand_open(self, key, evidence=(), **kw):
        inc, _ = incidents.open_incident(self.conn, board="test", kind="crash", key=key,
                                         now=int(time.time()), evidence=list(evidence), **kw)
        return inc

    def row(self, inc):
        return incidents.get(self.conn, inc)

    def test_a_refused_korin_session_burns_no_try_and_the_incident_stays_open(self):
        now = int(time.time())
        inc = self.hand_open("wall-korin")
        self.enable()
        self.script["implementer"] = [[IMPL_DONE]]
        self.korin_docs += [lambda i, k=k: wall_result(now + 301 * k) for k in range(4)]
        for k in range(4):
            self.tick.once(now=now + 301 * k)
        ended = self.conn.execute("SELECT outcome FROM task_runs WHERE profile='korin'"
                                  " AND ended_at IS NOT NULL").fetchall()
        self.assertTrue(ended and {r["outcome"] for r in ended} == {"rate_limited"},
                        [dict(r) for r in ended])
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE kind='korin_gave_up'").fetchone()[0], 0)
        self.assertEqual(self.korin_jobs[0]["inputs"]["incident"]["id"], inc)

    def test_a_blind_incident_gets_ids_and_counts_never_a_tail_or_an_events_text(self):
        spools = self.home / "boards" / "test" / "spool"
        tester, plain = spools / "tester", spools / "plain"
        for d in (tester, plain):
            d.mkdir(parents=True)
            (d / "worker.log").write_text(f"LOG {d.name}")
        clone = self.home / "boards" / "test" / "blind-work" / "t1"
        (tester / "job.json").write_text(json.dumps({"workspace": str(clone)}))
        (plain / "job.json").write_text(json.dumps({"workspace": str(self.home)}))
        blind = self.hand_open("blind-tester crash: SECRET LINE", [str(tester), "event:1"])
        ordinary = self.hand_open("plain crash", [str(plain)])
        self.enable(max_running=5)
        self.korin_docs += [lambda i: FATAL] * 4
        self.tick.once(now=int(time.time()))
        jobs = {j["inputs"]["incident"]["id"]: j["inputs"] for j in self.korin_jobs}
        self.assertEqual(set(jobs), {blind, ordinary})
        logged = [log["path"] for log in jobs[blind]["logs"]]
        self.assertFalse([p for p in logged if "tester" in p or "board events" in p], logged)
        self.assertEqual(jobs[blind]["evidence"], ["event:1"])   # the tester's folder: not named
        self.assertEqual(jobs[blind]["incident"]["root_key"], "crash: withheld: an incident"
                         " on a blind run")                       # its kind only: the rest is text
        self.assertIn(str(plain / "worker.log"), [log["path"] for log in jobs[ordinary]["logs"]])
        self.assertEqual(jobs[ordinary]["incident"]["root_key"], "crash: plain crash")

    def test_a_run_carrying_blind_rows_has_no_log_line_for_a_hang_and_no_tail_for_korin(self):
        spools = self.home / "boards" / "test" / "spool"
        rows, plain = spools / "rows", spools / "plain"
        for d in (rows, plain):
            d.mkdir(parents=True)
            (d / "worker.log").write_text(f"first\nLAST {d.name}\n")
        (rows / "job.json").write_text(json.dumps({"workspace": str(self.home), "inputs": {
            "outputs": [{"cmd": "blind:t1.test_x", "observed": "SECRET"}]}}))
        (plain / "job.json").write_text(json.dumps({"workspace": str(self.home), "inputs": {}}))
        self.assertEqual(tick._last_line(rows / "worker.log"), "")
        self.assertEqual(tick._last_line(plain / "worker.log"), "LAST plain")
        self.assertEqual(tick._last_line(spools / "none.log"), "")
        inc = self.hand_open("hang: timed out: x", [str(rows), "event:2"], role="validator")
        self.enable(max_running=5)
        self.korin_docs += [lambda i: FATAL] * 2
        self.tick.once(now=int(time.time()))
        got = [j["inputs"] for j in self.korin_jobs if j["inputs"]["incident"]["id"] == inc]
        self.assertEqual([g["logs"] for g in got], [[]])

    def test_an_incident_on_a_blind_role_stores_no_text_from_the_run(self):
        spool = self.home / "boards" / "test" / "spool" / "tester"
        spool.mkdir(parents=True)
        text = ["SECRET reason", "/etc/hosts", f"{spool}/worker.log", "event:4", str(spool)]
        for role, key in (("blind-tester", "fatal: exit 1: SECRET LINE"),
                          ("blind-tester", "timed out: SECRET LOG"),
                          ("blind_red:t_1", "runner failed"),
                          ("blind_runner:t_1", "inconclusive: SECRET")):
            inc = self.hand_open(key, text, role=role, task_id=self.tid, run_id="r1")
            rec = self.row(inc)
            self.assertNotIn("SECRET", json.dumps(rec), role)
            self.assertEqual(json.loads(rec["evidence"]), ["event:4", str(spool)])
            self.assertEqual((rec["task_id"], rec["run_id"], rec["role"]), (self.tid, "r1", role))
            self.assertTrue(rec["root_key"].startswith(f"crash: {role}: "), rec["root_key"])
            incidents.attach(self.conn, inc, ["SECRET again", "event:9"])
            kept = json.loads(self.row(inc)["evidence"])
            self.assertEqual(kept, ["event:4", str(spool), "event:9"])
        again = self.hand_open("fatal: exit 2: OTHER TEXT", text, role="blind-tester")
        self.assertEqual(self.row(again)["fingerprint"], self.row(
            self.hand_open("fatal: exit 1: SECRET LINE", [], role="blind-tester"))["fingerprint"])
        plain = self.hand_open("fatal: exit 1: keeps its text", ["SECRET reason"],
                               role="implementer")
        self.assertIn("keeps its text", self.row(plain)["root_key"])
        self.assertEqual(json.loads(self.row(plain)["evidence"]), ["SECRET reason"])

    def test_a_testers_crash_and_hang_reach_korin_as_ids_and_a_class_only(self):
        root = self.home / "boards" / "test" / "spool"
        for run in ("r7", "r8"):         # a spool whose job.json does not say "tester" either
            (root / run).mkdir(parents=True)
            (root / run / "worker.log").write_text("SECRET WORKER LOG\n")
            (root / run / "job.json").write_text(json.dumps({"workspace": str(self.home)}))
        incidents.from_events(self.conn, board="test", now=int(time.time()))      # the watermark
        for kind, body in (("crashed", {"role": "blind-tester", "error_class": "fatal", "exit": 1,
                                        "error": "SECRET CRASH LINE", "run_id": "r7"}),
                           ("timed_out", {"role": "blind-tester", "last_log": "SECRET LOG LINE",
                                          "run_id": "r8"})):
            db.event(self.conn, self.tid, kind, body, now=int(time.time()))
        opened = incidents.from_events(self.conn, board="test", spool_root=root,
                                       now=int(time.time()))
        self.assertEqual(len(opened), 2)
        self.enable(max_running=5)
        self.korin_docs += [lambda i: FATAL] * 4
        self.tick.once(now=int(time.time()))
        self.assertEqual(len(self.korin_jobs), 2)
        prompts = [bundles.korin({"id": "t", "title": "k", "body": ""}, job["inputs"]["incident"],
                                 job["inputs"]["evidence"], job["inputs"]["logs"])["prompt"]
                   for job in self.korin_jobs]
        seen = json.dumps(self.korin_jobs) + "".join(prompts) + json.dumps(
            [dict(r) for r in self.conn.execute("SELECT title, body FROM tasks")]) + json.dumps(
            [dict(r) for r in self.conn.execute("SELECT * FROM incidents")])
        self.assertNotIn("SECRET", seen)
        self.assertIn("r7", seen)
        self.assertIn("fatal", seen)
