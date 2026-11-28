"""R28 (B1b): the pieces of the blind run that need no tick: the two-oracle parser, the docker
profile of the second container, and the validator bundle's report-only rows. The run itself is
in test_blind_run.py, the real tick in test_blind_run_tick.py."""
import contextlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capsule import bundles, validator_report, workspace
from capsule.blind import checkout, cli, preflight
from capsule.blind import run as blind_run
from capsule.runners import docker
from capsule.tests import tmpclean
from capsule.tests.test_blind_run import BOARD, TESTS, blind_repo, make_ws, sh

AGREE = "Ran {n} tests in 0.001s\n\n{status}\n"
MOUNTS = '[{"Mounts": [{"Source": "/w"}]}]'   # a clean container's `docker inspect`


def result_rows(*pairs):
    return "".join(json.dumps({"id": f"t_1.{name}", "result": res}) + "\n" for name, res in pairs)


class TwoOracleParser(unittest.TestCase):
    """`_parse(results.jsonl, runner stdout, exit code)` -> (rows, problem)."""

    def parse(self, rows, ran, status, code):
        return blind_run._parse(rows, AGREE.format(n=ran, status=status), code)[1]

    def test_agreeing_oracles_have_no_problem(self):
        ok = result_rows(("a", "pass"), ("b", "pass"))
        self.assertIsNone(self.parse(ok, 2, "OK", 0))
        bad = result_rows(("a", "pass"), ("b", "fail"))
        self.assertIsNone(self.parse(bad, 2, "FAILED (failures=1)", 1))

    def test_more_rows_than_the_summary_counted_is_a_mismatch(self):
        three = result_rows(("a", "pass"), ("b", "pass"), ("c", "pass"))
        self.assertIn("3 rows but the summary says: Ran 2", self.parse(three, 2, "OK", 0))

    def test_fewer_rows_than_the_summary_counted_is_a_mismatch(self):
        self.assertIn("1 rows but", self.parse(result_rows(("a", "pass")), 2, "OK", 0))

    def test_a_holder_row_is_allowed_only_when_it_is_not_a_pass(self):
        held = result_rows(("a", "pass"), ("setUpClass", "error"))
        self.assertIsNone(self.parse(held, 1, "FAILED (errors=1)", 1))
        extra_pass = result_rows(("a", "pass"), ("b", "pass"), ("c", "pass"), ("d", "fail"))
        self.assertIn("rows but", self.parse(extra_pass, 2, "FAILED (failures=1)", 1))

    def test_the_verdict_and_the_exit_code_must_match_the_rows(self):
        one_fail = result_rows(("a", "fail"))
        self.assertIn("disagree", self.parse(one_fail, 1, "OK", 0))
        self.assertIn("disagree", self.parse(one_fail, 1, "FAILED (failures=1)", 0))
        self.assertIn("disagree", self.parse(result_rows(("a", "pass")), 1,
                                             "FAILED (failures=1)", 1))
        self.assertIn("disagree", self.parse(result_rows(("a", "pass")), 1, "OK", 1))

    def test_skips_cannot_exceed_the_summary(self):
        skip = result_rows(("a", "skipped"))
        self.assertIsNone(self.parse(skip, 1, "OK (skipped=1)", 0))
        self.assertIn("skipped", self.parse(skip, 1, "OK", 0))

    def test_unreadable_oracles_are_a_problem_never_a_pass(self):
        self.assertIn("no usable results.jsonl",
                      blind_run._parse(None, "Ran 1 test in 1s\n\nOK", 0)[1])
        self.assertIn("no tests", blind_run._parse("", "Ran 0 tests in 0.0s\n\nOK", 0)[1])
        self.assertIn("not JSON", self.parse("{oops\n", 1, "OK", 0))
        self.assertIn("not a result row", self.parse('{"id": "a", "result": "meh"}\n', 1, "OK", 0))
        dup = result_rows(("a", "pass"), ("a", "pass"))
        self.assertIn("repeats", self.parse(dup, 2, "OK", 0))
        self.assertIn("no usable summary", blind_run._parse(
            result_rows(("a", "pass")), "boom", 2)[1])
        self.assertIn("no usable summary", blind_run._parse(
            "", "Ran 0 tests in 0.0s\n\nNO TESTS RAN", 0)[1])
        bad_obs = '{"id": "a", "result": "pass", "observed": {"cmd": "x", "exit": "1"}}\n'
        self.assertIn("not a result row", self.parse(bad_obs, 1, "OK", 0))

    def test_the_status_is_the_last_status_line_not_the_last_line(self):
        text = "Ran 1 test in 0.1s\n\nOK\nsome login banner\n"
        self.assertIsNone(blind_run._parse(result_rows(("a", "pass")), text, 0)[1])


