import unittest

from capsule import bundles


class HarnessNote(unittest.TestCase):
    def test_implementer_brief_names_the_read_only_git_dir_and_the_red_proof_route(self):
        prompt = bundles.implementer({"id": "t_0000aaaa", "title": "t", "body": "b"})["prompt"]
        self.assertIn("git stash", prompt)
        self.assertIn("git dir is read-only", prompt)
        self.assertIn("bin/precheck-gates.py", prompt)
        self.assertIn("Rows (b) and (c)", prompt)
        self.assertIn("git show HEAD:", prompt)


if __name__ == "__main__":
    unittest.main()
