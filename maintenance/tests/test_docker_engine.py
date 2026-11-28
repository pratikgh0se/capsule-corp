"""Docker engine switch (docs/contracts/docker-engine-switch.md): resolution order and the engine
table (Python and its shell twin). Temp state dir only; never the live Docker."""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "maintenance"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import docker_engine  # noqa: E402
from capsule.tests.docker_engine_support import (  # noqa: E402
    DESKTOP_BIN, TABLE, Sandbox, run_lib)


class Resolve(Sandbox):
    def resolved(self, env_value, file_text):
        if file_text is not None:
            self.engine_file(file_text)
        env = {} if env_value is None else {"CAPSULE_DOCKER_ENGINE": env_value}
        return docker_engine.resolve(self.state, env)["name"]

    def test_default_is_orbstack(self):
        # contract: Resolution order row 1 (unset, absent -> orbstack); intent
        # default-engine-is-orbstack
        r = docker_engine.resolve(self.state, {})
        self.assertEqual(r["name"], "orbstack")
        self.assertEqual(r["bin"], os.path.expanduser("~/.orbstack/bin"))
        self.assertEqual(r["context"], "orbstack")

    def test_file_orbstack_with_trailing_newline(self):
        # contract: row 2 (unset, `orbstack\n` -> orbstack)
        self.assertEqual(self.resolved(None, "orbstack\n"), "orbstack")

    def test_file_surrounding_spaces_ignored(self):
        # contract: row 2, "trailing newline and spaces ignored"
        self.assertEqual(self.resolved(None, "  orbstack \n"), "orbstack")

    def test_env_beats_file(self):
        # contract: row 3 (env desktop, file orbstack -> desktop); intent env-beats-file
        self.assertEqual(self.resolved("desktop", "orbstack"), "desktop")

    def test_env_without_file(self):
        # contract: row 4 (env orbstack, file absent -> orbstack)
        self.assertEqual(self.resolved("orbstack", None), "orbstack")

    def test_env_beats_file_the_other_way(self):
        # contract: precedence is env over file, not "orbstack wins"
        self.assertEqual(self.resolved("orbstack", "desktop"), "orbstack")

    def test_empty_env_counts_as_unset(self):
        # contract: row 5 (empty env, file orbstack -> orbstack)
        self.assertEqual(self.resolved("", "orbstack"), "orbstack")

    def test_empty_file_is_default(self):
        # contract: row 6 (unset, empty file -> orbstack)
        self.assertEqual(self.resolved(None, ""), "orbstack")

    def test_unknown_env_value_raises_naming_value_and_valid_values(self):
        # contract: row 7 (env podman, any file -> ValueError naming podman and the valid values)
        for file_text in (None, "orbstack"):
            with self.subTest(file=file_text):
                if file_text:
                    self.engine_file(file_text)
                with self.assertRaises(ValueError) as cm:
                    docker_engine.resolve(self.state, {"CAPSULE_DOCKER_ENGINE": "podman"})
                msg = str(cm.exception)
                for word in ("podman", "desktop", "orbstack"):
                    self.assertIn(word, msg)

    def test_unknown_file_value_raises_naming_the_file(self):
        # contract: row 8 (unset, file podman -> same failure, message names the file path);
        # never a silent fallback to desktop
        self.engine_file("podman\n")
        with self.assertRaises(ValueError) as cm:
            docker_engine.resolve(self.state, {})
        self.assertIn("podman", str(cm.exception))
        self.assertIn(str(self.state / "docker-engine"), str(cm.exception))

    def test_env_defaults_to_os_environ(self):
        # contract: resolve(state_dir, env=None) reads the process env when env is None
        self.engine_file("desktop")
        with mock.patch.dict(os.environ, {"CAPSULE_DOCKER_ENGINE": "orbstack"}):
            self.assertEqual(docker_engine.resolve(self.state)["name"], "orbstack")


class Table(Sandbox):
    def test_python_table_values(self):
        # contract: Engine table, both rows (context, title, app, app path, open hint, cask)
        for engine, want in TABLE.items():
            row = docker_engine.ENGINES[engine]
            with self.subTest(engine):
                for key, value in want.items():
                    self.assertEqual(row[key], value, key)

    def test_python_bin_dirs(self):
        # contract: Engine table bin dir column ($HOME expanded for orbstack)
        self.assertEqual(docker_engine.ENGINES["desktop"]["bin"], DESKTOP_BIN)
        got = docker_engine.ENGINES["orbstack"]["bin"]
        self.assertIn(got, (str(self.home_orb()), "$HOME/.orbstack/bin", "~/.orbstack/bin"))

    def home_orb(self):
        return Path(os.path.expanduser("~")) / ".orbstack" / "bin"

    def test_shell_table_matches_contract_and_python(self):
        # contract: "one Python table, one shell mirror; both must agree"
        for engine, want in TABLE.items():
            out = run_lib(self, engine=engine, show=True)
            with self.subTest(engine):
                self.assertEqual(out["DOCKER_ENGINE"], engine)
                self.assertEqual(out["DOCKER_ENGINE_CONTEXT"], want["context"])
                self.assertEqual(out["DOCKER_ENGINE_TITLE"], want["title"])
                self.assertEqual(out["DOCKER_ENGINE_APP"], want["app"])
                self.assertEqual(out["DOCKER_ENGINE_APP_PATH"], want["app_path"])
                self.assertEqual(out["DOCKER_ENGINE_OPEN_HINT"], want["open_hint"])
                self.assertEqual(out["DOCKER_ENGINE_CASK"], want["cask"])
                row = docker_engine.ENGINES[engine]
                for key, var in (("context", "CONTEXT"), ("title", "TITLE"), ("app", "APP"),
                                 ("open_hint", "OPEN_HINT"), ("cask", "CASK")):
                    self.assertEqual(row[key], out["DOCKER_ENGINE_" + var], key)

    def test_shell_bin_dirs(self):
        # contract: Engine table bin dir column, shell side
        desktop = run_lib(self, engine="desktop", show=True)
        self.assertEqual(desktop["DOCKER_ENGINE_BIN"], DESKTOP_BIN)
        got = run_lib(self, engine="orbstack", show=True)["DOCKER_ENGINE_BIN"]
        self.assertIn(got, (f"{self.home}/.orbstack/bin", "$HOME/.orbstack/bin"))


if __name__ == "__main__":
    unittest.main()
