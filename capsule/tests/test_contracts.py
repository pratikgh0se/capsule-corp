# mutation-check: targets-predate-range b957739c9 -- owner-word rename in asserted prose ::
#   GuideVaultScope.test_guide_contract_does_not_promise_vault_search_or_the_alignment_note
import hashlib
import json
import re
import unittest
from pathlib import Path
from unittest import mock
from capsule import contracts, paths, bundles
from capsule import jsonschema
from capsule.jsonschema import load_all
from capsule.tests.test_contract_field_lint import _dropped

ROLES = ("implementer", "reviewer", "validator", "guide", "korin", "security-reviewer",
         "security-engineer", "lookout", "blind-tester", "staff-architect")


def _word_budget(path):
    """M141: (words, errors, warnings) for one contract file.

    Over WORD_CAP is an error, over WORD_TARGET a warning."""
    n = len(Path(path).read_text().split())
    errors, warns = [], []
    if n > contracts.WORD_CAP:
        errors.append(f"{path}: {n} words, over the {contracts.WORD_CAP} word cap")
    if n > contracts.WORD_TARGET:
        warns.append(f"{path}: {n} words, over the {contracts.WORD_TARGET} word target")
    return n, errors, warns


class Contracts(unittest.TestCase):
    def test_every_role_contract_is_within_the_word_budget(self):
        """M141: each role contract is <= WORD_CAP words (fails); over WORD_TARGET only warns."""
        import warnings
        for role in ROLES:
            with self.subTest(role=role):
                _, errors, warns = _word_budget(paths.REPO / "contracts" / f"{role}.md")
                for w in warns:
                    warnings.warn(w)
                self.assertEqual(errors, [])

    def test_word_budget_helper_fails_a_901_word_contract(self):
        import tempfile
        d = Path(tempfile.mkdtemp())
        p = d / "x.md"
        p.write_text(" ".join(["w"] * 901))
        self.assertEqual(len(_word_budget(p)[1]), 1)
        p.write_text(" ".join(["w"] * 900))
        self.assertEqual(_word_budget(p)[1:], ([], [f"{p}: 900 words, over the 650 word target"]))

    def test_five_fields_non_empty_under_cap_with_line_c(self):
        """Row 537 / t_bdaac20c (fix round 1): `contracts.lint` rejects any line outside what
        `render` emits. `reviewer.md` lines 10-11 (row 543, the owner) are folded into `done-when`;
        every real contract now lints clean with no allowlist. Review note 4 of the lint's round
        2: the parse-based oracle (`_dropped`, independent of `lint`'s own regex) must agree -
        finds nothing on any real contract either."""
        schemas = load_all(paths.SCHEMAS)
        for role in ROLES:
            with self.subTest(role=role):
                path = paths.REPO / "contracts" / f"{role}.md"
                self.assertEqual(contracts.lint(path, schemas), [])
                self.assertEqual(_dropped(path), [])
                self.assertIn(contracts.LINE_C, contracts.render(role))

    def test_every_contract_carries_the_zenkai_line_after_its_own(self):
        """Row 455: an extra may-not line is appended to the rendered may-not, the original kept first."""
        zenkai = "never bypass a Zenkai Rule (docs/ZENKAI.md) without an override that names it"
        for role in ROLES:
            with self.subTest(role=role):
                may_not = contracts.parse(contracts.contract_path(role))["may-not"]
                self.assertTrue(may_not.startswith("C;"))
                self.assertTrue(may_not.endswith(zenkai + "."))
                self.assertIn(zenkai, contracts.render(role))

    def test_lint_catches_missing_field_and_bloat(self):
        import tempfile, pathlib
        p = pathlib.Path(tempfile.mkdtemp()) / "x.md"
        p.write_text("# x\nschema: report.v1\nverbs:\n- gets: a\n- sees: b\n- done-when: c\n- reports: d\n")
        self.assertTrue(any("may-not" in e for e in contracts.lint(p, load_all(paths.SCHEMAS))))
        p.write_text(p.read_text() + "- may-not: C; " + "x" * 3000 + "\n")
        self.assertTrue(any("bytes" in e for e in contracts.lint(p, load_all(paths.SCHEMAS))))

    def test_lint_rejects_no_schema_line_and_unknown_schema(self):
        import tempfile, pathlib
        schemas = load_all(paths.SCHEMAS)
        no_schema = pathlib.Path(tempfile.mkdtemp()) / "x.md"
        no_schema.write_text(
            "# x\nverbs:\n- gets: a\n- sees: b\n- done-when: c\n- reports: d\n- may-not: C; e\n")
        self.assertTrue(any("schema" in e for e in contracts.lint(no_schema, schemas)))

        unknown_schema = pathlib.Path(tempfile.mkdtemp()) / "x.md"
        unknown_schema.write_text(
            "# x\nschema: no_such.v9\nverbs:\n- gets: a\n- sees: b\n- done-when: c\n- reports: d\n- may-not: C; e\n")
        self.assertTrue(any("no_such.v9" in e for e in contracts.lint(unknown_schema, schemas)))

    def test_every_contract_names_an_existing_schema(self):
        names = set(load_all(paths.SCHEMAS))
        self.assertEqual({contracts.schema_for(r) for r in ROLES},
                         {"report.v1", "review_verdict.v1", "validator_result.v1", "alignment_verdict.v1",
                          "postmortem.v1", "lookout_proposal.v1", "blind_tests.v1"})
        self.assertTrue({contracts.schema_for(r) for r in ROLES} <= names)

    def test_blind_contracts_cite_the_named_schema_fields(self):
        """Row 601, Z2: oracle 1 is the contract text, oracle 2 the schema validator. Every field
        name the blind contracts cite must exist in `blind_tests.v1` / `blind_result.v1`, and each
        schema must accept its fixture and refuse an unknown key."""
        schemas = load_all(paths.SCHEMAS)
        tests_schema, result_schema = schemas["blind_tests.v1"], schemas["blind_result.v1"]
        tester = contracts.parse(contracts.contract_path("blind-tester"))
        self.assertEqual(tester["schema"], "blind_tests.v1")
        self.assertIn("`card_hash`", tester["gets"] + tester["done-when"])
        self.assertIn("question", tests_schema["properties"])   # "a question for the guide"
        self.assertIn("question", tester["reports"])
        validator = contracts.parse(contracts.contract_path("validator"))
        item = result_schema["properties"]["tests"]["items"]["properties"]
        for word in ("given", "expect", "observed"):
            self.assertIn(word, validator["gets"])
            self.assertIn(word, item)
        self.assertIn("blind:<id>", validator["done-when"])
        self.assertIn("blind tests", validator["may-not"])
        implementer = contracts.parse(contracts.contract_path("implementer"))
        self.assertIn("blind test repo", implementer["may-not"])
        fixtures = Path(__file__).parent / "fixtures" / "schemas"
        for name, schema in (("blind_tests.v1", tests_schema), ("blind_result.v1", result_schema)):
            fixture = json.loads((fixtures / f"{name}.valid.json").read_text())
            self.assertEqual(jsonschema.validate(fixture, schema), [], name)
            self.assertTrue(jsonschema.validate(dict(fixture, test_code="x"), schema), name)

    def test_blind_tests_document_with_a_hedge_gets_a_hedge_finding(self):
        from capsule import style_lint
        fixtures = Path(__file__).parent / "fixtures" / "schemas"
        doc = json.loads((fixtures / "blind_tests.v1.valid.json").read_text())
        self.assertEqual(style_lint.findings(doc), [])
        doc["not_testable"][0]["reason"] = "the log line probably appears"
        self.assertEqual([r for r, _ in style_lint.findings(doc)], ["hedge"])
        self.assertIn("blind_tests.v1", style_lint.BLOCKING_SCHEMAS)

    def test_validator_bundle_never_contains_the_diff(self):
        task = {"id": "t_0000aaaa", "title": "t", "body": "BODY"}
        b = bundles.validator(task, [{"cmd": "c", "exit": 0, "output": "ok"}])
        self.assertNotIn("diff --git", b["prompt"])
        self.assertIn("BODY", b["prompt"])   # acceptance criteria inlined whole (row 384)

    def test_validator_bundle_judges_from_the_contract_outputs(self):
        """R1 (row 447): code-blind, the validator judges the outputs the tick already ran; never not_run."""
        task = {"id": "t_0000aaaa", "title": "t", "body": "- [ ] `test -f static/complete.html`"}
        b = bundles.validator(task, [{"cmd": "python3 -m unittest", "exit": 1, "output": "FAILED (failures=1)"}])
        section = b["prompt"].split("## Contract outputs", 1)[1]
        for part in ("python3 -m unittest", "exit: 1", "FAILED (failures=1)", "not_run"):
            self.assertIn(part, section)
        self.assertIn("one assertion per command", section)
        self.assertNotIn("## Command outputs", b["prompt"])

    def test_empty_verbs_line_renders_empty(self):
        """R1: `verbs:` with nothing after it must not swallow the next line (the validator's system
        prompt read `verbs: - gets: ...` and its verbs were that line)."""
        self.assertEqual(contracts.verbs_for("validator"), set())
        self.assertIn("\nverbs: \n- gets:", contracts.render("validator"))
        self.assertEqual(contracts.parse(paths.REPO / "contracts" / "validator.md")["gets"][:12], "the contract")

    def test_implementer_bundle_inlines_checkpoint(self):
        task = {"id": "t_0000aaaa", "title": "t", "body": "B" * 20000}
        b = bundles.implementer(task, checkpoint={"next_steps": ["finish idle state"]})
        self.assertIn("B" * 20000, b["prompt"]); self.assertIn("finish idle state", b["prompt"])

    def test_implementer_bundle_carries_the_human_answers(self):
        """R3 (row 445): the card's human entries (kind, text, timestamp) reach the worker."""
        task = {"id": "t_0000aaaa", "title": "t", "body": "BODY"}
        b = bundles.implementer(task, interventions=[{"kind": "accept", "text": "rows stay deferred", "at": 0}])
        section = b["prompt"].split("## Human answers", 1)[1]
        for part in ("accept", "rows stay deferred", "1970-01-01 00:00 UTC"):
            self.assertIn(part, section)
        self.assertNotIn("## Human answers", bundles.implementer(task)["prompt"])
        self.assertNotIn("## Human answers", bundles.implementer(task, interventions=[])["prompt"])

    def test_implementer_bundle_carries_the_worktree_pre_edit_check(self):
        """Row 465 (t_685fd87b, review I3/R1): the implementer's own copy of the pre-edit
        worktree note the harness's preflight also mechanises (`workspace.is_worktree`,
        `worker.run_one`), with a placeholder `worker._fill` fills from the real assigned path.
        Round-3 fix (R1): the note must not ask the model to compare its exec shell's `pwd -P`
        against that path - `mcp__capsule__exec` always runs `docker exec -w /workspace`
        (`exec_mcp.py`), so `pwd -P` prints `/workspace` on every run, never the host path, and a
        literal-minded model would stop on every single run. The note states the path and tells
        the model never to write outside it; it says plainly that its exec shell runs in
        `/workspace`, the same tree mounted, rather than asking for an equality check that can
        never hold. `is_worktree` (mechanised) stays the actual enforcer."""
        task = {"id": "t_0000aaaa", "title": "t", "body": "BODY"}
        prompt = bundles.implementer(task)["prompt"]
        self.assertIn("assigned worktree", prompt)
        self.assertIn("<workspace, given at dispatch>", prompt)
        self.assertIn("/workspace", prompt)
        self.assertNotIn("pwd -P", prompt)
        self.assertNotIn("git rev-parse --show-toplevel", prompt)

    def test_implementer_bundle_carries_the_review_findings(self):
        """R3 (row 447): the findings that sent the card back, verbatim, with their role."""
        task = {"id": "t_0000aaaa", "title": "t", "body": "BODY"}
        finding = "reviewer: static/complete.html:59 [design_fidelity] anchors render in link blue"
        b = bundles.implementer(task, review_findings=[finding])
        self.assertIn(finding, b["prompt"].split("## Review findings to fix", 1)[1])
        self.assertNotIn("## Review findings to fix", bundles.implementer(task)["prompt"])

    def test_implementer_bundle_findings_section_splits_settled_findings_into_notes(self):
        """Card t_8463feac fix round 1 (F2): bundles.py's `_findings_section` (the direct
        cause of both 2026-09-28 blocked runs, task_events 5079/5086 - the review's own
        finding) used to tell a worker every declined finding goes in `issues`, which always
        blocks, with no split for a finding a cited row or ruling already settles or one the
        reviewer marked as not requesting a change. Both real blocked strings were exactly
        that: a finding declined per a cited row, and a reviewer's own no-change observation."""
        task = {"id": "t_0000aaaa", "title": "t", "body": "BODY"}
        finding = "reviewer: static/complete.html:59 [design_fidelity] anchors render in link blue"
        section = bundles.implementer(task, review_findings=[finding])["prompt"].split(
            "## Review findings to fix", 1)[1]
        self.assertIn("a cited row or ruling already settles", section)
        self.assertIn("marked as not requesting a change", section)
        # Round-1 re-check R1: tie each destination to its case, not just "goes in `notes`"/
        # "goes in `issues`" as free-floating substrings - a mutant that swaps the two
        # destinations (settled finding to `issues`, disputed finding to `notes`) must go RED.
        self.assertIn("not requesting a change, goes in `notes`", section)
        self.assertIn("no such citation, goes in `issues`", section)

    def test_implementer_contract_renders_the_notes_classification_guidance(self):
        """Card t_8463feac fix round 1 (F1, blocker): the contract's old `- notes:` line was
        never rendered (`contracts.render` only emits FIELDS - gets/sees/done-when/reports/
        may-not) so no worker ever saw it; the guidance now lives in `reports:`, which does
        render."""
        rendered = contracts.render("implementer")
        self.assertIn("only when it names no in-scope gap", rendered)
        self.assertIn("`notes`", rendered)
        self.assertNotIn("- notes:", Path(contracts.contract_path("implementer")).read_text())

    def test_reviewer_contract_renders_the_notes_and_zero_findings_guidance(self):
        """Row t_7fb6ade3 fix round 1, I2: pins the preventive half of the row-362 gate - without
        this sentence on the reviewer's first attempt, every pass+findings burns a retry instead
        of never happening.

        Card t_3ff01dd1: this test was relocated to `test_review_findings.py` (commit d91a7fa) to
        dodge the mutation gate's old `references_intent_data` false positive - this file's own
        unrelated `ROLES` tuple quoting "reviewer", plus the word "intent" appearing anywhere else
        in the file at all, made the gate misread the whole file as exercising `contracts/intent/
        reviewer.jsonl`, whose revert this test has nothing to do with. `contracts/*.md` now
        enters the revert set on its own (this test's real dependency), and
        `mutation_contractdata.references_data_file` requires a real `contracts.<method>("role")`
        call rather than a bare word-and-quoted-string coincidence - moved back."""
        rendered = contracts.render("reviewer")
        self.assertIn("`notes`", rendered)
        self.assertIn("a pass carries zero findings (row 362)", rendered)
        task = {"id": "t_0000aaaa", "title": "t", "body": "B"}
        self.assertEqual(bundles.reviewer(task, "d")["system"], rendered)

    def test_guide_request_fields_come_from_the_schema(self):
        """R4 (row 447): pilot run 2's guide filed a request.v1 with `to`, `subject`, `body`, `asks`...
        (run 314, rejected: "#: unexpected field 'to'"). Neither the spec (§4, §6.2, Appendix B), the
        contract nor the schema names `to`: the bundle never told the role the request.v1 fields, so
        it invented a shape. The envelope now names exactly the schema's fields and the verbs'."""
        import json
        from capsule.jsonschema import validate
        schemas = load_all(paths.SCHEMAS)
        rejected = json.loads((paths.REPO / "capsule" / "tests" / "fixtures" / "pilot" /
                               "guide_request_run314.json").read_text())
        self.assertIn("unexpected field 'to'", validate(rejected, schemas["request.v1"])[0])
        prompt = bundles.guide({"id": "t_de1466d6", "title": "t"}, "", [])["prompt"]
        line = next(l for l in prompt.splitlines() if l.startswith("request.v1 fields"))
        allowed = set(schemas["request.v1"]["properties"])
        named = set(re.findall(r"`([a-z_-]+)`", line.split(":", 1)[1].split(".", 1)[0]))
        self.assertEqual(named, allowed)
        self.assertIn("comment (`text`)", prompt)
        # the rejected document, cut to the fields the bundle names, validates
        fixed = {k: v for k, v in rejected.items() if k in named}
        fixed["text"] = rejected["subject"] + "\n\n" + rejected["body"]
        self.assertEqual(validate(fixed, schemas["request.v1"]), [])
        # a role with no verbs is told only decision.v1
        self.assertNotIn("request.v1 fields", bundles.validator({"id": "t_0000aaaa", "title": "t"}, [])["prompt"])
        self.assertIn("decision.v1 fields", bundles.validator({"id": "t_0000aaaa", "title": "t"}, [])["prompt"])

    def test_implementer_bundle_says_the_harness_commits(self):
        """Pilot prep: a migrated legacy card says 'Commit ...'; git is not available to the run."""
        b = bundles.implementer({"id": "t_0000aaaa", "title": "t", "body": "- [ ] Commit `web: x`."})
        self.assertIn("do not commit", b["prompt"])
        self.assertIn("kanban_request_review", b["prompt"])
        self.assertNotIn("Harness notes", bundles.reviewer({"id": "t", "title": "t", "body": ""}, "")["prompt"])


