"""BLD-02: the security reviewer is a fourth blocking review behind `redesign.security_review`.
Contract rows replayed on the real tick (the scripted TickCase world of test_tick): rows 362, 366
and 508 of contracts/intent/review_gate.jsonl, plus the off-is-identical and cap statements."""
import copy
import json
import unittest

from capsule import config, dispatch, intent, security_review
from capsule.tests import test_tick as tt
from capsule.tests.test_tick import GUIDE_OK, IMPL_DONE, REV_PASS, VAL_PASS, TickCase

SEC_PASS = dict(REV_PASS, role="security", notes=["adds no network, secret or dependency path"])
DIFF = ("diff --git a/capsule/a.py b/capsule/a.py\n+x = 1\n"
        "diff --git a/package-lock.json b/package-lock.json\n+lock\n"
        "diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml\n+run: x\n"
        "diff --git a/docker/Dockerfile.worker b/docker/Dockerfile.worker\n+FROM x\n")


def row(row_id):
    return {r["id"]: r for r in intent.load("review_gate")}[row_id]   # a missing row is a KeyError


class World(TickCase):
    def setUp(self):
        super().setUp()
        self.script["implementer"] = [[IMPL_DONE]] * 3

    def switch(self, on=True, route=True):
        self.cfg["redesign"] = {"security_review": on}
        if route:
            self.cfg["routing"]["security"] = copy.deepcopy(self.cfg["routing"]["reviewer"])
        self.write_cfg()
        self.tick = self.make_tick()

    def drive(self, n=16):
        for _ in range(n):
            self.tick.once()
            task = self.task()
            if task["status"] in ("done", "blocked") or task["failure_count"]:
                return
        self.fail("the card never settled")

    def incidents(self):
        return self.conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]

    def attempts(self, names):
        bad = dict(SEC_PASS, verdict="bogus")
        docs = {"no document": [], "invalid": [bad], "pass": [SEC_PASS]}
        self.script["security"] = [docs[n] for n in names]


class Switch(World):
    def test_off_runs_the_three_reviews_and_the_gate_has_no_security_word(self):
        self.drive()
        self.assertEqual(self.task()["status"], "done")
        self.assertEqual(sorted(r["profile"] for r in self.runs()),
                         ["guide", "implementer", "reviewer", "validator"])
        gates = json.loads(self.conn.execute(
            "SELECT payload FROM task_events WHERE kind='completed'").fetchone()[0])["gates"]
        self.assertEqual(set(gates), {"validator", "reviewer", "guide", "rounds", "overrides"})
        self.assertEqual(security_review.roles(self.cfg), security_review.BASE)

    def test_on_starts_four_reviews_and_all_four_must_pass(self):
        self.switch()
        self.attempts(["pass"])
        self.drive()
        self.assertEqual(self.task()["status"], "done")
        self.assertEqual(sorted(r["profile"] for r in self.runs()),
                         ["guide", "implementer", "reviewer", "security", "validator"])
        gates = json.loads(self.conn.execute(
            "SELECT payload FROM task_events WHERE kind='completed'").fetchone()[0])["gates"]
        self.assertEqual(gates["security"], "pass")
        self.assertEqual(security_review.roles(self.cfg),
                         ("validator", "reviewer", "guide", "security"))

    def test_on_without_a_route_is_a_config_error_naming_the_route(self):
        self.cfg["redesign"] = {"security_review": True}
        with self.assertRaisesRegex(ValueError, r"routing\.security"):
            config.validate(self.cfg)
        self.cfg["routing"]["security"] = copy.deepcopy(self.cfg["routing"]["reviewer"])
        config.validate(self.cfg)

    def test_a_route_without_the_switch_is_fine_and_runs_nothing(self):
        self.switch(on=False)
        self.drive()
        self.assertEqual(self.runs("security"), [])

    def test_the_switch_must_be_a_bool(self):
        self.cfg["redesign"] = {"security_review": "yes"}
        with self.assertRaises(ValueError):
            config.validate(self.cfg)


