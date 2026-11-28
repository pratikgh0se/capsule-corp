"""Task 8a (The Lookout): A13 - the bridge's health rules ignore Lookout health rows. Oracle 1: this
structural test. Oracle 2: tests/test_migration_pg.sh evaluates the intent pairs on real Postgres."""
import hashlib
import json
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RULES = REPO / "observability" / "grafana" / "provisioning" / "alerting" / "rules.yaml"
INTENT = REPO / "contracts" / "intent" / "lookout"
# gateway-down left this list when it was retired 2026-10-30 (legacy gateways retired, D133)
A13 = ("bridge-deadman", "disk-high")


def sql_of(uid):
    block = re.split(r"(?m)^      - uid: ", RULES.read_text())
    block = [b for b in block if b.startswith(uid + "\n")][0]
    return re.search(r'rawSql: "((?:[^"\\]|\\.)*)"', block).group(1)


class A13Filters(unittest.TestCase):
    def test_bridge_health_rules_exclude_capsule_rows(self):
        for uid in A13:
            self.assertIn("kind='health' and source <> 'capsule'", sql_of(uid), uid)

    def test_bridge_deadman_pages_when_no_health_row_remains(self):
        """Retention can leave no health row: max(ts) is null, so the age is coalesced."""
        self.assertIn("select coalesce(extract(epoch from now()-max(ts))/60, 100000) from events",
                      sql_of("bridge-deadman"))

    def test_capsule_tick_deadman_is_untouched(self):
        self.assertIn("kind='tick_health'", sql_of("capsule-tick-deadman"))
        self.assertNotIn("source", sql_of("capsule-tick-deadman"))

    def test_every_a13_rule_has_a_firing_and_a_quiet_case(self):
        text = (INTENT / "alerts.jsonl").read_text()
        sums = dict(reversed(l.split(maxsplit=1)) for l in (INTENT / "SHA256SUMS").read_text().splitlines())
        self.assertEqual(sums["alerts.jsonl"], hashlib.sha256(text.encode()).hexdigest())
        cases = [json.loads(l) for l in text.splitlines()]
        for uid in A13:
            self.assertEqual({c["expect"] for c in cases if c["rule"] == uid}, {"fires", "quiet"}, uid)


if __name__ == "__main__":
    unittest.main()
