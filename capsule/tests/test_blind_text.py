"""Blind text and what may carry it (D161): a validator run whose job holds `blind:` rows never
lets a test id, a given/expect/observed line or an exception message reach the implementer or
Korin. Rows reach the implementer as one fixed phrase however they are mixed; a failure's class and
exit code still reach Korin; an incident record names no path Korin may not read."""
import io
import json
import subprocess
import time
import unittest
from unittest import mock

from capsule import (bundles, incident_inputs, incidents, paths, runaway, tick,
                     validator_report, worker)
from capsule.tests.test_host_wall import Board
from capsule.tests.test_korin import FATAL, KorinCase
from capsule.tests.test_tick import IMPL_DONE, TickCase

MARK = "BLINDMARK given: 7 expect: 8"


def blind_rows(report="report"):
    """A validator hook that writes blind rows into the run's job.json."""
    def hook(job, sp, run):
        rows = [{"cmd": "blind:t1.test_x", "blind": report, "given": MARK, "observed": MARK},
                {"cmd": "unit-tests", "output": "1 failure"}]
        (sp / "job.json").write_text(json.dumps(dict(job, inputs=dict(
            job.get("inputs") or {}, outputs=rows))))
    return hook


def fail(cmd, text):
    return {"cmd": cmd, "exit": 1, "result": "fail", "excerpt": text}


class Phrases(unittest.TestCase):
    OUT = [{"cmd": "blind:t1.test_x", "blind": "report"},
           {"cmd": "blind:t2.test_y", "blind": "report"}]

    def doc(self, *assertions):
        return {"verdict": "fail", "assertions": list(assertions), "not_run": []}

    def test_blind_rows_are_one_phrase_however_many_fail_or_mix_with_others(self):
        doc = self.doc(fail("blind:t1.test_x", f"given: {MARK}"), fail("unit-tests", "1 failure"),
                       fail("blind:t2.test_y", MARK))
        lines = validator_report.findings(doc)
        self.assertEqual(lines, [validator_report.BLIND_FINDING,
                                 "validator: `unit-tests` exit 1 fail: 1 failure"])
        self.assertNotIn("t1", "".join(lines))
        self.assertNotIn("BLINDMARK", "".join(lines))
        only = self.doc(fail("blind:t1.test_x", MARK), fail("blind:t2.test_y", MARK))
        self.assertEqual(validator_report.findings(only), [validator_report.BLIND_FINDING])

    def test_a_not_run_blind_row_is_the_phrase_too_unless_only_judged_rows_count(self):
        doc = self.doc({"cmd": "blind:t3.test_z", "exit": 0, "result": "not_run"})
        self.assertEqual(validator_report.findings(doc), [validator_report.BLIND_FINDING])
        self.assertEqual(validator_report.findings(doc, judged_only=True), [])

    def test_the_report_mode_violation_is_a_count_never_a_test_id(self):
        doc = self.doc(fail("blind:t1.test_x", MARK), fail("blind:t2.test_y", MARK))
        text = validator_report.not_run_violation(doc, self.OUT)
        self.assertIn("only on report-only blind row(s), 2 of them", text)
        self.assertNotIn("t1", text)
        self.assertNotIn("test_", text)
        mixed = self.doc(fail("blind:t1.test_x", MARK), fail("unit-tests", "x"))
        self.assertIsNone(validator_report.not_run_violation(mixed, self.OUT))

    def test_a_not_run_violation_does_not_name_a_blind_row(self):
        doc = {"verdict": "fail", "not_run": ["blind:t4.test_w", "make lint"], "assertions": []}
        text = validator_report.not_run_violation(doc, self.OUT + [{"cmd": "make lint"}])
        self.assertIn("make lint", text)
        self.assertNotIn("t4", text)


