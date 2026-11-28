"""The host wall under the real `sandbox-exec` (D161, D162), with `sh` and a stand-in `claude` in
temp dirs: what is refused, what still works, and what `ClaudeAdapter.run` does with the wall. The
real `claude` binary is `test_host_wall_cli`. Skipped off macOS (no sandbox-exec)."""
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

from capsule import prior_runs, worker
from capsule.models import claude
from capsule.runners import host_wall
from capsule.tests.test_host_wall import DARWIN, Board

ROUTE, BUNDLE, LIMITS = {"model": "m", "turn_budget": 3}, {"system": "s", "prompt": "p"}, \
    {"schema": {"type": "object"}}
STAND_IN = """#!/bin/sh
echo "$@" > "$OUTDIR/argv.txt"
cat "$BLIND_FILE" > "$OUTDIR/leak.txt" 2>/dev/null
cat "$OWN/own.txt" > "$OUTDIR/own.txt"
echo '{"structured_output": {"ok": true}, "usage": {}}'
"""


@DARWIN
class Live(Board):
    """The real sandbox-exec. A broken one fails loudly (Z4)."""

    def sh(self, wall, script):
        argv = host_wall.apply(["/bin/sh", "-c", script], wall)
        return subprocess.run(argv, capture_output=True, text=True, cwd=self.root, timeout=30,
                              env=dict(os.environ, SPOOL=str(self.spools)))

    def test_reads_are_refused_and_own_things_still_work(self):
        (self.ws / "link").symlink_to(self.repo)
        script = (f'cat {self.repo}/t.py; ls {self.repo}; stat {self.repo}/t.py; cat ws/link/t.py;'
                  ' cat "$SPOOL/run0/report.json"; ls "$SPOOL"; cat "$SPOOL/../blind-work/r9/t.py";'
                  ' ls "$SPOOL/../blind-work"')
        done = self.sh(self.plan(), script)
        self.assertNotIn("SECRET", done.stdout)
        self.assertEqual(done.stderr.count("Operation not permitted"), 8, done.stderr)
        ok = self.sh(self.plan(),
                     'cat "$SPOOL/run1/own.txt"; echo x > "$SPOOL/run1/w.txt" && echo w;'
                     ' echo y > ws/new && echo y; /usr/bin/git --version | cut -c1-11')
        self.assertEqual(ok.stdout.split(), ["OWNw", "y", "git", "version"], ok.stderr)

    def test_a_realpath_of_an_own_path_walks_through_the_denied_spool_root(self):
        real = ("import os,sys;print(os.path.realpath(sys.argv[1]));"
                "print(os.stat(sys.argv[2]).st_mode>0)")
        own = self.spool / "requests" / "w.json"
        done = self.sh(self.plan(), f'/usr/bin/python3 -c "{real}" "{own}" "$SPOOL"')
        self.assertEqual(done.stdout.split("\n")[:2], [str(own), "True"], done.stderr)
        listed = self.sh(self.plan(), 'ls "$SPOOL"')
        self.assertIn("Operation not permitted", listed.stderr)

    def test_a_blind_path_with_odd_characters_is_walled_and_allows_nothing_else(self):
        odd = self.root / 'rep"o (x)\\y'
        odd.mkdir()
        (odd / "t.py").write_text("SECRET")
        self.mode("report", repo=odd)
        done = self.sh(self.plan("guide"),
                       f'cat {self.repo}/t.py; cat \'{odd}/t.py\'; cat "$SPOOL/run1/own.txt"')
        self.assertEqual(done.stdout, "SECRETOWN")      # only the odd path is walled
        self.assertIn("Operation not permitted", done.stderr)

    def test_a_repo_that_is_a_symlink_is_walled_at_its_real_location(self):
        link = self.root / "link"
        link.symlink_to(self.repo)
        self.mode("gate", repo=link)
        done = self.sh(self.plan("reviewer"), f"cat {self.repo}/t.py")
        self.assertEqual(done.stdout, "")
        self.assertIn("Operation not permitted", done.stderr)

    def test_extra_walled_paths_other_transcripts_and_the_jobs_folder_are_refused(self):
        extra, other = self.root / "tests repo", self.cfg / "projects" / "-other-cwd"
        extra.mkdir()
        other.mkdir()
        (extra / "t.py").write_text("SECRET")
        (other / "s.jsonl").write_text("SECRET")
        (self.cfg / "jobs" / "j.json").write_text("SECRET")
        own = host_wall._project(self.cfg, str(self.ws))
        own.mkdir()
        (own / "s.jsonl").write_text("MINE")
        self.mode("gate", also_walled=[str(extra)])
        done = self.sh(self.plan(),
                       f'cat "{extra}/t.py" "{other}/s.jsonl" "{self.cfg}/jobs/j.json";'
                       f' cat "{own}/s.jsonl"; ls "{self.cfg}/projects"')
        self.assertEqual(done.stdout, "MINE")
        self.assertEqual(done.stderr.count("Operation not permitted"), 4, done.stderr)

    def test_prior_runs_are_read_from_the_own_spool_copies_and_a_validator_stays_hidden(self):
        validator = self.spools / "val"
        (validator / "report").mkdir(parents=True)
        (validator / "report" / "r.json").write_text("{}")
        (validator / "blind_result.json").write_text("SECRET")
        copied, limits = prior_runs.mount(
            {"prior_runs": [{"run_id": "val", "role": "validator",
                             "paths": [str(validator), str(validator / "report" / "r.json")]}]},
            self.spool, {})
        copy = copied["prior_runs"][0]["paths"][1]
        self.assertEqual(limits["add_dirs"], [str(self.spool / "prior")])
        done = self.sh(self.plan(),
                       f"cat {copy}; cat {validator}/blind_result.json; ls {validator}")
        self.assertEqual(done.stdout, "{}")
        self.assertEqual(done.stderr.count("Operation not permitted"), 2)

    def test_korin_reads_its_evidence_spools_but_not_blind_parts_or_a_testers_or_the_repo(self):
        ev, tester = self.spools / "ev", self.spools / "tester"
        for sp in (ev, tester):
            (sp / "sub").mkdir(parents=True)
            (sp / "blind-out").mkdir()
            (sp / "report.json").write_text("EVIDENCE")
            (sp / "blind-out" / "o.txt").write_text("SECRET")
            (sp / "sub" / "blind_result.json").write_text("SECRET")
            (sp / "notblind.txt").write_text("fine")
        self.run_job(ev, self.ws)
        self.run_job(tester, self.board / "blind-work" / "t1")
        wall = self.plan("korin", inputs={"evidence": [str(ev), str(tester), str(self.other)]})
        done = self.sh(wall, "for f in ev/report.json ev/notblind.txt; do cat $SPOOL/$f;"
                             " echo; done")
        self.assertEqual(done.stdout.split(), ["EVIDENCE", "fine"], done.stderr)
        refused = self.sh(wall, "for f in ev/blind-out/o.txt ev/sub/blind_result.json"
                                f" tester/report.json run0/report.json; do cat $SPOOL/$f; done;"
                                f" cat {self.repo}/t.py")
        self.assertEqual(refused.stdout, "")
        self.assertEqual(refused.stderr.count("Operation not permitted"), 5, refused.stderr)


