"""Factory autonomy Task 1 (spec 2.5): the `autonomy` block, every switch off by default. Kept apart
from test_autoamend.py so the mutation gate's revert of config.py bites here (it imports config only)."""
import json
import unittest

from capsule import config, paths

BASE = json.loads((paths.REPO / "capsule" / "config.example.json").read_text())


def cfg(**block):
    return dict(BASE, autonomy=block)


class AutonomyConfig(unittest.TestCase):
    def test_defaults_are_off(self):
        a = config.autonomy(BASE)
        self.assertEqual((a["auto_amend"], a["touch_cards"], a["amend_targets"], a["amend_max_lines"]),
                         (False, False, ["card"], 6))

    def test_validation(self):
        config.validate(cfg(auto_amend=True, amend_targets=["card", "spec_section"], amend_max_lines=12))
        for bad in ({"amend_max_lines": 13}, {"amend_max_lines": True}, {"amend_targets": []},
                    {"amend_targets": ["plan"]}, {"auto_amend": "yes"}, {"surprise": 1}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                config.validate(cfg(**bad))

    def test_result_is_a_copy_not_the_module_defaults(self):
        """`autonomy()` returns a fresh dict every call - mutating one caller's result (its
        `amend_targets` list included) must never leak into `AUTONOMY_DEFAULTS` or another
        caller's own result."""
        a = config.autonomy(BASE)
        a["amend_targets"].append("bogus")
        a["auto_amend"] = True
        b = config.autonomy(BASE)
        self.assertEqual(b["amend_targets"], ["card"])
        self.assertFalse(b["auto_amend"])
        self.assertIsNot(a["amend_targets"], config.AUTONOMY_DEFAULTS["amend_targets"])


def routed(**extra):
    """BASE with the implementer routing entry turned into a stub-adapter entry plus `extra`."""
    entry = dict(BASE["routing"]["implementer"], provider="stub", adapter="stub", **extra)
    return dict(BASE, routing=dict(BASE["routing"], implementer=entry))


class RoutingReports(unittest.TestCase):
    def test_stub_entry_keeps_its_reports(self):
        reports = ["done one", "done two"]
        c = routed(reports=reports)
        config.validate(c)
        self.assertEqual(config.route(c, "implementer")["reports"], reports)

    def test_claude_entry_with_reports_is_refused(self):
        entry = dict(BASE["routing"]["implementer"], adapter="claude", reports=["x"])
        c = dict(BASE, routing=dict(BASE["routing"], implementer=entry))
        with self.assertRaises(ValueError) as cm:
            config.validate(c)
        self.assertIn("reports", str(cm.exception))

    def test_bad_reports_values_are_refused(self):
        for bad in (5, None, "ab"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                config.validate(routed(reports=bad))

    def test_no_reports_gives_none_in_the_route(self):
        c = routed()
        config.validate(c)
        self.assertFalse(config.route(c, "implementer").get("reports"))


if __name__ == "__main__":
    unittest.main()