class ReportChannel(unittest.TestCase):
    """t_de1466d6 run 339 (event 4685): the envelope told every role to write its report "to the
    spool's report/", but no role can: the worker files the model's structured output there
    (worker.run_one), and report/ is outside every runner's writable paths (runners.host,
    runners.docker: requests/ only). The implementer blocked needs_input on "report/ was not
    writable or even present". Intent example (row 454 style, source recorded run
    4ccd1948d67f): given any role's bundle, expect it to name the structured output as the report
    and never tell the role to write into report/ itself."""

    TASK = {"id": "t_0000aaaa", "title": "t", "body": "B"}

    def bundles(self):
        ruling = {"role": "guide", "precedence": "p", "document": {}, "examples": []}
        return {"implementer": bundles.implementer(self.TASK), "reviewer": bundles.reviewer(self.TASK, ""),
                "validator": bundles.validator(self.TASK, []), "guide": bundles.guide(self.TASK, "S", [1]),
                "korin": bundles.korin(self.TASK, {"id": "inc_0000aaaa"}, [], []),
                "guide_ruling": bundles.guide_ruling(self.TASK, ruling)}

    def test_no_bundle_tells_a_role_to_write_into_report(self):
        for role, b in self.bundles().items():
            with self.subTest(role=role):
                self.assertNotRegex(b["prompt"], r"(?i)(write|to) [^\n]*<spool>/report/|to the spool's report/")

    def test_every_bundle_names_the_structured_output_as_the_report(self):
        for role, b in self.bundles().items():
            with self.subTest(role=role):
                self.assertIn(bundles.REPORT_CHANNEL, b["prompt"])
                self.assertIn("structured output", bundles.REPORT_CHANNEL)
                self.assertIn("report/", bundles.REPORT_CHANNEL)   # it says where the harness files it


