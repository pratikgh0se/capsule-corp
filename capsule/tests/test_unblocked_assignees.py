# mutation-check: targets-predate-range ff625e94 -- fixture rename only, no behaviour pinned ::
#   NonStringAssignee.test_non_string_assignee_is_a_clear_error
"""t_0ad50d1b: caps_from validates assignees with config's own check."""
import json
import tempfile
import unittest
from pathlib import Path

from capsule import config, unblocked


class NonStringAssignee(unittest.TestCase):
    def test_non_string_assignee_is_a_clear_error(self):
        """Oracle 1: the same input config.validate_assignees rejects; oracle 2: the
        error names `assignees` and is an EnvironmentError_, not a TypeError."""
        bad = {"impl-1": 5}
        with self.assertRaisesRegex(ValueError, "assignees"):
            config.validate_assignees(bad)
        home = Path(tempfile.mkdtemp())
        whole = {"board": "sample-app", "caps": {"max_running": 1}, "assignees": bad}
        (home / "config.json").write_text(json.dumps(whole))
        with self.assertRaisesRegex(unblocked.EnvironmentError_, "assignees"):
            unblocked.load_caps(home)
        (home / "config.json").unlink()
        (home / "boards" / "sample-app").mkdir(parents=True)
        (home / "boards" / "sample-app" / "config.json").write_text(json.dumps(whole))
        with self.assertRaisesRegex(unblocked.EnvironmentError_, "assignees") as cm:
            unblocked.load_caps_all(home)
        self.assertIn(str(home / "boards" / "sample-app" / "config.json"), str(cm.exception))


if __name__ == "__main__":
    unittest.main()
