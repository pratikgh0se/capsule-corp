"""R33 mount audit (blind spec 4.4 items 1-3, Z1 rows in contracts/intent/mount_audit.jsonl): a
started container whose docker mount list holds blind material is stopped and removed, the run
does not proceed and an incident opens; an unreadable mount list is never clean; prior runs carry
no blind material. A fake docker command only; never real docker."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capsule import intent, prior_runs
from capsule.runners import docker, scoped


def setUpModule():   # the runner never falls back to another image, so name one (statement 3)
    patch = mock.patch.dict(os.environ, {"OPS_WORKER_IMAGE": "stub-image:1"})
    patch.start()
    unittest.addModuleCleanup(patch.stop)


RUN = "a" * 32
NAME = docker.container_name(RUN)
CHECK = docker.BLIND_CHECK
NOW = 1_790_000_000


def listing(*sources):
    return json.dumps([{"Id": "x", "Mounts": [{"Type": "bind", "Source": s} for s in sources]}])


class Fake:
    """A fake docker: `mounts` is what `inspect` prints (or an exception to raise), `blind_rc` what
    the /blind test returns (3 = found), `probe_rc` the mount probe's code."""

    def __init__(self, mounts, blind_rc=0, probe_rc=0, inspect_rc=0):
        self.mounts, self.blind_rc = mounts, blind_rc
        self.probe_rc, self.inspect_rc = probe_rc, inspect_rc
        self.calls, self.exists = [], False

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        verb, rc, out = argv[1], 0, ""
        if verb == "run":
            self.exists = True
        elif verb == "exec":
            rc = self.blind_rc if argv[-1] == docker.BLIND_TEST else self.probe_rc
        elif verb == "rm":
            self.exists = False
        elif verb == "inspect":
            if isinstance(self.mounts, Exception):
                raise self.mounts
            rc, out = (self.inspect_rc, self.mounts) if self.exists else (1, "")
        return subprocess.CompletedProcess(argv, rc, out, "")

    def verbs(self):
        return [c[1] for c in self.calls]

    def execs(self):
        return [c[-1] for c in self.calls if c[1] == "exec"]


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.repo = self.dir / "x-blind"
        self.spool = self.dir / "board" / "spool"
        (self.spool / "r1" / "blind-out").mkdir(parents=True)
        (self.spool / "r1" / "blind").mkdir()
        (self.spool / "r1" / "requests").mkdir()
        self.repo.mkdir()

    def runner(self, fake, repo=True, spool=True):
        r = docker.StrongRunner()
        r._run, r._sleep = fake, lambda seconds: None
        r.blind_repo = str(self.repo) if repo else None
        r.spool_root = str(self.spool) if spool else None
        return r

    def start(self, fake, requests=True, **kw):
        runner = self.runner(fake, **{k: kw.pop(k) for k in ("repo", "spool") if k in kw})
        return runner.start(RUN, str(self.dir / "w"), requests_dir="/q" if requests else None, **kw)

    def refused(self, source, **kw):
        """The RuntimeError start() raises for a container mounting `source`; the container is
        gone and the start was not retried."""
        fake = Fake(listing(str(self.dir / "w"), source))
        with self.assertRaises(RuntimeError) as caught:
            self.start(fake, **kw)
        self.assertEqual(fake.verbs().count("run"), 1)                  # never retried
        self.assertIn("stop", fake.verbs())
        self.assertFalse(fake.exists)
        return str(caught.exception)


