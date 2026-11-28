"""deps_sync.py (the Python port of deps_sync.sh, D97 pilot): the second oracle. The first
oracle is tests/test_deps_sync.sh, run unchanged against the wrapper. Everything here uses a
temp cache, a temp registry and a faked subprocess/events - never the network, a real HOME or
the live cache."""
import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

MAINT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(MAINT))

import deps_sync  # noqa: E402
from deps_sync_fixture import Base, FakeFetch  # noqa: E402


class SplitSpec(unittest.TestCase):
    def test_shapes(self):
        self.assertEqual(deps_sync._split_spec("pip:httpx==0.27.2"), ("httpx", "0.27.2"))
        self.assertEqual(deps_sync._split_spec("npm:vitest@2.1.1"), ("vitest", "2.1.1"))
        self.assertEqual(deps_sync._split_spec("npm:@types/node@20.1.0"), ("@types/node", "20.1.0"))
        self.assertEqual(deps_sync._split_spec("gem:rails@7"), ("", ""))


class ArtifactExists(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.c = self.td.name
        os.makedirs(f"{self.c}/pip")
        os.makedirs(f"{self.c}/npm")

    def test_pip_name_prefix_match(self):
        self.assertFalse(deps_sync._artifact_exists(self.c, "pip:httpx==1", "httpx"))
        Path(self.c, "pip", "httpx-1-py3-none-any.whl").touch()
        self.assertTrue(deps_sync._artifact_exists(self.c, "pip:httpx==1", "httpx"))
        self.assertFalse(deps_sync._artifact_exists(self.c, "pip:other==1", "other"))

    def test_pip_underscore_name_matches_hyphenated_file(self):
        Path(self.c, "pip", "my-pkg-1.0.tar.gz").touch()
        self.assertTrue(deps_sync._artifact_exists(self.c, "pip:my_pkg==1.0", "my_pkg"))

    def test_pip_match_is_case_sensitive_like_the_shell_glob(self):
        Path(self.c, "pip", "Httpx-1.whl").touch()
        self.assertFalse(deps_sync._artifact_exists(self.c, "pip:httpx==1", "httpx"))

    def test_npm_needs_a_non_empty_cache_hidden_files_count(self):
        self.assertFalse(deps_sync._artifact_exists(self.c, "npm:v@1", "v"))
        Path(self.c, "npm", ".marker").touch()
        self.assertTrue(deps_sync._artifact_exists(self.c, "npm:v@1", "v"))

    def test_missing_dir_and_unknown_prefix_are_false(self):
        self.assertFalse(deps_sync._artifact_exists(self.c + "/nope", "pip:a==1", "a"))
        self.assertFalse(deps_sync._artifact_exists(self.c, "gem:a@1", ""))


class RecordKeepsLines(unittest.TestCase):
    def test_a_manifest_without_a_trailing_newline_does_not_glue_the_next_spec(self):
        with tempfile.TemporaryDirectory() as td:
            manifest = os.path.join(td, "synced.txt")
            Path(manifest).write_text("pip:a==1")
            deps_sync._record(manifest, "pip:b==2")
            self.assertEqual(Path(manifest).read_text(), "pip:a==1\npip:b==2\n")
            self.assertTrue(deps_sync.in_manifest(manifest, "pip:b==2"))


class FirstRunAndIdempotency(Base):
    def test_first_run_fetches_each_spec_with_the_exact_argv(self):
        rc, out, _, f = self.run_main()
        self.assertEqual(rc, 0)
        self.assertEqual(out, "GOT pip:httpx==0.27.2\nGOT npm:vitest@2.1.1\n"
                              "deps-sync: 0 ok, 2 got, 0 failed\n")
        (pip_cmd, pip_env), (npm_cmd, npm_env) = f.calls
        self.assertEqual(pip_cmd, ["pip", "download", "--no-deps", "--dest",
                                   f"{self.cache}/pip", "httpx==0.27.2"])
        self.assertEqual(npm_cmd, ["npm", "cache", "add", "vitest@2.1.1"])
        self.assertEqual(npm_env["npm_config_cache"], f"{self.cache}/npm")
        # only npm's call gets the cache variable
        self.assertNotEqual(pip_env.get("npm_config_cache"), f"{self.cache}/npm")
        self.assertEqual(self.manifest(), ["pip:httpx==0.27.2", "npm:vitest@2.1.1"])

    def test_one_dep_cache_event_per_new_package_with_the_owning_repo(self):
        self.run_main()
        rows = self.sync_events()
        got = [(r["kind"], r["source"], r["board"], r["text"], r["payload"]) for r in rows]
        self.assertEqual(got, [
            ("dep_cache", "deps_sync.sh", "repo-a", "deps-sync: got pip:httpx==0.27.2",
             {"event": "sync", "repo": "repo-a", "pkg": "pip:httpx==0.27.2"}),
            ("dep_cache", "deps_sync.sh", "repo-b", "deps-sync: got npm:vitest@2.1.1",
             {"event": "sync", "repo": "repo-b", "pkg": "npm:vitest@2.1.1"})])

    def test_second_run_fetches_nothing_and_logs_nothing(self):
        self.run_main()
        rc, out, _, f = self.run_main()
        self.assertEqual((rc, f.calls), (0, []))
        self.assertEqual(out, "ok pip:httpx==0.27.2\nok npm:vitest@2.1.1\n"
                              "deps-sync: 2 ok, 0 got, 0 failed\n")
        self.assertEqual(len(self.sync_events()), 2)

    def test_a_dupe_across_repos_is_fetched_once_and_credited_to_the_first_repo(self):
        self.add_repo("repo-c", "pip:httpx==0.27.2\n")
        _, out, _, f = self.run_main()
        self.assertEqual(sum(1 for c, _ in f.calls if c[0] == "pip"), 1)
        boards = [r["board"] for r in self.sync_events() if "httpx" in r["text"]]
        self.assertEqual(boards, ["repo-a"])

    def test_repo_without_an_allowlist_is_skipped(self):
        (self.t / "repo-d").mkdir()
        self.repos.append({"name": "repo-d", "path": str(self.t / "repo-d")})
        (self.t / "repos.json").write_text(json.dumps(self.repos))
        rc, out, _, _ = self.run_main()
        self.assertEqual(rc, 0)
        self.assertIn("2 got", out)

    def test_unreadable_repos_json_exits_2_and_fetches_nothing(self):
        (self.t / "repos.json").write_text("{{")
        rc, out, err, f = self.run_main()
        self.assertEqual((rc, out, f.calls), (2, "", []))
        self.assertIn("repos.json", err)

    def test_unreadable_allowlist_names_the_allowlist_not_the_registry(self):
        # followups: an unreadable deps-allowlist.txt was mislabelled "cannot read repos registry"
        bad = self.t / "repo-a" / "deps-allowlist.txt"
        bad.write_bytes(b"pip:httpx==0.27.2\n\xff\xfe\n")  # not UTF-8: the read itself fails
        rc, out, err, f = self.run_main()
        self.assertEqual((rc, out, f.calls), (2, "", []))
        self.assertIn(str(bad), err)
        self.assertIn("allowlist", err)
        self.assertNotIn("repos registry", err)

    def test_missing_repos_json_exits_2(self):
        (self.t / "repos.json").unlink()
        rc, out, err, f = self.run_main()
        self.assertEqual((rc, out, f.calls), (2, "", []))
        self.assertIn("repos.json", err)

    def test_repos_json_is_parsed_once_per_run(self):
        with unittest.mock.patch("json.load", side_effect=json.load) as spy:
            rc, _, _, _ = self.run_main()
        self.assertEqual(rc, 0)
        self.assertEqual(spy.call_count, 1)

    def test_events_failure_never_fails_the_sync(self):
        with unittest.mock.patch.object(deps_sync.events, "log", side_effect=RuntimeError("boom")):
            rc, out, err, _ = self.run_main()
        self.assertEqual(rc, 0)
        self.assertIn("2 got", out)
        self.assertEqual(err, "")

    def test_scoped_npm_name_reaches_npm_intact(self):
        self.add_repo("repo-b", "npm:@types/node@20.1.0\n")
        _, _, _, f = self.run_main()
        self.assertIn(["npm", "cache", "add", "@types/node@20.1.0"], [c for c, _ in f.calls])


class StdinAndRegistry(Base):
    def test_fetch_never_hands_the_run_its_own_stdin(self):
        # the shell's `while read ... <<EOF` loop gave pip/npm the heredoc as stdin: a pip that
        # read it swallowed the remaining specs and the run still exited 0
        _, _, _, f = self.run_main()
        self.assertEqual(len(f.kwargs), 2)
        for kw in f.kwargs:
            self.assertIs(kw.get("stdin"), subprocess.DEVNULL)

    def test_default_registry_is_the_repos_json_beside_the_script(self):
        os.environ.pop("AGENT_OPS_REPOS_JSON")  # setUp's patch.dict restores it afterwards
        maint = self.t / "maint"
        maint.mkdir()
        (maint / "repos.json").write_text(json.dumps([self.repos[0]]))
        with unittest.mock.patch.object(deps_sync, "MAINT_DIR", str(maint)):
            rc, out, _, _ = self.run_main()
        self.assertEqual(rc, 0)
        self.assertEqual(out, "GOT pip:httpx==0.27.2\ndeps-sync: 0 ok, 1 got, 0 failed\n")

    def test_an_empty_registry_variable_means_the_default_too(self):
        os.environ["AGENT_OPS_REPOS_JSON"] = ""
        maint = self.t / "maint"
        maint.mkdir()
        (maint / "repos.json").write_text("[]")
        with unittest.mock.patch.object(deps_sync, "MAINT_DIR", str(maint)):
            rc, out, _, _ = self.run_main()
        self.assertEqual((rc, out), (0, "deps-sync: 0 ok, 0 got, 0 failed\n"))

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root can write a 0444 file")
    def test_a_read_only_manifest_is_one_stderr_line_not_a_stop(self):
        (self.t / "repos.json").write_text(json.dumps([self.repos[0]]))
        self.cache.mkdir()
        manifest = self.cache / "synced.txt"
        manifest.write_text("")
        manifest.chmod(0o444)
        self.addCleanup(manifest.chmod, 0o644)
        rc, out, err, f = self.run_main()
        self.assertEqual(rc, 0)
        self.assertEqual(out, "GOT pip:httpx==0.27.2\ndeps-sync: 0 ok, 1 got, 0 failed\n")
        self.assertEqual(len(f.calls), 1)
        self.assertEqual(len(err.splitlines()), 1)
        self.assertTrue(err.startswith("deps_sync.sh: "))
        self.assertEqual(len(self.sync_events()), 1)  # the package still counts as synced


class CheckAndForce(Base):
    def test_check_on_an_empty_cache_lists_missing_exit_1_and_fetches_nothing(self):
        rc, out, _, f = self.run_main("--check")
        self.assertEqual(rc, 1)
        self.assertEqual(out, "MISSING pip:httpx==0.27.2\nMISSING npm:vitest@2.1.1\n"
                              "deps-sync --check: 2 missing\n")
        self.assertEqual(f.calls, [])
        self.assertEqual(self.sync_events(), [])

    def test_check_when_everything_is_synced_is_silent_exit_0(self):
        self.run_main()
        rc, out, _, f = self.run_main("--check")
        self.assertEqual((rc, out, f.calls), (0, "deps-sync --check: 0 missing\n", []))

    def test_manifest_line_without_its_artifact_is_missing_then_refetched_once(self):
        self.run_main()
        (self.cache / "pip" / "httpx-fake.whl").unlink()
        (self.cache / "npm" / ".marker").unlink()
        rc, out, _, _ = self.run_main("--check")
        self.assertEqual(rc, 1)
        self.assertIn("MISSING pip:httpx==0.27.2", out)
        self.assertIn("MISSING npm:vitest@2.1.1", out)
        rc, out, _, f = self.run_main()
        self.assertEqual((rc, len(f.calls)), (0, 2))
        # no duplicate lines
        self.assertEqual(self.manifest(), ["pip:httpx==0.27.2", "npm:vitest@2.1.1"])

    def test_manifest_match_is_a_whole_line_not_a_substring(self):
        self.run_main()
        longer = "pip:httpx==0.27.20\nnpm:vitest@2.1.10\n"  # the specs as substrings only
        (self.cache / "synced.txt").write_text(longer)
        rc, out, _, _ = self.run_main("--check")
        self.assertEqual(rc, 1)
        self.assertIn("2 missing", out)

    def test_force_refetches_everything_and_reports_got(self):
        self.run_main()
        rc, out, _, f = self.run_main("--force")
        self.assertEqual(rc, 0)
        self.assertEqual(len(f.calls), 2)
        self.assertIn("GOT pip:httpx==0.27.2", out)
        self.assertEqual(self.manifest(), ["pip:httpx==0.27.2", "npm:vitest@2.1.1"])

    def test_check_with_force_calls_everything_missing(self):
        self.run_main()
        rc, out, _, f = self.run_main("--check", "--force")
        self.assertEqual((rc, f.calls), (1, []))
        self.assertIn("2 missing", out)

    def test_check_still_creates_cache_dirs_and_empty_manifest(self):
        self.run_main("--check")
        self.assertTrue((self.cache / "pip").is_dir() and (self.cache / "npm").is_dir())
        self.assertEqual((self.cache / "synced.txt").read_text(), "")

    def test_check_on_an_unwritable_cache_location_still_reports(self):
        os.environ["AGENT_OPS_DEPS_CACHE"] = str(self.t / "file-not-dir" / "deps")
        (self.t / "file-not-dir").write_text("")
        rc, out, err, _ = self.run_main("--check")
        self.assertEqual(rc, 1)
        self.assertIn("2 missing", out)
        self.assertTrue(err)  # the mkdir failure is reported, not fatal


class FailuresAndRefusals(Base):
    def test_failed_fetch_is_FAIL_exit_1_no_manifest_line_no_event(self):
        rc, out, _, _ = self.run_main(fetch=FakeFetch(fail={"httpx==0.27.2"}))
        self.assertEqual(rc, 1)
        self.assertEqual(out, "FAIL pip:httpx==0.27.2\nGOT npm:vitest@2.1.1\n"
                              "deps-sync: 0 ok, 1 got, 1 failed\n")
        self.assertEqual(self.manifest(), ["npm:vitest@2.1.1"])
        self.assertEqual(len(self.sync_events()), 1)

    def test_pip_not_on_path_is_a_FAIL_not_a_crash(self):
        missing = unittest.mock.patch.object(deps_sync.subprocess, "run",
                                             side_effect=FileNotFoundError("pip"))
        with missing, contextlib.redirect_stdout(io.StringIO()) as out:
            rc = deps_sync.main([])
        self.assertEqual(rc, 1)
        self.assertIn("FAIL pip:httpx==0.27.2", out.getvalue())
        self.assertIn("0 ok, 0 got, 2 failed", out.getvalue())

    def test_bad_allowlist_line_refuses_the_whole_run_naming_repo_and_line(self):
        self.add_repo("repo-c", "pip:fine==1.0\npip:badpkg>=1.0.0\n")
        rc, out, err, f = self.run_main()
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")
        self.assertIn("repo-c/deps-allowlist.txt:2", err)
        self.assertEqual(f.calls, [])
        self.assertFalse((self.cache / "synced.txt").exists())  # refused before anything is touched

    def test_unreadable_allowlist_is_exit_2_with_a_message(self):
        (self.t / "repo-a" / "deps-allowlist.txt").write_bytes(b"\xff\xfe\x00bad")
        rc, out, err, f = self.run_main()
        self.assertEqual((rc, out, f.calls), (2, "", []))
        self.assertIn("deps_sync.sh:", err)

    def test_unknown_argument_is_exit_2_and_names_it(self):
        rc, out, err, f = self.run_main("--bogus")
        self.assertEqual((rc, out, f.calls), (2, "", []))
        self.assertEqual(err, "deps_sync.sh: unknown argument --bogus\n")

    def test_arguments_are_judged_in_order(self):
        rc, out, _, _ = self.run_main("-h", "--bogus")
        self.assertEqual(rc, 0)  # -h answered before the bogus argument is reached
        self.assertIn("Usage: deps_sync.sh [--check] [--force]", out)
        rc, _, err, _ = self.run_main("--check", "--bogus")
        self.assertEqual(rc, 2)

    def test_help_survives_python_dash_OO_which_strips_docstrings(self):
        r = subprocess.run([sys.executable, "-OO", str(MAINT / "deps_sync.py"), "-h"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Usage: deps_sync.sh [--check] [--force]", r.stdout)

    def test_help_shows_both_flags_and_the_exit_codes(self):
        _, out, _, _ = self.run_main("--help")
        for needle in ("--check", "--force", "Exit 1 if anything is missing", "exit 2"):
            self.assertIn(needle, out)


class WrapperAndPolicy(unittest.TestCase):
    def test_wrapper_is_a_thin_exec_of_the_python_module(self):
        sh = (MAINT / "deps_sync.sh").read_text()
        self.assertEqual(sh.splitlines()[0], "#!/usr/bin/env bash")
        self.assertIn('exec python3 "$MAINT_DIR/deps_sync.py" "$@"', sh)
        self.assertLessEqual(len(sh.splitlines()), 12)
        self.assertTrue(os.stat(MAINT / "deps_sync.sh").st_mode & stat.S_IXUSR)

    def test_wrapper_end_to_end_check_against_an_empty_registry(self):
        with tempfile.TemporaryDirectory() as td:
            Path(td, "repos.json").write_text("[]")
            env = dict(os.environ, AGENT_OPS_REPOS_JSON=f"{td}/repos.json",
                       AGENT_OPS_DEPS_CACHE=f"{td}/cache", EVENTS_SPOOL=f"{td}/ev.jsonl")
            r = subprocess.run(["bash", str(MAINT / "deps_sync.sh"), "--check"], env=env,
                               capture_output=True, text=True)
            self.assertEqual((r.returncode, r.stdout), (0, "deps-sync --check: 0 missing\n"))
            r = subprocess.run(["bash", str(MAINT / "deps_sync.sh"), "--nope"], env=env,
                               capture_output=True, text=True)
            self.assertEqual((r.returncode, r.stderr),
                             (2, "deps_sync.sh: unknown argument --nope\n"))

    def test_the_module_never_mentions_network_docker_or_a_registry_url(self):
        # the shell suite greps the .sh for these; the logic now lives here, so the grep follows it
        src = (MAINT / "deps_sync.py").read_text()
        self.assertNotIn("--network", src)
        self.assertNotIn("docker", src.lower())
        self.assertNotIn("http://", src)
        self.assertNotIn("https://", src)

    def test_the_only_subprocess_is_the_pip_or_npm_fetch(self):
        src = (MAINT / "deps_sync.py").read_text()
        self.assertEqual(src.count("subprocess.run("), 1)
        self.assertNotIn("shell=True", src)


if __name__ == "__main__":
    unittest.main()
