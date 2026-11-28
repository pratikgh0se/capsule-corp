"""The listener's start (which repairs half-written answers) runs once, before the first
connection and not on a reconnect (D225). Fakes only: no network, no child process."""
import io
import json
import os
import unittest
from contextlib import redirect_stdout
from unittest import mock

from capsule import slack_listen


class FakeWS:
    def __init__(self):
        self.last_rx = 0
        self.messages = [json.dumps({"type": "hello"})]

    def recv(self):
        if self.messages:
            return self.messages.pop(0)
        raise slack_listen.Closed("eof")

    def send(self, text):
        pass

    def close(self):
        pass


class StartBeforeConnect(unittest.TestCase):
    def run_main(self, start, sockets=2):
        self.order, left = [], [sockets]
        real = slack_listen._Listener

        def child(cmd, stdin=None, timeout=0):
            self.order.append(cmd)
            return start

        def listener(**kw):
            stop, connect = kw["stop"], kw["connect"]

            def counted(url):
                self.order.append("connect")
                if not left[0]:
                    stop.set()
                    raise slack_listen.Closed("done")
                left[0] -= 1
                return connect(url)
            return real(**{**kw, "connect": counted}, sleep=lambda d: None)

        buf = io.StringIO()
        env = {k: v for k, v in os.environ.items() if k != "AGENT_OPS_NO_SEND"}
        with mock.patch.dict(os.environ, env, clear=True), redirect_stdout(buf), \
                mock.patch.object(slack_listen, "_child", child), \
                mock.patch.object(slack_listen, "_Listener", listener), \
                mock.patch.object(slack_listen, "_open_url", lambda token: "wss://fake/x"), \
                mock.patch.object(slack_listen.slack_secret, "secret_token",
                                  lambda *a, **k: "xapp-1-fake"), \
                mock.patch.object(slack_listen, "_WebSocket", lambda url, stop=None: FakeWS()):
            rc = slack_listen.main()
        return rc, buf.getvalue()

    def test_start_runs_once_before_the_first_connection_and_not_on_reconnect(self):
        rc, out = self.run_main({"identity": "who", "repaired": 2})
        self.assertEqual(rc, 0)
        self.assertEqual(self.order[:2], ["start", "connect"])
        self.assertEqual(self.order.count("start"), 1)
        self.assertGreater(self.order.count("connect"), 1)
        self.assertIn("repaired 2", out)

    def test_nothing_repaired_logs_no_count(self):
        self.assertNotIn("repaired", self.run_main({"identity": "who", "repaired": 0})[1])


if __name__ == "__main__":
    unittest.main()
