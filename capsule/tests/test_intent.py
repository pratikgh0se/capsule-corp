# mutation-check: targets-predate-range b957739c9 -- owner-word rename in a word list ::
#   Checker.test_reviewer_contract_names_the_sweep_and_the_safety_carve_out
"""Intent examples beside every contract (row 454 items 1-3, t_450535e2).

`contracts/intent/<role>.jsonl`: one example per line, {id, given, expect, source}. The checker
is the tick's second oracle after schema validation: an example whose `given` matches a
schema-valid document but whose `expect` does not is a contradiction.
"""
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capsule import contracts, intent, paths, preflight, workspace
from capsule.tests.public_tree import PRIVATE_TREE, private_only

ROLES = ("implementer", "reviewer", "validator", "guide", "korin",   # korin.jsonl: row 456
         "security-reviewer",   # TEAM-09
         "advisor",   # advisor: row 394
         "auditor",   # auditor.jsonl: EVAL-06
         "lookout",   # lookout.jsonl: TEAM-14
         "security-engineer",   # security-engineer.jsonl: SEC-10
         "staff-architect")   # staff-architect.jsonl: TEAM-23
ENV = {"run_id": "0" * 32, "task_id": "t_0000aaaa", "claim_lock": "h:x"}


def write(lines):
    p = Path(tempfile.mkdtemp()) / "implementer.jsonl"
    p.write_text("".join((l if isinstance(l, str) else json.dumps(l)) + "\n" for l in lines))
    return p


GOOD = {"id": "done-with-left-undone", "given": {"document": {"schema": "report.v1", "status": "done",
        "left_undone": "<non-empty>"}}, "expect": {"$outcome": "hold"}, "source": "row 445"}


def _role_ids(text):
    return {json.loads(line)["id"] for line in text.splitlines() if line.strip()}