class GuideRowTexts(unittest.TestCase):
    """Row 364: the alignment review "reads ... the decision rows it cites". t_de1466d6 run 337's
    bundle carried only "- row 291" .. "- row 452" (numbers, no text) to a role with no Read tool
    (runners.host: guide gets Write only), so it left `rows` empty "rather than naming a row I did
    not read" - and the drifted-without-a-row example fired. The row texts are inlined whole."""

    TASK = {"id": "t_0000aaaa", "title": "t", "body": "B"}

    def test_guide_bundle_inlines_each_cited_row_text(self):
        texts = {"291": "| Screens epic: the three open questions answered | goldens re-rendered |"}
        prompt = bundles.guide(self.TASK, "S", [291, 452], row_texts=texts)["prompt"]
        section = prompt.split("## Decision rows cited", 1)[1].split("\n## ", 1)[0]
        self.assertIn("- row 291: | Screens epic: the three open questions answered | goldens re-rendered |", section)
        self.assertIn("- row 452: " + bundles.ROW_TEXT_MISSING, section)

    def test_guide_ruling_bundle_inlines_the_row_texts_too(self):
        ruling = {"role": "guide", "precedence": "p", "document": {}, "examples": []}
        prompt = bundles.guide_ruling(self.TASK, ruling, "S", [309], row_texts={"309": "| queued |"})["prompt"]
        self.assertIn("- row 309: | queued |", prompt)


