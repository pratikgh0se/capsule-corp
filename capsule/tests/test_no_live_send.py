"""Card rq_6dd2a60d: importing the capsule test package pins the no-send marker.

A bare `python3 -m unittest capsule.tests.test_x` (no env from the caller) can then never
ship to the live ops-pg."""
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class NoLiveSend(unittest.TestCase):
    def _run(self, code):
        keep = {k: v for k, v in os.environ.items()
                if not k.startswith("AGENT_OPS_") and k != "EVENTS_SPOOL"}
        return subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=keep, text=True,
                              capture_output=True, timeout=60)

    def test_importing_the_test_package_sets_the_marker(self):
        r = self._run("import os, capsule.tests; print(os.environ.get('AGENT_OPS_NO_SEND'))")
        self.assertEqual(r.stdout.strip(), "1", r.stderr)

    def test_a_bare_test_process_spools_to_tmp_not_live_state(self):
        r = self._run("import sys; sys.path.insert(0, 'maintenance'); "
                      "import capsule.tests, events; print(events.SPOOL)")
        self.assertNotIn(str(ROOT / "maintenance" / "state"), r.stdout, r.stderr)
        self.assertIn("no-send", r.stdout)

    def test_production_entry_points_never_load_unittest(self):
        # The docker_env guard keys on "unittest" in sys.modules; a production import of it would
        # silence shipping. Second oracle: importing it afterwards flips the guard (review F1).
        r = self._run(
            "import sys; sys.path.insert(0, 'maintenance')\n"
            "import capsule.__main__, capsule.tick, capsule.lookout_ship, capsule.lookout_cli\n"
            "import events, obs_probe, db_migrate, lookout_close_stale, board_bridge\n"
            "print('unittest' in sys.modules)\n"
            "import unittest\n"
            "try:\n    events.docker_env(); print('no raise')\n"
            "except RuntimeError:\n    print('raises')\n")
        self.assertEqual(r.stdout.split(), ["False", "raises"], r.stderr[-600:])


if __name__ == "__main__":
    unittest.main()
