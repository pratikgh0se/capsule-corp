"""ASD-STE100 output style (rows 570, 573; card t_0076770f): the checklist lives once in
`contracts/_style.md`, `contracts.render` appends it for every role, and `style_lint` checks the
mechanical subset of the rules at the tick's one report-ingest point. Two oracles (Z2): hand-written
good and bad documents, and a real past report kept as a fixture."""
import datetime
import hashlib
import io
import json
import re
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from capsule import contracts, intent, paths, preflight, style_lint
from capsule.tests.test_tick import IMPL_DONE, REV_FAIL, RUN_358_ISSUES, TickCase

ROLES = ("implementer", "reviewer", "validator", "guide", "korin", "lookout")
FIXTURE = Path(__file__).parent / "fixtures" / "style"
ENV = {"schema": "review_verdict.v1", "run_id": "0" * 32, "task_id": "t_0000aaaa", "claim_lock": "h:x", "role": "reviewer"}
GOOD_REVIEW = dict(ENV, verdict="fail", findings=[
    {"file": "capsule/tick.py", "line": 12, "class": "least_code", "text": "Delete the dead branch at line 12."}],
    not_reviewed=[], could_delete="The dead branch at capsule/tick.py:12.")
LONG = " ".join(["word"] * 26) + "."


def messages(doc):
    """One message per style problem in `doc`, for every rule."""
    return [msg for _, msg in style_lint.findings(doc)]


def cut_sentences(n):
    """A review whose `could_delete` holds `n` short sentences."""
    return dict(GOOD_REVIEW, could_delete=" ".join(["Cut it."] * n))


def reordered(doc, first):
    """`doc` with key `first` moved after every other key (the verdict buried at the end)."""
    out = {k: v for k, v in doc.items() if k != first}
    out[first] = doc[first]
    return out


class StyleFile(unittest.TestCase):
    def test_the_checklist_has_the_twelve_rules_under_its_own_cap(self):
        text = style_lint.style_text()
        self.assertLessEqual(len(text.encode()), style_lint.MAX_STYLE_BYTES)
        self.assertEqual(len(re.findall(r"^\d+\. ", text, re.M)), 12)
        self.assertEqual(style_lint.lint_file(), [])

    def test_the_file_is_pinned_and_a_change_is_caught(self):
        sums = (paths.REPO / "contracts" / "SHA256SUMS").read_text().split()
        self.assertEqual(sums[1], "_style.md")
        self.assertEqual(sums[0], hashlib.sha256((paths.REPO / "contracts" / "_style.md").read_bytes()).hexdigest())
        d = Path(tempfile.mkdtemp())
        (d / "_style.md").write_text("1. x\n")
        (d / "SHA256SUMS").write_text("0" * 64 + "  _style.md\n")
        self.assertEqual(preflight._sums_ok(d), ["_style.md"])
        self.assertEqual(preflight._sums_ok(paths.REPO / "contracts"), [])

    def test_lint_file_rejects_an_oversize_or_short_checklist(self):
        d = Path(tempfile.mkdtemp())
        (d / "big.md").write_text("1. x\n" * 400)
        (d / "short.md").write_text("1. a\n2. b\n")
        self.assertTrue(any("bytes" in e for e in style_lint.lint_file(d / "big.md")))
        self.assertTrue(any("12" in e for e in style_lint.lint_file(d / "short.md")))

    def test_rule_6_bans_the_progressive_only_and_rule_12_names_the_missing_evidence(self):
        text = style_lint.style_text()
        self.assertIn("no progressive", text)
        self.assertNotIn("no -ing", text)
        self.assertIn("name the missing evidence", text)

    def test_render_appends_the_checklist_once_for_every_role(self):
        text = style_lint.style_text()
        for role in ROLES:
            with self.subTest(role=role):
                out = contracts.render(role)
                self.assertEqual(out.count(text.strip()), 1)
                self.assertTrue(out.endswith(text.strip()))

    def test_the_per_contract_cap_and_contract_lint_are_unchanged(self):
        self.assertEqual(contracts.MAX_BYTES, 2048)
        schemas = {p.stem for p in paths.SCHEMAS.glob("*.json")}
        for role in ROLES:
            self.assertEqual(contracts.lint(contracts.contract_path(role), schemas), [])


