"""capsule/jobs_dir.py: the jobs config dir. Explicit (env, host.json) wins; else the new default
when it exists, else the legacy dir when only that holds a Capsule env file, else the new default.
maintenance/lib_config.sh and compose_live_config_dir (lib_compose.sh) must give the same
answer. Temp HOME only."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capsule import jobs_dir, paths


class JobsDir(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.home = Path(self._td.name).resolve()
        self.new = self.home / ".config" / "capsule-corp"
        self.legacy = self.home / ".config" / "obsidian-second-brain"
        self.chome = self.home / ".local" / "share" / "capsule"
        self.chome.mkdir(parents=True)

    def py(self, env=None):
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}):
            os.environ.pop("CAPSULE_JOBS_DIR", None)
            return jobs_dir.config_dir(self.chome, env or {})

    def shell(self, script, *args, extra=None):
        env = {"PATH": os.environ["PATH"], "HOME": str(self.home), "COMPOSE_FILE": "/dev/null"}
        env.update(extra or {})
        out = subprocess.run(["bash", "-c", script, "_", str(paths.REPO), *args], env=env,
                             capture_output=True, text=True, check=True)
        return Path(out.stdout.strip())

    def assert_all_agree(self, want):
        lib = '. "$1/maintenance/lib_config.sh"; printf %s "$CAPSULE_JOBS_DIR"'
        compose = '. "$1/maintenance/lib_compose.sh"; compose_live_config_dir "$2"'
        self.assertEqual(self.py(), want)
        self.assertEqual(self.shell(lib), want)
        self.assertEqual(self.shell(compose, str(self.home)), want)

    def test_explicit_wins_over_both_defaults(self):
        self.new.mkdir(parents=True)
        self.legacy.mkdir(parents=True)
        explicit = self.home / "elsewhere"
        self.assertEqual(self.py({"CAPSULE_JOBS_DIR": str(explicit)}), explicit)
        (self.chome / "host.json").write_text('{"config_dir": "%s"}\n' % explicit)
        self.assert_all_agree(explicit)

    def test_new_dir_present_is_used_even_when_legacy_exists(self):
        self.new.mkdir(parents=True)
        self.legacy.mkdir(parents=True)
        self.assert_all_agree(self.new)

    def test_only_legacy_present_is_used_when_it_holds_a_capsule_env_file(self):
        for name in ("observability.env", "ops.env"):
            with self.subTest(marker=name):
                self.legacy.mkdir(parents=True, exist_ok=True)
                (self.legacy / name).write_text("")
                self.assert_all_agree(self.legacy)
                (self.legacy / name).unlink()

    def test_legacy_dir_without_a_marker_is_not_adopted(self):
        self.legacy.mkdir(parents=True)
        (self.legacy / "vault.json").write_text("{}")
        self.assert_all_agree(self.new)

    @unittest.skipIf(os.geteuid() == 0, "mode 000 does not stop root")
    def test_unreadable_legacy_dir_gives_the_new_default_without_raising(self):
        self.legacy.mkdir(parents=True)
        (self.legacy / "ops.env").write_text("")
        self.legacy.chmod(0)
        self.addCleanup(self.legacy.chmod, 0o755)
        self.assert_all_agree(self.new)

    def test_legacy_as_a_plain_file_is_ignored(self):
        self.legacy.parent.mkdir(parents=True)
        self.legacy.write_text("x")
        self.assert_all_agree(self.new)

    def test_neither_present_gives_the_new_default(self):
        self.assert_all_agree(self.new)

    def test_home_with_spaces(self):
        spaced = self.home / "a home with spaces"
        self.new, self.legacy, self.chome = (spaced / p.relative_to(self.home)
                                             for p in (self.new, self.legacy, self.chome))
        self.home = spaced
        self.chome.mkdir(parents=True)
        self.legacy.mkdir(parents=True)
        (self.legacy / "ops.env").write_text("")
        self.assert_all_agree(self.legacy)
        self.new.mkdir(parents=True)
        self.assert_all_agree(self.new)

    def test_empty_env_var_is_unset(self):
        self.assertEqual(self.py({"CAPSULE_JOBS_DIR": ""}), self.new)
        lib = '. "$1/maintenance/lib_config.sh"; printf %s "$CAPSULE_JOBS_DIR"'
        self.assertEqual(self.shell(lib, extra={"CAPSULE_JOBS_DIR": ""}), self.new)

    def test_unusable_host_json_gives_the_default(self):
        for body in ('{"config_dir": null}', '{"config_dir": ""}', '["config_dir"]'):
            with self.subTest(body=body):
                (self.chome / "host.json").write_text(body)
                self.assert_all_agree(self.new)


if __name__ == "__main__":
    unittest.main()