class DockerProfile(unittest.TestCase):
    def args(self, ws="/w", **kw):
        return docker.run_args("ab" * 16, Path(ws), "img:tag", **kw)

    def volumes(self, argv):
        return [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]

    def test_the_default_profile_is_unchanged(self):
        """The argv a run (blind off) always had, pinned literally: only the uid and home vary."""
        deps = f"{Path.home()}/.local/share/capsule-corp/deps"
        self.assertEqual(self.args(), [
            "docker", "run", "-d", "--init",
            "--cap-drop", "ALL", "--cap-add", "DAC_OVERRIDE", "--cap-add", "CHOWN",
            "--cap-add", "FOWNER", "--security-opt", "no-new-privileges",
            "--tmpfs", "/tmp:rw,exec,nosuid,size=512m",
            "--tmpfs", "/var/tmp:rw,noexec,nosuid,size=256m",
            "--tmpfs", "/run:rw,noexec,nosuid,size=64m", "--network=none",
            "--cpus", "1", "--memory", "5120m", "--pids-limit", "256",
            "--user", f"{os.getuid()}:{os.getgid()}", "--label", "capsule=1",
            "--label", "capsule.kind=run", "--label", f"capsule-run-id={'ab' * 16}",
            "-v", "/w:/workspace", "-v", f"{deps}:/deps:ro",
            "-e", "TMPDIR=/workspace/.worker-exec", "-e", "HOME=/tmp",
            "--name", f"capsule-{'ab' * 6}", "img:tag", "sleep", "infinity"])

    def test_the_blind_profile_only_adds_ro_and_the_two_mounts(self):
        plain = self.args("/h/w", git_dir="/h/.gitdirs/w", tmpdir="/tmp")
        blind = self.args("/h/w", git_dir="/h/.gitdirs/w", tmpdir="/tmp", ro=True,
                          mounts=("/s/blind:/blind:ro", "/s/blind-out:/out"))
        self.assertIn("/h/w:/workspace:ro", self.volumes(blind))
        self.assertIn("/h/.gitdirs/w:/.gitdirs/w:ro", self.volumes(blind))   # not its host path
        self.assertEqual([v for v in self.volumes(blind) if v not in self.volumes(plain)],
                         ["/h/w:/workspace:ro", "/s/blind:/blind:ro", "/s/blind-out:/out"])
        strip = [a for a in blind if a not in self.volumes(blind)]
        for flag in docker.SECURITY_ARGS:                     # the network, caps, tmpfs: the same
            self.assertIn(flag, strip)
        self.assertEqual([a for a in strip if a != "-v"],
                         [a for a in plain if a not in self.volumes(plain) and a != "-v"])
        self.assertEqual([a for a in blind if a == "-e"], [a for a in plain if a == "-e"])

    @mock.patch.dict(os.environ, {"OPS_WORKER_IMAGE": "stub-image:1"})
    def test_strong_runner_start_passes_ro_and_mounts_through_the_one_door(self):
        seen = []
        runner = docker.StrongRunner(board="b")
        runner._run = lambda argv, **kw: seen.append(argv) or subprocess.CompletedProcess(
            argv, 0, MOUNTS)
        runner.start("ab" * 16, "/w", ro=True, mounts=("/s:/blind:ro",))
        self.assertIn("/w:/workspace:ro", self.volumes(seen[0]))
        self.assertIn("/s:/blind:ro", self.volumes(seen[0]))
        self.assertEqual([c[1] for c in seen], ["run", "exec", "inspect"])   # inspect: R33 audit


class ValidatorBundle(unittest.TestCase):
    TASK = {"id": "t_00000001", "title": "t", "body": "b", "status": "review"}

    def prompt(self, outputs):
        return bundles.validator(self.TASK, outputs)["prompt"]

    def test_blind_rows_are_marked_report_only_and_the_note_appears_once(self):
        row = {"cmd": "blind:t_1.x", "exit": 1, "output": "given: g", "blind": "report"}
        make = {"cmd": "make", "exit": 0, "output": "ok"}
        text = self.prompt([make, row, dict(row, cmd="blind:y")])
        self.assertEqual(text.count(bundles.BLIND_REPORT_NOTE), 1)
        self.assertEqual(text.count("  mode: report only"), 2)
        self.assertIn("- cmd: blind:t_1.x\n  exit: 1\n  mode: report only\n", text)
        self.assertIn("- cmd: make\n  exit: 0\n  output (last lines): ok", text)

    def test_without_blind_rows_the_bundle_is_what_it_was(self):
        text = self.prompt([{"cmd": "make", "exit": 0, "output": "ok"}])
        self.assertNotIn("report only", text)
        self.assertNotIn("blind:", text)


