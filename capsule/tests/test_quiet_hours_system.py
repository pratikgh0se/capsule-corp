"""M189 follow-up: `quiet_hours.timezone: "system"` follows the OS zone. The localtime path is
injected (`outbox.LOCALTIME`); nothing here depends on the machine's real zone."""
import contextlib
import datetime
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from capsule import config, outbox, paths, slack
from capsule.tests.test_slack import board

BASE = json.loads((paths.REPO / "capsule" / "config.example.json").read_text())
SYSTEM = {"quiet_hours": {"enabled": True, "timezone": "system"}}


def epoch(hh, zone):      # 2026-10-27 at hh:00 wall time in `zone`
    return int(datetime.datetime(2026, 10, 27, hh, tzinfo=ZoneInfo(zone)).timestamp())


class SystemZone(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.link = Path(tmp.name) / "localtime"
        for patch in (mock.patch.object(outbox, "LOCALTIME", self.link),
                      mock.patch.object(outbox, "_zone_warned", False),
                      mock.patch.dict(os.environ, clear=False)):
            patch.start()
            self.addCleanup(patch.stop)
        os.environ.pop("TZ", None)

    def point(self, target):
        self.link.unlink(missing_ok=True)
        os.symlink(target, self.link)

    def zone(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            return outbox._quiet_zone("system"), err.getvalue()

    def test_system_validates_but_other_junk_still_does_not(self):
        block = {"enabled": True, "timezone": "system"}
        self.assertEqual(config.validate(dict(BASE, quiet_hours=block))["quiet_hours"], block)
        for bad in ("System", "SYSTEM", "local", "Not/AZone"):
            with self.subTest(tz=bad):
                with self.assertRaisesRegex(ValueError, "not a known IANA zone"):
                    config.validate(dict(BASE, quiet_hours={"enabled": True, "timezone": bad}))

    def test_reads_the_localtime_symlink_target_after_zoneinfo(self):
        for target in ("/var/db/timezone/zoneinfo/Asia/Bangkok",
                       "../usr/share/zoneinfo/Asia/Bangkok"):
            with self.subTest(target=target):
                self.point(target)
                self.assertEqual(self.zone(), (ZoneInfo("Asia/Bangkok"), ""))

    def test_follows_the_symlink_when_it_changes(self):
        self.point("/var/db/timezone/zoneinfo/Asia/Bangkok")
        self.assertEqual(self.zone()[0].key, "Asia/Bangkok")
        self.point("/var/db/timezone/zoneinfo/Europe/Lisbon")
        self.assertEqual(self.zone()[0].key, "Europe/Lisbon")

    def test_falls_back_to_tz_when_there_is_no_usable_link(self):
        os.environ["TZ"] = "Asia/Tokyo"
        self.assertEqual(self.zone()[0].key, "Asia/Tokyo")          # no link at all
        self.point("/somewhere/else")                              # link without zoneinfo/
        self.assertEqual(self.zone()[0].key, "Asia/Tokyo")

    def test_the_link_wins_over_tz(self):
        os.environ["TZ"] = "Asia/Tokyo"
        self.point("/var/db/timezone/zoneinfo/Asia/Bangkok")
        self.assertEqual(self.zone()[0].key, "Asia/Bangkok")

    def test_unresolvable_is_none_and_warns_once(self):
        os.environ["TZ"] = "Not/AZone"
        zone, first = self.zone()
        self.assertIsNone(zone)
        self.assertIn("quiet_hours", first)
        self.assertEqual(self.zone(), (None, ""))

    def test_a_later_failure_warns_again_after_a_good_resolve(self):
        os.environ["TZ"] = "Not/AZone"
        self.assertIn("quiet_hours", self.zone()[1])
        self.point("/var/db/timezone/zoneinfo/Asia/Bangkok")
        self.assertEqual(self.zone()[0].key, "Asia/Bangkok")
        self.link.unlink()
        self.assertIn("quiet_hours", self.zone()[1])

    def test_explicit_zone_is_untouched_by_the_os(self):
        self.point("/var/db/timezone/zoneinfo/Asia/Bangkok")
        self.assertEqual(outbox._quiet_zone("Asia/Kolkata"), ZoneInfo("Asia/Kolkata"))


class SystemFlush(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.link = Path(tmp.name) / "localtime"
        for patch in (mock.patch.object(outbox, "LOCALTIME", self.link),
                      mock.patch.object(outbox, "_zone_warned", False)):
            patch.start()
            self.addCleanup(patch.stop)
        self.conn, self.sent = board(), []
        outbox.set_slack(self.conn, True, now=epoch(12, "UTC"))
        outbox.add(self.conn, "slack", slack.render("done", "done line"), now=epoch(12, "UTC"))

    def flush(self, now):
        with contextlib.redirect_stderr(io.StringIO()):
            return outbox.flush(self.conn, self.sent.append, now=now, quiet=SYSTEM)

    def test_the_window_moves_with_the_os_zone(self):
        instant = epoch(23, "Asia/Bangkok")        # 23:00 in Bangkok, 16:00 in Lisbon
        os.symlink("/var/db/timezone/zoneinfo/Asia/Bangkok", self.link)
        self.assertEqual((self.flush(instant), self.sent), (0, []))
        self.link.unlink()
        os.symlink("/var/db/timezone/zoneinfo/Europe/Lisbon", self.link)
        self.assertEqual(self.flush(instant), 1)

    def test_unresolvable_zone_fails_open_and_sends(self):
        with mock.patch.dict(os.environ, {"TZ": ""}):
            self.assertEqual(self.flush(epoch(23, "Asia/Bangkok")), 1)
        self.assertEqual(len(self.sent), 1)


if __name__ == "__main__":
    unittest.main()
