"""D270: the legacy board home reads exactly one environment variable, LEGACY_KANBAN_HOME; the old
environment name is no longer an alias."""
import os
import unittest
from unittest import mock

from capsule import paths


class ReadKeys(dict):
    """An environment that records every key `.get` asks for."""

    def __init__(self):
        super().__init__()
        self.asked = []

    def get(self, key, default=None):
        self.asked.append(key)
        return super().get(key, default)


class LegacyHomeEnv(unittest.TestCase):
    def test_the_home_reads_only_legacy_kanban_home(self):
        env = ReadKeys()
        # An empty environment has no HOME; pin Path.home so a container user with no passwd
        # entry (uid 501 in the landing image) does not make the lookup raise.
        with mock.patch.object(os, "environ", env), \
                mock.patch.object(paths.Path, "home", return_value=paths.Path("/home-for-test")):
            paths.legacy_home()
        self.assertEqual(env.asked, ["LEGACY_KANBAN_HOME"])


if __name__ == "__main__":
    unittest.main()