class Mixed(TickCase):
    def test_a_mixed_fail_reaches_the_implementer_as_the_fixed_phrase_only(self):
        val = {"schema": "validator_result.v1", "verdict": "fail", "not_run": [], "assertions": [
            fail("blind:t1.test_x", f"given: {MARK}"), fail("unit-tests", "1 failure")]}
        self.hooks["validator"] = blind_rows()
        seen = []
        self.hooks["implementer"] = lambda job, sp, run: seen.append(json.dumps(job))
        self.script["implementer"] = [[IMPL_DONE], [IMPL_DONE]]
        self.script["validator"] = [[val]]
        for _ in range(3):
            self.tick.once()
        self.runner.up = False
        for _ in range(3):
            self.tick.once()
        comments = [r[0] for r in self.conn.execute("SELECT body FROM task_comments")]
        failed = [c for c in comments if c.startswith("Review failed:")]
        self.assertTrue(failed, comments)
        self.assertIn(validator_report.BLIND_FINDING, failed[0])
        self.assertIn("unit-tests", failed[0])
        surfaces = "".join(failed) + "".join(seen[1:]) + json.dumps(
            [dict(r) for r in self.conn.execute("SELECT * FROM tasks")])
        self.assertNotIn("BLINDMARK", surfaces)
        self.assertNotIn("t1.test_x", surfaces)
        self.assertGreaterEqual(len(seen), 2)       # the rerun's job was read


class Errors(TickCase):
    def boom(self, profile="validator"):
        real = tick.Tick._close_one

        def close_one(inst, conn, cfg, root, pk, now, counts):
            row = conn.execute("SELECT profile FROM task_runs WHERE id=?", (pk,)).fetchone()
            if row[0] == profile:
                raise ValueError(f"bad report {MARK}")
            return real(inst, conn, cfg, root, pk, now, counts)
        return mock.patch.object(tick.Tick, "_close_one", close_one)

    def surfaces(self):
        return json.dumps({t: [dict(r) for r in self.conn.execute(f"SELECT * FROM {t}")]
                           for t in ("task_events", "outbox", "incidents")})

    def run_ticks(self):
        self.script["implementer"] = [[IMPL_DONE]]
        now = int(time.time())
        with self.boom():
            for k in range(6):
                self.tick.once(now + k)

    def test_a_tick_error_on_a_validator_with_blind_rows_keeps_the_type_and_drops_the_text(self):
        self.hooks["validator"] = blind_rows()
        self.run_ticks()
        errors = [json.loads(r[0])["error"] for r in self.conn.execute(
            "SELECT payload FROM task_events WHERE kind='tick_error'")]
        self.assertTrue(errors)
        self.assertIn("ValueError: withheld: the run carries blind rows", errors[0])
        self.assertNotIn("BLINDMARK", self.surfaces())
        keys = [r[0] for r in self.conn.execute("SELECT root_key FROM incidents")]
        self.assertTrue(any(k.startswith("crash: tick: valueerror: withheld") for k in keys), keys)

    def test_a_tick_error_on_an_ordinary_run_keeps_its_text(self):
        self.run_ticks()
        self.assertIn("BLINDMARK", self.surfaces())


class Rewritten(TickCase):
    """A run.json any process may have written: the tick withholds its text itself."""

    def spawn(self, board_db, spool_dir, run_id, role):
        super().spawn(board_db, spool_dir, run_id, role)
        if role == "validator":
            run = spool_dir / "run.json"
            done = json.loads(run.read_text())
            run.write_text(json.dumps(dict(done, error_class="fatal", error=f"boom {MARK}",
                                           denial_reason=MARK)))

    def crashed_text(self):
        self.script["implementer"] = [[IMPL_DONE]]
        for _ in range(5):
            self.tick.once()
        rows = self.conn.execute("SELECT payload FROM task_events WHERE kind='crashed'").fetchall()
        return " ".join(r[0] for r in rows)

    def test_a_validators_crash_text_is_withheld_when_its_job_has_blind_rows(self):
        self.hooks["validator"] = blind_rows()
        text = self.crashed_text()
        self.assertIn("fatal: " + incident_inputs.BLIND_RUN_ERROR, text)
        self.assertNotIn("BLINDMARK", text)

    def test_a_validators_crash_text_is_kept_for_an_ordinary_job(self):
        self.assertIn("BLINDMARK", self.crashed_text())