class ReviewBundlesNameWhatTheyRead(unittest.TestCase):
    """t_de1466d6 (events 4709, 4717): the guide bundle carried no card and an unnamed plan text.
    Every review bundle inlines the card as it is now under its sha256; the guide's spec section
    names the path and the epic-tip sha it was read at."""

    TASK = {"id": "t_0000aaaa", "title": "t", "body": "the amended card"}

    def test_guide_bundle_inlines_the_live_card_and_names_both_reads(self):
        h = bundles.card_hash("the amended card")
        self.assertEqual(h, hashlib.sha256(b"the amended card").hexdigest())
        prompt = bundles.guide(self.TASK, "S", [452], card_hash=h, spec_sha="f" * 40, spec_path="PLAN.md")["prompt"]
        self.assertIn(f"## Card as it is now (sha256 {h})\nthe amended card", prompt)
        self.assertIn("## Spec section (PLAN.md at " + "f" * 40 + ")\nS", prompt)
        self.assertNotIn("job recorded", prompt)

    def test_reviewer_and_validator_bundles_name_the_card_hash(self):
        h = bundles.card_hash("the amended card")
        for prompt in (bundles.reviewer(self.TASK, "d", card_hash=h)["prompt"],
                       bundles.validator(self.TASK, [], card_hash=h)["prompt"]):
            self.assertIn(f"## Card as it is now (sha256 {h})\nthe amended card", prompt)

    def test_a_recorded_hash_that_no_longer_matches_is_named_and_the_live_card_wins(self):
        prompt = bundles.guide(dict(self.TASK), "S", [452], card_hash="0" * 64)["prompt"]
        self.assertIn("the amended card", prompt)
        self.assertIn("job recorded sha256 " + "0" * 64, prompt)


