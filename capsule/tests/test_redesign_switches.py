"""R01 (D17): the `redesign` switch block, every switch off by default. Z1 rows in
contracts/intent/redesign_switches.jsonl; case names follow the rows. Oracles: the loader's parsed
value (config.load) and the status CLI text, checked against the raw JSON on disk."""
import io
import json
import tempfile
import unittest
from pathlib import Path

from capsule import config, paths, status_cli

EXAMPLES = ("config.example.json", "config.capsule-corp.example.json")
FLAGS = ("deps_ready", "lanes", "check_gate", "escalation", "flake_reruns",
         "night_runner", "interrupts", "memory_rules", "advisor_cards_only", "light_runner",
         "mac_runner", "security_review")


def base(name=EXAMPLES[0]):
    return json.loads((paths.REPO / "capsule" / name).read_text())


def status_text(legacy, **boards):
    """`status` output over a temp home: legacy config.json plus boards/<b>/config.json."""
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "config.json").write_text(json.dumps(legacy))
        for name, cfg in boards.items():
            (Path(d) / "boards" / name).mkdir(parents=True)
            (Path(d) / "boards" / name / "config.json").write_text(json.dumps(cfg))
        out = io.StringIO()
        args = type("A", (), dict(watch=False, json=False, refs=False, capsule_home=d))()
        status_cli.run(args, None, out)
        return out.getvalue()


ALL_OFF = {**{f: False for f in FLAGS}, "allocator": "off", "vault_context": "off",
           "blind_tests": "off"}


class RedesignSwitches(unittest.TestCase):
    def test_missing_is_off(self):
        cfg = base()
        cfg.pop("redesign")
        for block in (None, {}, {"lanes": True}):
            with self.subTest(block=block), tempfile.TemporaryDirectory() as d:
                if block is not None:
                    cfg["redesign"] = block
                (Path(d) / "c.json").write_text(json.dumps(cfg))
                self.assertEqual(config.redesign(config.load(Path(d) / "c.json")),
                                 {**ALL_OFF, "image_loop": config.IMAGE_RECORD, **(block or {})})
        config.redesign(cfg)["lanes"] = True   # a caller's result is a copy
        self.assertFalse(config.REDESIGN_DEFAULTS["lanes"])

    def test_missing_is_off_in_status_output(self):
        cfg = base()
        cfg.pop("redesign")
        text = status_text(cfg)
        for name in (*FLAGS, "allocator", "vault_context", "blind_tests"):
            self.assertIn(f"  {name}: off", text)

    def test_examples_ship_every_switch_off(self):
        for name in EXAMPLES:
            self.assertEqual(base(name)["redesign"], ALL_OFF)

    def test_status_reads_each_boards_own_config(self):
        legacy = base()
        block = dict(ALL_OFF, lanes=True, allocator="report", blind_tests="gate")
        text = status_text(legacy, **{"capsule-corp": dict(legacy, redesign=block)})
        corp, pomo = text.split("REDESIGN SWITCHES (capsule-corp)")[1].split(
            "REDESIGN SWITCHES (my-app)")
        for line in ("  lanes: on", "  deps_ready: off", "  allocator: report",
                     "  blind_tests: gate"):
            self.assertIn(line, corp)
        self.assertIn("  lanes: off", pomo)

    def test_status_shows_invalid_block_without_crashing(self):
        text = status_text(dict(base(), redesign={"blind_tests": ["x"]}))
        self.assertIn("(capsule-corp): invalid (redesign.blind_tests: one of", text)

    def test_status_shows_broken_json_as_invalid(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "boards" / "capsule-corp").mkdir(parents=True)
            (Path(d) / "boards" / "capsule-corp" / "config.json").write_text("{not json")
            (Path(d) / "config.json").write_text(json.dumps(base()))
            out = io.StringIO()
            args = type("A", (), dict(watch=False, json=False, refs=False, capsule_home=d))()
            status_cli.run(args, None, out)
        self.assertIn("(capsule-corp): invalid (config.json:", out.getvalue())
        self.assertIn("REDESIGN SWITCHES (my-app)\n  deps_ready: off", out.getvalue())

    def test_unknown_switch_refused(self):
        with self.assertRaisesRegex(ValueError, "redesign.time_travel: unknown switch"):
            config.validate(dict(base(), redesign={"time_travel": True}))

    def test_bad_mode_refused(self):
        for bad in ({"allocator": "maybe"}, {"vault_context": True}, {"lanes": "yes"},
                    {"lanes": 1}, {"blind_tests": "hard"}, {"blind_tests": {"x": "gate"}}, []):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                config.validate(dict(base(), redesign=bad))
        for ok in ({"blind_tests": "off"}, {"blind_tests": "report"}, {"blind_tests": "gate"},
                   {"allocator": "live", "vault_context": "report"}):
            cfg = dict(base(), redesign=ok)
            self.assertIs(config.validate(cfg), cfg)

    def test_image_loop_alone_also_takes_record(self):
        cfg = dict(base(), redesign={"image_loop": "record"})
        self.assertIs(config.validate(cfg), cfg)
        self.assertEqual(config.redesign(cfg)["image_loop"], config.IMAGE_RECORD)
        for legacy in (True, False, "on", "off"):      # Ruling 3: read as a missing key
            config.validate(dict(base(), redesign={"image_loop": legacy}))
        for bad in ({"image_loop": "Record"}, {"image_loop": "banana"}, {"image_loop": 1},
                    {"lanes": "record"}, {"check_gate": "record"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                config.validate(dict(base(), redesign=bad))
        self.assertIn("  image_loop: record", status_text(cfg))


if __name__ == "__main__":
    unittest.main()
