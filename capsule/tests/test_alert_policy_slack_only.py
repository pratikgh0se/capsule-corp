"""D133/D229: the Grafana policy sends alerts to Slack only. Telegram is retired, so the root
receiver is always capsule-slack-alerts and the old OPS_ALERT_SLACK flag no longer exists."""
import importlib.util
import re
import subprocess
import sys
import unittest

from capsule import paths

ALERT_DIR = paths.REPO / "observability" / "grafana" / "provisioning" / "alerting"
RENDER_SCRIPT = paths.REPO / "observability" / "grafana" / "render_alert_policy.py"
TEMPLATE = ALERT_DIR / "policies.yaml.template"
GOLDEN_SLACK1 = paths.REPO / "tests" / "fixtures" / "policies-slack1.yaml"


def _render():
    spec = importlib.util.spec_from_file_location("render_alert_policy", RENDER_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.render(TEMPLATE.read_text())


class SlackOnlyPolicy(unittest.TestCase):
    def test_render_is_byte_identical_to_the_slack_golden(self):
        self.assertEqual(_render(), GOLDEN_SLACK1.read_text())

    def test_receiver_names_are_exactly_the_two_slack_ones(self):
        names = set(re.findall(r"receiver: (\S+)", _render()))
        self.assertEqual(names, {"capsule-slack-alerts", "capsule-slack-activity"})

    def test_root_receiver_is_slack_alerts_and_notice_is_never_muted(self):
        out = _render()
        root = [ln for ln in out.splitlines() if ln.startswith("    receiver:")][0].split(": ")[1]
        self.assertEqual(root, "capsule-slack-alerts")
        self.assertNotIn("notice-quiet", out)

    def test_two_renders_are_identical_and_leave_no_placeholder(self):
        self.assertEqual(_render(), _render())
        self.assertNotIn("__", _render())

    def test_cli_matches_the_function(self):
        out = subprocess.run([sys.executable, str(RENDER_SCRIPT), str(TEMPLATE)],
                             capture_output=True, text=True, check=True).stdout
        self.assertEqual(out, _render())

    def test_no_telegram_contact_point_is_provisioned(self):
        for f in ALERT_DIR.iterdir():
            self.assertNotIn("telegram", f.read_text().lower(), f.name)


class GatewayDownRetired(unittest.TestCase):
    """D133, 2026-10-30: Legacy gateways are retired, so `gateway-down`
    would page forever on Slack. It is deleted through `deleteRules` and has no rule block."""

    def setUp(self):
        self.text = (ALERT_DIR / "rules.yaml").read_text()

    def test_gateway_down_is_listed_under_delete_rules(self):
        head = self.text.split("\ngroups:")[0]
        self.assertIn("uid: gateway-down", head)
        self.assertIn("uid: sync-deadman", head)    # the earlier retirement is untouched

    def test_gateway_down_has_no_rule_block_and_thirteen_rules_remain(self):
        uids = re.findall(r"(?m)^      - uid: (\S+)", self.text)
        self.assertNotIn("gateway-down", uids)
        self.assertIn("bridge-deadman", uids)       # the bridge dead-man switch stays
        self.assertEqual(len(uids), 14)             # incl. vault-eval-failed, loki-ingest-deadman


if __name__ == "__main__":
    unittest.main()