class GuideDocLengthGuidance(unittest.TestCase):
    """Deploy note (guide-rows review): the guide's first live output after row texts landed blew
    the request.v1 `text` schema cap and burned its retry. The bundle now states both caps it can
    hit, read from the schemas themselves - never a second hardcoded number that could drift from
    a schema edit - and asks for a verdict-first, terse document."""

    TASK = {"id": "t_0000aaaa", "title": "t", "body": "B"}

    def test_the_stated_caps_equal_the_schemas(self):
        import json
        schemas = load_all(paths.SCHEMAS)
        reason_cap = schemas["alignment_verdict.v1"]["properties"]["reason"]["maxLength"]
        text_cap = schemas["request.v1"]["properties"]["text"]["maxLength"]
        prompt = bundles.guide(self.TASK, "S", [417], row_texts={"417": "t"})["prompt"]
        self.assertIn("## Answer length", prompt)
        self.assertIn(f"capped at {reason_cap} chars", prompt)
        self.assertIn(f"capped at {text_cap} chars", prompt)
        self.assertIn("verdict-first", prompt.lower())

    def test_a_schema_cap_change_is_reflected_not_duplicated(self):
        """The bundle reads the cap live from the schema file; it never carries its own number."""
        import json, shutil, tempfile
        tmp = Path(tempfile.mkdtemp())
        for f in paths.SCHEMAS.glob("*.json"):
            shutil.copy(f, tmp / f.name)
        edited = json.loads((tmp / "request.v1.json").read_text())
        edited["properties"]["text"]["maxLength"] = 12345
        (tmp / "request.v1.json").write_text(json.dumps(edited))
        with mock.patch.object(paths, "SCHEMAS", tmp):
            prompt = bundles.guide(self.TASK, "S", [417], row_texts={"417": "t"})["prompt"]
        self.assertIn("capped at 12345 chars", prompt)