class Check(unittest.TestCase):
    def test_good_documents_pass(self):
        report = {"schema": "report.v1", "run_id": "0" * 32, "task_id": "t_0000aaaa", "claim_lock": "h:x",
                  "role": "dev", "status": "done", "implemented": ["Add the lint."], "left_undone": [],
                  "commands": [{"cmd": "python3 -m unittest", "exit": 0}], "issues": [],
                  "procedures_followed": ["tests first"]}
        for doc in (GOOD_REVIEW, report):
            self.assertEqual(messages(doc), [])

    def test_a_long_sentence_is_flagged_with_its_path_and_a_25_word_boundary(self):
        doc = dict(GOOD_REVIEW, could_delete=LONG)
        self.assertEqual(len(messages(doc)), 1)
        self.assertIn("could_delete", messages(doc)[0])
        ok = dict(GOOD_REVIEW, could_delete=" ".join(["word"] * 25) + ".")
        self.assertEqual(messages(ok), [])

    def test_a_numbered_step_has_the_20_word_cap(self):
        step = lambda n: "1. " + " ".join(["word"] * n) + "."
        self.assertEqual(messages(dict(GOOD_REVIEW, could_delete=step(20))), [])
        self.assertEqual(len(messages(dict(GOOD_REVIEW, could_delete=step(21)))), 1)

    def test_more_than_six_sentences_in_a_paragraph_is_flagged(self):
        self.assertEqual(messages(cut_sentences(6)), [])
        self.assertEqual(len(messages(cut_sentences(7))), 1)

    def test_envelope_ids_commands_and_code_spans_are_not_prose(self):
        doc = dict(GOOD_REVIEW, claim_lock="x " * 40, could_delete="Run `" + " ".join(["a"] * 40) + "` now.")
        doc["findings"] = [dict(GOOD_REVIEW["findings"][0], file="a/" * 40)]
        self.assertEqual(messages(doc), [])
        report = {"schema": "report.v1", "status": "done", "commands": [{"cmd": " ".join(["x"] * 40), "exit": 0}]}
        self.assertEqual(messages(report), [])

    def test_the_verdict_must_come_first_after_the_envelope(self):
        problems = messages(reordered(GOOD_REVIEW, "verdict"))
        self.assertEqual(len(problems), 1)
        self.assertIn("verdict", problems[0])
        report = {"schema": "report.v1", "status": "done", "implemented": ["x"]}
        self.assertEqual(messages(report), [])
        self.assertEqual(len(messages(reordered(report, "status"))), 1)

    def test_documents_without_a_verdict_key_need_no_verdict_position(self):
        doc = {"schema": "postmortem.v1", "run_id": "0" * 32, "incident_id": "inc_1", "root_cause": "Cut."}
        self.assertEqual(messages(doc), [])

    def test_a_hedge_in_a_verdict_field_is_flagged_but_not_in_notes(self):
        bad = dict(GOOD_REVIEW, findings=[dict(GOOD_REVIEW["findings"][0], text="This probably leaks a handle.")])
        problems = messages(bad)
        self.assertEqual(len(problems), 1)
        self.assertIn("probably", problems[0])
        notes = dict(GOOD_REVIEW, notes=["This might be fine."])
        self.assertEqual(messages(notes), [])
        pm = {"schema": "postmortem.v1", "root_cause": "The lock should be fine after a restart."}
        self.assertEqual(len(messages(pm)), 1)
        self.assertEqual(messages(dict(pm, root_cause="A lockout cuts the run.")), [])

    def test_a_real_past_report_is_judged_sentence_by_sentence(self):
        """Oracle 2: the round-2 report of t_b982aacb, written before this style existed. A throwaway
        script with a different sentence splitter counted three of its sentences by hand: 44 words
        ("Picked up mid-flight"), 26 ("No guard was weakened") and a 27-word step ("Z7")."""
        text = (FIXTURE / "real-report-lookout-round2.md").read_text()
        problems = style_lint.check_text(text)
        self.assertTrue(any("sentence of 44 words" in p and "Picked up mid-flight" in p for p in problems), problems)
        self.assertTrue(any("sentence of 26 words" in p and "No guard" in p for p in problems), problems)
        self.assertTrue(any("step of 27 words" in p and "Z7" in p for p in problems), problems)
        self.assertFalse(any("status: DONE" in p for p in problems), problems)

    def test_a_non_object_document_has_no_problems(self):
        self.assertEqual(messages(["x"]), [])


BURIED = reordered(GOOD_REVIEW, "verdict")                                  # rule 1: blocking
HEDGED = dict(GOOD_REVIEW, findings=[dict(GOOD_REVIEW["findings"][0], text="This probably leaks a handle.")])  # rule 12
RUN = {"profile": "reviewer", "metadata": "{}"}
RETRY_RUN = {"profile": "reviewer", "metadata": json.dumps({"invalid_document_error": "style_lint (reviewer): x"})}
BEFORE, FROM = datetime.date(2026, 10, 20), datetime.date(2026, 10, 21)


def gate(doc, run=RUN, today=FROM, **kw):
    err = io.StringIO()
    with redirect_stderr(err):
        return style_lint.gate(doc, run, today=today, **kw), err.getvalue()