class Adapter(Board):
    """`ClaudeAdapter.run` with a stand-in `claude` first on PATH."""

    def setUp(self):
        super().setUp()
        bin_, self.out = self.root / "bin", self.root / "out"
        bin_.mkdir()
        self.out.mkdir()
        (bin_ / "claude").write_text(STAND_IN)
        (bin_ / "claude").chmod(0o755)
        patch = mock.patch.dict(os.environ, PATH=f"{bin_}:{os.environ['PATH']}",
                                OUTDIR=str(self.out), OWN=str(self.spool),
                                BLIND_FILE=str(self.repo / "t.py"))
        patch.start()
        self.addCleanup(patch.stop)

    def run_as(self, role, wall):
        limits = dict(LIMITS, wall=wall)
        return claude.ClaudeAdapter(ROUTE).run(role, BUNDLE, str(self.root), limits)

    def seen(self, name):
        return (self.out / name).read_text() if (self.out / name).exists() else None

    def fake_exe(self, text):
        exe = self.root / "sandbox-exec"
        exe.write_text(text)
        exe.chmod(0o755)
        return mock.patch.object(host_wall, "SANDBOX_EXEC", str(exe))

    @DARWIN
    def test_a_walled_session_reads_its_own_spool_and_not_the_blind_repo(self):
        for role in ("implementer", "reviewer", "guide", "korin"):
            res = self.run_as(role, self.plan(role))
            self.assertEqual((res.report, res.error_class), ({"ok": True}, None))
            self.assertEqual((self.seen("leak.txt"), self.seen("own.txt")), ("", "OWN"), role)

    def test_off_leaves_the_argv_byte_identical_and_a_wall_would_show(self):
        want = claude.build_argv(ROUTE, BUNDLE, str(self.root), LIMITS)
        self.mode("off")
        with mock.patch.object(claude.subprocess, "run", wraps=subprocess.run) as run:
            self.run_as("implementer", self.plan())
        self.assertEqual(run.call_args.args[0], want)
        self.assertEqual((self.seen("argv.txt").strip(), self.seen("leak.txt")),
                         (" ".join(want[1:]), "SECRET"))
        if sys.platform == "darwin":
            self.mode("report")
            with mock.patch.object(claude.subprocess, "run", wraps=subprocess.run) as run:
                self.run_as("implementer", self.plan())
            self.assertEqual(run.call_args.args[0][:2], ["/usr/bin/sandbox-exec", "-f"])
            self.assertEqual(run.call_args.args[0][3:], want)

    def test_a_wall_that_cannot_be_applied_starts_no_session_and_waits_to_retry(self):
        wall, before = self.plan(), int(time.time())
        for exe in ("/nonexistent/sandbox-exec", "/usr/bin/false"):
            with mock.patch.object(host_wall, "SANDBOX_EXEC", exe):
                res = self.run_as("implementer", wall)
            self.assertEqual((res.error_class, res.exit, res.report), ("wall_unavailable", 1, None))
            self.assertTrue(before + 290 <= res.retry_after <= before + 320, res.retry_after)
            self.assertIn("host wall unavailable", res.raw)
            self.assertIsNone(self.seen("argv.txt"))

    def test_a_multi_line_refusal_keeps_its_name_in_the_error_line(self):
        script = "#!/bin/sh\necho 'sandbox-exec: first' >&2\necho 'second Error: x' >&2\nexit 71\n"
        with self.fake_exe(script):
            res = self.run_as("implementer", self.plan())
        self.assertTrue(worker._error_line(res.raw).startswith("host wall unavailable"))

    def test_the_light_tier_has_its_own_class_and_never_retries(self):
        res = self.run_as("implementer", self.plan(limits={"tier": "light"}))
        self.assertEqual((res.error_class, res.retry_after), ("wall_light_tier", None))
        self.assertIn("light-tier", res.raw)
        self.assertIsNone(self.seen("argv.txt"))

    def test_only_the_blind_tester_runs_without_a_transcript(self):
        for role, want in (("blind-tester", True), ("implementer", False), ("korin", False)):
            self.mode("off")
            self.run_as(role, self.plan(role))
            self.assertEqual("--no-session-persistence" in self.seen("argv.txt"), want, role)


if __name__ == "__main__":
    unittest.main()
