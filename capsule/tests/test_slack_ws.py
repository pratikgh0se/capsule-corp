"""The Socket Mode websocket client: frames, limits, deadlines (D225). No sockets: a fake one."""
import struct
import threading
import unittest

from capsule import slack, slack_listen


class FakeSock:
    def __init__(self, data, on_idle=None):
        self.data, self.on_idle = data, on_idle

    def recv(self, n):
        if not self.data:
            if self.on_idle:
                self.on_idle()
            raise slack_listen.socket.timeout()
        out, self.data = self.data[:n], self.data[n:]
        return out


class Frames(unittest.TestCase):
    def ws(self, data, on_idle=None, stop=None):
        w = slack_listen._WebSocket.__new__(slack_listen._WebSocket)
        self.now = 0
        w.sock, w.buf, w.midframe, w.msg_since = FakeSock(data, on_idle), b"", False, None
        w.stop, w.clock, w.last_rx, w.since = stop, lambda: self.now, 0, 0
        w.sent = []
        w._send = lambda op, d: w.sent.append((op, d))
        return w

    def test_a_half_sent_frame_is_dropped_after_the_deadline_not_waited_on_forever(self):
        w = self.ws(bytes([0x81, 5]) + b"ab", on_idle=lambda: setattr(self, "now", self.now + 31))
        with self.assertRaises(slack_listen.Closed):
            w.recv()

    def test_a_stop_signal_ends_a_half_sent_frame(self):
        stop = threading.Event()
        stop.set()
        with self.assertRaises(slack_listen.Closed):
            self.ws(bytes([0x81, 5]) + b"ab", stop=stop).recv()

    def test_a_stalled_fragmented_message_is_dropped(self):
        w = self.ws(self.frame(0x1, b"he", fin=False),
                    on_idle=lambda: setattr(self, "now", self.now + 31))
        with self.assertRaises(slack_listen.Closed):
            w.recv()

    def test_protocol_violations_close_the_connection(self):
        bad = {
            "reserved bit": bytes([0xC1, 1]) + b"x",
            "unknown opcode": bytes([0x83, 1]) + b"x",
            "binary frame": bytes([0x82, 1]) + b"x",
            "ping over 125 bytes": bytes([0x89, 126]) + struct.pack(">H", 126) + b"x" * 126,
            "fragmented control": self.frame(0x9, b"x", fin=False),
            "continuation with nothing before it": self.frame(0x0, b"x"),
            "new message mid-message": self.frame(0x1, b"a", fin=False) + self.frame(0x1, b"b"),
            "frame over the cap": bytes([0x81, 127])
            + struct.pack(">Q", slack_listen.MAX_FRAME + 1),
        }
        for name, data in bad.items():
            with self.subTest(name), self.assertRaises(slack_listen.Closed):
                self.ws(data).recv()

    def test_a_fragmented_message_over_the_cap_is_refused(self):
        piece = b"x" * 60000
        n = slack_listen.MAX_FRAME // len(piece) + 2
        data = b"".join(bytes([0x01 if i == 0 else 0x00, 126]) + struct.pack(">H", len(piece))
                        + piece for i in range(n))
        with self.assertRaises(slack_listen.Closed):
            self.ws(data).recv()

    def test_a_message_has_one_deadline_not_one_per_frame(self):
        w = self.ws(b"".join(self.frame(0x1 if i == 0 else 0x0, b"x", fin=False)
                            for i in range(5)))
        ticks = iter(range(0, 1000, 20))
        w.clock = lambda: next(ticks)
        with self.assertRaises(slack_listen.Closed):
            w.recv()

    def test_too_many_empty_fragments_are_refused(self):
        n = slack_listen.MAX_FRAGMENTS + 1
        w = self.ws(b"".join(self.frame(0x1 if i == 0 else 0x0, b"", fin=False) for i in range(n)))
        with self.assertRaises(slack_listen.Closed):
            w.recv()

    def test_the_handshake_has_a_deadline_and_honours_stop(self):
        w = self.ws(b"x" * 100)
        ticks = iter(range(0, 1000, 10))
        w.clock = lambda: next(ticks)
        with self.assertRaises(slack_listen.Closed):
            w._read_until(b"\r\n\r\n")
        stop = threading.Event()
        stop.set()
        with self.assertRaises(slack_listen.Closed):
            self.ws(b"x", stop=stop)._read_until(b"\r\n\r\n")

    def test_bad_urls_are_refused_before_any_connection(self):
        for url in ("ws://wss-primary.slack.com/x", "wss://evil.example.com/x",
                    "wss://slack.com.evil.io/x", "wss://wss-primary.slack.com:444/x"):
            with self.subTest(url), self.assertRaises(slack.SendError):
                slack_listen._WebSocket(url)

    @staticmethod
    def frame(op, payload, fin=True):
        return bytes([(0x80 if fin else 0) | op, len(payload)]) + payload

    def test_text_ping_and_fragmented_message(self):
        w = self.ws(self.frame(0x9, b"hi") + self.frame(0x1, b"he", fin=False)
                    + self.frame(0x0, b"llo"))
        self.assertEqual(w.recv(), "hello")
        self.assertEqual(w.sent, [(0xA, b"hi")])
        self.assertIsNone(w.recv())            # idle: timeout, no data lost

    def test_extended_length_and_close(self):
        big = b"x" * 300
        w = self.ws(bytes([0x81, 126]) + struct.pack(">H", 300) + big + self.frame(0x8, b""))
        self.assertEqual(w.recv(), "x" * 300)
        with self.assertRaises(slack_listen.Closed):
            w.recv()


if __name__ == "__main__":
    unittest.main()
