"""The host wall's start-up check (D161, D162): `host_wall.apply` writes the profile, proves it with
a real `sandbox-exec` and refuses a session when it does not hold (S29, S31, S35, S36). The plan and
the rendered text are `test_host_wall`; the real `claude` binary is `test_host_wall_cli`."""
import concurrent.futures
import json
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from capsule.runners import host_wall
from capsule.tests.test_host_wall import DARWIN, NO_WRAP, Board


def start_in_a_child(wall):
    """One walled start in its own process (module level: a process pool pickles it)."""
    try:
        host_wall.apply(["claude"], wall)
    except host_wall.WallUnavailable as exc:
        return str(exc)


class Apply(Board):
    def fake_exe(self, text):
        exe = self.root / "sandbox-exec"
        exe.write_text(text)
        exe.chmod(0o755)
        return mock.patch.object(host_wall, "SANDBOX_EXEC", str(exe))

    def test_apply_failures_raise_before_any_session(self):
        wall = self.plan()
        for exe in ("/nonexistent/sandbox-exec", "/usr/bin/false"):
            with mock.patch.object(host_wall, "SANDBOX_EXEC", exe), \
                    self.assertRaises(host_wall.WallUnavailable):
                host_wall.apply(["claude"], wall)

    def test_a_wrapper_that_applies_nothing_is_caught_by_the_probe(self):
        with self.fake_exe(NO_WRAP), self.assertRaisesRegex(host_wall.WallUnavailable,
                                                            "did not refuse a walled path"):
            host_wall.apply(["claude"], self.plan())

    def test_a_multi_line_refusal_is_one_line(self):
        script = "#!/bin/sh\necho 'sandbox-exec: first' >&2\necho 'line two' >&2\nexit 71\n"
        with self.fake_exe(script), self.assertRaises(host_wall.WallUnavailable) as caught:
            host_wall.apply(["claude"], self.plan())
        self.assertNotIn("\n", str(caught.exception))
        self.assertIn("refused the profile", str(caught.exception))
        self.assertIn("line two", str(caught.exception))

    def test_a_symlink_at_the_profile_is_refused_and_one_at_the_probe_name_is_not(self):
        victim = self.root / "victim.txt"
        victim.write_text("KEEP")
        link = self.spool / host_wall.PROFILE
        link.symlink_to(victim)
        with self.assertRaises(OSError):                        # the profile writer never follows
            host_wall._write(str(link), "x")
        self.assertEqual(victim.read_text(), "KEEP")
        link.unlink()
        left = self.spool / host_wall.PROBE                     # a leftover at the bare probe name
        left.symlink_to(victim)
        with self.fake_exe(NO_WRAP), self.assertRaisesRegex(host_wall.WallUnavailable,
                                                            "did not refuse a walled path"):
            host_wall.apply(["claude"], self.plan())            # reaches the check; no write
        self.assertEqual(victim.read_text(), "KEEP")
        self.assertTrue(left.is_symlink())

    @DARWIN
    def test_a_folder_allowed_back_inside_a_walled_path_is_caught_by_the_probe(self):
        wall = self.plan()
        host_wall.apply(["claude"], wall)                       # the good wall passes
        wall["allow"].append(str(self.board / "blind-work" / "r9"))
        with self.assertRaisesRegex(host_wall.WallUnavailable, "did not refuse a walled path"):
            host_wall.apply(["claude"], wall)
        deep = self.plan()
        deep["allow"].append(str(self.repo))                    # the walled folder itself
        with self.assertRaisesRegex(host_wall.WallUnavailable, "did not refuse a walled path"):
            host_wall.apply(["claude"], deep)

    @DARWIN
    def test_an_allow_two_levels_inside_a_walled_folder_is_caught(self):
        deep = self.board / "blind-work" / "r9" / "deep"
        deep.mkdir()
        (deep / "t.py").write_text("SECRET")
        wall = self.plan()
        host_wall.apply(["claude"], wall)
        self.assertIn(str(deep), host_wall._must_refuse(wall, "x")[1])     # probed two levels in
        wall["allow"].append(str(deep))                         # only that folder, not r9
        with self.assertRaisesRegex(host_wall.WallUnavailable, "did not refuse a walled path"):
            host_wall.apply(["claude"], wall)

    @DARWIN
    def test_a_path_that_vanished_between_the_listing_and_the_check_is_a_skip(self):
        wall = self.plan()
        real, listed = host_wall._must_refuse, []

        def racing(w, tag):
            made = self.repo / "listed-then-gone.py"
            made.write_text("X")
            blind, found = real(w, tag)
            listed.append(str(made) in found)
            made.unlink()                                       # removed after the listing
            return blind, found + [str(self.repo / "never-was"), str(self.cfg / "projects" / "no")]
        with mock.patch.object(host_wall, "_must_refuse", racing):
            host_wall.apply(["claude"], wall)                   # no WallUnavailable
        self.assertEqual(listed, [True])

    @DARWIN
    def test_starts_on_one_spool_at_once_do_not_refuse_each_other(self):
        wall = self.plan("korin")

        def start(_):
            try:
                host_wall.apply(["claude"], wall)
            except host_wall.WallUnavailable as exc:
                return str(exc)
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            failed = [r for r in pool.map(start, range(24)) if r]
        self.assertEqual(failed, [])
        self.assertEqual(list(self.spool.glob("hostwall.probe*")), [])      # each start's own file
        self.assertEqual(list(self.spool.glob(f"{host_wall.BLIND_PROBE}*")), [])
        names = {host_wall._must_refuse(wall, t)[0][0] for t in ("a", "b")}
        self.assertEqual(len(names), 2)                         # a name per start

    @DARWIN
    def test_korins_real_profile_cannot_read_a_validators_job_json_with_blind_rows(self):
        carry, plain = self.spools / "valid", self.spools / "validplain"
        for sp, cmd in ((carry, "blind:t1.test_x"), (plain, "make test")):
            sp.mkdir()
            (sp / "job.json").write_text(json.dumps({"workspace": str(self.ws), "inputs": {
                "outputs": [{"cmd": cmd, "given": "BLINDMARK", "expect": "E", "observed": "O"}]}}))
            (sp / "worker.log").write_text("fine")
        wall = self.plan("korin", inputs={"evidence": [str(carry), str(plain)]})
        argv = host_wall.apply(["claude"], wall)[:3]
        cat = [subprocess.run(argv + ["/bin/cat", str(p)], capture_output=True, text=True)
               for p in (carry / "job.json", carry / "worker.log", plain / "worker.log")]
        self.assertEqual([c.stdout for c in cat], ["", "", "fine"])
        self.assertEqual([c.returncode != 0 for c in cat], [True, True, False])

    @DARWIN
    def test_a_korin_profile_without_the_blind_deny_is_caught_and_leaves_no_probe_file(self):
        self.run_job(self.other, self.ws)
        wall = self.plan("korin", inputs={"evidence": [str(self.other)]})
        host_wall.apply(["claude"], wall)
        real = host_wall.render

        def without(w):
            return "\n".join(ln for ln in real(w).splitlines() if "regex" not in ln) + "\n"
        with mock.patch.object(host_wall, "render", without), \
                self.assertRaisesRegex(host_wall.WallUnavailable, "did not refuse"):
            host_wall.apply(["claude"], wall)
        self.assertEqual(list(self.other.glob(f"{host_wall.BLIND_PROBE}*")), [])   # any tag

    @DARWIN
    def test_a_korin_session_with_no_evidence_still_proves_blind_names_in_its_own_spool(self):
        wall = self.plan("korin")
        self.assertEqual(wall["blind"], [str(self.spool)])
        host_wall.apply(["claude"], wall)
        real = host_wall.render

        def without(w):
            return "\n".join(ln for ln in real(w).splitlines() if "regex" not in ln) + "\n"
        with mock.patch.object(host_wall, "render", without), \
                self.assertRaisesRegex(host_wall.WallUnavailable, "did not refuse"):
            host_wall.apply(["claude"], wall)
        self.assertEqual(list(self.spool.glob(f"{host_wall.BLIND_PROBE}*")), [])
        self.assertEqual((self.spool / "own.txt").read_text(), "OWN")      # only its own file goes

    @DARWIN
    def test_apply_wraps_and_writes_the_profile_in_the_own_spool(self):
        argv = host_wall.apply(["claude", "-p"], self.plan())
        profile = Path(argv[2])
        self.assertEqual((profile.parent, profile.name.startswith(host_wall.PROFILE + ".")),
                         (self.spool, True))                    # a per-start name
        self.assertEqual(argv,["/usr/bin/sandbox-exec", "-f", str(profile), "claude", "-p"])
        self.assertEqual(profile.stat().st_mode & 0o777, 0o600)
        done = subprocess.run(argv[:3] + ["/bin/cat", str(self.other / "report.json")],
                              capture_output=True, text=True)
        self.assertEqual((done.stdout, done.returncode != 0), ("", True))


    @DARWIN
    def test_an_allow_at_any_depth_of_two_or_more_inside_a_walled_folder_is_caught(self):
        for top in (self.repo, self.board / "blind-work" / "r9"):
            for depth in (2, 3, 4, 7):
                deep = top.joinpath(*[f"d{n}" for n in range(depth)])
                deep.mkdir(parents=True)
                (deep / "t.py").write_text("SECRET")
                wall = self.plan()
                host_wall.apply(["claude"], wall)               # nothing allowed: it holds
                wall["allow"].append(str(deep))
                with self.assertRaisesRegex(host_wall.WallUnavailable,
                                            "did not refuse a walled path", msg=(top, depth)):
                    host_wall.apply(["claude"], wall)

    @DARWIN
    def test_starts_in_separate_processes_on_one_spool_do_not_refuse_each_other(self):
        for role in ("implementer", "korin"):
            evidence = {"evidence": [str(self.other)]} if role == "korin" else {}
            wall = self.plan(role, inputs=evidence)
            with concurrent.futures.ProcessPoolExecutor(8) as pool:
                failed = [r for r in pool.map(start_in_a_child, [wall] * 32) if r]
            self.assertEqual(failed, [], role)
        self.assertEqual(list(self.spool.glob("hostwall.probe*")), [])


if __name__ == "__main__":
    unittest.main()
