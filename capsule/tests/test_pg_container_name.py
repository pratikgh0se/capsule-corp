import re
import unittest
from pathlib import Path

from capsule import lookout_ship, paths

REPO = Path(__file__).resolve().parents[2]


class PgContainerName(unittest.TestCase):
    def test_constant_value(self):
        self.assertEqual(paths.PG_CONTAINER, "capsule-corp-postgres")

    def test_lookout_ship_uses_constant(self):
        self.assertIs(lookout_ship.CONTAINER, paths.PG_CONTAINER)

    def test_only_events_keeps_the_literal(self):
        for f in sorted((REPO / "maintenance").glob("*.py")):
            if f.name == "events.py":
                continue
            self.assertNotIn('"capsule-corp-postgres"', f.read_text(), f.name)

    def test_protected_events_literal_has_not_drifted(self):
        src = (REPO / "maintenance" / "events.py").read_text()
        literals = set(re.findall(r'"(capsule-corp-postgres[^"]*)"', src))
        self.assertEqual(literals, {paths.PG_CONTAINER})


if __name__ == "__main__":
    unittest.main()
