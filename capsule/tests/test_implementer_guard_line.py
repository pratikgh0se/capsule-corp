"""Row 568 (card t_f51d9056): the implementer contract carries the never-delete-a-guard line, and
it stays inside the 2048-byte cap and the render lint (capsule/contracts.py)."""
import unittest

from capsule import contracts, paths
from capsule.jsonschema import load_all

GUARD = "never delete, weaken or move away a guard test to satisfy the gate"


class ImplementerGuardLine(unittest.TestCase):
    def test_render_carries_the_guard_sentence_and_the_contract_still_lints(self):
        self.assertIn(GUARD, contracts.render("implementer").lower())
        path = contracts.contract_path("implementer")
        self.assertEqual(contracts.lint(path, load_all(paths.SCHEMAS)), [])
        self.assertLessEqual(path.stat().st_size, contracts.MAX_BYTES)

    def test_the_other_roles_are_untouched(self):
        for role in ("reviewer", "validator", "guide", "korin"):
            with self.subTest(role=role):
                self.assertNotIn(GUARD, contracts.render(role).lower())


if __name__ == "__main__":
    unittest.main()
