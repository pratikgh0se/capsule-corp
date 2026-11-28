"""COM-11 R1: the weekly rhythm calendar. A table-driven week replay (2026-11-09 is a Monday)
plus config validation and the CLI. Each row: (local time, (dispatch, land_product,
land_harness), reason)."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from capsule import config, paths, rhythm

ON = {"enabled": True}
ALL, NONE = (True, True, True), (False, False, False)
WEEK = [
    ("2026-11-09T00:00", ALL, "default"),                 # Mon
    ("2026-11-09T14:00", ALL, "default"),
    ("2026-11-10T23:59", ALL, "default"),                 # Tue
    ("2026-11-11T12:59", ALL, "default"),                 # Wed before the pause
    ("2026-11-11T13:00", (False, True, True), "wednesday epic pause"),   # start is inside
    ("2026-11-11T16:59", (False, True, True), "wednesday epic pause"),
    ("2026-11-11T17:00", ALL, "default"),                 # end is outside
    ("2026-11-12T20:00", ALL, "default"),                 # Thu
    ("2026-11-13T15:59", ALL, "default"),                 # Fri before the freeze
    ("2026-11-13T16:00", (True, False, True), "friday freeze"),          # freeze starts
    ("2026-11-13T23:59", (True, False, True), "friday freeze"),
    ("2026-11-14T00:00", NONE, "saturday zero"),          # Sat
    ("2026-11-14T12:00", NONE, "saturday zero"),
    ("2026-11-14T23:59", NONE, "saturday zero"),
    ("2026-11-15T00:00", NONE, "sunday review"),          # Sun before 18:00
    ("2026-11-15T17:59", NONE, "sunday review"),
    ("2026-11-15T18:00", ALL, "default"),                 # Sun 18:00 is outside
    ("2026-11-15T23:59", ALL, "default"),
    ("2026-11-16T00:00", ALL, "default"),                 # next Mon
]


def post(block, at):
    """The posture for a `rhythm` block at an ISO time (or datetime)."""
    when = datetime.fromisoformat(at) if isinstance(at, str) else at
    return rhythm._posture(rhythm.parse(block), when)


def _row(p):
    return (p["dispatch"], p["land_product"], p["land_harness"]), p["reason"]


class Week(unittest.TestCase):
    def test_week_replay(self):
        for at, flags, reason in WEEK:
            with self.subTest(at=at):
                self.assertEqual(_row(post(ON, at)), (flags, reason))

    def test_exact_keys_day_and_tz(self):
        p = post(ON, datetime(2026, 11, 11, 9, 0))
        self.assertEqual(set(p), {"day", "dispatch", "land_product", "land_harness",
                                  "reason", "tz"})
        self.assertEqual((p["day"], p["tz"]), ("Wed", "Asia/Bangkok"))
        days = [post(ON, datetime(2026, 11, 9 + i, 1, 0))["day"] for i in range(7)]
        self.assertEqual(days, list(rhythm.DAYS))

    def test_off_is_all_true_at_any_time(self):
        for block in (None, {}, {"enabled": False}, {"enabled": False, "tz": "Europe/Paris"}):
            for at, _flags, _reason in WEEK:
                with self.subTest(block=block, at=at):
                    self.assertEqual(_row(post(block, at)), (ALL, "rhythm off"))

    def test_saturday_zero_false_frees_saturday_but_not_the_sunday_review(self):
        cfg = {"enabled": True, "saturday_zero": False}
        rows = [("2026-11-13T15:59", ALL, "default"),
                ("2026-11-14T12:00", (True, False, True), "friday freeze"),
                ("2026-11-15T00:00", NONE, "sunday review"),
                ("2026-11-15T10:00", NONE, "sunday review"),
                ("2026-11-15T17:59", NONE, "sunday review"),
                ("2026-11-15T18:00", ALL, "default")]
        for at, flags, reason in rows:
            with self.subTest(at=at):
                self.assertEqual(_row(post(cfg, at)), (flags, reason))

    def test_freeze_until_bounds_only_the_friday_freeze(self):
        cfg = {"enabled": True, "freeze_until": "Sat 06:00"}
        rows = [("2026-11-14T05:59", NONE, "saturday zero"),
                ("2026-11-14T13:00", NONE, "saturday zero"),     # zero window still all Saturday
                ("2026-11-15T10:00", NONE, "sunday review"),
                ("2026-11-15T18:00", ALL, "default")]
        for at, flags, reason in rows:
            with self.subTest(at=at):
                self.assertEqual(_row(post(cfg, at)), (flags, reason))

    def test_custom_values_move_the_windows(self):
        cfg = {"enabled": True, "tz": "Europe/Paris", "wed_epic_pause": ["09:00", "10:30"],
               "fri_freeze": "12:00", "freeze_until": "Sat 06:00", "saturday_zero": False}
        rows = [("2026-11-11T09:00", (False, True, True)), ("2026-11-11T10:30", ALL),
                ("2026-11-13T11:59", ALL), ("2026-11-13T12:00", (True, False, True)),
                ("2026-11-14T05:59", (True, False, True)), ("2026-11-14T06:00", ALL)]
        for at, flags in rows:
            with self.subTest(at=at):
                p = post(cfg, at)
                self.assertEqual((_row(p)[0], p["tz"]), (flags, "Europe/Paris"))

    def test_aware_datetime_is_read_in_the_board_zone(self):
        utc = datetime(2026, 11, 13, 9, 30, tzinfo=ZoneInfo("UTC"))    # 16:30 in Bangkok
        self.assertEqual(post(ON, utc)["reason"], "friday freeze")


BAD = [
    ({"bogus": 1}, "bogus"), ({"enabled": "yes"}, "enabled"), ({"tz": "Mars/Base"}, "tz"),
    ({"tz": 5}, "tz"), ({"saturday_zero": 1}, "saturday_zero"),
    ({"wed_epic_pause": ["13:00"]}, "wed_epic_pause"),
    ({"wed_epic_pause": ["13:00", "17:00", "18:00"]}, "wed_epic_pause"),
    ({"wed_epic_pause": ["13:00", "25:00"]}, "wed_epic_pause"),
    ({"wed_epic_pause": ["17:00", "13:00"]}, "wed_epic_pause"),
    ({"wed_epic_pause": "13:00-17:00"}, "wed_epic_pause"),
    ({"fri_freeze": "4pm"}, "fri_freeze"), ({"fri_freeze": "16:60"}, "fri_freeze"),
    ({"freeze_until": "18:00"}, "freeze_until"),
    ({"freeze_until": "Sunday 18:00"}, "freeze_until"),
    ({"freeze_until": "Fri 15:00"}, "freeze_until"), ("nope", "rhythm"),
]


class Validation(unittest.TestCase):
    def test_parse_fills_defaults(self):
        self.assertEqual(rhythm.parse(None), rhythm.DEFAULTS)
        self.assertEqual(rhythm.parse({"enabled": True})["tz"], "Asia/Bangkok")

    def test_bad_blocks_name_rhythm_and_the_key(self):
        for block, key in BAD:
            with self.subTest(block=block):
                with self.assertRaises(ValueError) as ctx:
                    rhythm.parse(block)
                self.assertIn("rhythm", str(ctx.exception))
                self.assertIn(key, str(ctx.exception))

    def test_config_validate_rejects_a_bad_rhythm_block_and_accepts_good_ones(self):
        base = {"board": "b", "epics": ["e"], "contract_commands": ["true"],
                "caps": {k: 1 for k in config.CAPS_KEYS},
                "routing": {r: {"provider": "claude", "account": "a", "model": "m",
                                "turn_budget": 1} for r in config.ROUTING_ROLES}}
        for block in ({}, {"enabled": True, "tz": "UTC"}):
            config.validate({**base, "rhythm": block})
        config.validate(base)
        with self.assertRaises(ValueError) as ctx:
            config.validate({**base, "rhythm": {"tz": "Mars/Base"}})
        self.assertIn("rhythm.tz", str(ctx.exception))


class Cli(unittest.TestCase):
    def _run(self, cfg_text, *argv, name="b"):
        """cfg_text None: a board folder with no config.json. name: the board asked about."""
        home = Path(tempfile.mkdtemp())
        (home / "boards" / "b").mkdir(parents=True)
        if cfg_text is not None:
            (home / "boards" / "b" / "config.json").write_text(cfg_text)
        before = sorted(p.relative_to(home) for p in home.rglob("*"))
        r = subprocess.run([sys.executable, "-m", "capsule", "rhythm", name, *argv,
                            "--capsule-home", str(home)], cwd=paths.REPO, capture_output=True,
                           text=True, timeout=30, env={**os.environ, "HOME": str(home)})
        self.assertEqual(sorted(p.relative_to(home) for p in home.rglob("*")), before)
        return r

    def test_prints_the_posture_json(self):
        on = json.dumps({"rhythm": {"enabled": True}})
        r = self._run(on, "--at", "2026-11-11T13:00")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), {
            "day": "Wed", "dispatch": False, "land_product": True, "land_harness": True,
            "reason": "wednesday epic pause", "tz": "Asia/Bangkok"})

    def test_off_when_missing_key_or_missing_config_file(self):
        for cfg in ('{"board": "b"}', None):
            r = self._run(cfg, "--at", "2026-11-14T12:00")
            self.assertEqual(json.loads(r.stdout)["reason"], "rhythm off", cfg)

    def test_unknown_board_exits_1_naming_it(self):
        r = self._run("{}", "--at", "2026-11-14T12:00", name="ghost")
        self.assertEqual(r.returncode, 1)
        self.assertIn("ghost", r.stderr)
        self.assertEqual(r.stdout, "")

    def test_bad_board_names_exit_1_before_reading_anything(self):
        for name in ("../x", "/etc", "a/b", ".."):
            with self.subTest(name=name):
                r = self._run("{}", "--at", "2026-11-14T12:00", name=name)
                self.assertEqual((r.returncode, r.stdout), (1, ""))
                self.assertIn("board name", r.stderr)

    def test_config_that_is_not_an_object_exits_1_naming_config_json(self):
        for text in ("[]", '"oops"', "null", "{"):
            with self.subTest(text=text):
                r = self._run(text, "--at", "2026-11-14T12:00")
                self.assertEqual((r.returncode, r.stdout), (1, ""))
                self.assertIn("config.json", r.stderr)

    def test_freeze_until_not_after_fri_freeze_says_so(self):
        with self.assertRaises(ValueError) as ctx:
            rhythm.parse({"freeze_until": "Fri 16:00"})
        self.assertIn("after fri_freeze", str(ctx.exception))
        self.assertNotIn("past Sunday", str(ctx.exception))

    def test_bad_rhythm_block_exits_nonzero_naming_the_key(self):
        r = self._run(json.dumps({"rhythm": {"tz": "Mars/Base"}}), "--at", "2026-11-11T13:00")
        self.assertEqual(r.returncode, 1)
        self.assertIn("rhythm.tz", r.stderr)

    def test_bad_at_exits_2(self):
        for at in ("2026-11-11", "2026-11-11 13:00", "2026-11-11T25:00", "x",
                   "2026-11-11T13:00:00"):
            with self.subTest(at=at):
                self.assertEqual(self._run("{}", "--at", at).returncode, 2)
        self.assertEqual(self._run("{}").returncode, 2)


if __name__ == "__main__":
    unittest.main()