def _show(ref, rel):
    r = subprocess.run(["git", "show", f"{ref}:{rel}"], cwd=paths.REPO, capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


def _merge_base(ref):
    r = subprocess.run(["git", "merge-base", "HEAD", ref], cwd=paths.REPO, capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def _role_diff_base():
    """(merge-base sha or None, whether the repo has an `origin` remote).

    With an origin: only `origin/main`, never local `main`. t_0f123965 (round-3 fix R5): a
    factory clone's local `main` goes stale while `origin/main` moves, which made already-landed
    rows count as new. t_30645655: a validator run counted rows against an old base, so a
    silent fallback to a possibly stale local `main` is gone and the cap test fails, naming
    origin/main (the cause was not reproduced; a stale origin/main is not fixed here, the
    failure message carries the base's date to tell the two apart). With no origin (an archive
    copy or a `git init` tree) local `main` is the base, and only when it is missing too is
    this not a git checkout, where skipping is correct. (After merge to main this is vacuous
    by design: a branch-time gate.)"""
    r = subprocess.run(["git", "remote", "get-url", "origin"], cwd=paths.REPO,
                       capture_output=True, text=True)
    has_origin = r.returncode == 0
    return _merge_base("origin/main" if has_origin else "main"), has_origin


def _fake_git(origin, refs):
    """A `subprocess.run` stand-in: `git remote get-url origin` succeeds iff `origin`, and
    `git merge-base HEAD <ref>` returns refs[ref] or fails."""
    def fake_run(args, **kwargs):
        if args[1] == "remote":
            return subprocess.CompletedProcess(args, 0 if origin else 2, "", "")
        if args[-1] in refs:
            return subprocess.CompletedProcess(args, 0, refs[args[-1]] + "\n", "")
        return subprocess.CompletedProcess(args, 1, "", "fatal: no such ref\n")

    return fake_run


class RoleDiffBase(unittest.TestCase):
    """Round-3 fix, R5 + t_0f123965, t_30645655: `_role_diff_base` (behind the I1 cap test) uses
    only `origin/main` when the repo has an origin (a stale local `main` must never stand in for
    it) and local `main` when it has none (an archive copy or a `git init` tree)."""

    CAP_TEST = "test_at_most_three_new_examples_per_round_since_the_merge_base"

    def _base(self, origin, refs):
        with mock.patch.object(subprocess, "run", side_effect=_fake_git(origin, refs)):
            return _role_diff_base()

    def test_origin_present_and_origin_main_resolves_uses_it_over_local_main(self):
        self.assertEqual(self._base(True, {"origin/main": "cafef00d", "main": "deadbeef"}),
                         ("cafef00d", True))

    def test_origin_present_and_origin_main_missing_does_not_fall_back_to_local_main(self):
        self.assertEqual(self._base(True, {"main": "deadbeef"}), (None, True))

    def test_no_origin_uses_local_main(self):
        self.assertEqual(self._base(False, {"origin/main": "cafef00d", "main": "deadbeef"}),
                         ("deadbeef", False))

    def test_no_origin_and_no_local_main_skips_the_cap_test(self):
        got = self._base(False, {})
        self.assertEqual(got, (None, False))
        # this module, however it was loaded (`-t .` or a bare discover: `test_intent`)
        with mock.patch.object(sys.modules[__name__], "_role_diff_base", return_value=got):
            result = unittest.TestResult()
            Lint(self.CAP_TEST).run(result)
        self.assertEqual((len(result.skipped), result.failures, result.errors), (1, [], []))


class Lint(unittest.TestCase):
    def test_every_shipped_role_file_lints_clean_and_is_non_empty(self):
        for role in ROLES:
            with self.subTest(role=role):
                path = paths.INTENT / f"{role}.jsonl"
                self.assertEqual(intent.lint(path, contracts.schema_for(role)), [])
                self.assertGreaterEqual(len(intent.load(role)), 1)

    def test_at_most_three_new_examples_per_round_since_the_merge_base(self):
        """Row 454 (review I1); row 483 fix round 1, I2: the "1-3" cap bounds how many examples
        one round adds to a role file OR a `STANDALONE` script's file (row 472(3): per addition
        round, not a file-lifetime total) - checked against the ids that were already there at
        the branch's merge-base with `main`, not a fixed file-wide bound that a later, legitimate
        round would only have to raise again. `STANDALONE` used to get a separate, stricter,
        file-lifetime cap of 3 below (row 483 review I2: that contradicted row 472(3) - the fix
        is to fold it into this same per-round rule, not carve it out)."""
        base_sha, has_origin = _role_diff_base()
        if not base_sha and has_origin:
            self.fail("origin/main did not resolve although the repo has an origin remote: "
                      "no fallback to local `main` (t_30645655, a stale base miscounts rows)")
        if not base_sha:
            self.skipTest("not resolvable here (no origin, no local main / not a git checkout)")
        day = subprocess.run(["git", "show", "-s", "--format=%cs", base_sha], cwd=paths.REPO,
                             capture_output=True, text=True).stdout.strip()
        for name in ROLES + intent.STANDALONE:
            with self.subTest(name=name):
                if not PRIVATE_TREE and not (paths.INTENT / f"{name}.jsonl").exists():
                    self.skipTest("private file not in public release")
                rel = f"contracts/intent/{name}.jsonl"
                before = _role_ids(_show(base_sha, rel))
                after = _role_ids((paths.INTENT / f"{name}.jsonl").read_text())
                new = after - before
                self.assertLessEqual(len(new), 3,
                                     f"{name}: {sorted(new)} vs base {base_sha[:12]} ({day})")

    def test_lint_all_covers_the_directory(self):
        self.assertEqual(intent.lint_all(), [])

    def test_every_shipped_standalone_file_lints_clean_and_has_at_least_one_example(self):
        """Row 470: `unblocked` ships intent examples (Z1) without a tick-role contract (there is
        no run-envelope document for a read-only detector's own oracle to judge). Row 483 review
        I2: the per-round new-ids cap above already bounds growth (row 472(3)) - `STANDALONE`
        gets no separate file-lifetime upper bound either, matching every role file."""
        for name in intent.STANDALONE:
            with self.subTest(name=name):
                path = paths.INTENT / f"{name}.jsonl"
                if not PRIVATE_TREE and not path.exists():
                    self.skipTest("private file not in public release")
                self.assertEqual(intent.lint(path, None, name), [])
                self.assertGreaterEqual(len(intent.load(name)), 1)

    def test_lint_rejects_bad_lines(self):
        cases = {
            "not json": ["{nope"],
            "missing key": [{k: v for k, v in GOOD.items() if k != "source"}],
            "extra key": [dict(GOOD, why="x")],
            "bad source": [dict(GOOD, source="a hunch")],
            "bad id": [dict(GOOD, id="Has Spaces")],
            "id with a trailing newline": [dict(GOOD, id="done-with-left-undone\n")],   # review I7: fullmatch, not match
            "duplicate id": [GOOD, GOOD],
            "empty expect": [dict(GOOD, expect={})],
            "given without document": [dict(GOOD, given={"inputs": {}})],
            "unknown given key": [dict(GOOD, given={"document": {}, "run": {}})],
            "unknown operator": [dict(GOOD, expect={"status": "<nonempty>"})],
            "unknown dict operator": [dict(GOOD, expect={"status": {"<none>": "done"}})],
            "wrong schema": [dict(GOOD, given={"document": {"schema": "review_verdict.v1"}})],
            "unknown outcome": [dict(GOOD, expect={"$outcome": "shipped"})],
            "outcome in given": [dict(GOOD, given={"document": {"$outcome": "hold"}})],
            # I2 (review probe): a given that matches every document, and misspelled fields
            "empty given.document": [dict(GOOD, given={"document": {}}, expect={"status": "failed"})],
            "schema-only given.document": [dict(GOOD, given={"document": {"schema": "report.v1"}})],
            "vacuous given": [dict(GOOD, given={"document": {"left_undone": "<empty>"}})],
            "misspelled given field": [dict(GOOD, given={"document": {"statsu": "done"}})],
            "misspelled expect field": [dict(GOOD, expect={"isues": "<non-empty>"})],
            "misspelled nested field": [dict(GOOD, given={"document": {"status": "done",
                                                                       "commands": {"<some>": {"exitt": 1}}}})],
            "unknown inputs key": [dict(GOOD, given={"document": {"status": "done"}, "inputs": {"diff": "x"}})],
            "operator inside a list": [dict(GOOD, expect={"issues": ["<any>"]})],
        }
        for name, lines in cases.items():
            with self.subTest(name):
                self.assertNotEqual(intent.lint(write(lines), "report.v1"), [], name)
        self.assertEqual(intent.lint(write([GOOD]), "report.v1"), [])
        nested = dict(GOOD, given={"document": {"status": "done", "commands": {"<some>": {"exit": {"<not>": 0}}}},
                                   "inputs": {"guide_rulings": "<empty>"}})
        self.assertEqual(intent.lint(write([nested]), "report.v1"), [])
        for src in ("row 454", "recorded run 0f3a9c", "owner", "row 452, 2026-09-27"):
            self.assertEqual(intent.lint(write([dict(GOOD, source=src)]), "report.v1"), [], src)
        for bad in ("row 452, 2026/09/27", "row 452 2026-09-27", "row 452,2026-09-27", "row 452, 26-09-27",
                    "row 452, 2026-09-27\n", "row 452, 0000-99-99"):
            self.assertNotEqual(intent.lint(write([dict(GOOD, source=bad)]), "report.v1"), [], bad)

    def test_a_role_file_without_a_contract_fails_lint_all(self):
        d = Path(tempfile.mkdtemp())
        (d / "stranger.jsonl").write_text(json.dumps(GOOD) + "\n")
        self.assertTrue(any("stranger" in e for e in intent.lint_all(d)))


class Sums(unittest.TestCase):
    def test_pinned_hashes_cover_every_intent_file(self):
        """Same mechanism as the schemas: a change is a new SHA256SUMS line under the row 393 gate."""
        sums = {}
        for line in (paths.INTENT / "SHA256SUMS").read_text().splitlines():
            digest, name = line.split(maxsplit=1)
            sums[name] = digest
        self.assertEqual(set(sums), {p.name for p in paths.INTENT.glob("*.jsonl")})
        for name, digest in sums.items():
            self.assertEqual(hashlib.sha256((paths.INTENT / name).read_bytes()).hexdigest(), digest, name)

    def test_preflight_checks_intent_sums(self):
        self.assertEqual(preflight._sums_ok(paths.INTENT), [])
        d = Path(tempfile.mkdtemp())
        (d / "guide.jsonl").write_text("x\n")
        (d / "SHA256SUMS").write_text(hashlib.sha256(b"y\n").hexdigest() + "  guide.jsonl\n")
        self.assertEqual(preflight._sums_ok(d), ["guide.jsonl"])
        (d / "reviewer.jsonl").write_text("{}\n")                    # on disk, never pinned
        (d / "SHA256SUMS").write_text((d / "SHA256SUMS").read_text() + "0" * 64 + "  gone.jsonl\n")
        self.assertEqual(preflight._sums_ok(d, "*.jsonl"), ["guide.jsonl", "gone.jsonl (missing)",
                                                            "reviewer.jsonl (not pinned)"])


# the tick puts the live card's hash in every review job (t_de1466d6); real guide inputs carry it
HASH = "c" * 64


class Checker(unittest.TestCase):
    def test_validator_lists_not_run_while_outputs_exist(self):
        """Reachable past R1: R1 rejects a not_run *assertion*; this is the `not_run` list itself."""
        doc = dict(ENV, schema="validator_result.v1", verdict="pass", not_run=["the golden rows"],
                   assertions=[{"cmd": "make test", "exit": 0, "result": "pass"}])
        found = intent.check("validator", doc, inputs={"outputs": [{"cmd": "make test", "exit": 0, "output": "ok"}]})
        self.assertEqual([c["id"] for c in found], ["not-run-listed-while-outputs-exist"])
        self.assertIn("not_run", found[0]["differs"][0])
        self.assertEqual(found[0]["source"], "row 447")
        # no outputs in the bundle: the example does not match, no contradiction
        self.assertEqual(intent.check("validator", doc, inputs={"outputs": []}), [])

    def test_validator_not_run_assertion_while_outputs_exist(self):
        """t_99136481: the same row-447 rule on the OTHER surface - an assertion's own `result`
        can be `not_run` (the schema allows it) instead of the top-level `not_run` list."""
        doc = dict(ENV, schema="validator_result.v1", verdict="fail", not_run=[],
                   assertions=[{"cmd": "make test", "exit": 0, "result": "pass"},
                               {"cmd": "test -f new.py", "exit": 1, "result": "not_run"}])
        found = intent.check("validator", doc, inputs={"outputs": [{"cmd": "make test", "exit": 0, "output": "ok"}]})
        self.assertEqual([c["id"] for c in found], ["not-run-assertion-while-outputs-exist"])
        self.assertEqual(intent.check("validator", doc, inputs={"outputs": []}), [])

    def test_reviewer_verdict_without_simplicity_answers(self):
        """Z8 Task 8 (row 559, 571): the schema key is optional, so this example is the enforcer."""
        doc = dict(ENV, schema="review_verdict.v1", role="reviewer", verdict="pass", findings=[])
        found = intent.check("reviewer", doc)
        self.assertEqual([c["id"] for c in found], ["review-without-simplicity-answers"])
        self.assertEqual(intent.check("reviewer", dict(doc, simplicity=list("abcde"))), [])

    def test_review_must_flag_the_single_caller_layer_and_the_unrequested_option(self):
        """BLD-04 (Z8 item 2): a clean pass over a diff that adds a one-caller wrapper, or options
        nobody asked for, contradicts the reviewer's simplicity checklist."""
        by_id = {e["id"]: e for e in intent.load("reviewer")}
        for ex_id, klass in (("review-passes-single-caller-wrapper", "least_code"),
                             ("review-passes-unrequested-options", "more_than_needed")):
            with self.subTest(ex_id):
                diff = by_id[ex_id]["given"]["inputs"]["diff"]
                doc = dict(ENV, schema="review_verdict.v1", role="reviewer", verdict="pass",
                           findings=[], simplicity=list("abcde"))
                inputs = {"diff": diff}
                found = intent.check("reviewer", doc, inputs=inputs)
                self.assertEqual([c["id"] for c in found], [ex_id])
                finding = {"file": "capsule/x.py", "line": 1, "class": klass, "text": "t"}
                flagged = dict(doc, verdict="fail", findings=[finding])
                self.assertEqual(intent.check("reviewer", flagged, inputs=inputs), [])
                self.assertEqual(intent.check("reviewer", doc, inputs={"diff": "other"}), [])

    def test_reviewer_run_must_not_claim_the_security_role(self):
        """Row 602 hunt: the simplicity example keys on role reviewer, so a reviewer-profile
        verdict claiming role security would dodge it; role must equal the profile."""
        doc = dict(ENV, schema="review_verdict.v1", role="security", verdict="pass", findings=[])
        found = intent.check("reviewer", doc)
        self.assertIn("reviewer-run-claims-security-role", [c["id"] for c in found])
        ok = dict(doc, role="reviewer", simplicity=list("abcde"))
        self.assertEqual(intent.check("reviewer", ok), [])

    def test_the_security_role_reads_its_own_example_file(self):
        doc = dict(ENV, schema="review_verdict.v1", role="security", verdict="pass",
                   findings=[{"file": "a.py", "line": 1, "class": "security", "text": "t"}],
                   notes="paths: none")
        found = intent.check("security", doc)
        self.assertIn("pass-with-findings", [c["id"] for c in found])
        claim = dict(doc, role="reviewer", findings=[])
        found = intent.check("security", claim)
        self.assertIn("security-run-claims-reviewer-role", [c["id"] for c in found])

    def test_validator_pass_with_a_failing_assertion(self):
        doc = dict(ENV, schema="validator_result.v1", verdict="pass", not_run=[],
                   assertions=[{"cmd": "a", "exit": 0, "result": "pass"}, {"cmd": "b", "exit": 1, "result": "fail"}])
        self.assertEqual([c["id"] for c in intent.check("validator", doc, inputs={"outputs": [1]})],
                         ["pass-with-a-failing-assertion"])
        doc["verdict"] = "fail"
        self.assertEqual(intent.check("validator", doc, inputs={"outputs": [1]}), [])

    def test_guide_drifted_without_a_row_on_a_cited_card(self):
        doc = dict(ENV, schema="alignment_verdict.v1", verdict="drifted", rows=[], reason="changes an interface")
        cited = {"card_hash": HASH, "spec_section": "", "rows": [417], "uncited": False}
        found = intent.check("guide", doc, inputs=cited)
        self.assertEqual([c["id"] for c in found], ["drifted-without-a-row"])
        self.assertEqual(found[0]["differs"], ["rows: expected <non-empty>, got []"])
        self.assertEqual(intent.check("guide", dict(doc, rows=[364]), inputs=cited), [])
        # an uncited packet (the card cites nothing at all) is the row 364 fallback's case
        # (aligned_uncited, tick.py's own reclassification), not this example's: a guide may say
        # "drifted" here and the run still passes (test_tick.GuideCitationFallback)
        self.assertEqual(intent.check("guide", doc, inputs={"card_hash": HASH, "spec_section": "", "rows": [], "uncited": True}), [])

    def test_guide_judges_the_card_as_it_is_now_cited_by_hash(self):
        """t_de1466d6 (events 4709, 4717): two drifted verdicts judged a card text the guide was
        never handed (its bundle carried no card and a stale plan). A drifted verdict from a packet
        that names no card hash was not a judgement of the card as it is now."""
        doc = dict(ENV, schema="alignment_verdict.v1", verdict="drifted", rows=[452], reason="pre-amendment text")
        blind = {"spec_section": "S", "rows": [452], "uncited": False}
        found = intent.check("guide", doc, inputs=blind)
        self.assertEqual([c["id"] for c in found], ["drifted-on-a-card-it-was-not-handed"])
        self.assertEqual(found[0]["source"], "row 452")
        # the same verdict from a packet that carried the live card, by hash, stands
        seen = dict(blind, card_hash="a" * 64, spec_sha="b" * 40)
        self.assertEqual(intent.check("guide", doc, inputs=seen), [])
        # an aligned verdict is not this example's business
        self.assertEqual(intent.check("guide", dict(doc, verdict="aligned"), inputs=blind), [])
        # the example names a job input the tick really records, so it lints clean against the guide's inputs
        self.assertEqual(intent.lint(intent.role_path("guide"), "alignment_verdict.v1"), [])
        self.assertLessEqual({"card_hash", "spec_sha", "spec_path"}, intent.INPUT_KEYS["guide"])
        self.assertIn("card_hash", intent.INPUT_KEYS["reviewer"] & intent.INPUT_KEYS["validator"])

    def test_guide_may_not_cite_a_row_outside_its_bundle(self):
        """Row 468: the guide judges only from its bundle (spec section, cited row texts, the card
        document, the implementer report) - it has no vault path and no Read tool (runners.host),
        so a row number in a drifted verdict that its own `inputs.rows` never carried can only have
        come from outside the bundle - the mirror of drifted-without-a-row (row 364), which catches
        the opposite gap (a drifted verdict naming no row when rows were in the bundle)."""
        doc = dict(ENV, schema="alignment_verdict.v1", verdict="drifted", rows=[417], reason="looks unfinished")
        uncited = {"card_hash": HASH, "spec_section": "", "rows": [], "uncited": True}
        found = intent.check("guide", doc, inputs=uncited)
        self.assertEqual([c["id"] for c in found], ["rows-cited-beyond-the-bundle"])
        self.assertEqual(found[0]["differs"], ["rows: expected <empty>, got [417]"])
        self.assertEqual(found[0]["source"], "row 468")
        # the same document, with the row actually in the bundle, is no contradiction
        self.assertEqual(intent.check("guide", doc, inputs={"card_hash": HASH, "spec_section": "", "rows": [417], "uncited": False}), [])
        # an aligned verdict reusing a fixture's stale row number is outside this example's scope
        # (drifted only, like row 364's own example) - it is not what this row 468 example catches
        self.assertEqual(intent.check("guide", dict(doc, verdict="aligned"), inputs=uncited), [])

    def test_guide_spec_conflict_rows_subset_example_row_472(self):
        """Row 472 (design ruling on row 455): `spec_conflict_rows` is additive, never a third
        verdict value - `verdict` stays `aligned`/`drifted`. One intent example pins one
        illustrative case of the subset rule at the intent layer (Zenkai Z1) - a conflict row named
        outside the ones the guide actually cited; the general rule (checked against the rows the
        guide was actually HANDED, `inputs.rows`, not just the document's own `rows`) is a structural
        check in tick.py, `Tick._guide_structural_checks` (round-3 re-review R4/R5: renamed from
        `_guide_subset_violation`/`spec-conflict-rows-subset` so its own finding id, now
        `spec-conflict-rows-not-cited`, never collides with this jsonl example's id in one
        `contradiction` event). (I1, review round: this test's real dependency is a data file,
        contracts/intent/guide.jsonl - tests/mutation_check.py's revert-mutation step now reverts
        that file too, `is_intent_data_file`/`references_intent_data`, so this discriminates.)"""
        cited = {"card_hash": HASH, "spec_section": "S", "rows": [417], "uncited": False}
        doc = dict(ENV, schema="alignment_verdict.v1", verdict="drifted", rows=[417],
                   spec_conflict_rows=[999], reason="row 417 stands, but a stray row is named")
        found = intent.check("guide", doc, inputs=cited)
        self.assertEqual([c["id"] for c in found], ["spec-conflict-rows-subset"])
        # the same row, now actually cited: no longer a subset violation
        self.assertEqual(intent.check("guide", dict(doc, spec_conflict_rows=[417]), inputs=cited), [])

    def test_guide_aligned_without_a_row_on_a_cited_card(self):
        """Row 454: the "aligned" mirror of `drifted-without-a-row` - a card that does cite a row
        gets a guide verdict of `aligned` with an empty `rows` field, dropping the very citation
        it was given (not the uncited case, row 364's own fallback, which this must not catch)."""
        cited = {"spec_section": "", "rows": [417], "uncited": False}
        doc = dict(ENV, schema="alignment_verdict.v1", verdict="aligned", rows=[], reason="matches the spec")
        found = intent.check("guide", doc, inputs=cited)
        self.assertEqual([c["id"] for c in found], ["aligned-without-a-row"])
        self.assertEqual(found[0]["differs"], ["rows: expected <non-empty>, got []"])
        self.assertEqual(intent.check("guide", dict(doc, rows=[417]), inputs=cited), [])
        # uncited (no citation given at all): this example does not apply (row 364's fallback)
        self.assertEqual(intent.check("guide", doc, inputs={"spec_section": "", "rows": [], "uncited": True}), [])

    def test_implementer_done_with_left_undone_is_a_hold_not_done(self):
        """The example judges the tick's routing (`$outcome`), not only the document's fields."""
        doc = dict(ENV, schema="report.v1", role="implementer", status="done", implemented=["x"],
                   left_undone=["golden rows stay deferred"], commands=[], issues=[], procedures_followed=[])
        self.assertEqual(intent.check("implementer", doc, outcome="hold"), [])
        found = intent.check("implementer", doc, outcome="review")
        self.assertEqual([c["id"] for c in found], ["done-with-left-undone"])
        self.assertEqual(found[0]["differs"], ["$outcome: expected 'hold', got 'review'"])
        self.assertEqual(intent.check("implementer", dict(doc, left_undone=[]), outcome="review"), [])
        self.assertEqual(intent.check("implementer", dict(doc, status="checkpoint"), outcome="checkpoint"), [])
        # no outcome given (a caller judging the document alone): `$outcome` is not judged
        self.assertEqual(intent.check("implementer", doc), [])

    def test_reviewer_pass_with_findings(self):
        doc = dict(ENV, schema="review_verdict.v1", role="reviewer", verdict="pass", not_reviewed=[],
                   simplicity=list("abcde"),
                   findings=[{"file": "a.py", "line": 1, "class": "slop", "text": "t"}])
        self.assertEqual([c["id"] for c in intent.check("reviewer", doc)], ["pass-with-findings"])

    def test_reviewer_pass_with_notes_still_needs_empty_findings(self):
        """row 362: `notes` does not excuse a real, still-open finding left in `findings`."""
        doc = dict(ENV, schema="review_verdict.v1", role="reviewer", verdict="pass", not_reviewed=[],
                   notes=["a.py:1 already fixed in this diff"], simplicity=list("abcde"),
                   findings=[{"file": "a.py", "line": 1, "class": "slop", "text": "t"}])
        found = intent.check("reviewer", doc)
        self.assertIn("pass-with-notes-still-needs-empty-findings", [c["id"] for c in found])
        self.assertEqual(intent.check("reviewer", dict(doc, findings=[])), [])

    def test_reviewer_fail_with_a_new_finding_at_round_4(self):
        """Row 540 (outranks row 511's round 3, Z3): from round 4 on the verdict is pass or a
        decision.v1, no new findings unless a regression - the settings-page card (row 251) ran
        five rounds of one nit each."""
        doc = dict(ENV, schema="review_verdict.v1", role="reviewer", verdict="fail", not_reviewed=[],
                   simplicity=list("abcde"),
                   findings=[{"file": "a.py", "line": 1, "class": "slop", "text": "t"}])
        found = intent.check("reviewer", doc, inputs={"diff": "x", "card_hash": HASH, "round": 4})
        self.assertEqual([c["id"] for c in found], ["fail-with-new-finding-at-round-4"])
        # round 3 is an earlier round under row 540 - not this example's business
        self.assertEqual(intent.check("reviewer", doc, inputs={"diff": "x", "card_hash": HASH, "round": 3}), [])
        # the rule keeps applying past round 4 too
        found5 = intent.check("reviewer", doc, inputs={"diff": "x", "card_hash": HASH, "round": 5})
        self.assertEqual([c["id"] for c in found5], ["fail-with-new-finding-past-round-4"])
        # a pass at round 4 is not a contradiction
        self.assertEqual(intent.check("reviewer", dict(doc, verdict="pass", findings=[]),
                                      inputs={"diff": "x", "card_hash": HASH, "round": 4}), [])

    def test_reviewer_late_safety_finding_fails_but_ordinary_one_passes(self):
        """W199 (the owner 2026-10-21): row 540's round-4 pass rule skips a finding that breaks a
        safety invariant (`safety: true`); that fail stays a fail, so a `pass` is the
        contradiction."""
        def doc(verdict, *safety):
            return dict(ENV, schema="review_verdict.v1", role="reviewer", verdict=verdict,
                        not_reviewed=[], simplicity=list("abcde"),
                        findings=[{"file": "a.py", "line": 1, "class": "slop", "text": "t", **s}
                                  for s in safety])

        def ids(d, rnd):
            return [c["id"] for c in intent.check(
                "reviewer", d, inputs={"diff": "x", "card_hash": HASH, "round": rnd})]
        self.assertEqual(ids(doc("fail", {"safety": True}), 4), [])
        self.assertEqual(ids(doc("fail", {"safety": True}), 5), [])
        self.assertEqual(ids(doc("fail", {"safety": True}, {}), 4), [])   # mixed: still a fail
        self.assertEqual(ids(doc("fail", {"safety": False}), 4),
                         ["fail-with-new-finding-at-round-4"])
        self.assertEqual(ids(doc("fail", {}), 5), ["fail-with-new-finding-past-round-4"])
        passing = dict(doc("pass", {"safety": True}), verdict="pass")
        self.assertIn("late-safety-finding-fails-at-round-4", ids(passing, 4))
        self.assertIn("late-safety-finding-fails-past-round-4", ids(passing, 5))

    def test_reviewer_contract_names_the_sweep_and_the_safety_carve_out(self):
        text = (paths.REPO / "contracts" / "reviewer.md").read_text()
        self.assertIn("list every sibling instance in the SAME round", text)
        self.assertIn("every finding seen in round 1", text)
        self.assertIn("a finding with `safety: true`", text)
        self.assertIn("which still fails at any round", text)
        for word in ("incident hold", "protected path", "owner-only action", "data loss",
                     "secret"):
            self.assertIn(word, text)

    def test_a_role_without_examples_has_no_contradictions(self):
        self.assertEqual(intent.check("security", {"verdict": "pass"}), [])

    def test_operators(self):
        m = intent.matches
        self.assertTrue(m("<empty>", intent.MISSING) and m("<empty>", []) and m("<empty>", ""))
        self.assertFalse(m("<non-empty>", intent.MISSING) or m("<non-empty>", []))
        self.assertTrue(m("<any>", 0) and not m("<any>", intent.MISSING))
        self.assertTrue(m({"<each>": {"r": 1}}, [{"r": 1}, {"r": 1, "x": 2}]))
        self.assertFalse(m({"<each>": {"r": 1}}, []))
        self.assertTrue(m({"<some>": {"r": 2}}, [{"r": 1}, {"r": 2}]))
        self.assertTrue(m({"<not>": "done"}, "failed") and not m({"<not>": "done"}, "done"))
        self.assertTrue(m({"a": {"b": 1}}, {"a": {"b": 1, "c": 2}}))
        self.assertFalse(m({"a": 1}, {"b": 1}))
        self.assertTrue(m([1, 2], [1, 2]) and not m([1], [1, 2]))
        # M2: no bool/int/float coercion
        self.assertFalse(m(1, True) or m(True, 1) or m(1, 1.0) or m([1], [True]) or m({"a": 0}, {"a": False}))


class Protected(unittest.TestCase):
    @private_only
    def test_a_worker_cannot_write_the_intent_dir(self):
        """Row 395 + 454: every file this branch ships under contracts/intent (the role files and their
        SHA256SUMS), and any new file there, is flagged by `workspace.touches_protected` - the exact
        check `Tick._verdict` runs on a worker's diff - through the `contracts/**` PROTECTED entry."""
        self.assertIn("contracts/**", workspace.PROTECTED)
        rel = paths.INTENT.relative_to(paths.REPO).as_posix()
        self.assertEqual(rel, "contracts/intent")
        shipped = sorted(p.relative_to(paths.REPO).as_posix() for p in paths.INTENT.iterdir())
        names = sorted(f"{r}.jsonl" for r in (*ROLES, *intent.STANDALONE))
        # Ruling (task-2, ambiguity): `lookout` ships as its own subdirectory (its own
        # collect.jsonl + SHA256SUMS, per the Lookout plan's Task 2 brief) rather than a flat
        # `<role>.jsonl` file like the other role/STANDALONE contracts, so it is listed as one
        # extra top-level directory entry here and its contents checked for protection below.
        want = sorted([f"{rel}/{n}" for n in ("SHA256SUMS", *names)] + [f"{rel}/lookout"])
        self.assertEqual(shipped, want)
        # R1-a (review round 2): read the lookout/ subdirectory's own contents rather than a fixed
        # [SHA256SUMS, collect.jsonl] list, so a later task's own *.jsonl there (Task 8a's
        # alerts.jsonl) never turns this test red - it only requires SHA256SUMS plus at least one
        # jsonl example file, and still proves every file in the directory is protected.
        lookout_names = sorted(p.name for p in (paths.INTENT / "lookout").iterdir())
        self.assertIn("SHA256SUMS", lookout_names)
        jsonls = [n for n in lookout_names if n.endswith(".jsonl")]
        self.assertGreaterEqual(len(jsonls), 1)
        lookout_files = sorted(f"{rel}/lookout/{n}" for n in lookout_names)
        touched = shipped + lookout_files + [f"{rel}/x.jsonl", "capsule/intent.py"]
        self.assertEqual(workspace.touches_protected(touched), touched)

    def test_intent_py_itself_is_protected(self):
        """Row 470 fix round 1, M7: intent.py governs the second-oracle gate (row 454/455) - a
        worker must not be able to edit its own oracle any more than it can edit contracts/**."""
        self.assertIn("capsule/intent.py", workspace.PROTECTED)
        self.assertEqual(workspace.touches_protected(["capsule/intent.py", "capsule/tick.py"]),
                         ["capsule/intent.py"])

    def test_a_standalone_script_may_not_also_carry_a_role_contract(self):
        """Row 470 fix round 1, M7: `lint_all` would otherwise silently take the `STANDALONE`
        (schema-free) branch for a role that also shipped a `contracts/<role>.md`, skipping the
        schema lint that page's own `schema:` line calls for - fail loud instead."""
        d = Path(tempfile.mkdtemp())
        (d / "unblocked.jsonl").write_text((paths.INTENT / "unblocked.jsonl").read_text())
        (paths.REPO / "contracts" / "unblocked.md").write_text("stray")
        try:
            errors = intent.lint_all(d)
        finally:
            (paths.REPO / "contracts" / "unblocked.md").unlink()
        self.assertTrue(any("unblocked.md" in e and "STANDALONE" in e for e in errors), errors)


def standalone_format_errors(source):
    """The `STANDALONE = (...)` block of intent.py source: one quoted name per line, sorted, with
    a trailing comma. Branches that add different names then touch different lines and merge."""
    head, _, rest = source.partition("\nSTANDALONE = (\n")
    body = rest.partition("\n)\n")[0].split("\n") if rest else []
    entry = re.compile(r'    "(\w+)",')
    names = [m.group(1) for m in map(entry.fullmatch, body) if m]
    errors = [f"not one quoted name with a trailing comma: {line!r}" for line in body
              if not entry.fullmatch(line)]
    if names != sorted(names):
        errors.append("names are not sorted")
    if not body:
        errors.append("no STANDALONE block")
    return errors


class StandaloneFormatTest(unittest.TestCase):
    """The format is the contract (t_a40ad33d): every intent-adding branch edits this tuple."""

    def test_the_shipped_tuple_is_one_sorted_name_per_line(self):
        source = (paths.REPO / "capsule" / "intent.py").read_text()
        self.assertEqual(standalone_format_errors(source), [])
        self.assertEqual(len(intent.STANDALONE), len(set(intent.STANDALONE)))

    def test_a_shared_line_or_an_unsorted_entry_is_reported(self):
        block = "\nSTANDALONE = (\n    \"a\",\n    \"b\",\n    \"c\",\n)\n"
        self.assertEqual(standalone_format_errors(block), [])
        self.assertTrue(standalone_format_errors(block.replace('"b",\n    "c"', '"c",\n    "b"')))
        self.assertTrue(standalone_format_errors(block.replace('"a",\n    "b"', '"a", "b"')))
        self.assertTrue(standalone_format_errors(block.replace('"c",', '"c"')))