class Denylist(Base):
    def test_a_real_hit_stops_the_run_removes_the_container_and_names_only_check_and_path(self):
        text = self.refused(str(self.repo))
        self.assertEqual(text, f"{CHECK}: {self.repo} [run {NAME}]")

    def test_the_repo_inside_outside_and_by_other_spellings_is_a_hit(self):
        for source in (str(self.repo / "cards"), str(self.repo.parent), str(self.repo).upper(),
                       "/host_mnt" + str(self.repo)):
            self.assertIn(CHECK, self.refused(source), source)

    def test_a_link_to_the_repo_is_a_hit(self):
        link = self.dir / "lnk"
        link.symlink_to(self.repo)
        self.assertIn(CHECK, self.refused(str(link)))

    def test_spool_blind_folders_a_run_folder_and_the_spool_root_are_hits(self):
        run = self.spool / "r1"
        for source in (run / "blind", run / "blind-out" / "results.jsonl", run, self.spool,
                       self.spool.parent):
            self.assertIn(CHECK, self.refused(str(source)), source)

    def test_a_board_with_no_blind_repo_still_checks_spool_folders(self):
        self.assertIn(CHECK, self.refused(str(self.spool / "r1" / "blind"), repo=False))
        blind_out = str(self.spool / "r1" / "blind-out")
        self.assertIn(CHECK, self.refused(blind_out, repo=False, spool=False))

    def test_an_unconfigured_runner_still_sees_a_spool_blind_path(self):
        blind = str(self.spool / "r1" / "blind")
        self.assertIn(CHECK, self.refused(blind, repo=False, spool=False))

    def test_the_requests_folder_and_ordinary_mounts_are_clean(self):
        fake = Fake(listing(str(self.dir / "w"), str(self.spool / "r1" / "requests"), "/deps"))
        self.assertEqual(self.start(fake), NAME)
        self.assertEqual(fake.verbs(), ["run", "exec", "inspect", "exec"])

    def test_every_role_container_is_audited_whatever_the_blind_switch(self):
        for requests in (True, False):                  # implementer / contract container
            self.refused(str(self.repo), requests=requests)

    def test_the_blind_runs_own_container_may_mount_its_own_snapshot_and_out_folder(self):
        own = (str(self.spool / "r1" / "blind"), str(self.spool / "r1" / "blind-out"))
        fake = Fake(listing(str(self.dir / "w"), *own))
        self.assertEqual(self.start(fake, requests=False, allow_blind=own), NAME)
        self.assertIn("inspect", fake.verbs())          # the audit still ran

    def test_the_blind_runs_own_container_is_caught_holding_anything_else_blind(self):
        own = (str(self.spool / "r1" / "blind"), str(self.spool / "r1" / "blind-out"))
        (self.spool / "r2" / "blind").mkdir(parents=True)
        for other in (str(self.spool / "r2" / "blind"), str(self.repo), str(self.spool)):
            fake = Fake(listing(str(self.dir / "w"), *own, other))
            with self.assertRaises(RuntimeError) as caught:
                self.start(fake, requests=False, allow_blind=own)
            self.assertIn(CHECK, str(caught.exception))
            self.assertFalse(fake.exists)

    def test_the_audit_runs_even_when_the_mount_probe_failed(self):
        fake = Fake(listing(str(self.repo)), probe_rc=1)
        with self.assertRaises(RuntimeError) as caught:
            self.start(fake)
        self.assertIn(CHECK, str(caught.exception))
        self.assertEqual(fake.verbs().count("run"), 1)


class Unreadable(Base):
    def test_an_unreadable_mount_list_is_never_clean_and_is_not_a_blind_hit(self):
        bad = ["", "not json", "[]", "null", "true", "{}", '[{"Id": "x"}]', '[{"Mounts": []}]',
               '[{"Mounts": null}]', '[{"Mounts": [{"Type": "bind"}]}]',
               '[{"Mounts": [{"Source": ""}]}]',
               '[{"Mounts": [{"Source": "/w"}]}, {"Mounts": [{"Source": "/w"}]}]', "[1]"]
        for text in bad:
            fake = Fake(text)
            with self.assertRaises(subprocess.CalledProcessError, msg=text) as caught:
                self.start(fake)
            self.assertIn("bind mounts missing", caught.exception.stderr)
            self.assertEqual(fake.verbs().count("run"), docker._START_TRIES, text)
            self.assertFalse(fake.exists)

    def test_a_failing_inspect_a_hung_inspect_and_a_dead_docker_are_not_clean(self):
        for fake in (Fake(listing("/w"), inspect_rc=1),
                     Fake(subprocess.TimeoutExpired("docker", 30)), Fake(OSError("gone"))):
            with self.assertRaises(subprocess.CalledProcessError) as caught:
                self.start(fake)
            self.assertNotIn(CHECK, str(caught.exception.stderr))   # a failed probe, not a hit


class TmpfsRows(Base):
    def test_a_tmpfs_row_without_a_source_is_skipped_not_unreadable(self):
        doc = [{"Mounts": [{"Type": "tmpfs", "Source": ""}, {"Type": "bind", "Source": "/w"}]}]
        self.assertEqual(self.start(Fake(json.dumps(doc))), NAME)

    def test_a_list_of_only_tmpfs_rows_is_still_unreadable(self):
        doc = [{"Mounts": [{"Type": "tmpfs", "Source": ""}]}]
        with self.assertRaises(subprocess.CalledProcessError):
            self.start(Fake(json.dumps(doc)))

    def test_a_blind_mount_beside_a_tmpfs_row_is_still_a_hit(self):
        doc = [{"Mounts": [{"Type": "tmpfs", "Source": ""},
                           {"Type": "bind", "Source": str(self.repo)}]}]
        self.assertIn(CHECK, self.refused_doc(json.dumps(doc)))

    def refused_doc(self, text):
        with self.assertRaises(RuntimeError) as caught:
            self.start(Fake(text))
        return str(caught.exception)


