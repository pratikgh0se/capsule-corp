"""M150 pin: Opus (or stronger) for spec and judgement seats, Sonnet for implementation.

The policy used to live only as config values. This pins both example configs and
`maintenance/run.sh` by model FAMILY (substring), so version bumps do not break it.
The RUNBOOK "Model routing by role" table documents the same shape.
"""
import copy
import re
import unittest

import contextlib
import io
import json
import tempfile
from pathlib import Path

from capsule import config, paths
from capsule.config import family, policy_violations

EXAMPLES = ("config.capsule-corp.example.json", "config.example.json")
RUN_SH = paths.REPO / "maintenance" / "run.sh"
class RoutingPolicyTest(unittest.TestCase):
    def test_example_configs_follow_the_policy(self):
        for name in EXAMPLES:
            with self.subTest(config=name):
                cfg = config.load(paths.REPO / "capsule" / name)
                self.assertEqual(policy_violations(cfg), [])

    def test_load_warns_on_stderr_but_still_loads_a_config_off_the_pin(self):
        raw = json.loads((paths.REPO / "capsule" / EXAMPLES[0]).read_text())
        with tempfile.TemporaryDirectory() as d:
            for model, warned in (("claude-sonnet-5", False), ("claude-opus-5", True)):
                raw["routing"]["implementer"]["model"] = model
                p = Path(d) / "c.json"
                p.write_text(json.dumps(raw))
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    cfg = config.load(p)
                self.assertEqual("implementer must be sonnet" in err.getvalue(), warned)
                self.assertEqual(cfg["routing"]["implementer"]["model"], model)

    def _load_with_implementer(self, model):
        raw = json.loads((paths.REPO / "capsule" / EXAMPLES[0]).read_text())
        raw["routing"]["implementer"]["model"] = model
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.json"
            p.write_text(json.dumps(raw))
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                config.load(p)
            return err.getvalue()

    def test_load_is_silent_for_an_unclassifiable_model_name(self):
        self.assertEqual(self._load_with_implementer("m"), "")

    def test_load_still_warns_for_a_known_wrong_family(self):
        err = self._load_with_implementer("claude-opus-5-5")
        self.assertIn("implementer must be sonnet, got claude-opus-5-5", err)

    def test_deploy_check_main_still_reports_an_unknown_model_name(self):
        raw = json.loads((paths.REPO / "capsule" / EXAMPLES[0]).read_text())
        raw["routing"]["implementer"]["model"] = "m"
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.json"
            p.write_text(json.dumps(raw))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = config.main([str(p)])
        self.assertEqual(rc, 1)
        self.assertIn("implementer must be sonnet, got m", out.getvalue())

    def test_sprint_plan_runs_opus(self):
        line = next(l for l in RUN_SH.read_text().splitlines() if re.match(r"\s*sprint-plan\)", l))
        model = re.search(r'MODEL="([^"]+)"', line).group(1)
        self.assertEqual(family(model), "opus", line)

    # Z2 second oracle: the check itself must fail on a policy break.
    def test_a_policy_break_is_caught(self):
        flips = {
            "implementer": "claude-opus-5",
            "reviewer": "claude-sonnet-5",
            "guide": "claude-haiku-4-5-20251001",
            "validator": "claude-opus-5",
        }
        for name in EXAMPLES:
            for role, model in flips.items():
                with self.subTest(config=name, role=role):
                    broken = config.load(paths.REPO / "capsule" / name)
                    broken["routing"][role]["model"] = model
                    self.assertTrue(policy_violations(broken))

    def test_validator_may_be_sonnet_but_haiku_needs_a_sonnet_fallback(self):
        cfg = config.load(paths.REPO / "capsule" / EXAMPLES[0])
        cfg["routing"]["validator"].update(model="claude-sonnet-5", fallback=None)
        self.assertEqual(policy_violations(cfg), [])
        cfg["routing"]["validator"].update(model="claude-haiku-4-5-20251001")
        self.assertTrue(policy_violations(cfg))

    def test_routed_korin_must_be_opus_or_stronger(self):
        ok = config.load(paths.REPO / "capsule" / EXAMPLES[1])
        self.assertIn("korin", ok["routing"])
        bad = copy.deepcopy(ok)
        bad["routing"]["korin"]["model"] = "claude-sonnet-5"
        self.assertEqual(policy_violations(ok), [])
        self.assertTrue(policy_violations(bad))


if __name__ == "__main__":
    unittest.main()