class ReportOnlyVerdict(unittest.TestCase):
    """`validator_report.not_run_violation` also refuses a card failed only on report-only rows."""
    ROW = {"cmd": "blind:t_1.x", "exit": 1, "output": "", "blind": "report"}
    MAKE = {"cmd": "make", "exit": 0, "output": "ok"}

    def doc(self, *fails):
        return {"verdict": "fail", "not_run": [], "assertions": [
            {"cmd": c, "exit": 1, "result": "fail"} for c in fails]}

    def test_a_fail_only_on_report_only_blind_rows_is_invalid(self):
        why = validator_report.not_run_violation(self.doc("blind:t_1.x"), [self.MAKE, self.ROW])
        self.assertIn("only on report-only blind row", why)

    def test_a_fail_that_also_names_a_real_failure_stands(self):
        doc = self.doc("blind:t_1.x", "make")
        self.assertIsNone(validator_report.not_run_violation(doc, [self.MAKE, self.ROW]))

    def test_a_pass_and_a_non_report_blind_row_are_untouched(self):
        passing = dict(self.doc(), verdict="pass")
        self.assertIsNone(validator_report.not_run_violation(passing, [self.MAKE, self.ROW]))
        gate_row = dict(self.ROW, blind="gate")        # R32's mode: failing on it is allowed
        self.assertIsNone(validator_report.not_run_violation(self.doc("blind:t_1.x"),
                                                             [self.MAKE, gate_row]))


