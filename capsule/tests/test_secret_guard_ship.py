"""obs-security-events statements 3-6: the secret-read-guard log reaches ops-pg as `secret_guard`
events. Oracle: a fake `docker` captures the psql script; nothing here reaches psql or a network."""
import contextlib
import csv
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capsule import lookout_cli, paths, secret_guard_ship
from capsule.tests import tmpclean
from capsule.tests.test_lookout_ship import FAKEBIN

TOKEN = "ghp_" + "c" * 36
GOOD = "2026-11-08T10:00:00Z\twarn\tRead\t/w/.env\t/w/.env\n"


class GuardShip(unittest.TestCase):
    def setUp(self):
        self.state = Path(tempfile.mkdtemp())
        self.cap = self.state / "captured.sql"
        keys = ("AGENT_OPS_NO_SEND", "AGENT_OPS_DOCKER_BIN", "FAKE_PSQL_CAPTURE", "FAKE_PSQL_RC",
                "FAKE_PSQL_SLEEP")
        self.env = {k: os.environ.get(k) for k in keys}
        os.environ.update(AGENT_OPS_DOCKER_BIN=str(FAKEBIN), FAKE_PSQL_CAPTURE=str(self.cap),
                          FAKE_PSQL_RC="0", FAKE_PSQL_SLEEP="0")
        os.environ.pop("AGENT_OPS_NO_SEND", None)     # the send-mode seam: NO_SEND unset + fake

    def tearDown(self):
        for k, v in self.env.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        tmpclean.sweep(self.state)

    def log(self, text):
        with open(self.state / secret_guard_ship.LOG, "a", encoding="utf-8") as f:
            f.write(text)

    def sent(self):
        """The `events` rows of every captured psql script."""
        if not self.cap.exists():
            return []
        rows = []
        for script in self.cap.read_text().split("--FAKE-PSQL-END--"):
            m = re.search(r"FROM STDIN WITH \(FORMAT csv\);\n(.*?)\\\.\n", script, re.S)
            for seq, table, doc in csv.reader(io.StringIO(m.group(1) if m else "")):
                rows.append(json.loads(doc))
        return rows

    def test_a_line_becomes_one_secret_guard_event_with_tool_mode_and_path(self):
        self.log(GOOD)
        self.assertEqual(secret_guard_ship.ship(self.state), 0)
        (row,) = self.sent()
        self.assertEqual(row["kind"], "secret_guard")
        self.assertEqual(row["payload"], {"tool": "Read", "mode": "warn", "path": "/w/.env"})
        self.assertEqual(row["ts"], "2026-11-08T10:00:00+00:00")

    def test_the_path_is_cut_to_200_characters(self):
        self.log(f"2026-11-08T10:00:00Z\tblock\tGrep\t/{'a' * 400}\t/x\n")
        secret_guard_ship.ship(self.state)
        self.assertEqual(len(self.sent()[0]["payload"]["path"]), 200)

    def test_the_same_log_shipped_twice_carries_the_same_event_ids(self):
        self.log(GOOD + "2026-11-08T10:00:01Z\tblock\tBash\t/w/id_rsa\t/w/id_rsa\n")
        secret_guard_ship.ship(self.state)
        (self.state / secret_guard_ship.OFFSET).unlink()       # a replay from the top
        secret_guard_ship.ship(self.state)
        ids = [r["event_id"] for r in self.sent()]
        self.assertEqual(len(ids), 4)
        self.assertEqual(ids[:2], ids[2:])                      # pg's unique index drops them
        self.assertEqual(len(set(ids)), 2)
        self.assertEqual(ids[0], "secret_guard:" + hashlib.sha256(GOOD.rstrip("\n").encode())
                         .hexdigest()[:32])

    def test_a_shipped_line_is_not_sent_again_and_a_new_one_is(self):
        self.log(GOOD)
        secret_guard_ship.ship(self.state)
        secret_guard_ship.ship(self.state)
        self.assertEqual(len(self.sent()), 1)
        self.log("2026-11-08T10:00:05Z\twarn\tRead\t/w/other.pem\t/w/other.pem\n")
        secret_guard_ship.ship(self.state)
        self.assertEqual([r["payload"]["path"] for r in self.sent()], ["/w/.env", "/w/other.pem"])

    def test_a_planted_fake_token_is_redacted_everywhere(self):
        self.log(f"2026-11-08T10:00:00Z\twarn\tRead\t/w/{TOKEN}/.env\t/w/{TOKEN}\n")
        secret_guard_ship.ship(self.state)
        self.assertNotIn(TOKEN, self.cap.read_text())
        self.assertIn("<redacted:", self.sent()[0]["payload"]["path"])

    def test_a_malformed_line_is_skipped_and_counted_not_fatal(self):
        self.log("garbage\n" + "not-a-time\twarn\tRead\ta\tb\n" + GOOD)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            secret_guard_ship.ship(self.state)
        self.assertEqual(len(self.sent()), 1)
        self.assertIn("skipped 2 malformed", err.getvalue())

    def test_a_failed_send_keeps_the_offset_so_the_line_goes_again(self):
        self.log(GOOD)
        os.environ["FAKE_PSQL_RC"] = "1"
        self.assertEqual(secret_guard_ship.ship(self.state), 1)
        self.assertFalse((self.state / secret_guard_ship.OFFSET).exists())

    def test_no_send_counts_without_sending_or_printing_a_path(self):
        self.log(GOOD + "junk\n")
        os.environ["AGENT_OPS_NO_SEND"] = "1"      # the docker fake stays set: NO_SEND still wins
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            secret_guard_ship.ship(self.state)
        self.assertIn("would send 1 event", out.getvalue())
        self.assertNotIn(".env", out.getvalue())
        self.assertFalse(self.cap.exists())

    def test_help_exits_before_any_read_or_send_and_an_unknown_arg_exits_2(self):
        self.log(GOOD)
        for argv, code in ((["--help"], 0), (["--bogus"], 2)):
            with self.assertRaises(SystemExit) as cm, contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                secret_guard_ship.main(argv + ["--state-dir", str(self.state)])
            self.assertEqual(cm.exception.code, code)
        self.assertFalse(self.cap.exists())
        self.assertFalse((self.state / secret_guard_ship.OFFSET).exists())
        out = io.StringIO()
        with self.assertRaises(SystemExit), contextlib.redirect_stdout(out):
            secret_guard_ship.main(["--help"])
        for word in ("AGENT_OPS_DOCKER_BIN", "AGENT_OPS_NO_SEND=1", "--state-dir"):
            self.assertIn(word, out.getvalue())

    def test_the_seam_end_to_end_a_fake_docker_gets_the_rows_and_the_offset_advances(self):
        self.log(GOOD)
        r = subprocess.run([sys.executable, "-m", "capsule.secret_guard_ship", "--state-dir",
                            str(self.state)], cwd=paths.REPO, env=dict(os.environ),
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        (row,) = self.sent()
        self.assertEqual(row["payload"]["path"], "/w/.env")
        fields = (self.state / secret_guard_ship.OFFSET).read_text().split()
        self.assertEqual(int(fields[1]), len(GOOD))
        self.assertEqual(len(fields), 3)                    # inode, bytes, head print

    def test_a_nul_in_a_field_is_stripped_so_one_bad_row_cannot_block_the_chunk(self):
        self.log("2026-11-08T10:00:00Z\twa\x00rn\tRe\x01ad\t/w/a\x00b\t/x\n" + GOOD)
        self.assertEqual(secret_guard_ship.ship(self.state), 0)
        rows = self.sent()
        self.assertEqual(len(rows), 2)
        self.assertNotIn("\x00", self.cap.read_text())
        self.assertEqual(rows[0]["payload"], {"tool": "Re ad", "mode": "wa rn", "path": "/w/a b"})

    def test_a_replaced_log_is_shipped_from_the_top(self):
        self.log(GOOD + GOOD.replace("10:00:00", "10:00:01"))
        secret_guard_ship.ship(self.state)
        log = self.state / secret_guard_ship.LOG
        log.unlink()                                        # rotated: a new file, a new inode
        self.log(GOOD.replace("10:00:00", "11:00:00") * 3)  # longer than the old offset
        secret_guard_ship.ship(self.state)
        self.assertEqual(len(self.sent()), 5)

    def test_no_send_without_a_state_dir_uses_the_events_temp_state_not_the_real_log(self):
        os.environ["AGENT_OPS_NO_SEND"] = "1"
        os.environ.pop("AGENT_OPS_STATE_DIR", None)
        with mock.patch.object(secret_guard_ship.lookout_ship.events, "state_root",
                               return_value=self.state) as root:
            self.log(GOOD)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                secret_guard_ship.ship()
        root.assert_called_once()
        self.assertIn("would send 1 event", out.getvalue())

    def test_a_secret_ship_that_raises_costs_the_lookout_verb_nothing(self):
        with mock.patch("capsule.lookout_ship.ship", return_value=0), \
                mock.patch.object(secret_guard_ship, "ship", side_effect=RuntimeError("boom")), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            rc = lookout_cli.main(["ship", "--capsule-home", str(self.state)])
        self.assertEqual(rc, 1)
        self.assertIn("boom", err.getvalue())
        with mock.patch("capsule.lookout_ship.ship", return_value=3), \
                mock.patch.object(secret_guard_ship, "ship", side_effect=RuntimeError("boom")), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(lookout_cli.main(["ship", "--capsule-home", str(self.state)]), 3)

    def test_a_missing_log_is_a_quiet_no_op(self):
        self.assertEqual(secret_guard_ship.ship(self.state), 0)
        self.assertFalse(self.cap.exists())


if __name__ == "__main__":
    unittest.main()