class DecisionCapsReachEveryRole(unittest.TestCase):
    """Row 507(b) fix round 1, F4: the evidence run (t_de1466d6 run 378) that burned a full card
    failure over an option `label` past its cap was the IMPLEMENTER - a role that had never been
    told the cap at all (the fix round 0 commit only added a sentence to guide.md and reviewer.md,
    read by nobody: `bundles._request_lines` builds every role's envelope, so stating the caps
    there, read live from decision.v1.json, reaches implementer/reviewer/validator/guide alike and
    can never drift from the schema, unlike a hand-copied contract sentence)."""

    TASK = {"id": "t_0000aaaa", "title": "t", "body": "B"}

    def test_the_stated_caps_equal_the_schema_for_every_role(self):
        schema = load_all(paths.SCHEMAS)["decision.v1"]
        props = schema["properties"]
        question_cap = props["question"]["maxLength"]
        min_opts, max_opts = props["options"]["minItems"], props["options"]["maxItems"]
        label_cap = props["options"]["items"]["properties"]["label"]["maxLength"]
        context_cap = props["context"]["maxLength"]
        expect = (f"question` <={question_cap} chars, {min_opts}-{max_opts} `options` each with a"
                  f" `label` <={label_cap} chars, `context` <={context_cap} chars")
        prompts = {
            "implementer": bundles.implementer(self.TASK)["prompt"],
            "reviewer": bundles.reviewer(self.TASK, "diff")["prompt"],
            "validator": bundles.validator(self.TASK, [])["prompt"],
            "guide": bundles.guide(self.TASK, "S", [417], row_texts={"417": "t"})["prompt"],
        }
        for role, prompt in prompts.items():
            with self.subTest(role=role):
                self.assertIn(expect, prompt)

    def test_a_schema_cap_change_is_reflected_not_duplicated(self):
        import json, shutil, tempfile
        tmp = Path(tempfile.mkdtemp())
        for f in paths.SCHEMAS.glob("*.json"):
            shutil.copy(f, tmp / f.name)
        edited = json.loads((tmp / "decision.v1.json").read_text())
        edited["properties"]["options"]["items"]["properties"]["label"]["maxLength"] = 999
        (tmp / "decision.v1.json").write_text(json.dumps(edited))
        with mock.patch.object(paths, "SCHEMAS", tmp):
            prompt = bundles.implementer(self.TASK)["prompt"]
        self.assertIn("`label` <=999 chars", prompt)


class DecisionRowPrecedenceIsDistinctFromIntentPrecedence(unittest.TestCase):
    """Q1 (review, guide-bundle card): `intent.PRECEDENCE` ("intent rows > spec > contract > script
    > code") is Z3's tie-break for a document an intent example calls wrong - its "intent rows"
    names the intent-EXAMPLES artifact (contracts/intent/*.jsonl), never a vault decision row. Row
    455's ranking (a cited decision row outranks the spec section it contradicts) is a different
    fact about a different kind of "row" and must not be explained by quoting the first constant."""

    TASK = {"id": "t_0000aaaa", "title": "t", "body": "B"}

    def test_the_guide_bundle_cites_the_decision_row_constant_not_intent_precedence(self):
        from capsule import intent
        prompt = bundles.guide(self.TASK, "S", [417], row_texts={"417": "t"})["prompt"]
        self.assertIn("## Verdict (row 455)", prompt)
        self.assertIn(bundles.DECISION_ROW_PRECEDENCE, prompt)
        self.assertNotIn(intent.PRECEDENCE, prompt)
        self.assertNotEqual(bundles.DECISION_ROW_PRECEDENCE, intent.PRECEDENCE)

    def test_the_constant_names_the_row_and_the_spec_not_a_script_or_contract(self):
        # intent.PRECEDENCE's ladder (contract/script/code) has no place in a row-vs-spec fact
        self.assertIn("decision row", bundles.DECISION_ROW_PRECEDENCE)
        self.assertIn("spec", bundles.DECISION_ROW_PRECEDENCE)
        self.assertNotIn("contract", bundles.DECISION_ROW_PRECEDENCE)
        self.assertNotIn("script", bundles.DECISION_ROW_PRECEDENCE)


