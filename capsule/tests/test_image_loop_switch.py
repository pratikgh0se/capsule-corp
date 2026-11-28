# mutation-check: targets-predate-range b957739c9 -- rename only, no behaviour pinned ::
#   Setting.test_preflight_inspects_the_recorded_image
"""t_73573060: `image_loop` is not a switch any more: "record" (the default, and the only value)
runs the image recorded in $CAPSULE_HOME/images.lock. Oracles: the argv a stubbed `docker run`
receives, checked against the raw record text on disk; never a real docker.

The record path is fixed under the capsule home: an env var that picks the file would let a
worker's environment redirect the image (D39, D7: the image files are protected paths)."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capsule import config, paths, preflight
from capsule.runners import docker

NO_OVERRIDE = {"OPS_ENV_FILE": "/nonexistent/ops.env"}
MOUNTS = '[{"Mounts": [{"Source": "/w"}]}]'   # a clean container's `docker inspect`
MINE = "capsule-python@sha256:" + "ab" * 32


def started_image(runner):
    """The image a `docker run` argv names: the word before `sleep infinity`."""
    argv = []
    runner._run = lambda a, **kw: argv.extend(a) or subprocess.CompletedProcess(
        a, 0, MOUNTS, "")
    runner.start("a" * 32, Path("/w"))
    return argv[argv.index("sleep") - 1]


class Record(unittest.TestCase):
    """The image is the capsule-python row of $CAPSULE_HOME/images.lock, which docker/build-base.sh
    writes, and nothing else."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, True)
        env = mock.patch.dict(os.environ, {**NO_OVERRIDE, "CAPSULE_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("OPS_WORKER_IMAGE", None)

    def record(self, body):
        (self.home / "images.lock").write_bytes(body)

    def test_the_record_is_the_image_and_the_runner_starts_on_it(self):
        base = b"capsule-base@sha256:" + b"cd" * 32
        self.record(base + b"\n" + MINE.encode() + b"\n")
        self.assertEqual(docker.worker_image(), MINE)
        self.assertEqual(started_image(docker.StrongRunner()), MINE)

    def test_the_tracked_lock_is_never_the_image(self):
        self.assertIn("capsule-python@sha256:", (paths.REPO / "docker" / "IMAGES.lock").read_text())
        self.assertEqual(docker.worker_image(), "")     # the record is empty: no image

    def test_nothing_usable_is_no_image(self):
        bad = (None, b"", b"capsule-base@sha256:" + b"cd" * 32 + b"\n", b"capsule-python:local\n",
               b"capsule-python@sha256:" + b"ab" * 31 + b"\n", b"\xff\xfe\n")
        for body in bad:
            with self.subTest(body=body), mock.patch("sys.stderr"):
                if body is not None:
                    self.record(body)
                self.assertEqual(docker.worker_image(), "")

    def test_the_old_env_opt_in_does_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            scratch = Path(d) / "IMAGES.lock"
            scratch.write_text(MINE + "\n")
            with mock.patch.dict(os.environ, {"IMAGES_LOCK": str(scratch)}):
                self.assertEqual(docker.worker_image(), "")

    def test_an_operator_override_wins(self):
        self.record(MINE.encode() + b"\n")
        with mock.patch.dict(os.environ, {"OPS_WORKER_IMAGE": "b:2"}):
            self.assertEqual(docker.worker_image(), "b:2")
            self.assertEqual(started_image(docker.StrongRunner()), "b:2")

    def test_the_runner_refuses_to_start_with_no_image_and_says_how_to_build_one(self):
        runner = docker.StrongRunner(board="bd")
        runner._run = lambda a, **kw: self.fail("docker must not be called")
        with self.assertRaises(RuntimeError) as ctx:
            runner.start("a" * 32, Path("/w"))
        self.assertEqual(str(ctx.exception), docker.missing_image_message("bd"))
        self.assertIn("python3 -m capsule image build --project DIR --board bd",
                      str(ctx.exception))

    def test_the_home_can_be_named(self):
        elsewhere = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, elsewhere, True)
        (elsewhere / "images.lock").write_text(MINE + "\n")
        self.assertEqual(docker.worker_image(elsewhere), MINE)
        self.assertEqual(docker.record_image(elsewhere), MINE)
        self.assertEqual(docker.record_image(), "")        # CAPSULE_HOME holds no record

    def test_the_build_command_keeps_a_non_default_home(self):
        plain = "docker/build-base.sh base && docker/build-base.sh python"
        self.assertEqual((docker.build_command(), docker.BUILD_COMMAND), (plain, plain))
        self.assertEqual(docker.build_command(paths.default_home()), plain)
        self.assertEqual(docker.build_command("/x y"), f"export CAPSULE_HOME='/x y' && {plain}")


class Setting(unittest.TestCase):
    def test_a_missing_key_behaves_as_record(self):
        self.assertEqual(config.REDESIGN_DEFAULTS["image_loop"], config.IMAGE_RECORD)
        self.assertEqual(config.redesign({})["image_loop"], "record")
        self.assertEqual(config.redesign({"redesign": {"deps_ready": True}})["image_loop"],
                         "record")

    def test_record_is_accepted_and_anything_else_is_a_config_error_naming_the_key(self):
        config._validate_redesign({"image_loop": "record"})
        for legacy in (True, False, "on", "off"):      # Ruling 3: read as a missing key
            config._validate_redesign({"image_loop": legacy})
        for bad in ("banana", "Record", "", None, 1):
            with self.subTest(bad=bad), self.assertRaises(ValueError) as ctx:
                config._validate_redesign({"image_loop": bad})
            self.assertIn("redesign.image_loop", str(ctx.exception))
            self.assertIn('"record"', str(ctx.exception))

    def test_preflight_inspects_the_recorded_image(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            (home / "images.lock").write_text(MINE + "\n")
            cfg = json.loads((paths.REPO / "capsule" / "config.example.json").read_text())
            cfg.update(repo_url=str(home))
            (home / "config.json").write_text(json.dumps(cfg))
            inspected = []

            def run(argv, **kw):
                if "inspect" in argv:
                    inspected.append(argv[-1])
                return subprocess.CompletedProcess(argv, 0, "28.0\n", "")
            with mock.patch.dict(os.environ, {**NO_OVERRIDE, "CAPSULE_HOME": str(home),
                                              "LEGACY_KANBAN_HOME": d}):
                os.environ.pop("OPS_WORKER_IMAGE", None)
                preflight.run(home, run_cmd=run, which=lambda n: "/bin/claude")
            self.assertIn(MINE, inspected)


if __name__ == "__main__":
    unittest.main()