class KorinBlind(KorinCase):
    def hand_open(self, key, evidence, role):
        inc, _ = incidents.open_incident(self.conn, board="test", kind="crash", key=key, role=role,
                                         now=int(time.time()), evidence=evidence)
        return inc

    def job_for(self, inc):
        return next(j["inputs"] for j in self.korin_jobs if j["inputs"]["incident"]["id"] == inc)

    def test_a_validator_incident_gives_korin_the_class_and_exit_and_no_path_it_cannot_read(self):
        spools = self.home / "boards" / "test" / "spool"
        rows, plain = spools / "rows", spools / "plain"
        for sp, cmd in ((rows, "blind:t1.test_x"), (plain, "make test")):
            sp.mkdir(parents=True)
            (sp / "job.json").write_text(json.dumps({"workspace": str(self.home), "inputs": {
                "outputs": [{"cmd": cmd, "observed": "SECRET"}]}}))
            (sp / "worker.log").write_text(f"LOG {sp.name}")
        blind = self.hand_open("fatal: exit 1: SECRET LINE", [str(rows), "event:1"], "validator")
        ordinary = self.hand_open("fatal: exit 2: plain", [str(plain), "event:1"], "validator")
        self.enable(max_running=5)
        self.korin_docs += [lambda i: FATAL] * 4
        self.tick.once(now=int(time.time()))
        got, other = self.job_for(blind), self.job_for(ordinary)
        self.assertEqual(got["evidence"], ["event:1"])           # no path it would be refused
        self.assertEqual(got["incident"]["root_key"], "crash: validator: fatal: exit 1: withheld:"
                         " an incident on a blind run")           # class and exit code, no text
        self.assertEqual([log for log in got["logs"] if "rows" in log["path"]], [])
        self.assertEqual(other["evidence"], [str(plain), "event:1"])
        self.assertEqual(other["incident"]["root_key"], "crash: validator: fatal: exit 2: plain")
        card = self.conn.execute("SELECT body FROM tasks WHERE id=?",
                                 (incidents.get(self.conn, blind)["korin_task"],)).fetchone()[0]
        self.assertNotIn("secret", card.lower())                  # the key is stored lower-cased
        self.assertIn("fatal: exit 1", card)
        prompt = bundles.korin({"id": "t", "title": "k", "body": ""}, got["incident"],
                               got["evidence"], got["logs"])["prompt"]
        self.assertNotIn(str(rows), prompt)
        self.assertNotIn("secret", prompt.lower())

    def test_the_outbox_log_tail_gives_korin_a_decisions_criterion_and_no_test_id_or_excerpt(self):
        ctx = "Criterion 3: c\n  Check blind.t.T.test_a\n    given: GIVENTXT\n    observed: SEENTXT"
        doc = json.dumps({"schema": "decision.v1", "context": ctx})
        log = paths.outbox_log(self.tick.home, self.tick.board)
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(f"1\t{doc}\n2\tdecision.v1 {{broken GIVENTXT\n3\tplain line\n")
        inc = self.hand_open("fatal: exit 2: plain", ["event:1"], "implementer")
        self.enable(max_running=5)
        self.korin_docs += [lambda i: FATAL] * 4
        self.tick.once(now=int(time.time()))
        text = json.dumps(self.job_for(inc)["logs"])
        self.assertIn("Criterion 3", text)
        self.assertNotIn("blind.t.T.test_a", text)
        self.assertIn("plain line", text)
        self.assertNotIn("GIVENTXT", text)
        self.assertNotIn("SEENTXT", text)
        self.assertIn("GIVENTXT", log.read_text())            # the human's record is unchanged