class InsideTheContainer(Base):
    def test_a_blind_folder_inside_the_implementer_container_raises_the_blind_error(self):
        fake = Fake(listing(str(self.dir / "w")), blind_rc=docker.BLIND_FOUND)
        with self.assertRaises(RuntimeError) as caught:
            self.start(fake)
        self.assertEqual(str(caught.exception), f"{CHECK}: /blind [run {NAME}]")
        self.assertEqual(fake.execs()[-1], "test ! -e /blind || exit 3")
        self.assertEqual(fake.verbs().count("run"), 1)
        self.assertFalse(fake.exists)

    def test_the_second_oracle_runs_for_the_implementer_only_and_after_the_mount_probe(self):
        fake = Fake(listing(str(self.dir / "w")))
        self.start(fake)
        self.assertEqual(len(fake.execs()), 2)
        self.assertIn("rev-parse", fake.execs()[0])
        fake = Fake(listing(str(self.dir / "w")))
        self.start(fake, requests=False)
        self.assertEqual(fake.execs().count(docker.BLIND_TEST), 0)

    def test_a_test_that_could_not_run_is_a_failed_probe_not_a_blind_hit(self):
        # `docker exec` exits 1 itself on a daemon or no-such-container error: not a hit
        for rc in (1, 2, 125, 126, 127):
            fake = Fake(listing(str(self.dir / "w")), blind_rc=rc)
            with self.assertRaises(subprocess.CalledProcessError, msg=rc):
                self.start(fake)


class Wiring(unittest.TestCase):
    def test_scoped_hands_each_runner_the_boards_blind_repo_and_spool_root(self):
        from types import SimpleNamespace
        cfg = {"board": "b", "blind_tests": {"repo": "/srv/x-blind", "adapter": "unittest"}}
        home = Path("/h")
        make = scoped(lambda tier, role: SimpleNamespace(), lambda: cfg, home)
        runner = make("strong", "implementer")
        self.assertEqual(Path(runner.blind_repo).name, "x-blind")
        self.assertEqual(runner.spool_root, home / "boards" / "b" / "spool")
        bare = scoped(lambda t, r: SimpleNamespace(), lambda: {"board": "b"}, home)("strong", "x")
        self.assertIsNone(bare.blind_repo)
        self.assertEqual(bare.spool_root, home / "boards" / "b" / "spool")

    def test_scoped_sets_blind_work_to_the_testers_clones_only_with_a_board(self):
        from types import SimpleNamespace
        home = Path("/h")
        runner = scoped(lambda t, r: SimpleNamespace(), lambda: {"board": "b"}, home)("strong", "x")
        self.assertEqual(runner.blind_work, home / "boards" / "b" / "blind-work")
        for cfg_of in (lambda: None, dict):
            bare = scoped(lambda t, r: SimpleNamespace(), cfg_of, home)("strong", "x")
            self.assertFalse(bare.blind_work)

    def test_the_blind_container_exempts_its_own_clone_and_git_dir(self):
        from types import SimpleNamespace

        from capsule import workspace
        from capsule.blind import run as blind_run
        seen = {}
        strong = SimpleNamespace(start=lambda *a, **kw: seen.update(kw) or "n", stop=lambda n: None)
        ws = Path("/h/boards/b/blind-work/r1")
        with blind_run.container(strong, "r1", ws, "/s/blind", "/s/out"):
            pass
        self.assertEqual(set(seen["allow_blind"]),
                         {"/s/blind", "/s/out", str(ws), str(workspace.gitdir(ws))})