class Bundle(World):
    def test_the_security_bundle_carries_the_diff_manifest_and_risk_only_row_366(self):
        ex = row("security-reads-diff-and-manifest-only")
        spool = ex["given"]["document"]["spool"]
        got = security_review.bundle({"id": "t_00000001", "title": "x"}, spool)["prompt"]
        for mark, present in ex["expect"]["bundle_contains"].items():
            self.assertEqual(mark in got, present, mark)

    def test_the_manifest_part_keeps_only_dependency_lock_image_and_ci_files(self):
        part = security_review._manifest_diff(DIFF)
        self.assertNotIn("capsule/a.py", part)
        for name in ("package-lock.json", ".github/workflows/ci.yml", "Dockerfile.worker"):
            self.assertIn(name, part)

    def test_every_common_dependency_and_ci_manifest_name_is_a_manifest_l423(self):
        names = ("pom.xml", "app/build.gradle", "build.gradle.kts", "gradle.properties",
                 "gradle/wrapper/gradle-wrapper.properties", "x/foo.gemspec", "Gemfile.lock",
                 ".npmrc", ".yarnrc", ".yarnrc.yml", "pnpm-lock.yaml", "pyproject.toml",
                 "setup.cfg", "setup.py", "uv.lock", "poetry.lock", "Pipfile", "Pipfile.lock",
                 "Cargo.toml", "Cargo.lock", "go.sum", "src/App.csproj", "packages.lock.json",
                 "nuget.config", "NuGet.Config", "azure-pipelines.yml", ".gitlab-ci.yml",
                 ".circleci/config.yml", "Jenkinsfile", "bitbucket-pipelines.yml",
                 ".buildkite/pipeline.yml", ".buildkite/hooks/pre-command")
        for name in names:
            self.assertTrue(security_review.MANIFEST.search(name), name)
        for name in ("docs/pom.xml.md", "mysetup.py", "xpom.xml", "build.gradle.bak",
                     "notes.npmrc.txt", "gemspec", "a.csproj.user", "my.npmrc", "x.yarnrc",
                     "buildkite/pipeline.yml", "pom.xml/readme.md"):
            self.assertFalse(security_review.MANIFEST.search(name), name)

    def test_a_started_run_gets_those_inputs_and_no_other_verdict(self):
        self.switch()
        self.attempts(["pass"])
        jobs = []
        self.hooks["security"] = lambda job, sp, run: jobs.append(job)
        self.drive()
        (job,) = jobs
        self.assertEqual({"diff", "dependency_manifest_diff", "risk_class"} - set(job["inputs"]),
                         set())
        self.assertEqual(job["inputs"]["risk_class"], "Risk flags: none")
        text = json.dumps(job["inputs"])
        for other in ("verdict", "spec_section", "row_texts", "impl_report"):
            self.assertNotIn(other, text)
        self.assertEqual(job["route"]["model"], self.cfg["routing"]["reviewer"]["model"])
        bundle = json.loads((self.spool_dir(self.runs("security")[0]["run_id"])
                             / "bundle.json").read_text())
        self.assertIn("review_verdict.v1", bundle["prompt"])


class Gate(World):
    def test_findings_block_the_merge_and_go_back_to_the_implementer_row_362(self):
        ex = row("security-verdict-blocks-merge")
        doc = ex["given"]["document"]
        self.switch(on=doc["reviews_security"] == "on")
        found = dict(SEC_PASS, verdict="fail", findings=doc["security_findings"])
        self.script["security"] = [[found]]
        real, claims = self.tick._claim_and_launch, []
        # the card goes back to `ready`; hold it there instead of letting this pass reclaim it
        self.tick._claim_and_launch = lambda *a: None if claims else (claims.append(1), real(*a))[1]
        self.drive()
        task = self.task()
        want = ex["expect"]
        self.assertEqual((task["status"], task["failure_count"]),
                         (want["card_state"], want["failure_count"]))
        self.assertNotEqual(task["status"], "done")
        bodies = [r[0] for r in self.conn.execute("SELECT body FROM task_comments")]
        self.assertTrue(any(want["finding_text_returned_to_implementer"] in b for b in bodies))
        self.assertIn(want["finding_text_returned_to_implementer"],
                      self.tick._open_findings(self.conn, self.cfg, self.tid, [])[0])

    def replay_attempts(self, name):
        ex = row("security-missing-or-invalid-is-review-infra-not-a-strike")
        self.switch()
        self.attempts(ex["given"]["document"]["security_attempts"][name])
        self.drive()
        task, want = self.task(), ex["expect"][name]
        got = {"merged": task["status"] == "done", "failure_count": task["failure_count"],
               "incidents_opened": self.incidents(), "security_runs": len(self.runs("security"))}
        for key, value in want.items():
            if key == "card_state":
                self.assertEqual(task["status"], value)
            elif key == "blocker":
                self.assertEqual(task["block_kind"], value)
            else:
                self.assertEqual(got[key], value, key)

    def test_a_missing_document_is_review_infra_not_a_strike_row_508(self):
        self.replay_attempts("missing")

    def test_two_invalid_documents_are_review_infra_not_a_strike_row_508(self):
        self.replay_attempts("invalid_twice")

    def test_invalid_then_pass_merges_row_508(self):
        self.replay_attempts("invalid_then_pass")


class Cap(World):
    def test_a_live_security_run_takes_a_review_seat(self):
        self.cfg["caps"]["max_review"] = 1
        self.write_cfg()
        cfg = config.load(self.home / "config.json")
        task = self.task()
        free = dispatch.admit_reviews(self.conn, cfg, self.home, task, ["validator"], 1000.0)
        self.assertEqual(free, ["validator"])
        self.conn.execute("INSERT INTO task_runs (task_id, profile, run_id, started_at, status)"
                          " VALUES (?, 'security', 'sec1', 1, 'running')", (self.tid,))
        self.conn.commit()
        held = dispatch.admit_reviews(self.conn, cfg, self.home, task, ["validator"], 1000.0)
        self.assertEqual(held, [])


if __name__ == "__main__":
    unittest.main()