class Scrub(Board):
    def test_only_a_plain_class_and_an_exit_code_survive_of_a_blind_key(self):
        tester = self.spools / "tester"
        tester.mkdir()
        clone = self.root / "blind-work" / "x"
        (tester / "job.json").write_text(json.dumps({"workspace": str(clone)}))
        self.run_job(self.spool, self.ws)               # an ordinary run's spool
        ev = [str(tester)]
        tail = "withheld: an incident on a blind run"
        cases = {
            "crash: validator: fatal: exit 1: SECRET": f"crash: validator: fatal: exit 1: {tail}",
            "hang: validator: timed out: SECRET LOG": f"hang: validator: timed out: {tail}",
            "crash: tick: valueerror: SECRET": f"crash: tick: valueerror: {tail}",
            "crash: validator: fatal: exit 1x: SECRET": f"crash: validator: fatal: {tail}",
            "crash: blind-tester crash: secret line": f"crash: {tail}",
            "crash: validator: SECRET, with a comma": f"crash: validator: {tail}",
            "": tail,
        }
        for key, want in cases.items():
            self.assertEqual(incident_inputs.scrub(ev, key), {"root_key": want}, key)
        self.assertEqual(incident_inputs.scrub([str(self.spool)], "crash: validator: fatal"), {})
        self.assertEqual(incident_inputs.readable([str(tester), "event:3", str(self.spool), 7]),
                         ["event:3", str(self.spool), 7])


class Texts(TickCase):
    def run_of(self, profile, run_id, cmd=None, workspace=None):
        self.conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, started_at, run_id)"
            " VALUES (?, ?, 'completed', 1, ?)", (self.tid, profile, run_id))
        sp = self.spool_dir(run_id)
        sp.mkdir(parents=True)
        outputs = [{"cmd": cmd, "observed": MARK}] if cmd else []
        (sp / "job.json").write_text(json.dumps({"workspace": str(workspace or self.home),
                                                 "inputs": {"outputs": outputs}}))
        return sp

    def test_an_exception_text_is_withheld_for_a_run_or_a_card_that_carries_blind_rows(self):
        root, exc = self.spool_dir("x").parent, ValueError("quoted text")
        self.run_of("validator", "plain1", "make test")
        self.assertEqual(incident_inputs.tick_text(self.conn, exc, root, self.tid), "quoted text")
        self.assertEqual(incident_inputs.tick_text(self.conn, exc, root, "t_other", "nope"),
                         "quoted text")
        self.run_of("validator", "blind1", "blind:t1.test_x")
        withheld = "withheld: the run carries blind rows"
        for run_id in (None, "blind1"):
            self.assertEqual(incident_inputs.tick_text(self.conn, exc, root, self.tid, run_id),
                             withheld, run_id)
        self.assertEqual(incident_inputs.tick_text(self.conn, exc, root, self.tid, "plain1"),
                         "quoted text")
        self.assertEqual(incident_inputs.tick_text(self.conn, exc, root, "t_unrelated"),
                         "quoted text")                          # another card's error: its text
        self.run_of("blind-tester", "tester1")             # a tester run, whatever its job says
        self.assertEqual(incident_inputs.tick_text(self.conn, exc, root, self.tid, "tester1"),
                         withheld)

    def test_a_run_json_written_by_anyone_has_its_text_withheld_for_a_blind_run(self):
        raw = {"error_class": "fatal", "error": f"boom {MARK}", "denial_reason": MARK, "exit": 1}
        for profile, cmd in (("validator", "blind:t1.test_x"), ("blind-tester", None)):
            sp = self.run_of(profile, f"{profile}1", cmd)
            got = incident_inputs.run_text(dict(raw), sp, profile)
            self.assertEqual((got["error"], got["denial_reason"], got["exit"]),
                             (f"fatal: {incident_inputs.BLIND_RUN_ERROR}", None, 1), profile)
        sp = self.run_of("validator", "plain1", "make test")
        self.assertEqual(incident_inputs.run_text(dict(raw), sp, "validator"), raw)
        self.assertIsNone(incident_inputs.run_text(None, sp, "validator"))
        self.assertEqual(incident_inputs.BLIND_RUN_ERROR, worker.BLIND_ERROR)

    def test_the_guides_runaway_packet_names_blind_rows_as_the_fixed_phrase(self):
        doc = {"assertions": [fail("blind:t1.test_x", MARK), fail("blind:t2.test_y", MARK),
                              fail("unit-tests", "1 failure")]}
        lines = runaway._lines("r1", "validator", doc)
        self.assertEqual(lines, [f"run r1 {validator_report.BLIND_FINDING}",
                                 "run r1 validator `unit-tests` exit 1 1 failure"])


