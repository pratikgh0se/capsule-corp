"""t_734ff813: the base worker image carries a neutral git identity, never a person's. Static:
reads docker/base/Dockerfile; no image is built. The operator's own identity reaches a container
through GIT_AUTHOR_* / GIT_COMMITTER_* at start, which git prefers over the system config."""
import importlib.util
import re
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from capsule import config, paths
from capsule.runners import docker

_spec = importlib.util.spec_from_file_location(
    "public_lint", paths.REPO / "maintenance" / "public_lint.py")
public_lint = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(public_lint)

DOCKERFILE = paths.REPO / "docker" / "base" / "Dockerfile"
CONFIG = re.compile(r"git config --system (?:--add )?(\S+) (\"[^\"]*\"|\S+)")


class RunnerGitIdentity(unittest.TestCase):
    """The board's `git_identity` reaches the container as four GIT_* variables, only when set."""
    WHO = {"name": "Test Operator", "email": "op@example.test"}

    def argv(self, identity):
        seen = []
        runner = docker.StrongRunner(board="B")
        runner.git_identity = identity
        runner._run = lambda argv, **kw: seen.append(argv) or subprocess.CompletedProcess(
            argv, 0, '[{"Mounts": [{"Source": "/w"}]}]')
        with mock.patch.object(docker, "worker_image", lambda *a: "img:1"):
            runner.start("a" * 32, Path("/w"))
        return seen[0]

    def test_a_configured_identity_is_passed_as_four_env_vars(self):
        argv = self.argv(self.WHO)
        for item in ("GIT_AUTHOR_NAME=Test Operator", "GIT_COMMITTER_NAME=Test Operator",
                     "GIT_AUTHOR_EMAIL=op@example.test", "GIT_COMMITTER_EMAIL=op@example.test"):
            self.assertEqual(argv[argv.index(item) - 1], "-e")

    def test_no_identity_adds_no_git_env_var(self):
        self.assertFalse([a for a in self.argv(None) if a.startswith("GIT_")])

    def test_config_rejects_a_non_string_identity(self):
        for bad in ("me", {"name": 1, "email": "op@example.test"}, {"name": "Test Operator"},
                    {"name": "", "email": "op@example.test"}):
            with self.assertRaises(ValueError):
                config._validate_git_identity(bad)
        config._validate_git_identity(self.WHO)
        config._validate_git_identity(None)


class BaseGitIdentity(unittest.TestCase):
    def identity(self):
        text = DOCKERFILE.read_text().replace("\\\n", " ")
        return {k: v.strip('"') for k, v in CONFIG.findall(text)}

    def test_the_default_identity_is_the_neutral_worker(self):
        got = self.identity()
        self.assertEqual(got["user.name"], "Capsule Worker")
        self.assertEqual(got["user.email"], "worker@capsule.localhost")

    def test_no_person_name_or_email_is_baked_into_docker(self):
        """The repo's own public_lint over docker/: the private denylist when this checkout has
        it, else the generic rules (the public edition has no denylist)."""
        files = [p.relative_to(paths.REPO).as_posix()
                 for p in (paths.REPO / "docker").rglob("*") if p.is_file()]
        has_list = (paths.REPO / public_lint.DEFAULT_DENYLIST).is_file()
        rules, exempt = public_lint.load_rules(paths.REPO, generic_only=not has_list)
        hits = public_lint._scan(paths.REPO, files, rules, exempt)
        # Identity categories only: other denylist words in old comments are not this card's.
        self.assertEqual([h for h in hits if h[2] in ("personal-name", "launchd-prefix")], [])