class NeutralCheckout(unittest.TestCase):
    """`checkout.build(repo, commit, dest)`: the product checkout a blind test runs against, the
    same whether it is a red check or a real run. Z2 oracles: `git archive` of the source, and git
    and the file system read of the result, against literals pinned here."""

    TIME = 946684800                          # 2000-01-01T00:00:00Z
    FIXTURE_HEAD = "3a20bd8abcb23705bfecc8651901c4d11f2e4a49"   # `build`'s one commit of the tree

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(tmpclean.sweep, self.root)
        self.repo = self.root / "src"
        self.repo.mkdir()
        self.url = "https://token@example.test/secret/product.git"
        self.git("init", "-q", "-b", "main")
        self.git("remote", "add", "origin", self.url)
        self.commit = self.make_commit({"hello.txt": "hi\n", "bin/run.sh": "#!/bin/sh\n",
                                        "docs/a/b.md": "deep\n",
                                        ".gitattributes": "skipped.txt export-ignore\n",
                                        "skipped.txt": "no\n"})

    def git(self, *args, cwd=None):
        return sh("git", "-C", str(cwd or self.repo), *args)

    def make_commit(self, files, message="work", when="2026-11-01T10:00:00", only=False):
        """Commits `files` (and everything else changed, unless `only`)."""
        for rel, text in files.items():
            path = self.repo / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(text if isinstance(text, bytes) else text.encode())
            path.chmod(0o755 if rel.endswith(".sh") else 0o644)
        self.git("add", *(files if only else ["-A"]))
        env = {**os.environ, "GIT_COMMITTER_DATE": when, "GIT_AUTHOR_DATE": when}
        subprocess.run(["git", "-C", str(self.repo), *workspace.IDENTITY, "commit", "-q",
                        "--allow-empty", "-m", message], check=True, env=env, capture_output=True)
        return self.git("rev-parse", "HEAD").strip()

    def build(self, name="dest", commit=None):
        return checkout.build(self.repo, commit or self.commit, self.root / name)

    def listing(self, root):
        """(path, bytes or link target, executable) of every file and link outside `.git`."""
        return sorted((str(p.relative_to(root)),
                       os.readlink(p) if p.is_symlink() else p.read_bytes(),
                       not p.is_symlink() and bool(p.stat().st_mode & 0o100))
                      for p in Path(root).rglob("*") if (p.is_file() or p.is_symlink())
                      and p.relative_to(root).parts[0] != ".git")

    def test_it_returns_dest_with_exactly_the_files_of_the_tree_and_their_bytes_and_modes(self):
        (self.repo / "link").symlink_to("hello.txt")
        commit = self.make_commit({})
        dest = self.build(commit=commit)
        self.assertEqual(dest, self.root / "dest")
        want, tar = self.root / "archive", self.root / "a.tar"
        want.mkdir()
        self.git("archive", "--format=tar", "-o", str(tar), commit)
        subprocess.run(["tar", "-xf", str(tar), "-C", str(want)], check=True)
        names = {n: (t, x) for n, t, x in self.listing(dest)}
        # everything `git archive` has, plus what its attributes drop (`skipped.txt`)
        self.assertEqual(sorted(set(names) - {"skipped.txt"}),
                         sorted(n for n, _, _ in self.listing(want)))
        self.assertEqual(names["skipped.txt"], (b"no\n", False))
        self.assertEqual(names["bin/run.sh"], (b"#!/bin/sh\n", True))
        self.assertEqual(names["link"], ("hello.txt", False))
        self.assertEqual(self.git("status", "--porcelain", cwd=dest), "")

    def test_blobs_are_byte_for_byte_whatever_the_attributes_of_the_tree_say(self):
        raw = b"line1\nline2\r\n$Format:%H %cd$\n"
        files = {"sub.txt": raw, "keep.dat": b"kept\n", "a.bin": raw}
        self.make_commit(files)                     # no attributes yet: the blobs are raw
        attrs = b"*.txt export-subst\nkeep.dat export-ignore\n* text eol=crlf\n*.bin -text\n"
        commit = self.make_commit({".gitattributes": attrs}, only=True)     # no re-adding the rest
        dest = self.build(commit=commit)
        for rel, data in {**files, ".gitattributes": attrs}.items():
            blob = subprocess.run(["git", "-C", str(self.repo), "cat-file", "blob",
                                   f"{commit}:{rel}"], check=True, capture_output=True).stdout
            self.assertEqual((dest / rel).read_bytes(), blob, rel)
            self.assertEqual(blob, data, rel)
        self.assertIn(b"$Format:%H %cd$", (dest / "sub.txt").read_bytes())    # never expanded
        self.assertIn(b"\r\n", (dest / "sub.txt").read_bytes())               # no eol conversion

    def test_a_tree_sha_works_as_well_as_a_commit(self):
        tree = self.git("rev-parse", f"{self.commit}^{{tree}}").strip()
        self.assertEqual(self.listing(self.build("a", tree)), self.listing(self.build("b")))
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.root / "a"),
                         self.git("rev-parse", "HEAD", cwd=self.root / "b"))

    def test_a_link_in_the_tree_is_never_followed_when_modes_and_times_are_set(self):
        outside = self.root / "outside"
        outside.mkdir()
        outside.chmod(0o700)
        os.utime(outside, (1_000_000_000, 1_000_000_000))
        (self.repo / "x").symlink_to(outside)                       # to a folder
        (self.repo / "dead").symlink_to(self.root / "nowhere")      # to nothing
        dest = self.build(commit=self.make_commit({}))
        after = outside.stat()
        self.assertEqual((stat.S_IMODE(after.st_mode), after.st_mtime), (0o700, 1_000_000_000))
        self.assertEqual(os.readlink(dest / "x"), str(outside))
        self.assertEqual(os.lstat(dest / "x").st_mtime, self.TIME)  # the link has the fixed time

    def test_a_file_is_never_written_through_a_link_already_at_its_path(self):
        target = self.root / "victim.txt"
        target.write_text("keep")
        dest = self.root / "d"
        dest.mkdir()
        (dest / "f").symlink_to(target)                 # as a case collision on macOS would leave
        with self.assertRaises(FileExistsError):
            checkout._write_file(dest, checkout.FILE, b"f", b"overwritten")
        self.assertEqual(target.read_text(), "keep")

    def test_no_folder_is_made_through_a_link_in_dest_as_a_case_collision_would(self):
        outside = self.root / "outside"
        outside.mkdir()
        dest = self.root / "d"
        dest.mkdir()
        (dest / "Foo").symlink_to(outside)          # what `Foo` (a link) leaves on a macOS disk
        for path in (b"Foo/a/b", b"Foo/c"):
            with self.subTest(path), self.assertRaises(ValueError):
                checkout._write_file(dest, checkout.FILE, path, b"x")
        with self.assertRaises(ValueError):
            checkout._mkdirs(dest, "Foo/sub")
        self.assertEqual(list(outside.iterdir()), [])
        # the same through `build`: a link `Foo` and a file `foo/a/b` in one tree
        def blob(data):         # in the object store only: the disk here may fold the case
            done = subprocess.run(["git", "-C", str(self.repo), "hash-object", "-w", "--stdin"],
                                  input=data, capture_output=True, check=True)
            return done.stdout.decode().strip()

        link, deep = blob(str(outside).encode()), blob(b"x")
        self.git("update-index", "--add", "--cacheinfo", f"120000,{link},Foo")
        self.git("update-index", "--add", "--cacheinfo", f"100644,{deep},foo/a/b")
        tree = self.git("write-tree").strip()
        with contextlib.suppress(ValueError):       # a case-insensitive disk refuses; else fine
            self.build("viabuild", tree)
        self.assertEqual(list(outside.iterdir()), [])               # never outside, either way

    def test_folders_build_made_above_dest_go_with_a_failed_build_and_others_stay(self):
        deep = self.root / "p" / "q" / "dest"
        with self.assertRaises(Exception):
            checkout.build(self.repo, "no-such-ref", deep)
        self.assertFalse((self.root / "p").exists())                # made by build: gone
        (self.root / "kept").mkdir()
        with self.assertRaises(Exception):
            checkout.build(self.repo, "no-such-ref", self.root / "kept" / "new" / "dest")
        self.assertEqual(list((self.root / "kept").iterdir()), [])  # `kept` was there before
        ok = checkout.build(self.repo, self.commit, self.root / "p" / "q" / "dest")
        self.assertTrue((ok / ".git").is_dir())                     # a good build keeps them

    def test_a_blob_mode_of_100664_becomes_100644(self):
        blob = subprocess.run(["git", "-C", str(self.repo), "hash-object", "-w", "--stdin"],
                              input="odd\n", text=True, capture_output=True, check=True).stdout
        raw = b"100664 odd.txt\0" + bytes.fromhex(blob.strip())      # git itself writes 100644
        tree = subprocess.run(
            ["git", "-C", str(self.repo), "hash-object", "-t", "tree", "-w", "--literally",
             "--stdin"], input=raw, capture_output=True, check=True).stdout.decode().strip()
        stored = subprocess.run(["git", "-C", str(self.repo), "cat-file", "tree", tree],
                                capture_output=True, check=True).stdout
        self.assertTrue(stored.startswith(b"100664 odd.txt"))        # raw in the object
        dest = self.build(commit=tree)
        self.assertIn("100644 blob", self.git("ls-tree", "HEAD", "odd.txt", cwd=dest))
        self.assertEqual((dest / "odd.txt").read_text(), "odd\n")
        self.assertFalse((dest / "odd.txt").stat().st_mode & 0o111)

    def test_dest_is_a_repo_of_one_commit_on_one_branch_and_no_other_ref(self):
        dest = self.build()
        refs = self.git("for-each-ref", "--format=%(refname)", cwd=dest).split()
        self.assertEqual(refs, ["refs/heads/main"])
        self.assertEqual(self.git("rev-list", "--all", "--count", cwd=dest).strip(), "1")
        self.assertEqual(self.git("branch", "--show-current", cwd=dest).strip(), "main")
        self.assertEqual(self.git("remote", cwd=dest), "")
        self.assertEqual(self.git("stash", "list", cwd=dest), "")
        self.assertEqual(self.git("notes", "list", cwd=dest), "")
        self.assertEqual(self.git("status", "--porcelain", cwd=dest), "")      # a clean checkout

    def test_the_one_commit_has_the_fixed_identity_dates_and_message(self):
        dest = self.build()
        shown = self.git("log", "-1", "--date=raw", "--format=%an|%ae|%ad|%cn|%ce|%cd|%B", cwd=dest)
        self.assertEqual(shown.strip(),
                         "Capsule|capsule@localhost|946684800 +0000|Capsule|capsule@localhost|"
                         "946684800 +0000|Initial commit")

    def test_equal_trees_give_one_head_and_it_is_never_the_source_commit(self):
        other = self.make_commit({}, message="same tree, other commit", when="2026-11-05T00:00:00")
        self.assertNotEqual(other, self.commit)
        heads = [self.git("rev-parse", "HEAD", cwd=self.build(n, c)).strip()
                 for n, c in (("a", self.commit), ("b", other), ("c", self.commit))]
        self.assertEqual(set(heads), {self.FIXTURE_HEAD})
        self.assertNotIn(self.FIXTURE_HEAD, (self.commit, other))

    def test_a_different_tree_gives_a_different_head(self):
        later = self.make_commit({"new.txt": "x\n"})
        self.assertNotEqual(self.git("rev-parse", "HEAD", cwd=self.build("a")),
                            self.git("rev-parse", "HEAD", cwd=self.build("b", later)))

    def test_the_reflog_holds_at_most_the_one_commit_entry_and_no_stray_ref_files(self):
        dest = self.build()
        log = self.git("reflog", "--date=raw", "--format=%gn|%ge|%gd", cwd=dest).splitlines()
        self.assertLessEqual(len(log), 1)
        for line in log:
            self.assertEqual(line, "Capsule|capsule@localhost|HEAD@{946684800 +0000}")
        for name in ("FETCH_HEAD", "ORIG_HEAD", "MERGE_HEAD", "packed-refs"):
            self.assertFalse((dest / ".git" / name).exists(), name)

    def test_every_file_and_folder_outside_git_has_the_one_fixed_mtime(self):
        (self.repo / "link").symlink_to("hello.txt")
        later = self.make_commit({"x/y/z.txt": "z\n"})
        for name in ("a", "b"):
            dest = self.build(name, later)
            times = {p.lstat().st_mtime for p in [dest, *dest.rglob("*")]
                     if p == dest or p.relative_to(dest).parts[0] != ".git"}
            self.assertEqual(times, {self.TIME})

    def test_an_existing_dest_or_an_unknown_commit_raises_and_leaves_no_partial_dest(self):
        taken = self.root / "taken"
        taken.mkdir()
        (taken / "mine.txt").write_text("keep")
        with self.assertRaises(Exception):
            self.build("taken")
        self.assertEqual([p.name for p in taken.iterdir()], ["mine.txt"])
        for bad in ("0" * 40, "no-such-ref", "--upload-pack=x"):
            with self.subTest(bad), self.assertRaises(Exception):
                self.build("gone", bad)
            self.assertFalse((self.root / "gone").exists())
        self.assertEqual(sorted(p.name for p in self.root.iterdir() if p.name != "src"),
                         ["taken"])                             # no temp folder left beside dest

    def test_nothing_in_dest_names_the_repo_the_remote_or_the_source_commit(self):
        dest = self.build()
        blob = b"".join(p.read_bytes() for p in dest.rglob("*")
                        if p.is_file() and not p.is_symlink())
        for secret in (str(self.repo), self.url, "example.test", self.commit, self.commit[:12]):
            self.assertNotIn(secret.encode(), blob)

    def test_the_operators_git_config_and_environment_cannot_leak_in(self):
        home = self.root / "home"
        (home / "hooks").mkdir(parents=True)
        (home / ".gitconfig").write_text(f"[user]\n\tname = Leaky\n\temail = leak@example.test\n"
                                         f"[core]\n\thooksPath = {home}/hooks\n")
        env = {"HOME": str(home), "GIT_AUTHOR_NAME": "Env", "GIT_COMMITTER_EMAIL": "e@example.test",
               "GIT_AUTHOR_DATE": "2020-02-02T02:02:02",
               "GIT_CONFIG_GLOBAL": str(home / ".gitconfig")}
        with mock.patch.dict(os.environ, env):
            dest = self.build()
        shown = self.git("log", "-1", "--format=%an|%ae|%cn|%ce", cwd=dest)
        self.assertEqual(shown.strip(), "Capsule|capsule@localhost|Capsule|capsule@localhost")
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=dest).strip(), self.FIXTURE_HEAD)
        self.assertEqual(sorted((dest / ".git" / "hooks").glob("*")), [])


