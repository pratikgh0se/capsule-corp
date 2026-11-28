"""The maintenance scripts that read host.json: they import `capsule` when run as plain scripts
(run.sh does `python3 maintenance/<x>.py`), and a bad host.json never stops them."""
import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

MAINT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAINT))
sys.path.insert(0, str(MAINT.parent))
import live_snapshot  # noqa: E402
from obs_inventory import BASE_COMPONENT_ROWS, component_rows  # noqa: E402
import quality_nightly  # noqa: E402
import secrets_import  # noqa: E402
from capsule import host  # noqa: E402

SECRET = "sk_live_SECRET"


def _home(text):
    home = tempfile.mkdtemp()
    Path(home, "host.json").write_text(text)
    return home


class RunAsScript(unittest.TestCase):
    def test_each_script_starts_from_another_cwd(self):
        for name in ("quality_nightly", "obs_inventory", "secrets_import", "board_bridge"):
            env = {"PATH": os.environ["PATH"], "HOME": tempfile.mkdtemp(),
                   "CAPSULE_HOME": tempfile.mkdtemp()}
            r = subprocess.run([sys.executable, str(MAINT / f"{name}.py"), "--help"],
                               cwd=tempfile.mkdtemp(), env=env, capture_output=True, text=True,
                               timeout=60)
            self.assertEqual(r.returncode, 0, f"{name}: {r.stderr[-300:]}")
            self.assertIn("usage", r.stdout.lower(), name)


class BadHostJsonNeverStopsAJob(unittest.TestCase):
    def fallback(self, fn, text):
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"CAPSULE_HOME": _home(text)}), redirect_stderr(err):
            out = fn()
        return out, err.getvalue()

    def test_quality_repos_unrelated_bad_key_is_ignored(self):
        out, err = self.fallback(lambda: host.get_or_default("quality.repos"),
                                 '{"vault": {"path": 5}, "quality": {"repos": ["a"]}}')
        self.assertEqual((out, err), (["a"], ""))

    def test_bad_value_falls_back_with_a_note_naming_the_key_only(self):
        out, err = self.fallback(lambda: host.get_or_default("quality.repos"),
                                 '{"quality": {"repos": "%s"}}' % SECRET)
        self.assertEqual(out, ["capsule-corp"])
        self.assertIn("quality.repos", err)
        self.assertNotIn(SECRET, err)

    def test_malformed_file_falls_back_for_each_consumer(self):
        bad = "{not json"
        self.assertEqual(self.fallback(component_rows, bad)[0],
                         list(BASE_COMPONENT_ROWS))
        self.assertEqual(self.fallback(secrets_import.project_slugs, bad)[0], ["capsule-corp"])
        self.assertEqual(self.fallback(lambda: host.get_or_default("boards.default"), bad)[0],
                         "capsule-corp")
        self.assertIn("observability.components",
                      self.fallback(component_rows, bad)[1])

    def test_quality_main_default_survives_a_bad_file(self):
        env = {"CAPSULE_HOME": _home("{not json"), "MERGE_GATE_STATE": tempfile.mkdtemp()}
        err = io.StringIO()
        with mock.patch.dict(os.environ, env), redirect_stderr(err), \
                mock.patch.object(quality_nightly, "next_repo", return_value=None):
            self.assertEqual(quality_nightly.main(["--dry-run"]), 0)
        self.assertIn("quality.repos", err.getvalue())

    def test_live_snapshot_observe_survives_a_bad_file(self):
        ok = mock.Mock(returncode=0, stdout="")
        seen = {}
        with mock.patch.dict(os.environ, {"CAPSULE_HOME": _home("{not json")}), \
                redirect_stderr(io.StringIO()), \
                mock.patch.object(live_snapshot, "_docker", return_value=ok), \
                mock.patch.object(live_snapshot, "_run",
                                  return_value=mock.Mock(stdout="1 python -m capsule tick\n")), \
                mock.patch.object(live_snapshot, "_disk_free_gb", return_value=1), \
                mock.patch.object(live_snapshot, "_dark_result", return_value=None), \
                mock.patch.object(live_snapshot, "_read_seats", return_value=[]), \
                mock.patch.object(live_snapshot, "_build_health",
                                  side_effect=lambda c, t, *a: seen.setdefault("t", set(t)) or []):
            live_snapshot._observe(tempfile.mkdtemp())
        self.assertEqual(seen["t"], {"capsule-corp"})


class BareTickBoard(unittest.TestCase):
    def observed_board(self, home, host_doc=None):
        ok = mock.Mock(returncode=0, stdout="")
        seen = {}
        if host_doc is not None:
            Path(home, "host.json").write_text(host_doc)
        with mock.patch.dict(os.environ, {"CAPSULE_HOME": home}), \
                mock.patch.object(live_snapshot, "_docker", return_value=ok), \
                mock.patch.object(live_snapshot, "_run",
                                  return_value=mock.Mock(stdout="1 python -m capsule tick\n")), \
                mock.patch.object(live_snapshot, "_disk_free_gb", return_value=1), \
                mock.patch.object(live_snapshot, "_dark_result", return_value=None), \
                mock.patch.object(live_snapshot, "_read_seats", return_value=[]), \
                mock.patch.object(live_snapshot, "_build_health",
                                  side_effect=lambda c, t, *a: seen.setdefault("t", set(t)) or []):
            live_snapshot._observe(home)
        return seen["t"]

    def test_a_bare_tick_serves_the_board_in_the_legacy_config_json(self):
        home = tempfile.mkdtemp()
        Path(home, "config.json").write_text('{"board": "legacy-board"}')
        self.assertEqual(self.observed_board(home, '{"boards": {"default": "from-host"}}'),
                         {"legacy-board"})

    def test_without_a_legacy_board_host_json_then_capsule_corp(self):
        self.assertEqual(self.observed_board(tempfile.mkdtemp(),
                                             '{"boards": {"default": "from-host"}}'), {"from-host"})
        self.assertEqual(self.observed_board(tempfile.mkdtemp()), {"capsule-corp"})


if __name__ == "__main__":
    unittest.main()
