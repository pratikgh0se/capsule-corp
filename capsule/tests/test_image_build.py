"""INFRA-10: `capsule image build` and `image which` (capsule/image_build.py), and the board-aware
`worker_image` the runner, preflight and run record share. Oracle 1: the argv a fake `docker`
script logs (AGENT_OPS_DOCKER_BIN always names it, never the real daemon) and the raw images.lock
text on disk. Oracle 2: the board's `image which` answer vs the image the runner itself starts,
both from `worker_image`. Z1: contracts/intent has no image_build file yet."""
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

from capsule import config, image_build, paths, preflight, run_record
from capsule.runners import docker

H1, H2, H3, H4 = ("a" * 64, "b" * 64, "c" * 64, "d" * 64)
BASE, PY = f"capsule-base@sha256:{H1}", f"capsule-python@sha256:{H2}"
OTHER = f"capsule-proj-other@sha256:{H3}"
NEW = f"sha256:{H4}"
FAKE = """#!/bin/sh
case "$1$2$5" in buildx*|imagels*|imageinspectcapsule-python@*) echo "$*" >> "$FAKE_LOG.pre";;
  *) printf '%s\\n' "$*" >> "$FAKE_LOG";; esac
case "$1$2" in imagels) echo "$FAKE_LOCAL_ID"; exit 0;; esac
case "$1" in
build) for last; do :; done; ls -A "$last" > "$FAKE_LOG.ctx"
  cat > "$FAKE_LOG.dockerfile"; [ "$FAKE_FAIL" = build ] && exit 1;;
image) case "$5" in capsule-python@*) echo "${FAKE_PYTHON_OUT:-sha256:@H2@ capsule-python:local}"
  [ -n "$FAKE_PYTHON_ABSENT" ] && exit 1;;
  *) echo "${FAKE_DIGEST:-sha256:@H4@}"; [ "$FAKE_FAIL" = inspect ] && exit 1;; esac;;
buildx) [ -n "$FAKE_NO_BUILDX" ] && exit 1;;
run) [ -n "$FAKE_BAD_LINE" ] && case "$*" in *"$FAKE_BAD_LINE"*) exit 1;; esac;;
esac
exit 0
""".replace("@H4@", H4).replace("@H2@", H2)
MANIFEST = '[image]\nstacks = ["python"]\nsmoke = ["python -V", "true"]\n'


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home, self.project, self.bin = (self.tmp / n for n in ("home", "proj", "bin"))
        for d in (self.home, self.project, self.bin):
            d.mkdir()
        (self.bin / "docker").write_text(FAKE)
        (self.bin / "docker").chmod(0o755)
        self.log = self.tmp / "log"
        self.record = self.home / "images.lock"
        self.record.write_text(f"{BASE}\n{OTHER}\n{PY}\n")
        self.lockdir = Path(f"{self.record}.lockdir")
        (self.project / "capsule.toml").write_text(MANIFEST)
        env = {"AGENT_OPS_DOCKER_BIN": str(self.bin), "FAKE_LOG": str(self.log),
               "CAPSULE_HOME": str(self.home), "OPS_ENV_FILE": str(self.tmp / "no.env")}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("OPS_WORKER_IMAGE", None)

    def write_config(self, text, board="web1"):
        d = self.home / "boards" / board
        d.mkdir(parents=True, exist_ok=True)
        (d / "config.json").write_text(text)

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def run_main(self, cmd, *more, board="web1"):
        args = Namespace(image_cmd=cmd, board=board, capsule_home=None,
                         project=self.project, dry_run="--dry-run" in more)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = image_build.main(args)
        return rc, out.getvalue(), err.getvalue()