class VerdictIsAnOrderedProcedure(unittest.TestCase):
    """Round-3 re-review R1/L1: the verdict definitions must read as an explicit ORDERED procedure,
    not two independent yes/no questions that can disagree on the core row-455 case (the work
    follows a cited row the spec disagrees with) - both the bundle (`bundles.VERDICT_NOTE`) and the
    contract (`contracts/guide.md`'s done-when field) state the same four-step order, and step 3
    (L3) tells the guide never to double-list a drifted row in `spec_conflict_rows`."""

    TASK = {"id": "t_0000aaaa", "title": "t", "body": "B"}
    STEP_3 = "the work follows a cited row, and that row's own text is what contradicts the spec section"

    def test_verdict_note_states_an_ordered_procedure(self):
        prompt = bundles.guide(self.TASK, "S", [417], row_texts={"417": "t"})["prompt"]
        self.assertIn("Decide in this order", prompt)
        self.assertIn(self.STEP_3, prompt)
        # a mutant that loosened the ordered procedure to two free-standing questions must fail here
        self.assertNotIn("Decide freely", prompt)

    def test_guide_contract_states_the_same_ordered_procedure(self):
        fields = contracts.parse(contracts.contract_path("guide"))
        done_when = fields["done-when"]
        self.assertIn("decided in this order", done_when)
        self.assertIn("the work follows a cited row and that row's own text is what contradicts"
                      " the spec section", done_when)
        self.assertNotIn("decided freely", done_when)

    def test_step_1_forbids_double_listing_the_drifted_row(self):
        """L3: a row named `drifted` must never also appear in `spec_conflict_rows` - that pairing
        is exactly the self-contradiction `Tick._guide_structural_checks` now catches."""
        prompt = bundles.guide(self.TASK, "S", [417], row_texts={"417": "t"})["prompt"]
        self.assertIn("do not list the row you name as drifted in `spec_conflict_rows`", prompt)
        fields = contracts.parse(contracts.contract_path("guide"))
        self.assertIn("never listing that same row in `spec_conflict_rows`", fields["done-when"])


class KorinContract(unittest.TestCase):
    """Row 456: Korin's contract, its schema and its intent examples are pinned like the others."""

    def test_korin_reports_postmortem_and_may_not_close_or_merge(self):
        self.assertEqual(contracts.schema_for("korin"), "postmortem.v1")
        self.assertEqual(contracts.verbs_for("korin"), set())
        may_not = contracts.parse(contracts.contract_path("korin"))["may-not"]
        for part in ("merge", "close an incident", "contract, SOUL or CLAUDE.md", "write outside"):
            self.assertIn(part, may_not)
        sees = contracts.parse(contracts.contract_path("korin"))["sees"]
        self.assertIn("Knowledge/Incidents/", sees)

    def test_intent_examples_are_pinned(self):
        import hashlib
        intent = paths.REPO / "contracts" / "intent"
        sums = dict(reversed(line.split(maxsplit=1)) for line in (intent / "SHA256SUMS").read_text().splitlines())
        self.assertIn("korin.jsonl", sums)
        self.assertEqual(hashlib.sha256((intent / "korin.jsonl").read_bytes()).hexdigest(), sums["korin.jsonl"])

    def test_korin_prompt_tells_the_fix_card_to_cite_the_governing_spec(self):
        """inc_22b42704: a fix card is checked against the spec before it asks for behaviour."""
        task = {"id": "t_0000aaaa", "title": "Korin post-mortem", "body": "B"}
        prompt = bundles.korin(task, {"id": "inc_0000aaaa"}, [], [])["prompt"]
        flat = " ".join(prompt.split())
        for part in ("read the spec that governs the changed behaviour",
                     "cite its lines on the fix card", "never ask for what the spec forbids",
                     "if the spec must change, ask for a decision"):
            self.assertIn(part, flat)

    def test_human_answers_head_says_a_cos_entry_is_not_owners_ruling(self):
        task = {"id": "t_0000aaaa", "title": "t", "body": "BODY"}
        entries = [{"kind": "answer", "text": "x", "at": 0, "author": "cos"},
                   {"kind": "answer", "text": "y"}]
        prompt = bundles.implementer(task, interventions=entries)["prompt"]
        self.assertIn("## Human answers", prompt)
        self.assertIn("a `cos` entry is the chief of staff's note, not the owner's ruling", prompt)
        self.assertIn("answer by cos: x", prompt)
        self.assertIn("answer by owner: y", prompt)

    def test_korin_bundle_carries_the_incident_evidence_and_logs(self):
        task = {"id": "t_0000aaaa", "title": "Korin post-mortem inc_0000aaaa (crash)", "body": "B"}
        b = bundles.korin(task, {"id": "inc_0000aaaa", "kind": "crash", "repeat_of": None},
                          ["event:7", "/spool/abc"], [{"path": "/spool/abc/worker.log", "tail": "Traceback boom"}])
        for part in ("inc_0000aaaa", "event:7", "/spool/abc", "Traceback boom", "schema: postmortem.v1",
                     "recurrence_test"):
            self.assertIn(part, b["prompt"])
        self.assertIn(contracts.LINE_C, b["system"])


