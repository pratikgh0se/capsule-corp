# mutation-check: targets-predate-range b957739c9 -- rename only, no behaviour pinned ::
#   HermeticHome.test_the_retired_usage_names_are_gone_from_ai_usage,
#   HermeticHome.test_home_and_state_roots_live_in_the_suite_temp_root,
#   HermeticHome.test_the_legacy_board_home_resolves_inside_the_temp_root
"""The maintenance suite never sees the real home: tmpclean.install() (test_0_tmpsweep.py)
pointed HOME and the old-system and Capsule roots at the per-run temp root before any import."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from capsule.tests import tmpclean  # noqa: E402
from capsule.tests.private_names import retired_only, retired_word  # noqa: E402

tmpclean.install()  # idempotent; makes a run of this module alone as hermetic as discover


class HermeticHome(unittest.TestCase):
    def test_home_and_state_roots_live_in_the_suite_temp_root(self):
        root = Path(tempfile.gettempdir()).resolve()
        # test_obs_probe pops CAPSULE_HOME at import; it then falls back under the temp HOME
        for name in ("HOME", "LEGACY_KANBAN_HOME", "OBSIDIAN_VAULT_PATH"):
            self.assertTrue(Path(os.environ[name]).resolve().is_relative_to(root), name)
        home = Path(os.environ["HOME"]).resolve()
        self.assertEqual(Path(os.path.expanduser("~")).resolve(), home)

    def test_the_legacy_board_home_resolves_inside_the_temp_root(self):
        """The new name wins in `paths.legacy_home`; tmpclean points it at the temp root."""
        from capsule import paths
        root = Path(tempfile.gettempdir()).resolve()
        self.assertTrue(paths.legacy_home().resolve().is_relative_to(root))

    def test_a_module_read_at_import_resolved_into_the_temp_home(self):
        import ai_usage
        home = Path(os.environ["HOME"]).resolve()
        self.assertTrue(Path(ai_usage.CLAUDE_ROOT).resolve().is_relative_to(home))

    @retired_only
    def test_the_retired_usage_names_are_gone_from_ai_usage(self):
        import ai_usage
        self.assertFalse(hasattr(ai_usage, retired_word().upper() + "_ROOT"))
        self.assertFalse(hasattr(ai_usage, "scan_" + retired_word()))


if __name__ == "__main__":
    unittest.main()
