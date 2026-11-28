# mutation-check: targets-predate-range b957739c9 -- names out of tests, no behaviour pinned ::
#   LaunchdLabels.test_labels_come_from_the_template_names_and_the_prefix
"""P03: obs_probe resolves the launchd prefix through bin/lib_prefix.sh, never a private copy."""
import os
import re
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from capsule.tests import tmpclean  # noqa: E402
tmpclean.install()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import obs_probe  # noqa: E402


class LaunchdPrefix(unittest.TestCase):
    def resolve(self, host_json=None, **env_extra):
        home = Path(tempfile.mkdtemp(prefix="obs-home-"))
        if host_json is not None:
            (home / "host.json").write_text(host_json)
        env = {k: v for k, v in os.environ.items()
               if k not in ("CAPSULE_LAUNCHD_PREFIX", "CAPSULE_HOME")}
        env.update(CAPSULE_HOME=str(home), CAPSULE_LA_DIR=str(home), **env_extra)
        with unittest.mock.patch.dict(os.environ, env, clear=True):
            return obs_probe._launchd_prefix()

    def test_host_json_sets_the_prefix(self):
        self.assertEqual(self.resolve('{"launchd": {"label_prefix": "ex.legacy"}}'), "ex.legacy")

    def test_nothing_configured_resolves_to_the_neutral_prefix(self):
        self.assertEqual(self.resolve(), "dev.capsule")

    def test_the_variable_beats_host_json(self):
        self.assertEqual(self.resolve('{"launchd": {"label_prefix": "ex.legacy"}}',
                                      CAPSULE_LAUNCHD_PREFIX="x.y"), "x.y")

    def test_an_invalid_host_json_value_raises_instead_of_guessing(self):
        with self.assertRaises(RuntimeError) as cm:
            self.resolve('{"launchd": {"label_prefix": "a b"}}')
        self.assertIn("invalid launchd prefix", str(cm.exception))


if __name__ == "__main__":
    unittest.main()


class LaunchdLabels(unittest.TestCase):
    """collect_launchd labels every template: __PREFIX__ ones under the machine prefix, overlay
    ones (literal prefix in the file name) as named; loaded state matches by label."""

    def test_labels_come_from_the_template_names_and_the_prefix(self):
        tdir = Path(tempfile.mkdtemp(prefix="obs-tpl-"))
        (tdir / "__PREFIX__.capsule-a.plist").touch()
        (tdir / "q.r.overlay-b.plist").touch()
        state = Path(tempfile.mkdtemp(prefix="obs-state-"))
        out = type("R", (), {"returncode": 0, "stderr": "",
                             "stdout": "-\t0\tdev.capsule.capsule-a\n1\t0\tq.r.overlay-b\n"})()
        with unittest.mock.patch.object(obs_probe, "TEMPLATES_DIR", tdir):
            samples = obs_probe.collect_launchd(lambda *a, **k: out, state, prefix="dev.capsule")
        got = {dict(s.labels)["label"]: s.value for s in samples if s.name == "capsule.job.loaded"}
        self.assertEqual(got, {"dev.capsule.capsule-a": 1.0, "q.r.overlay-b": 1.0})
