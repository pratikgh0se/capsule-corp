"""M071 (row 128): the optional route key `vault_read` (none | decisions | notes), the retrieval
contract as config rows beside turn_budget. Nothing reads it at dispatch yet. Oracles: the loader's
parsed value (config.load) and the raw example JSON on disk, checked against ROW_128 below, a
hand-copied dict of the row 128 rule: only the reviewer reads, and it reads `decisions`."""
import json
import tempfile
import unittest
from pathlib import Path

from capsule import config, intent, paths

EXAMPLES = ("config.example.json", "config.capsule-corp.example.json")
# Row 128's table, as the value each role holds. Roles with no row (validator, security-reviewer,
# implementer, guide, korin, ...) hold `none`; only the reviewer reads, and only `Decisions/` at
# one hit.
ROW_128 = {"reviewer": "decisions"}
VALUES = ("none", "decisions", "notes")


def raw(name):
    return json.loads((paths.REPO / "capsule" / name).read_text())


def loaded(cfg):
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "c.json").write_text(json.dumps(cfg))
        return config.load(Path(d) / "c.json")


class VaultReadKey(unittest.TestCase):
    def test_values(self):
        self.assertEqual(config.VAULT_READ, VALUES)

    def test_missing_is_none(self):
        cfg = raw(EXAMPLES[0])
        cfg["routing"]["implementer"].pop("vault_read", None)
        self.assertEqual(loaded(cfg)["routing"]["implementer"].get("vault_read", "none"), "none")

    def test_each_value_loads(self):
        for value in VALUES:
            with self.subTest(value=value):
                cfg = raw(EXAMPLES[0])
                cfg["routing"]["guide"]["vault_read"] = value
                self.assertEqual(loaded(cfg)["routing"]["guide"]["vault_read"], value)

    def test_bad_value_refused_with_a_clear_error(self):
        for bad in ("all", "push", "", "Decisions", None, True, 1, ["decisions"]):
            with self.subTest(bad=bad):
                cfg = raw(EXAMPLES[0])
                cfg["routing"]["reviewer"]["vault_read"] = bad
                with self.assertRaises(ValueError) as cm:
                    loaded(cfg)
                self.assertIn("routing.reviewer.vault_read", str(cm.exception))
                self.assertIn("none", str(cm.exception))

    def test_bad_value_refused_on_an_unlisted_role_too(self):
        cfg = raw(EXAMPLES[1])
        cfg["routing"]["scribe"] = {**cfg["routing"]["guide"], "vault_read": "everything"}
        with self.assertRaises(ValueError):
            loaded(cfg)

    def test_legacy_fable_route_still_validates(self):
        cfg = raw(EXAMPLES[1])
        cfg["routing"]["fable"] = {**cfg["routing"]["guide"], "model": "claude-opus-5"}
        self.assertIn("fable", loaded(cfg)["routing"])
        self.assertEqual(config.policy_violations(cfg), [])   # no fable-specific pin any more


class ExampleConfigsHoldRow128(unittest.TestCase):
    def test_every_example_role_states_the_row_explicitly(self):
        for name in EXAMPLES:
            for role, entry in raw(name)["routing"].items():
                with self.subTest(file=name, role=role):
                    self.assertEqual(entry.get("vault_read"), ROW_128.get(role, "none"))


def overlay(routing, merge):
    """The example config with `routing` entries laid over it: merged field by field for a
    partial entry (`merge`), replaced whole for a complete one."""
    cfg = json.loads((paths.REPO / "capsule" / EXAMPLES[0]).read_text())
    for role, entry in routing.items():
        cfg["routing"][role] = {**cfg["routing"].get(role, {}), **entry} if merge else entry
    return cfg


class IntentExamples(unittest.TestCase):
    """contracts/intent/vault_read_routes.jsonl (row 128), replayed on the real validator (Z1)."""

    def test_every_example_holds_on_the_real_code(self):
        examples = intent.load("vault_read_routes")
        self.assertEqual(len(examples), 3)                # an absent file would pass vacuously
        for ex in examples:
            with self.subTest(ex["id"]):
                doc, exp = ex["given"]["document"], ex["expect"]
                self.assertEqual(doc["check"], "vault_read_routes")
                if exp.keys() == {"load"}:
                    self.assertEqual(exp["load"], "ValueError")
                    self._refused([doc["config"], *doc.get("also_refused", [])])
                else:
                    self._reads(doc["config"], exp)

    def _refused(self, bad_configs):   # the config and each extra: partial entries, merged
        for bad in bad_configs:
            with self.assertRaisesRegex(ValueError, "vault_read"):
                config.validate(overlay(bad["routing"], merge=True))

    def _reads(self, src, exp):
        if isinstance(src, str):       # a path to a shipped example
            cfg = json.loads((paths.REPO / src).read_text())
        else:                          # complete entries, replaced whole: the key is missing
            cfg = overlay(src["routing"], merge=False)
        self.assertIs(config.validate(cfg), cfg)
        for role, want in exp.items():
            self.assertEqual(cfg["routing"][role].get("vault_read", "none"), want)


if __name__ == "__main__":
    unittest.main()
