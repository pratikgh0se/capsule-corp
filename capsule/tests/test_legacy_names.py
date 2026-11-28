# mutation-check: targets-predate-range b957739c9 -- rename only, no behaviour pinned ::
#   LegacyHome.test_the_name_alone_sets_the_home
"""D270: LEGACY_KANBAN_HOME is the one environment name for the legacy board home; an empty value
falls through to the old home folder under $HOME (its name lives in capsule/legacy_names.py)."""
import os
import unittest
from pathlib import Path
from unittest import mock

from capsule import legacy_names, paths

NAMES = ("LEGACY_KANBAN_HOME",)


def _home(**names):
    env = {k: v for k, v in os.environ.items() if k not in NAMES}
    env.update(names)
    with mock.patch.dict(os.environ, env, clear=True):
        return paths.legacy_home()


class LegacyHome(unittest.TestCase):
    def test_the_name_alone_sets_the_home(self):
        self.assertEqual(_home(LEGACY_KANBAN_HOME="/tmp/not-a-real-legacy-home-xyz"),
                         Path("/tmp/not-a-real-legacy-home-xyz"))

    def test_unset_defaults_to_the_old_home_folder_under_home(self):
        self.assertEqual(_home(), Path.home() / legacy_names.LEGACY_HOME_DIRNAME)

    def test_an_empty_name_falls_through_to_the_old_home_folder(self):
        self.assertEqual(_home(LEGACY_KANBAN_HOME=""),
                         Path.home() / legacy_names.LEGACY_HOME_DIRNAME)

    def test_the_old_function_name_is_gone(self):
        self.assertFalse(hasattr(paths, legacy_names.NAME + "_home"))


if __name__ == "__main__":
    unittest.main()
