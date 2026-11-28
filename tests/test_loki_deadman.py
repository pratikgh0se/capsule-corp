"""loki-ingest-deadman: the Grafana rule that pages when Loki gets no log lines."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RULES = ROOT / "observability/grafana/provisioning/alerting/rules.yaml"


def rule_block(uid):
    text = RULES.read_text()
    for b in re.split(r"(?=      - uid: )", text):
        if re.match(rf"      - uid: {re.escape(uid)}\n", b):
            return b
    return ""


class LokiIngestDeadman(unittest.TestCase):
    def setUp(self):
        self.b = rule_block("loki-ingest-deadman")

    def test_rule_exists(self):
        self.assertTrue(self.b, "loki-ingest-deadman is missing from rules.yaml")

    def test_reads_loki_over_30_minutes(self):
        self.assertIn("datasourceUid: ops-loki", self.b)
        self.assertIn("count_over_time(", self.b)
        self.assertIn("[30m]", self.b)
        self.assertIn("from: 1800", self.b)

    def test_zero_or_no_data_pages_like_the_tick_deadman(self):
        self.assertIn("noDataState: Alerting", self.b)
        self.assertIn("execErrState: Error", self.b)
        self.assertIn("severity: page", self.b)
        self.assertIn("for: 15m", self.b)
        self.assertIn('{job="vault", vault_job=~"heal|obs-probe"}', self.b)
        self.assertRegex(self.b, r"type: lt\n\s+params: \[1\]")

    def test_matches_the_tick_deadman_routing(self):
        tick = rule_block("capsule-tick-deadman")
        for line in ("severity: page", "runbook: docs/RUNBOOK.md#failure-alerts"):
            self.assertIn(line, tick)
            self.assertIn(line, self.b)


if __name__ == "__main__":
    unittest.main()
