"""plan check|cut (spec plan-cut). Oracle 1: the exit code and printed lines. Oracle 2: the files
the cutter did or did not write under boards/<board>/intake/."""
import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capsule import db, paths
from capsule.__main__ import main
from capsule.plan_cut import parse_plan

os.environ.setdefault("AGENT_OPS_NO_SEND", "1")

SPEC = """# Spec
```mermaid
graph TD; A-->B
```
Diagrams approved: 2026-11-01
Research validated: 2026-11-02
"""
PLAN = """# Plan
Signed: Owner 2026-11-03
Spec: spec.md
Epic: alpha
Preview: none (test plan, no UI)
Trace: none (test plan, no design)

## Cards
### A1 - first card
Files: a.py, b.py
Check: python3 -m unittest a
Expect: OK
Size: S
Risk: data, public
Cites: s3.2 rows 355
Body: do the first thing

### A2 - second card
Files: c.py
Check: true
Deps: A1

**B3 - third card**
**Files:** d.py
**Check:** true
"""


def git(cwd, *a):
    subprocess.run(["git", "-C", str(cwd), *a], check=True, capture_output=True)

NOAPP = SPEC.replace("Diagrams approved: 2026-11-01\n", "")
NORES = SPEC.replace("Research validated: 2026-11-02\n", "")


class PlanCase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.home = Path(tempfile.mkdtemp())
        self.spec = self.dir / "spec.md"
        self.plan = self.dir / "plan.md"
        self.real = os.path.realpath(self.plan)
        self.spec.write_text(SPEC)
        self.plan.write_text(PLAN)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = main(list(argv))
            except SystemExit as exc:
                rc = exc.code
        return rc, out.getvalue()

    def check(self, plan=None):
        return self.run_cli("plan", "check", str(plan or self.plan))

    def cut(self, *extra):
        return self.run_cli("plan", "cut", str(self.plan), "--board", "b1",
                            "--capsule-home", str(self.home), *extra)

    def spool(self, board="b1"):
        d = self.home / "boards" / board / "intake"
        return sorted(d.glob("rq_*.json")) if d.is_dir() else []

    def docs(self):
        return [json.loads(p.read_text()) for p in self.spool()]

    def refuse(self, plan_text=None, spec_text=None):
        if plan_text is not None:
            self.plan.write_text(plan_text)
        if spec_text is not None:
            self.spec.write_text(spec_text)
        rc, out = self.check()
        self.assertEqual(rc, 1)
        self.assertEqual(self.cut()[0], 1)
        self.assertEqual(self.spool(), [])
        return out


