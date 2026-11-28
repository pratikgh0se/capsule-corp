"""The mirrored guard flags `outbox.slack` and `outbox.routing` read safely: anything but JSON
`true` (unset, not JSON, wrong type) is False, and a non-JSON value warns once, no traceback."""
import contextlib
import io
import unittest

from capsule import outbox
from capsule.tests.test_slack import board

KEYS = ((outbox.SLACK_FLAG, outbox.slack_enabled), (outbox.ROUTING_FLAG, outbox.routing_v2))


def put(conn, key, text):
    conn.execute("INSERT INTO guard_state (key, value, version, updated_at) VALUES (?,?,1,0)",
                 (key, text))


class GuardFlags(unittest.TestCase):
    def read(self, key, read, text):
        conn = board()
        if text is not None:
            put(conn, key, text)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            return read(conn), err.getvalue()

    def test_true_json_is_on(self):
        for key, read in KEYS:
            self.assertEqual(self.read(key, read, "true"), (True, ""))

    def test_missing_is_off_and_quiet(self):
        for key, read in KEYS:
            self.assertEqual(self.read(key, read, None), (False, ""))

    def test_wrong_type_is_off(self):
        for key, read in KEYS:
            for text in ('"v2"', "1", "null", '"true"'):
                self.assertEqual(self.read(key, read, text)[0], False, (key, text))

    def test_non_json_is_off_with_one_warning_naming_the_key(self):
        for key, read in KEYS:
            for text in ("True", "yes", ""):
                value, err = self.read(key, read, text)
                self.assertFalse(value)
                self.assertEqual(err.count("\n"), 1)
                self.assertIn(key, err)

    def test_mirror_heals_a_bad_value(self):
        conn = board()
        put(conn, outbox.SLACK_FLAG, "garbage")
        with contextlib.redirect_stderr(io.StringIO()):
            outbox.set_slack(conn, True, now=5)
        self.assertTrue(outbox.slack_enabled(conn))


if __name__ == "__main__":
    unittest.main()