class Build(Base):
    def test_success_runs_build_inspect_smoke_and_records_one_row(self):
        rc, out, err = self.run_main("build")
        row = f"capsule-proj-web1@{NEW}"
        self.assertEqual((rc, out.strip(), err), (0, row, ""))
        self.assertEqual(self.record.read_text(), f"{BASE}\n{OTHER}\n{PY}\n{row}\n")
        calls = self.calls()
        self.assertRegex(calls[0], r"^build .*-t capsule-proj-web1:local -f - "
                                   r"\S+capsule-image-ctx-\w+$")
        self.assertNotIn(str(self.project), calls[0])
        self.assertEqual(calls[1], "image inspect --format {{.Id}} capsule-proj-web1:local")
        self.assertIn("--provenance=false", calls[0].split())
        self.assertIn("--sbom=false", calls[0].split())
        self.assertEqual(len(calls), 4)
        for call, line in zip(calls[2:], ("python -V", "true")):
            self.assertRegex(call, rf"^run --rm --network=none .* {row} sh -c {line}$")
        self.assertFalse(any("--network=host" in c for c in calls))
        self.assertIn(f"FROM {PY}", Path(f"{self.log}.dockerfile").read_text())

    def keep_run(self, local_id=NEW, fail_tag=False):
        env = {"FAKE_LOCAL_ID": local_id}
        if fail_tag:
            (self.bin / "docker").write_text(FAKE.replace(
                'case "$1" in\nbuild', 'case "$1" in\ntag) exit 1;;\nbuild', 1))
        with mock.patch.dict(os.environ, env):
            return self.run_main("build")

    def test_an_existing_local_image_is_keep_tagged_before_the_build(self):
        rc, _, err = self.keep_run()
        self.assertEqual(rc, 0, err)
        calls = self.calls()
        self.assertEqual(calls[0], f"tag {NEW} capsule-proj-web1:prev-{H4[:12]}")
        self.assertTrue(calls[1].startswith("build "))

    def test_a_rerun_keeps_the_same_tag_and_an_absent_local_gets_none(self):
        self.assertEqual(self.keep_run()[0], 0)
        self.assertEqual(self.keep_run()[0], 0)
        self.assertEqual(sum(c.startswith("tag ") for c in self.calls()), 2)
        self.assertEqual({c for c in self.calls() if c.startswith("tag ")},
                         {f"tag {NEW} capsule-proj-web1:prev-{H4[:12]}"})
        self.log.unlink()
        self.assertEqual(self.keep_run(local_id="")[0], 0)
        self.assertFalse(any(c.startswith("tag ") for c in self.calls()))

    def test_a_failed_keep_tag_stops_the_build_and_records_nothing(self):
        before = self.record.read_text()
        rc, out, err = self.keep_run(fail_tag=True)
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("could not keep-tag capsule-proj-web1:local", err)
        self.assertFalse(any(c.startswith("build ") for c in self.calls()))
        self.assertEqual(self.record.read_text(), before)

    def test_a_failed_lookup_stops_the_build_and_records_nothing(self):
        (self.bin / "docker").write_text(FAKE.replace(
            'case "$1$2" in imagels) echo', 'case "$1$2" in imagels) exit 3;; imagexx) echo', 1))
        before = self.record.read_text()
        rc, out, err = self.run_main("build")
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("could not look up capsule-proj-web1:local", err)
        self.assertFalse(any(c.startswith(("build ", "tag ")) for c in self.calls()))
        self.assertEqual(self.record.read_text(), before)

    def test_dry_run_runs_no_keep_tag(self):
        with mock.patch.dict(os.environ, {"FAKE_LOCAL_ID": NEW}):
            self.assertEqual(self.run_main("build", "--dry-run")[0], 0)
        self.assertEqual(self.calls(), [])

    def test_named_context_maps_the_pinned_from_to_the_local_tag(self):
        rc, _, _ = self.run_main("build")
        self.assertEqual(rc, 0)
        self.assertIn(f"--build-context capsule-python@sha256:{H2}="
                      "docker-image://capsule-python:local", self.calls()[0])

    def test_no_buildx_passes_no_context(self):
        with mock.patch.dict(os.environ, {"FAKE_NO_BUILDX": "1"}):
            rc, _, _ = self.run_main("build")
        self.assertEqual(rc, 0)
        self.assertNotIn("--build-context", self.calls()[0])

    def test_local_tag_that_is_not_the_pinned_digest_is_refused_before_build(self):
        with mock.patch.dict(os.environ, {"FAKE_PYTHON_ABSENT": "1"}):
            rc, out, err = self.run_main("build")
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("capsule-python@sha256:", err)
        self.assertIn("docker/build-base.sh python", err)
        self.assertEqual(self.calls(), [])

    def test_the_context_follows_any_tag_of_the_pinned_digest(self):
        ctx = f"--build-context capsule-python@sha256:{H2}=docker-image://"
        cases = (("sha256:%s capsule-python:old,capsule-python:local" % H2, "capsule-python:local"),
                 ("sha256:%s capsule-python:old" % H2, "capsule-python:old"))
        for answer, tag in cases:
            with self.subTest(tag=tag), mock.patch.dict(os.environ, {"FAKE_PYTHON_OUT": answer}):
                self.assertEqual(self.run_main("build")[0], 0)
                self.assertIn(ctx + tag, self.calls()[0])
                self.log.unlink()
                self.record.write_text(f"{BASE}\n{OTHER}\n{PY}\n")

    def test_an_untagged_pinned_image_gets_no_context_and_a_note(self):
        with mock.patch.dict(os.environ, {"FAKE_PYTHON_OUT": f"sha256:{H2}"}):
            rc, _, err = self.run_main("build")
        self.assertEqual(rc, 0, err)
        self.assertIn("has no local tag", err)
        self.assertNotIn("--build-context", self.calls()[0])

    def test_a_pinned_from_the_mapping_cannot_read_is_refused_loudly(self):
        for line in (f"from capsule-python@sha256:{H2}",
                     f"FROM capsule-python@sha256:{H2} AS base"):
            with self.subTest(line=line):
                args, problem = image_build._contexts(f"{line}\nRUN true\n")
                self.assertEqual(args, [])
                self.assertIn("alone on its line", problem)
        self.assertEqual(image_build._contexts("FROM debian:13\n"), ([], ""))

    def test_dry_run_runs_no_docker_and_says_what_a_real_build_adds(self):
        rc, out, err = self.run_main("build", "--dry-run")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("--build-context capsule-python@", out.splitlines()[0])
        self.assertIn(f"(a real build adds --build-context capsule-python@sha256:{H2}="
                      "docker-image://<local tag> after checking the image is local)", out)
        self.assertEqual(self.calls(), [])
        self.assertFalse(Path(f"{self.log}.pre").exists())   # not even buildx or the digest inspect

    def test_which_falls_back_to_the_legacy_single_board_config(self):
        self.record.write_text(f"{BASE}\n{PY}\ncapsule-proj-web1@{NEW}\n")
        (self.home / "config.json").write_text(json.dumps(
            {"board": "web1", "redesign": {"image_loop": config.IMAGE_RECORD}}))
        rc, out, _ = self.run_main("which")
        self.assertEqual((rc, out.strip()), (0, f"capsule-proj-web1@{NEW}"))

    def context(self):
        return Path(f"{self.log}.ctx").read_text().split()

    def lock_project(self, name="requirements.lock"):
        (self.project / "capsule.toml").write_text(
            f'[image]\nstacks = ["python"]\nlockfiles = ["{name}"]\nsmoke = ["true"]\n')
        (self.project / name).write_text(f"six==1.16.0 --hash=sha256:{'0' * 64}\n")

    def test_context_has_only_what_the_recipe_copies_never_secrets(self):
        self.lock_project()
        (self.project / ".env").write_text("TOKEN=x")
        (self.project / "id_rsa").write_text("key")
        (self.project / "server.pem").write_text("pem")
        (self.project / ".git").mkdir()
        self.assertEqual(self.run_main("build")[0], 0)
        self.assertEqual(self.context(), ["requirements.lock"])

    def test_no_copy_recipe_sends_an_empty_context(self):
        (self.project / ".env").write_text("TOKEN=x")
        self.assertEqual(self.run_main("build")[0], 0)
        self.assertEqual(self.context(), [])

    def test_recipe_copying_a_secret_is_refused_before_docker(self):
        self.lock_project(".env")
        rc, _, err = self.run_main("build")
        self.assertEqual(rc, 1)
        self.assertIn("refused", err)
        self.assertEqual(self.calls(), [])
        text = "FROM x\nCOPY id_rsa /tmp/k\n"
        with tempfile.TemporaryDirectory() as dest:
            (self.project / "id_rsa").write_text("key")
            with self.assertRaises(ValueError):
                image_build._stage(self.project, text, dest)
            self.assertEqual(os.listdir(dest), [])

    def stage(self, text):
        with tempfile.TemporaryDirectory() as dest:
            image_build._stage(self.project, text, dest)
            return sorted(os.listdir(dest))

    def test_stage_refuses_symlink_and_hard_link(self):
        (self.project / "real.txt").write_text("x")
        (self.project / "sym.txt").symlink_to(self.project / "real.txt")
        os.link(self.project / "real.txt", self.project / "hard.txt")
        for name in ("sym.txt", "hard.txt"):
            with self.assertRaises(ValueError):
                self.stage(f"COPY {name} /tmp/{name}\n")

    def test_stage_web_copies_both_npm_files(self):
        for name in ("package.json", "package-lock.json"):
            (self.project / name).write_text("{}")
        self.assertEqual(self.stage("COPY package.json package-lock.json /opt/p/\n"),
                         ["package-lock.json", "package.json"])

    def test_stage_refuses_add_and_lowercase_copy(self):
        (self.project / "a.txt").write_text("x")
        for line in ("ADD a.txt /tmp/a", "copy a.txt /tmp/a"):
            with self.subTest(line=line), self.assertRaises(ValueError):
                self.stage(f"FROM x\n{line}\n")

    def test_dry_run_refuses_what_the_build_refuses(self):
        self.lock_project("requirements-secret.lock")
        rc, out, err = self.run_main("build", "--dry-run")
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("refused", err)

    def test_rebuild_replaces_only_this_boards_row(self):
        self.record.write_text(f"{BASE}\ncapsule-proj-web1@sha256:{H3}\n{OTHER}\n{PY}")
        self.assertEqual(self.run_main("build")[0], 0)
        self.assertEqual(self.record.read_text(),
                         f"{BASE}\n{OTHER}\n{PY}\ncapsule-proj-web1@{NEW}\n")

    def test_record_file_is_created_when_missing(self):
        path = self.tmp / "fresh" / "x" / "images.lock"
        image_build._record(path, "capsule-proj-web1", f"capsule-proj-web1@{NEW}")
        self.assertEqual(path.read_text(), f"capsule-proj-web1@{NEW}\n")
        self.assertFalse(Path(f"{path}.lockdir").exists())

    def test_no_record_says_to_run_build_base(self):
        self.record.unlink()
        rc, out, err = self.run_main("build")
        self.assertEqual((rc, out, self.calls()), (1, "", []))
        self.assertIn("docker/build-base.sh python first", err)
        self.assertNotIn("Errno", err)

    def test_lockdir_held_by_someone_else_waits_then_fails_cleanly(self):
        self.lockdir.mkdir()
        before = self.record.read_text()
        with mock.patch.multiple(image_build, LOCK_TRIES=3, LOCK_WAIT=0.01):
            rc, out, err = self.run_main("build")
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("still held after 2 minutes", err)
        self.assertTrue(self.lockdir.is_dir())        # never taken over, never removed
        self.assertEqual(self.record.read_text(), before)

    def test_lockdir_released_after_another_writer_finishes(self):
        self.lockdir.mkdir()
        sleeps = []

        def release(wait):
            sleeps.append(wait)
            self.lockdir.rmdir()
        with mock.patch.object(image_build.time, "sleep", side_effect=release):
            image_build._record(self.record, "capsule-proj-web1", f"capsule-proj-web1@{NEW}")
        self.assertEqual(len(sleeps), 1)
        self.assertFalse(self.lockdir.exists())
        self.assertIn(f"capsule-proj-web1@{NEW}\n", self.record.read_text())

    def test_write_error_is_one_line_exit_1_and_cleans_up(self):
        before = self.record.read_text()
        with mock.patch.object(image_build.os, "replace", side_effect=OSError("disk full")):
            rc, out, err = self.run_main("build")
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("cannot record the image", err)
        self.assertIn("disk full", err)
        self.assertEqual(self.record.read_text(), before)
        self.assertEqual(sorted(p.name for p in self.home.iterdir() if "tmp" in p.name), [])
        self.assertFalse(self.lockdir.exists())

    def test_board_names_docker_rejects_exit_2_without_docker(self):
        for board in ("MyApp.", "a.", "a._b", "A-", "a___b"):
            rc, out, err = self.run_main("build", board=board)
            self.assertEqual((rc, out), (2, ""), board)
            self.assertIn("not a docker repository name", err)
        self.assertEqual(self.calls(), [])

    def test_uppercase_board_builds_a_lowercase_row_which_finds_it(self):
        rc, out, _ = self.run_main("build", board="MyApp")
        row = f"capsule-proj-myapp@{NEW}"
        self.assertEqual((rc, out.strip()), (0, row))
        calls = self.calls()
        self.assertIn("-t capsule-proj-myapp:local", calls[0])
        self.assertTrue(all("MyApp" not in c for c in calls))
        self.assertEqual(self.record.read_text().splitlines()[-1], row)
        self.write_config(json.dumps({"redesign": {"image_loop": "record"}}), "MyApp")
        self.assertEqual(self.run_main("which", board="MyApp")[:2], (0, row + "\n"))
        self.assertEqual(docker.project_image_name("MyApp"), "capsule-proj-myapp")

    def test_board_names_docker_accepts_build(self):
        for board in ("a", "a-b", "a_b", "a.b", "a__b", "a--b", "web1"):
            self.assertEqual(self.run_main("build", board=board)[0], 0, board)

    def test_rebuild_drops_an_indented_old_row_too(self):
        self.record.write_text(f"{BASE}\n  capsule-proj-web1@sha256:{H3}\n{PY}\n")
        self.assertEqual(self.run_main("build")[0], 0)
        self.assertEqual(self.record.read_text(), f"{BASE}\n{PY}\ncapsule-proj-web1@{NEW}\n")

    def test_refused_manifest_runs_no_docker_and_keeps_record(self):
        before = self.record.read_text()
        for toml in ('[image]\nstacks = ["python"]\napt = ["a b"]\n', '[other]\nx = 1\n', None):
            if toml is None:
                (self.project / "capsule.toml").unlink()
            else:
                (self.project / "capsule.toml").write_text(toml)
            rc, out, err = self.run_main("build")
            self.assertEqual((rc, out), (1, ""))
            self.assertIn("refused" if toml and "apt" in toml else "capsule image:", err)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.record.read_text(), before)

    def test_missing_stack_row_in_record_is_refused(self):
        self.record.write_text(f"{BASE}\n")
        rc, _, err = self.run_main("build")
        self.assertEqual(rc, 1)
        self.assertIn("capsule-python", err)
        self.assertEqual(self.calls(), [])

    def test_failed_build_runs_no_smoke_and_keeps_record(self):
        before = self.record.read_text()
        with mock.patch.dict(os.environ, {"FAKE_FAIL": "build"}):
            rc, out, err = self.run_main("build")
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("docker build failed", err)
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(self.record.read_text(), before)

    def test_bad_digest_fails_with_record_unchanged(self):
        before = self.record.read_text()
        for digest, fail in (("sha256:abc", ""), ("latest", ""), (NEW, "inspect")):
            with mock.patch.dict(os.environ, {"FAKE_DIGEST": digest, "FAKE_FAIL": fail}):
                rc, out, err = self.run_main("build")
            self.assertEqual((rc, out), (1, ""))
            self.assertIn("bad image digest", err)
        self.assertEqual(self.record.read_text(), before)
        self.assertFalse(any(c.startswith("run ") for c in self.calls()))

    def test_failing_smoke_line_is_named_and_record_unchanged(self):
        before = self.record.read_text()
        with mock.patch.dict(os.environ, {"FAKE_BAD_LINE": "true"}):
            rc, out, err = self.run_main("build")
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("smoke line failed: 'true'", err)
        self.assertEqual(self.record.read_text(), before)

    def test_dry_run_prints_plan_and_runs_and_writes_nothing(self):
        before = self.record.read_text()
        rc, out, err = self.run_main("build", "--dry-run")
        self.assertEqual((rc, err), (0, ""))
        lines = out.splitlines()
        self.assertEqual(len(lines), 6)
        self.assertTrue(lines[0].startswith("docker build "))
        self.assertIn("docker image inspect --format '{{.Id}}' capsule-proj-web1:local", lines[1])
        self.assertIn("--network=none", lines[2])
        self.assertIn("(a real build adds --build-context", lines[4])
        self.assertIn("would record capsule-proj-web1@sha256:<digest>", lines[5])
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.record.read_text(), before)

    def test_bad_board_name_is_exit_2_before_docker(self):
        for name in ("new", "a/b", "..", ""):
            rc, out, err = self.run_main("build", board=name)
            self.assertEqual((rc, out), (2, ""))
            self.assertIn("board name", err)
        self.assertEqual(self.calls(), [])

    def test_docker_failures_to_run_are_failures(self):
        for exc in (subprocess.TimeoutExpired("docker", 1), FileNotFoundError("docker")):
            with mock.patch.object(image_build.docker_door, "docker_run", side_effect=exc):
                rc, _, err = self.run_main("build")
            self.assertEqual(rc, 1)
            self.assertIn("could not look up", err)   # the keep-tag probe is the first step

    def test_a_docker_file_in_the_override_is_used_as_is(self):
        with mock.patch.dict(os.environ, {"AGENT_OPS_DOCKER_BIN": str(self.bin / "docker")}):
            rc, out, _ = self.run_main("build")
        self.assertEqual((rc, out.strip()), (0, f"capsule-proj-web1@{NEW}"))

    def test_timeouts_per_call(self):
        seen = []

        def spy(args, **kw):
            if args[0] == "buildx" or f"capsule-python@sha256:{H2}" in args:   # context preflight
                return subprocess.CompletedProcess(args, 0, stdout=f"sha256:{H2} x:1", stderr="")
            seen.append((args[0], kw["timeout"]))
            return subprocess.CompletedProcess(args, 0, stdout=NEW, stderr="")
        with mock.patch.object(image_build.docker_door, "docker_run", side_effect=spy):
            self.run_main("build")
        self.assertEqual(seen, [("image", 60), ("tag", 60), ("build", 1200), ("image", 60),
                                ("run", 300), ("run", 300)])   # keep-tag probe and tag first