class Gate(PlanCase):
    def test_pass_prints_count(self):
        self.assertEqual(self.check(), (0, "gate: ok (3 cards)\ntrace: ok (0 items, 0 waivers)\n"
                                        "trace: none (test plan, no design)\n"))

    def test_missing_signed(self):
        out = self.refuse(PLAN.replace("Signed: Owner 2026-11-03\n", ""))
        self.assertIn("Signed", out)

    def test_missing_spec_line_and_file(self):
        out = self.refuse(PLAN.replace("Spec: spec.md\n", ""))
        self.assertIn("Spec", out)
        out = self.refuse(PLAN.replace("spec.md", "nope.md"))
        self.assertIn("does not exist", out)

    def test_absolute_spec_path(self):
        self.plan.write_text(PLAN.replace("Spec: spec.md", f"Spec: {self.spec}"))
        self.assertEqual(self.check()[0], 0)

    def test_spec_needs_mermaid_and_approval(self):
        out = self.refuse(spec_text=SPEC.replace("```mermaid", "```text"))
        self.assertIn("mermaid", out)
        out = self.refuse(spec_text=NOAPP)
        self.assertIn("Diagrams approved", out)

    def test_research_line(self):
        out = self.refuse(spec_text=NORES)
        self.assertIn("Research", out)
        none_needed = SPEC.replace("Research validated: 2026-11-02", "Research: none needed")
        self.spec.write_text(none_needed)
        self.assertEqual(self.check()[0], 0)

    def test_unknown_needs_poc_line_and_branch(self):
        self.spec.write_text(SPEC + "Unknown: queue\n")
        rc, out = self.check()
        self.assertEqual(rc, 1)
        self.assertIn("POC queue", out)
        self.spec.write_text(SPEC + "Unknown: queue\nPOC queue: poc/q.py\n")
        git(self.dir, "init", "-q", "-b", "main")
        git(self.dir, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
            "--allow-empty", "-m", "i")
        rc, out = self.check()
        self.assertEqual((rc, "poc/queue" in out), (1, True))
        git(self.dir, "branch", "poc/queue")
        self.assertEqual(self.check()[0], 0)

    def test_remote_tracking_poc_branch(self):
        self.spec.write_text(SPEC + "Unknown: queue\nPOC queue: x\n")
        git(self.dir, "init", "-q", "-b", "main")
        git(self.dir, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
            "--allow-empty", "-m", "i")
        git(self.dir, "update-ref", "refs/remotes/origin/poc/queue", "HEAD")
        self.assertEqual(self.check()[0], 0)

    def test_card_rules_name_the_card(self):
        out = self.refuse(PLAN.replace("Files: a.py, b.py\n", ""))
        self.assertIn("A1: missing Files", out)
        out = self.refuse(PLAN.replace("Check: true\nDeps", "Deps"))
        self.assertIn("A2: missing Check", out)
        out = self.refuse(PLAN.replace("### A2 - second", "### A1 - second"))
        self.assertIn("A1: duplicate", out)
        out = self.refuse(PLAN.replace("Deps: A1", "Deps: Z9"))
        self.assertIn("A2: Deps names Z9", out)
        out = self.refuse("Signed: P 2026-11-03\nSpec: spec.md\n")
        self.assertIn("no cards", out)

    def test_check_writes_nothing_and_usage_errors(self):
        self.check()
        self.assertEqual(self.spool(), [])
        self.assertEqual(self.run_cli("plan", "cut", str(self.plan))[0], 2)
        self.assertEqual(self.run_cli("plan")[0], 2)

    def test_parse_forms(self):
        _, cards, problems = parse_plan(PLAN)
        self.assertEqual(problems, [])
        self.assertEqual([c["id"] for c in cards], ["A1", "A2", "B3"])
        self.assertEqual(cards[0]["body"], "do the first thing")
        self.assertEqual(cards[2]["fields"]["Files"], "d.py")
        _, cards, _ = parse_plan("### A1 – dash\nFiles: x\n## Next\nCheck: no\n")
        self.assertEqual(cards[0]["fields"], {"Files": "x"})

    def test_fences_hide_headings_fields_and_cards(self):
        fenced = ("### A1 - one\n```\n# comment\nCheck: make clean\n### C09 - x\n```\n"
                  "Files: a.py\nCheck: true\n")
        _, cards, problems = parse_plan("Signed: P 2026-11-03\n" + fenced)
        self.assertEqual([c["id"] for c in cards], ["A1"])
        self.assertEqual(cards[0]["fields"], {"Files": "a.py", "Check": "true"})
        self.assertIn("# comment", cards[0]["body"])
        self.assertEqual(problems, [])
        self.plan.write_text(PLAN.replace("## Cards", "```\n### C09 - x\n```\n## Cards"))
        self.assertEqual(self.cut()[0], 0)
        self.assertEqual(len(self.spool()), 3)

    def test_repeated_field_is_a_failure(self):
        out = self.refuse(PLAN.replace("Check: true\nDeps", "Check: true\nCheck: false\nDeps"))
        self.assertIn("A2: Check: given more than once", out)

    def test_unclosed_fence_is_a_failure(self):
        a1_end = "Check: python3 -m unittest a\n"
        plan = PLAN.replace("Check: python3 -m unittest a\n", a1_end + "```\nnever closed\n", 1)
        out = self.refuse(plan)
        self.assertIn("code fence opened at line", out)

    def test_malformed_card_headers_name_the_line(self):
        for bad in ("### a1 - x", "### A1 — x", "### C3: x", "### A1-x", "**C3**"):
            self.plan.write_text(PLAN + f"\n{bad}\nFiles: z\nCheck: true\n")
            rc, out = self.check()
            self.assertEqual(rc, 1)
            self.assertIn("plan line", out)

    def test_poc_branch_names_match_exactly(self):
        self.spec.write_text(SPEC + "Unknown: a\nPOC a: x\n")
        git(self.dir, "init", "-q", "-b", "main")
        git(self.dir, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
            "--allow-empty", "-m", "i")
        git(self.dir, "branch", "poc/a/x")
        git(self.dir, "update-ref", "refs/remotes/origin/z/poc/a", "HEAD")
        rc, out = self.check()
        self.assertEqual(rc, 1)
        self.assertIn("poc/a", out)

    def test_non_utf8_plan_or_spec_refuses_cleanly(self):
        self.plan.write_bytes(b"\xff\xfe### A1 - x\n")
        rc, out = self.check()
        self.assertEqual(rc, 1)
        self.assertIn("plan.md", out)
        self.plan.write_text(PLAN)
        self.spec.write_bytes(b"\xff\xfe")
        rc, out = self.check()
        self.assertEqual(rc, 1)
        self.assertIn("spec.md", out)


WARN = "plan: board b1 has deps_ready off; Deps links will not hold cards"