class PriorRuns(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.root = self.dir / "spool"
        run = self.root / "r1"
        for sub in ("report", "done", "blind", "blind-out"):
            (run / sub).mkdir(parents=True)
        (run / "blind" / "t.py").write_text("secret")
        (run / "blind-out" / "results.jsonl").write_text("secret")
        (run / "report" / "1.json").write_text("{}")
        (run / "done" / "2.json").write_text("{}")
        (run / "blind_result.json").write_text("{}")
        (run / "report" / "3.json").symlink_to(run / "blind" / "t.py")
        self.repo = self.dir / "x-blind"
        (self.repo / "cards").mkdir(parents=True)
        (self.repo / "cards" / "a.py").write_text("secret")
        (run / "done" / "4.json").symlink_to(self.repo / "cards" / "a.py")
        (self.root / "r2").mkdir()
        self.conn = db_rows(("implementer", "r1"), ("blind-tester", "r2"))

    def test_paths_for_leaves_out_blind_folders_links_and_blind_tester_runs(self):
        out = prior_runs.paths_for(self.conn, self.root, "t", "zz")
        self.assertEqual([o["run_id"] for o in out], ["r1"])
        names = sorted(Path(p).name for p in out[0]["paths"][1:])
        self.assertEqual(names, ["1.json", "2.json"])

    def test_mount_never_mirrors_blind_material_even_from_a_hand_built_list(self):
        run = self.root / "r1"
        listed = [{"run_id": "r1", "role": "implementer", "started_at": 1,
                   "paths": [str(run), str(run / "blind" / "t.py"),
                             str(run / "blind-out" / "results.jsonl"),
                             str(run / "report" / "3.json"), str(run / "done" / "4.json"),
                             str(run / "report" / "1.json")]},
                  {"run_id": "r2", "role": "blind-tester", "started_at": 2,
                   "paths": [str(self.root / "r2")]}]
        dest = self.dir / "sp"
        prior_runs.mount({"prior_runs": listed}, dest, {})
        self.addCleanup(lambda: [p.chmod(0o755) for p in dest.rglob("*")])
        mirrored = sorted(str(p.relative_to(dest)) for p in dest.rglob("*")
                          if p.is_file() or p.is_symlink())
        self.assertEqual(mirrored, ["prior/r1/report/1.json"])
        self.assertFalse((dest / "prior" / "r2").exists())

    def test_the_shipped_rows_match_what_the_cases_observe(self):
        rows = {r["id"]: r for r in intent.load("mount_audit")}
        self.assertEqual(set(rows), {"blind-mount-stops-the-run",
                                     "unreadable-mount-list-is-never-clean",
                                     "prior-runs-carry-no-blind-material"})
        out = prior_runs.paths_for(self.conn, self.root, "t", "zz")
        copied = [Path(p).name for o in out for p in o["paths"][1:]]
        seen = {"mirrored_blind_paths": [n for n in copied if n in ("t.py", "results.jsonl")],
                "mirrored_links": [n for n in copied if n in ("3.json", "4.json")],
                "blind_tester_runs": sum(o["role"] == "blind-tester" for o in out)}
        for key, want in rows["prior-runs-carry-no-blind-material"]["expect"].items():
            self.assertEqual(seen[key], want, key)
        for row in rows.values():
            self.assertEqual(row["source"], "row 601")


class ValidatorRuns(unittest.TestCase):
    """A validator whose spool has a `blind-out/` carried the blind rows' output: only its
    directory reaches a rerun (`report/`, `done/`, `worker.log` stay out), whatever the switch."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.root = self.dir / "spool"
        for run in ("v1", "v2", "i1"):
            (self.root / run / "report").mkdir(parents=True)
            (self.root / run / "done").mkdir()
            (self.root / run / "report" / "1.json").write_text("{}")
            (self.root / run / "done" / "2.json").write_text("{}")
            (self.root / run / "worker.log").write_text("log")
        (self.root / "v1" / "blind-out").mkdir()     # the blind run used this validator's spool
        (self.root / "i1" / "blind-out").mkdir()     # the rule never touches an implementer
        self.conn = db_rows(("implementer", "i1"), ("validator", "v1"), ("validator", "v2"))

    def seen(self):
        return {o["run_id"]: sorted(str(Path(p).relative_to(self.root)) for p in o["paths"][1:])
                for o in prior_runs.paths_for(self.conn, self.root, "t", "zz", limit=5)}

    def test_a_validator_that_ran_blind_tests_contributes_only_its_directory(self):
        self.assertEqual(self.seen()["v1"], [])

    def test_a_validator_without_blind_output_and_the_other_roles_keep_their_paths(self):
        seen = self.seen()
        full = ["done/2.json", "report/1.json", "worker.log"]
        self.assertEqual(seen["v2"], [f"v2/{n}" for n in full])
        self.assertEqual(seen["i1"], [f"i1/{n}" for n in full])

    def test_mount_honours_the_same_rule_for_a_hand_built_list(self):
        run = self.root / "v1"
        listed = [{"run_id": "v1", "role": "validator", "started_at": 1,
                   "paths": [str(run), str(run / "report" / "1.json"), str(run / "done" / "2.json"),
                             str(run / "worker.log")]}]
        dest = self.dir / "sp"
        prior_runs.mount({"prior_runs": listed}, dest, {})
        self.addCleanup(lambda: [p.chmod(0o755) for p in dest.rglob("*")])
        self.assertEqual([p for p in dest.rglob("*") if p.is_file()], [])
        self.assertTrue((dest / "prior" / "v1").is_dir())


def db_rows(*runs):
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE task_runs(id INTEGER PRIMARY KEY, task_id, profile, run_id,"
                 " started_at)")
    conn.executemany("INSERT INTO task_runs(task_id, profile, run_id, started_at)"
                     " VALUES ('t',?,?,1)", runs)
    return conn


if __name__ == "__main__":
    unittest.main()