class WorktreeTree(unittest.TestCase):
    """`checkout.worktree_tree(ws)`: the tree of the card worktree as it is now, uncommitted work
    in. Z2 oracles: git's own `ls-tree` and `cat-file` of the returned sha, against the files on
    disk the test wrote."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(tmpclean.sweep, self.root)
        self.ws = make_ws(self.root)
        self.gd = workspace.gitdir(self.ws)
        for rel, data in (("keep.txt", b"keep\n"), ("gone.txt", b"gone\n"),
                          ("run.sh", b"#!/bin/sh\n"), (".gitignore", b"ignored.log\n"),
                          ("d/secret.txt", b"tracked\n")):
            (self.ws / rel).parent.mkdir(exist_ok=True)
            (self.ws / rel).write_bytes(data)
        (self.ws / "run.sh").chmod(0o755)
        sh("git", "-C", str(self.ws), "add", "-A")
        sh("git", "-C", str(self.ws), *workspace.IDENTITY, "commit", "-q", "-m", "more")
        (self.ws / "hello.txt").write_bytes(b"edited\n")                    # uncommitted edit
        (self.ws / "new").mkdir()
        (self.ws / "new" / "x.txt").write_bytes(b"a\r\nb\n")                  # untracked, CRLF
        (self.ws / "link").symlink_to("hello.txt")                          # untracked link
        (self.ws / "gone.txt").unlink()                                     # deletion
        (self.ws / "ignored.log").write_bytes(b"ignored\n")                  # ignored
        (self.ws / ".worker-exec").mkdir()
        (self.ws / ".worker-exec" / "scratch").write_bytes(b"scratch\n")     # never in the tree

    def ls(self, tree):
        out = sh("git", "--git-dir", str(self.gd), "ls-tree", "-r", tree)
        return {line.split("\t")[1]: line.split()[0] for line in out.splitlines()}

    def blob(self, tree, rel):
        return subprocess.run(["git", "--git-dir", str(self.gd), "cat-file", "blob",
                               f"{tree}:{rel}"], check=True, capture_output=True).stdout

    def test_the_tree_holds_the_files_as_they_are_now(self):
        tree = checkout.worktree_tree(self.ws)
        self.assertEqual(self.ls(tree), {
            ".gitignore": "100644", "d/secret.txt": "100644", "hello.txt": "100644",
            "keep.txt": "100644", "link": "120000", "new/x.txt": "100644", "run.sh": "100755"})
        self.assertEqual(self.blob(tree, "hello.txt"), b"edited\n")
        self.assertEqual(self.blob(tree, "new/x.txt"), b"a\r\nb\n")    # bytes on disk
        self.assertEqual(self.blob(tree, "link"), b"hello.txt")

    def test_the_worktrees_index_head_and_files_do_not_change(self):
        def state():
            files = sorted((str(p), p.lstat().st_mtime_ns) for p in self.ws.rglob("*"))
            return (workspace.head(self.ws), sh("git", "-C", str(self.ws), "status", "--porcelain"),
                    files)

        before = state()
        index = (self.gd / "index").read_bytes()        # after the status call's own refresh
        checkout.worktree_tree(self.ws)
        self.assertEqual((self.gd / "index").read_bytes(), index)
        self.assertEqual(state(), before)

    def test_the_index_copy_keeps_the_real_indexs_mtime_so_gits_racy_check_still_works(self):
        seen = []
        real = checkout._run

        def spy(env, where, *args, **kw):
            if args[:1] == ("add",):
                seen.append(os.stat(env["GIT_INDEX_FILE"]).st_mtime_ns)
            return real(env, where, *args, **kw)

        os.utime(self.gd / "index", ns=(1_700_000_000_123_456_789, 1_700_000_000_123_456_789))
        with mock.patch.object(checkout, "_run", spy):
            checkout.worktree_tree(self.ws)
        self.assertEqual(seen, [1_700_000_000_123_456_789])

    def test_a_folder_that_is_a_link_to_the_outside_gives_the_outside_nothing_to_the_tree(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_bytes(b"HOST SECRET\n")
        (outside / "other.txt").write_bytes(b"HOST OTHER\n")
        shutil.rmtree(self.ws / "d")
        (self.ws / "d").symlink_to(outside)         # `d/secret.txt` is tracked: now beyond a link
        tree = checkout.worktree_tree(self.ws)
        self.assertEqual(self.ls(tree)["d"], "120000")             # the link itself, as a link
        self.assertNotIn("d/secret.txt", self.ls(tree))
        self.assertEqual(self.blob(tree, "d"), str(outside).encode())
        objects = sh("git", "--git-dir", str(self.gd), "cat-file", "--batch-all-objects",
                     "--batch-check")
        for name in ("secret.txt", "other.txt"):
            host = subprocess.run(["git", "--git-dir", str(self.gd), "hash-object", "--stdin"],
                                  input=(outside / name).read_bytes(), capture_output=True,
                                  check=True).stdout.decode().strip()
            self.assertNotIn(host, objects)                         # never even hashed

    def test_a_path_with_a_newline_and_an_intent_to_add_file_are_in_the_tree(self):
        (self.ws / "two\nlines.txt").write_bytes(b"newline name\n")
        (self.ws / "later.txt").write_bytes(b"added with -N\n")
        sh("git", "-C", str(self.ws), "add", "-N", "later.txt")
        out = sh("git", "--git-dir", str(self.gd), "ls-tree", "-r", "-z", "--name-only",
                 checkout.worktree_tree(self.ws))
        names = out.split("\0")
        self.assertIn("two\nlines.txt", names)
        self.assertIn("later.txt", names)
        tree = checkout.worktree_tree(self.ws)
        self.assertEqual(self.blob(tree, "later.txt"), b"added with -N\n")
        self.assertEqual(self.blob(tree, "two\nlines.txt"), b"newline name\n")
        dest = checkout.build(self.gd, tree, self.root / "dest")
        self.assertEqual((dest / "two\nlines.txt").read_bytes(), b"newline name\n")

    def test_more_untracked_files_than_the_cap_raise_and_the_cap_itself_is_fine(self):
        for n in range(4):
            (self.ws / f"u{n}.txt").write_bytes(b"u\n")             # with new/x.txt and link: 6
        with mock.patch.object(checkout, "MAX_UNTRACKED", 5):
            with self.assertRaises(checkout.TooManyFiles) as caught:
                checkout.worktree_tree(self.ws)
        self.assertIn("6 untracked files", str(caught.exception))
        with mock.patch.object(checkout, "MAX_UNTRACKED", 6):
            self.assertIn("u3.txt", self.ls(checkout.worktree_tree(self.ws)))
        self.assertEqual(checkout.MAX_UNTRACKED, 5000)

    def test_an_unborn_branch_gives_the_tree_git_would_commit_and_changes_nothing(self):
        ws = self.root / "u" / "unborn"
        gd = workspace.gitdir(ws)
        gd.parent.mkdir(parents=True)
        sh("git", "init", "-q", "-b", "main", "--separate-git-dir", str(gd), str(ws))
        (ws / "a.txt").write_bytes(b"a\r\nb\n")
        (ws / "sub").mkdir()
        (ws / "sub" / "b.txt").write_bytes(b"b\n")
        (ws / "link").symlink_to("a.txt")
        (ws / ".worker-exec").mkdir()
        (ws / ".worker-exec" / "s").write_bytes(b"s\n")
        before = sh("git", "-C", str(ws), "status", "--porcelain=v2", "--branch")
        tree = checkout.worktree_tree(ws)
        out = sh("git", "--git-dir", str(gd), "ls-tree", "-r", "--name-only", tree)
        self.assertEqual(out.split(), ["a.txt", "link", "sub/b.txt"])
        self.assertEqual(sh("git", "-C", str(ws), "status", "--porcelain=v2", "--branch"), before)
        self.assertFalse((gd / "index").exists())
        self.assertEqual(list((gd / "refs" / "heads").glob("*")), [])

    def test_a_checkout_of_the_tree_is_the_worktree_without_history_or_scratch(self):
        dest = checkout.build(self.gd, checkout.worktree_tree(self.ws), self.root / "dest")
        found = {str(p.relative_to(dest)): p.read_bytes() for p in dest.rglob("*")
                 if p.is_file() and not p.is_symlink() and p.relative_to(dest).parts[0] != ".git"}
        self.assertEqual(found["hello.txt"], b"edited\n")
        self.assertEqual(found["new/x.txt"], b"a\r\nb\n")
        self.assertEqual(os.readlink(dest / "link"), "hello.txt")
        for gone in ("gone.txt", "ignored.log", ".worker-exec/scratch"):
            self.assertFalse((dest / gone).exists(), gone)
        self.assertEqual(sh("git", "-C", str(dest), "rev-list", "--all", "--count").strip(), "1")


class Harmless(unittest.TestCase):
    """The pieces the seam leans on keep their shape."""

    def test_the_preflight_still_asks_for_a_toml_and_this_file_reads_the_committed_run_line(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(tmpclean.sweep, root)
        repo = blind_repo(root, "t_00000001", TESTS)
        commit = sh("git", "-C", str(repo), "rev-parse", "HEAD").strip()
        line = blind_run._run_line(repo, commit, "t_00000001")
        self.assertEqual(line, cli.RUN_LINE.format(card="t_00000001"))
        self.assertEqual(preflight.toml_problems(repo / "blind.toml", BOARD, "unittest"), [])


if __name__ == "__main__":
    unittest.main()