class Which(Base):
    def config(self, loop="record", board="web1"):
        self.write_config(json.dumps({"redesign": {"image_loop": loop}}), board)

    def test_project_row_then_python_row_then_nothing(self):
        self.config()
        self.record.write_text(f"{BASE}\n{PY}\ncapsule-proj-web1@sha256:{H3}\n")
        self.assertEqual(self.run_main("which")[:2], (0, f"capsule-proj-web1@sha256:{H3}\n"))
        self.record.write_text(f"{BASE}\n{PY}\n")
        self.assertEqual(self.run_main("which")[:2], (0, PY + "\n"))
        self.record.write_text(f"{BASE}\n")
        rc, out, err = self.run_main("which")
        self.assertEqual((rc, out), (1, "\n"))
        self.assertIn("image build --project DIR --board web1", err)

    def test_config_that_is_not_an_object_is_exit_2_not_a_traceback(self):
        for text in ("[1]", '{"redesign": 5}', '"x"', "7"):
            self.write_config(text)
            rc, out, err = self.run_main("which")
            self.assertEqual((rc, out), (2, ""), text)
            self.assertIn("no usable config", err)

    def test_similar_board_names_do_not_collide(self):
        rows = {name: f"capsule-proj-{name}@sha256:{h}"
                for name, h in (("a", H3), ("a-b", H4), ("a_b", H1), ("a.b", H2))}
        for name in rows:
            self.config(board=name)
        for name, row in rows.items():          # each board gets only its own row
            self.record.write_text(f"{PY}\n" + "".join(f"{r}\n" for r in rows.values()))
            self.assertEqual(self.run_main("which", board=name)[:2], (0, row + "\n"))
        for name in rows:                       # and a board with no row falls to python
            others = [r for n, r in rows.items() if n != name]
            self.record.write_text(f"{PY}\n" + "".join(f"{r}\n" for r in others))
            self.assertEqual(self.run_main("which", board=name)[:2], (0, PY + "\n"))

    def test_override_beats_everything(self):
        self.config()
        self.record.write_text(f"{PY}\ncapsule-proj-web1@sha256:{H3}\n")
        with mock.patch.dict(os.environ, {"OPS_WORKER_IMAGE": "pinned:1"}):
            self.assertEqual(self.run_main("which")[:2], (0, "pinned:1\n"))

    def test_a_missing_image_loop_key_behaves_as_record(self):
        for text in ("{}", '{"redesign": {}}'):
            self.write_config(text)
            self.assertEqual(self.run_main("which")[:2], (0, PY + "\n"), text)

    def test_a_bad_image_loop_is_a_config_error_naming_the_key_and_record(self):
        for bad in ("", "yes", "RECORD", 1, None):
            self.config(loop=bad)
            rc, out, err = self.run_main("which")
            self.assertEqual((rc, out), (2, ""), bad)
            self.assertIn("redesign.image_loop", err)
            self.assertIn('"record"', err)
            with self.assertRaises(ValueError):
                config._validate_redesign({"image_loop": bad})

    def test_a_legacy_image_loop_value_behaves_as_a_missing_key(self):
        for legacy in (True, False, "on", "off"):
            self.config(loop=legacy)
            self.assertEqual(self.run_main("which")[:2], (0, PY + "\n"), legacy)
            config._validate_redesign({"image_loop": legacy})
            self.assertEqual(config.redesign({"redesign": {"image_loop": legacy}})["image_loop"],
                             "record")

    def test_no_usable_row_prints_an_empty_line_and_the_runner_refuses_the_same_way(self):
        self.config()
        self.record.write_text(f"{BASE}\n")
        rc, out, err = self.run_main("which")
        self.assertEqual((rc, out), (1, "\n"))
        message = docker.missing_image_message("web1")
        self.assertIn(message, err)
        self.assertIn("python3 -m capsule image build --project DIR --board web1", message)
        runner = docker.StrongRunner(board="web1", home=self.home)
        runner._run = lambda *a, **k: self.fail("docker must not be called")
        with self.assertRaises(RuntimeError) as ctx:
            runner.start("a" * 32, Path("/w"))
        self.assertEqual(str(ctx.exception), message)

    def test_missing_or_broken_config_is_exit_2(self):
        rc, out, err = self.run_main("which")
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("no usable config", err)

    def test_cli_parser_registers_build_and_which(self):
        self.config()
        env = {**os.environ, "AGENT_OPS_NO_SEND": "1"}
        run = subprocess.run([sys.executable, "-m", "capsule", "image", "which", "--board", "web1",
                              "--capsule-home", str(self.home)], cwd=paths.REPO, env=env,
                             capture_output=True, text=True, timeout=60)
        self.assertEqual((run.returncode, run.stdout), (0, PY + "\n"))
        run = subprocess.run([sys.executable, "-m", "capsule", "image", "build", "--project",
                              str(self.project), "--board", "web1", "--dry-run",
                              "--capsule-home", str(self.home)], cwd=paths.REPO, env=env,
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("would record", run.stdout)
        self.assertEqual(self.calls(), [])


class WorkerImage(Base):
    ROW = f"capsule-proj-web1@sha256:{H3}"

    def test_board_row_first(self):
        self.record.write_text(f"{PY}\n{self.ROW}\n")
        self.assertEqual(docker.worker_image(self.home, "web1"), self.ROW)
        self.assertEqual(docker.worker_image(self.home), PY)
        self.assertEqual(docker.worker_image(self.home, "web2"), PY)

    def test_malformed_or_other_rows_do_not_count(self):
        for bad in (f"capsule-proj-web1@sha256:{H3[:-1]}", "capsule-proj-web1:local",
                    f"xcapsule-proj-web1@sha256:{H3}", f"capsule-proj-web1@sha256:{H3} x",
                    f"capsule-proj-web.@sha256:{H3}"):
            self.record.write_text(f"{PY}\n{bad}\n")
            self.assertEqual(docker.worker_image(self.home, "web1"), PY)
        self.record.unlink()
        self.assertEqual(docker.worker_image(self.home, "web1"), "")

    def test_override_pin_beats_the_project_row(self):
        self.record.write_text(f"{PY}\n{self.ROW}\n")
        with mock.patch.dict(os.environ, {"OPS_WORKER_IMAGE": "pinned:1"}):
            self.assertEqual(docker.worker_image(self.home, "web1"), "pinned:1")

    def test_project_row_beats_the_ops_env_pin_which_beats_the_python_row(self):
        env_file = self.tmp / "ops.env"
        env_file.write_text("OPS_WORKER_IMAGE=machine:1\n")
        self.record.write_text(f"{PY}\n{self.ROW}\n")
        with mock.patch.dict(os.environ, {"OPS_ENV_FILE": str(env_file)}):
            self.assertEqual(docker.worker_image_source(self.home, "web1"),
                             (self.ROW, "project row"))
            self.assertEqual(docker.worker_image_source(self.home, "web2"),
                             ("machine:1", "ops.env OPS_WORKER_IMAGE"))
            env_file.write_text("")
            self.assertEqual(docker.worker_image_source(self.home, "web2"),
                             (PY, "capsule-python row"))

    def test_environment_pin_beats_row_and_ops_env_and_is_named(self):
        env_file = self.tmp / "ops.env"
        env_file.write_text("OPS_WORKER_IMAGE=machine:1\n")
        self.record.write_text(f"{PY}\n{self.ROW}\n")
        with mock.patch.dict(os.environ, {"OPS_ENV_FILE": str(env_file),
                                          "OPS_WORKER_IMAGE": "run:1"}):
            self.assertEqual(docker.worker_image_source(self.home, "web1"),
                             ("run:1", "environment OPS_WORKER_IMAGE"))

    def test_check_names_an_override_only_when_a_pin_is_the_image_in_use(self):
        env_file = self.tmp / "ops.env"
        env_file.write_text("OPS_WORKER_IMAGE=machine:1\n")
        self.record.write_text(f"{PY}\n{self.ROW}\n")

        def gone(*a, **k):
            return subprocess.CompletedProcess(a, 1, stdout="", stderr="")
        with mock.patch.dict(os.environ, {"OPS_ENV_FILE": str(env_file)}):
            level, _, detail = preflight._worker_image_row({"board": "web1"}, self.home, gone)
            self.assertEqual(level, "FAIL")      # the project row wins: no override, a build hint
            self.assertIn(self.ROW, detail)
            self.assertNotIn("override", detail)
            self.assertIn("build", detail)
            level, _, detail = preflight._worker_image_row({"board": "web2"}, self.home, gone)
            self.assertEqual(level, "FAIL")      # the ops.env pin is the image in use here
            self.assertIn("machine:1 not present locally", detail)
            self.assertIn("override is active", detail)

    def test_runner_preflight_and_run_record_use_the_board_row(self):
        self.record.write_text(f"{self.ROW}\n")      # no python row at all
        runner = docker.StrongRunner(board="web1", home=self.home)
        runner._run = lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="1", stderr="")
        self.assertTrue(runner.available())
        self.assertEqual(run_record.job_image(runner, True), {"runner_image": self.ROW})
        other = docker.StrongRunner(board="web2", home=self.home)
        other._run = runner._run
        self.assertFalse(other.available())
        row = preflight._worker_image_row({"board": "web1"}, self.home, runner._run)
        self.assertEqual(row, ("ok", "worker_image", self.ROW))


if __name__ == "__main__":
    unittest.main()