class Cut(PlanCase):
    def setUp(self):
        super().setUp()
        self.write_config(True)

    def write_config(self, deps_ready):
        cfg = json.loads((Path(__file__).resolve().parents[1] / "config.example.json").read_text())
        cfg.update(board="b1", redesign={"deps_ready": deps_ready})
        path = paths.config_path(self.home, "b1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cfg))

    def test_deps_ready_off_warns_once_and_still_files(self):
        self.write_config(False)
        rc, out = self.cut()
        self.assertEqual(rc, 0)
        self.assertEqual(out.splitlines().count(WARN), 1)
        self.assertEqual(len(self.spool()), 3)

    def test_files_cards_in_order(self):
        rc, out = self.cut()
        self.assertEqual(rc, 0)
        docs = self.docs()
        by_id = {d["title"].split(" - ")[0]: d for d in docs}
        self.assertEqual(sorted(by_id), ["A1", "A2", "B3"])
        lines = out.splitlines()
        self.assertIn("A2 waits for A1", lines)
        cards = [ln for ln in lines if " waits for " not in ln]
        self.assertEqual([ln.split()[0] for ln in cards], ["A1", "A2", "B3"])
        self.assertEqual(out.splitlines()[0].split()[1], by_id["A1"]["intake_id"])
        a1 = by_id["A1"]
        self.assertEqual(a1["title"], "A1 - first card")
        self.assertEqual(a1["file_set"], ["a.py", "b.py"])
        self.assertEqual(a1["epic"], "alpha")
        self.assertEqual(a1["card_fields"], {"check_cmd": "python3 -m unittest a",
                                             "check_expect": "OK", "size_tag": "S",
                                             "risk_flags": ["data", "public"]})
        self.assertEqual(a1["fingerprint"], f"plan:{os.path.realpath(self.plan)}:A1")
        for want in ("do the first thing", str(self.plan), str(self.spec), "s3.2 rows 355"):
            self.assertIn(want, a1["body"])
        self.assertIn("A1", by_id["A2"]["body"])

    def test_epic_flag_overrides(self):
        self.cut("--epic", "beta")
        self.assertEqual({d["epic"] for d in self.docs()}, {"beta"})

    def test_gate_failure_files_nothing(self):
        out = self.refuse(PLAN.replace("Signed: Owner 2026-11-03\n", ""))
        self.assertIn("Signed", out)

    def test_rerun_files_no_duplicate(self):
        self.cut()
        rc, out = self.cut()
        self.assertEqual(rc, 0)
        self.assertEqual(out.splitlines(), ["A1 already filed", "A2 already filed",
                                            "B3 already filed"])
        self.assertEqual(len(self.spool()), 3)

    def test_ingested_card_counts_as_filed(self):
        dbp = paths.board_db("b1", self.home)
        db.create_board(dbp)
        conn = db.connect(dbp)
        tid = db.new_task(conn, title="x")
        db.event(conn, tid, "intake_ingested", {"intake_id": "rq_1", "kind": "card",
                                                "fingerprint": f"plan:{self.real}:A1",
                                                "source_board": "operator", "source_task": None})
        conn.commit()
        conn.close()
        before = dbp.read_bytes()
        os.chmod(dbp, 0o444)
        with mock.patch("capsule.db.connect", side_effect=AssertionError("writer opened")):
            rc, out = self.cut()
        self.assertEqual(out.splitlines()[0], "A1 already filed")
        self.assertEqual(len(self.spool()), 2)
        self.assertEqual(dbp.read_bytes(), before)

    def test_fingerprint_is_repo_relative_inside_a_git_repo(self):
        git(self.dir, "init", "-q", "-b", "main")
        self.cut()
        self.assertEqual({d["fingerprint"] for d in self.docs()},
                         {f"plan:plan.md:{i}" for i in ("A1", "A2", "B3")})

    def test_other_board_or_plan_is_not_filed(self):
        self.cut()
        self.assertEqual(self.run_cli("plan", "cut", str(self.plan), "--board", "b2",
                                      "--capsule-home", str(self.home))[0], 0)
        self.assertEqual(len(self.spool("b2")), 3)

    def test_dry_run_writes_nothing(self):
        rc, out = self.cut("--dry-run")
        self.assertEqual(rc, 0)
        self.assertEqual(len(out.splitlines()), 4)
        self.assertIn("A2 waits for A1", out.splitlines())
        self.assertEqual(self.spool(), [])
        self.assertFalse(paths.board_db("b1", self.home).exists())

    def test_invalid_field_refuses_whole_cut(self):
        bad_risk = PLAN.replace("Check: true\nDeps", "Risk: bogus\nCheck: true\nDeps")
        bad_size = PLAN.replace("**Check:** true", "**Check:** true\n**Size:** XL")
        for plan, card in ((bad_risk, "A2"), (bad_size, "B3")):
            self.plan.write_text(plan)
            rc, out = self.cut()
            self.assertEqual(rc, 1)
            self.assertIn(card, out)
            self.assertEqual(self.spool(), [])


if __name__ == "__main__":
    unittest.main()