class Switch(unittest.TestCase):
    def test_the_switch_is_one_dated_constant_two_weeks_out(self):
        self.assertEqual(style_lint.BLOCKING_FROM, FROM)

    def test_before_the_date_every_problem_warns_and_passes(self):
        for doc in (BURIED, HEDGED, dict(GOOD_REVIEW, could_delete=LONG)):
            why, warned = gate(doc, today=BEFORE)
            self.assertIsNone(why)
            self.assertIn("advisory", warned)

    def test_from_the_date_only_rules_1_and_12_refuse(self):
        for doc, word in ((BURIED, "verdict first"), (HEDGED, "probably")):
            why, warned = gate(doc)
            self.assertIn(word, why)
            self.assertEqual(warned, "")
        why, warned = gate(dict(GOOD_REVIEW, could_delete=LONG))        # rule 3 stays advisory (row 575)
        self.assertIsNone(why)
        self.assertIn("sentence of 26 words", warned)

    def test_the_refusal_names_only_the_blocking_problems(self):
        why, _ = gate(dict(HEDGED, could_delete=LONG))
        self.assertIn("probably", why)
        self.assertNotIn("26 words", why)

    def test_a_retry_run_is_accepted_with_a_warning_and_never_refused_twice(self):
        why, warned = gate(BURIED, RETRY_RUN)
        self.assertIsNone(why)
        self.assertIn("style_lint", warned)

    def test_only_review_documents_can_be_refused_implementer_and_korin_only_warn(self):
        pm = {"schema": "postmortem.v1", "root_cause": "The lock should be fine after a restart."}
        impl = {"schema": "report.v1", "status": "done", "implemented": ["x"], "issues": ["This probably leaks."]}
        for doc, role in ((pm, "korin"), (impl, "implementer"), (reordered(impl, "status"), "implementer")):
            self.assertTrue(messages(doc))
            why, warned = gate(doc, {"profile": role, "metadata": "{}"})
            self.assertIsNone(why)
            self.assertIn("style_lint", warned)

    def test_a_retry_is_found_in_the_job_inputs_too(self):
        d = Path(tempfile.mkdtemp())
        (d / "job.json").write_text(json.dumps({"inputs": {"invalid_document_error": "style_lint x"}}))
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertIsNone(style_lint.gate(BURIED, RUN, today=FROM, sp=d))
        self.assertIn("style_lint", err.getvalue())
        self.assertIn("verdict first", style_lint.gate(BURIED, RUN, today=FROM, sp=Path(tempfile.mkdtemp())))

    def test_a_failed_report_is_never_refused_or_warned(self):
        failed = {"schema": "report.v1", "status": "failed", "error": "the lock appears to be held " + LONG,
                  "implemented": []}
        self.assertEqual(gate(failed), (None, ""))

    def test_the_clock_comes_from_the_ticks_now(self):
        stamp = lambda d: int(datetime.datetime(d.year, d.month, d.day, 12).timestamp())
        self.assertIsNone(style_lint.gate(BURIED, RUN, now=stamp(BEFORE)))
        self.assertIn("verdict first", style_lint.gate(BURIED, RUN, now=stamp(FROM)))

    def test_a_clean_document_is_never_refused_or_warned(self):
        for day in (datetime.date(2026, 10, 7), datetime.date(2030, 1, 1)):
            self.assertEqual(gate(GOOD_REVIEW, today=day), (None, ""))

    def test_the_cli_exits_one_only_for_a_blocking_rule_once_blocking(self):
        d = Path(tempfile.mkdtemp())
        files = {}
        for name, doc in (("long", dict(GOOD_REVIEW, could_delete=LONG)), ("buried", BURIED), ("good", GOOD_REVIEW)):
            files[name] = d / f"{name}.json"
            files[name].write_text(json.dumps(doc))
        for day, long_rc, buried_rc in ((BEFORE, 0, 0), (FROM, 0, 1)):
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(style_lint.main([str(files["long"])], today=day), long_rc)
                self.assertEqual(style_lint.main([str(files["buried"])], today=day), buried_rc)
                self.assertEqual(style_lint.main([str(files["good"])], today=day), 0)
            self.assertIn("could_delete", out.getvalue())

    def test_findings_carry_a_rule_name(self):
        rules = {r for r, _ in style_lint.findings(dict(BURIED, could_delete=LONG, findings=HEDGED["findings"]))}
        self.assertEqual(rules, {"verdict-first", "sentence", "hedge"})


