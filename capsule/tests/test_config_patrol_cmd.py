import unittest

from capsule import config


class PatrolCmdTest(unittest.TestCase):
    def test_default_agent_cmd_is_restricted_to_its_cwd(self):
        argv = config.patrol({})["agent_cmd"]
        self.assertIn("--restricted", argv)
        self.assertEqual(argv[0], "claude")

    def test_default_agent_cmd_keeps_read_only_tools(self):
        argv = config.patrol({})["agent_cmd"]
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Grep,Glob")


if __name__ == "__main__":
    unittest.main()