class SecurityReviewerContract(unittest.TestCase):
    """TEAM-09 (spec A.6, rows 362, 366, 381): a read-only security role, review_verdict.v1."""

    def test_reports_review_verdict_with_role_security_and_may_not_edit_or_reach_network(self):
        fields = contracts.parse(contracts.contract_path("security-reviewer"))
        self.assertEqual(fields["schema"], "review_verdict.v1")
        self.assertIn("role: security", fields["reports"])
        for part in ("edit code", "reach the network"):
            self.assertIn(part, fields["may-not"])
        self.assertIn("no checkout, no network", fields["sees"])
        for part in ("network path", "secret path", "dependency path"):
            self.assertIn(part, fields["done-when"])

    def test_system_prompt_renders_with_line_c(self):
        rendered = contracts.render("security-reviewer")
        self.assertTrue(rendered.startswith("# security-reviewer"))
        self.assertIn(contracts.LINE_C, rendered)

    def test_intent_examples_judge_a_security_verdict(self):
        from capsule import intent
        doc = {"run_id": "0" * 32, "task_id": "t_0000aaaa", "claim_lock": "h:x",
               "schema": "review_verdict.v1", "role": "security", "verdict": "pass",
               "findings": [{"file": "a.py", "line": 1, "class": "security", "text": "high: x"}],
               "not_reviewed": [], "notes": ["adds no network path"]}
        found = intent.check("security-reviewer", doc)
        self.assertEqual([c["id"] for c in found], ["pass-with-findings"])
        self.assertEqual(intent.check("security-reviewer", dict(doc, verdict="fail")), [])
        silent = dict(doc, verdict="fail", notes=[])
        found = intent.check("security-reviewer", silent)
        self.assertEqual([c["id"] for c in found], ["security-review-silent-on-paths"])
        wrong = dict(doc, role="reviewer", verdict="pass", findings=[])
        self.assertEqual([c["id"] for c in intent.check("security-reviewer", wrong)],
                         ["security-run-claims-reviewer-role"])


class GuideVaultScope(unittest.TestCase):
    """Row 468: the guide's runner grants Write only, no Read (runners.host.HostRunner.limits) - it
    has no vault path today. The contract must not promise host-side vault search, or keeping the
    row-414 resume note `Projects/<project> - Alignment.md` current; a later card grants a
    read-only vault path."""

    def test_guide_contract_does_not_promise_vault_search_or_the_alignment_note(self):
        fields = contracts.parse(contracts.contract_path("guide"))
        sees, done_when = fields["sees"], fields["done-when"]
        self.assertNotIn("host-side vault search", sees)
        self.assertIn("no vault search", sees)
        self.assertNotIn("Alignment.md", done_when)
        self.assertNotIn("resume note", done_when)
        # the rest of the boundary still holds
        self.assertIn("no code, no diff", sees)
        self.assertIn("first screen goes to the owner before task 2", done_when)


class SimplicityAnswers(unittest.TestCase):
    """Z8 Task 8 (row 559, Q6 row 571): optional `simplicity[5]` in review_verdict.v1."""

    def _errors(self, **extra):
        schema = json.loads((paths.SCHEMAS / "review_verdict.v1.json").read_text())
        doc = {"schema": "review_verdict.v1", "run_id": "0" * 32, "task_id": "t_0000aaaa",
               "claim_lock": "l", "role": "reviewer", "verdict": "pass", "findings": [],
               "not_reviewed": [], **extra}
        return jsonschema.validate(doc, schema)

    def test_four_answers_are_rejected(self):
        self.assertTrue(self._errors(simplicity=["a", "b", "c", "d"]))

    def test_five_answers_pass(self):
        self.assertEqual(self._errors(simplicity=["a", "b", "c", "d", "e"]), [])

    def test_an_empty_answer_is_rejected(self):
        self.assertTrue(self._errors(simplicity=["a", "b", "", "d", "e"]))

    def test_the_key_stays_optional_for_the_security_role(self):
        self.assertEqual(self._errors(), [])

    def test_the_reviewer_contract_lists_the_five_questions(self):
        done = contracts.parse(contracts.contract_path("reviewer"))["done-when"]
        for n in range(1, 6):
            self.assertIn(f"({n})", done)
        self.assertIn("simplicity", contracts.parse(contracts.contract_path("reviewer"))["reports"])

    def test_question_two_names_the_under_two_callers_rule(self):
        done = contracts.parse(contracts.contract_path("reviewer"))["done-when"]
        self.assertIn("under two callers", done)
        self.assertIn("least_code", done.split("(3)")[0])