class Wiring(unittest.TestCase):
    """`Tick._report` is the one ingest point every role's document passes (validator, reviewer,
    guide, implementer, Korin). The date is passed in, so these tests hold on any day."""

    def report(self, doc, today, run_extra=None):
        from capsule import tick
        from capsule.jsonschema import load_all
        sp = Path(tempfile.mkdtemp())
        (sp / "report").mkdir()
        (sp / "report" / "1.json").write_text(json.dumps(doc))
        run = {"profile": "reviewer", "run_id": ENV["run_id"], "task_id": ENV["task_id"],
               "claim_lock": ENV["claim_lock"], "metadata": "{}", **(run_extra or {})}
        me = types.SimpleNamespace(schemas=load_all(paths.SCHEMAS), _ruling_of=tick.Tick._ruling_of)
        now = int(datetime.datetime(today.year, today.month, today.day, 12).timestamp())
        err = io.StringIO()
        with redirect_stderr(err):
            return tick.Tick._report(me, sp, run, None, now), err.getvalue()

    def test_a_bad_document_is_accepted_with_a_warning_while_advisory(self):
        (doc, why), warned = self.report(BURIED, BEFORE)
        self.assertIsNone(why)
        self.assertEqual(doc["verdict"], "fail")
        self.assertIn("style_lint advisory", warned)

    def test_a_rule_1_document_is_refused_once_blocking(self):
        (doc, why), warned = self.report(BURIED, FROM)
        self.assertIsNone(doc)
        self.assertIn("verdict first", why)

    def test_a_clean_document_passes_in_both_modes(self):
        for day in (BEFORE, FROM):
            (doc, why), warned = self.report(GOOD_REVIEW, day)
            self.assertEqual((why, warned), (None, ""))


class TickPath(TickCase):
    """The real tick, with blocking forced on (the date is patched to the past)."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(style_lint, "BLOCKING_FROM", datetime.date(2000, 1, 1))
        patcher.start()
        self.addCleanup(patcher.stop)

    def kinds(self):
        return [r[0] for r in self.conn.execute("SELECT kind FROM task_events WHERE task_id=?", (self.tid,))]

    def test_the_run_358_report_is_accepted_under_blocking_and_routes_as_before(self):
        """The real run-358 report (a 33-word issue) is a rule 3 problem only: it warns, and the
        card still parks for the genuine out-of-scope issue, never for style."""
        self.script["implementer"] = [[dict(IMPL_DONE, issues=list(RUN_358_ISSUES))]]
        self.tick.once()
        self.tick.once()
        self.assertEqual((self.task()["status"], self.task()["block_kind"]), ("blocked", "needs_input"))
        self.assertNotIn("invalid_document", self.kinds())
        self.assertIn("style_warning", self.kinds())

    def test_a_failed_report_keeps_its_own_error_text(self):
        self.script["implementer"] = [[dict(IMPL_DONE, status="failed", error="the lock appears to be held by another run")]]
        self.tick.once()
        self.tick.once()
        self.assertIn("appears to be held", self.runs("implementer")[0]["error"])
        self.assertNotIn("invalid_document", self.kinds())
        self.assertNotIn("style_warning", self.kinds())

    def test_a_style_refusal_retries_once_then_accepts_and_never_parks(self):
        hedged = dict(REV_FAIL, findings=[dict(REV_FAIL["findings"][0], text="This probably leaks a handle.")],
                      not_reviewed=[])
        self.script["implementer"] = [[IMPL_DONE]]
        self.script["reviewer"] = [[hedged], [hedged]]
        seen = []
        self.hooks["reviewer"] = lambda job, sp, run: seen.append(job["inputs"].get("invalid_document_error"))
        for _ in range(6):
            self.tick.once()
            if len(self.runs("reviewer")) >= 2 and self.runs("reviewer")[1]["ended_at"]:
                break
        first, second = self.runs("reviewer")[:2]
        self.assertEqual((first["error_class"], second["outcome"]), ("invalid_output", "completed"))
        self.assertIn("style_lint", first["error"])
        self.assertIsNone(seen[0])
        self.assertIn("probably", seen[1])                      # the retry bundle names the reason
        self.assertNotEqual(self.task()["block_kind"], "review_infra")
        event = self.conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='style_warning'",
                                  (self.tid,)).fetchone()
        self.assertEqual(json.loads(event[0])["rules"], ["hedge"])


class Intent(unittest.TestCase):
    def test_style_lint_ships_standalone_intent_examples(self):
        self.assertIn("style_lint", intent.STANDALONE)
        self.assertTrue(1 <= len(intent.load("style_lint")) <= 3)
        self.assertEqual(intent.lint(paths.INTENT / "style_lint.jsonl", None, "style_lint"), [])

    def test_every_example_is_judged_by_the_real_lint(self):
        """Each example names a document shape a human judged and the lint's own answer for it."""
        for ex in intent.load("style_lint"):
            with self.subTest(ex["id"]):
                doc = ex["given"]["document"]
                got = {"problems": len(messages(doc["doc"]))}
                self.assertEqual(intent._differs(ex["expect"], got, None), [], ex["id"])