class Record(KorinCase):
    def test_a_blind_incident_record_and_a_plain_one(self):
        spool = self.home / "boards" / "test" / "spool" / "rows"
        spool.mkdir(parents=True)
        (spool / "job.json").write_text(json.dumps({"workspace": str(self.home), "inputs": {
            "outputs": [{"cmd": "blind:t1.test_x", "observed": "SECRET"}]}}))
        blind, _ = incidents.open_incident(self.conn, board="test", kind="crash", role="validator",
                                           key="fatal: exit 1: SECRET", evidence=[str(spool)])
        plain, _ = incidents.open_incident(self.conn, board="test", kind="crash", role="validator",
                                           key="fatal: exit 2: kept text", evidence=["event:1"])
        got = incident_inputs.record(incidents.get(self.conn, blind))
        self.assertEqual(got["root_key"], "crash: validator: fatal: exit 1: withheld: an incident"
                         " on a blind run")
        self.assertEqual(sorted(got), sorted(incident_inputs.RECORD_KEYS))
        kept = incident_inputs.record(incidents.get(self.conn, plain))["root_key"]
        self.assertIn("kept text", kept)


if __name__ == "__main__":
    unittest.main()


class DockerArgvText(Texts):
    def test_tick_text_masks_a_docker_commands_mounts_and_leaves_other_commands(self):
        root = self.spool_dir("x").parent
        argv = ["docker", "run", "-v", "/srv/SECRET/clone:/workspace", "img"]
        with mock.patch("sys.stderr", new_callable=io.StringIO) as log:
            got = incident_inputs.tick_text(
                self.conn, subprocess.CalledProcessError(125, argv), root, self.tid)
        self.assertNotIn("SECRET", got)
        self.assertIn("<masked>:/workspace", got)
        self.assertIn("/srv/SECRET/clone:/workspace", log.getvalue())      # the tick log only
        plain = subprocess.CalledProcessError(1, ["git", "-v", "/a:/b"])
        self.assertEqual(incident_inputs.tick_text(self.conn, plain, root, self.tid), str(plain))
        err = incident_inputs.spawn_error(
            self.conn, subprocess.CalledProcessError(125, argv), root, self.tid, "plain9")
        self.assertTrue(err.startswith("CalledProcessError: ") and "SECRET" not in err)

    def test_the_blind_repo_and_spool_root_in_dockers_output_are_masked_not_mount_sources(self):
        root = self.spool_dir("x").parent
        cfg = {"blind_tests": {"repo": "/srv/BLINDREPO/tests"}}
        argv = ["docker", "run", "-v", "/srv/SECRET/clone:/workspace", "img"]
        err = (f"error: cannot read /srv/BLINDREPO/tests/t_1/test_x.py and {root}/run1/blind"
               " near /srv/SECRET/clone")
        with mock.patch("sys.stderr", new_callable=io.StringIO):
            got = incident_inputs.tick_text(
                self.conn, subprocess.CalledProcessError(125, argv, stderr=err), root, self.tid,
                None, cfg)
        for leak in ("BLINDREPO", str(root), "SECRET"):
            self.assertNotIn(leak, got)
        self.assertEqual(got.count("path-"), 2)
        self.assertIn("/t_1/test_x.py", got)
        self.assertIn("exit status 125", got)
