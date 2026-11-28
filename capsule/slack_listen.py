"""Host-side Slack Socket Mode listener (D225). Intake only: acks every envelope at once, hands
clicks and thread replies to `python -m capsule.slack_answer event` (a subprocess: it decides
everything and posts the reply). Stdlib only, importing nothing from capsule but the protected
`slack_secret` (its import closure is pinned by test_sandbox_slack_protected): the WebSocket
client is the RFC 6455 subset Socket Mode needs. Run: `python3 -m capsule.slack_listen`
(launchd job `capsule-corp-slack-listen`)."""
import base64
import contextlib
import hashlib
import http.client
import json
import os
import queue
import re
import secrets
import signal
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.request
from urllib.parse import urlsplit

from capsule import slack_secret
from capsule.slack_secret import SendError

APP_TOKEN_NAME = "SLACK_APP_TOKEN"        # capsule-corp project key (xapp-, connections:write)
APP_TOKEN_RE = re.compile(r"xapp-[A-Za-z0-9-]+")
OPEN_URL = "https://slack.com/api/apps.connections.open"
BACKOFF_START, BACKOFF_CAP, STABLE_S = 1, 60, 60   # seconds; STABLE_S up resets the backoff
ENVELOPE_ID_MAX = 100          # longest envelope_id we echo back in an ack
QUEUE_MAX = 100                # answer queue size
PING_S, DEAD_S = 30, 90       # silence before a ping, and before the connection is called dead
START_S, EVENT_S = 300, 120   # seconds a start (repair) or one answer may run
POLL_S = 1.0                  # recv wakes at least this often so a stop signal is seen
MAX_FRAME, MAX_FRAGMENTS = 1024 * 1024, 64   # bytes; fragments
FRAME_DEADLINE_S, HANDSHAKE_S = 30, 15   # a started frame or message / the handshake must finish
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class Closed(Exception):
    """The connection ended (peer close, EOF or a protocol error)."""


class _WebSocket:
    """Client side of RFC 6455, enough for Socket Mode. `recv()` returns a text message, or None
    when nothing arrived within POLL_S (so the caller can look at its stop flag)."""

    def __init__(self, url, *, timeout=10, stop=None, clock=time.monotonic):
        parts = urlsplit(url)
        host = parts.hostname or ""
        if (parts.scheme != "wss" or not host.endswith(".slack.com")
                or parts.port not in (None, 443)):
            raise SendError("websocket url is not a wss://*.slack.com address")
        raw = socket.create_connection((host, 443), timeout=timeout)
        self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        self.stop, self.clock, self.last_rx, self.since = stop, clock, clock(), clock()
        self.buf, self.midframe, self.msg_since = b"", False, None
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        self.sock.sendall((f"GET {path or '/'} HTTP/1.1\r\nHost: {parts.hostname}\r\n"
                           "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\n"
                           "Sec-WebSocket-Version: 13\r\n\r\n").encode())
        head = self._read_until(b"\r\n\r\n")
        want = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        if not head.startswith(b"HTTP/1.1 101") or want.encode() not in head:
            raise Closed("websocket handshake refused")
        self.sock.settimeout(POLL_S)

    def _read_until(self, marker):
        began = self.clock()
        while marker not in self.buf:
            if self.clock() - began > HANDSHAKE_S or (self.stop and self.stop.is_set()):
                raise Closed("handshake timed out or stopping")
            chunk = self.sock.recv(4096)
            if not chunk:
                raise Closed("eof")
            self.buf += chunk
            if len(self.buf) > MAX_FRAME:
                raise Closed("handshake too long")
        head, _, self.buf = self.buf.partition(marker)
        return head

    def _stalled(self):
        if self.stop is not None and self.stop.is_set():
            raise Closed("stopping")
        if self.clock() - (self.msg_since or self.since) > FRAME_DEADLINE_S:
            raise Closed("frame or message stalled")

    def _need(self, n):
        while len(self.buf) < n:
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                if not self.midframe:
                    raise
                self._stalled()            # a frame has started: keep its bytes, not forever
                continue
            if not chunk:
                raise Closed("eof")
            self.last_rx = self.clock()
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _frame(self):
        self.midframe = False
        b0, b1 = self._need(2)
        self.midframe, self.since = True, self.clock()
        op, fin, n = b0 & 0x0F, bool(b0 & 0x80), b1 & 0x7F
        if n == 126:
            (n,) = struct.unpack(">H", self._need(2))
        elif n == 127:
            (n,) = struct.unpack(">Q", self._need(8))
        if b0 & 0x70 or op not in (0, 1, 8, 9, 10) or n > MAX_FRAME or \
                (op >= 8 and (not fin or n > 125)):
            raise Closed("protocol error")
        mask = self._need(4) if b1 & 0x80 else None
        data = self._need(n)
        if mask:
            data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
        self.midframe = False
        return op, fin, data

    def recv(self):
        parts, size, started = [], 0, False
        while True:
            try:
                op, fin, data = self._frame()
            except socket.timeout:
                if not started:
                    return None
                self._stalled()            # between fragments of one message
                continue
            except OSError as exc:
                raise Closed(type(exc).__name__) from None
            if op >= 0x8:
                self._control(op, data)
                continue
            size += len(data)
            self._accept(op, started, size, len(parts))
            parts.append(data)
            started = True
            if fin:
                self.msg_since = None
                return b"".join(parts).decode("utf-8", "replace")

    def _accept(self, op, started, size, fragments):
        if (op == 0x0) != started or size > MAX_FRAME or fragments >= MAX_FRAGMENTS:
            raise Closed("protocol error or message too large")
        if not started:
            self.msg_since = self.clock()
        self._stalled()

    def _control(self, op, data):
        if op == 0x8:
            raise Closed("peer closed")
        if op == 0x9:
            self._send(0xA, data)

    def _send(self, op, data):
        mask = secrets.token_bytes(4)
        if len(data) > 125:               # only acks, pings, pongs and closes go out
            raise Closed("outgoing frame too large")
        body = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
        self.sock.sendall(bytes([0x80 | op, 0x80 | len(data)]) + mask + body)

    def send(self, text):
        self._send(0x1, text.encode("utf-8"))

    def close(self):
        with contextlib.suppress(OSError), contextlib.closing(self.sock):
            self._send(0x8, b"")


def log(msg):
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} slack-listen {msg}"
    print(slack_secret.SECRET_RE.sub("***", line), flush=True)


def _open_url(token, opener=None, timeout=10):
    req = urllib.request.Request(OPEN_URL, data=b"{}", method="POST",
                                 headers={"Authorization": f"Bearer {token}",
                                          "Content-Type": "application/json"})
    try:
        with (opener or slack_secret.default_opener())(req, timeout=timeout) as resp:
            doc = json.loads(resp.read(slack_secret.MAX_RESPONSE).decode("utf-8"))
    except (OSError, ValueError) as exc:
        raise SendError(f"apps.connections.open failed: {type(exc).__name__}") from None
    url = str(doc.get("url")) if isinstance(doc, dict) and doc.get("ok") else ""
    if not url.startswith("wss://"):
        why = str(doc.get("error") or "no url")[:60] if isinstance(doc, dict) else "not a document"
        raise SendError(f"apps.connections.open: {why}")
    return url


def _forwardable(envelope, owner=None):
    """The payload for `handle`: a `block_actions` click, or a message from `owner` (a thread
    reply, or a top-level post for brainstorm intake; slack_answer decides which count), else
    None. Anyone else's message never spawns a child."""
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        return None
    if envelope.get("type") == "interactive":
        return payload if payload.get("type") == "block_actions" else None
    if envelope.get("type") == "events_api":
        ev = payload.get("event")
        if (isinstance(ev, dict) and ev.get("type") == "message" and owner
                and ev.get("user") == owner and not ev.get("bot_id")
                and ev.get("subtype") in (None, "thread_broadcast")):
            return payload
    return None


_UID = re.compile(r"[UW][A-Z0-9]{2,20}\Z")


def _log_stranger(env, owner):
    """Log (id only, never text) a human message event that is not the owner's: it was refused."""
    ev = (env.get("payload") or {}).get("event") if env.get("type") == "events_api" else None
    if not (isinstance(ev, dict) and ev.get("type") == "message" and not ev.get("bot_id")
            and ev.get("subtype") in (None, "thread_broadcast") and ev.get("user") != owner):
        return
    who = ev.get("user")
    shown = who if isinstance(who, str) and _UID.match(who) else "unknown"
    log(f"refused message from non-owner {shown}")


class _Listener:
    """`run()` until `stop` is set. `connect(url)` returns a WebSocket-like (recv/send/close);
    `get_url()` returns a fresh wss URL; `handle(payload)` is True when the answer was accepted."""

    def __init__(self, *, get_url, connect, handle, owner=None, stop=None, sleep=None,
                 clock=time.monotonic):
        self.get_url, self.connect, self.handle, self.owner = get_url, connect, handle, owner
        self.clock, self.stop = clock, stop or threading.Event()
        self.sleep = sleep or self.stop.wait
        self.work = queue.Queue(maxsize=QUEUE_MAX)
        self.ready, self.up_since = False, 0

    def _worker(self):
        while (payload := self.work.get()) is not None:
            try:
                self._answer(payload)
            except Exception as exc:       # a malformed payload never kills the worker
                log(f"answer failed: {type(exc).__name__}")

    def _answer(self, payload):
        try:
            ok = self.handle(payload)
        except Exception as exc:       # one bad answer never takes the listener down
            return log(f"handler failed: {type(exc).__name__}")
        log(f"handled {payload.get('type')} ok={bool(ok)}")

    def _serve(self, ws):
        """Read one connection to its end. Every envelope is acked before anything else."""
        pinged = self.clock()
        while not self.stop.is_set():
            raw = ws.recv()
            if raw is None:
                pinged = self._quiet(ws, pinged)
            elif self._envelope(ws, raw):
                return

    def _quiet(self, ws, pinged):
        now = self.clock()
        if now - ws.last_rx > DEAD_S:
            raise Closed("no traffic")
        if now - ws.last_rx > PING_S and now - pinged > PING_S:
            ws._send(0x9, b"")
            return now
        return pinged

    def _envelope(self, ws, raw):
        try:
            env = json.loads(raw)
        except (ValueError, RecursionError, TypeError):
            env = None
        if not isinstance(env, dict):
            return False
        eid = env.get("envelope_id")
        if isinstance(eid, str) and 0 < len(eid) <= ENVELOPE_ID_MAX:
            ws.send(json.dumps({"envelope_id": eid}))
        elif eid:
            log("envelope_id is not a short string; not acked")
        kind = env.get("type")
        if kind == "hello":
            log("connected")
            self.ready, self.up_since = True, self.clock()
        elif kind == "disconnect":
            log(f"slack asked to reconnect ({str(env.get('reason'))[:40]})")
            return True
        elif (payload := _forwardable(env, self.owner)) is None:
            _log_stranger(env, self.owner)
        else:
            try:
                self.work.put_nowait(payload)
            except queue.Full:
                log("answer queue full; dropped one")
        return False

    def run(self):
        worker = threading.Thread(target=self._worker, daemon=True)
        worker.start()
        delay = BACKOFF_START
        while not self.stop.is_set():
            self.ready = False
            ws = None
            try:
                ws = self.connect(self.get_url())
                self._serve(ws)
            except (Closed, OSError, ValueError, http.client.HTTPException, SendError) as exc:
                log(f"connection lost: {type(exc).__name__}: {str(exc)[:80]}")
            finally:
                if ws is not None:
                    ws.close()
            if self.stop.is_set():
                break
            stable = self.ready and self.clock() - self.up_since >= STABLE_S
            delay = BACKOFF_START if stable else min(delay * 2, BACKOFF_CAP)
            log(f"reconnecting in {delay}s")
            self.sleep(delay)
        self.work.put(None)
        worker.join(timeout=10)
        log("stopped")


def _child(cmd, stdin=None, timeout=EVENT_S):
    """Run `python -m capsule.slack_answer <cmd>` (the payload on stdin) from the repo root; its
    one JSON object, or None after logging why not. No retry: a killed answer is repaired at the
    next start."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        out = subprocess.run([sys.executable, "-m", "capsule.slack_answer", cmd], cwd=root,
                             input=None if stdin is None else json.dumps(stdin), text=True,
                             stdin=None if stdin is not None else subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
                             check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log(f"answer failed: {'timeout' if isinstance(exc, subprocess.TimeoutExpired) else 'exec'}")
        return None
    sys.stderr.write(slack_secret.SECRET_RE.sub("***", out.stderr or ""))   # masked like log()
    try:
        doc = json.loads(out.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        doc = None
    if out.returncode != 0 or not isinstance(doc, dict):
        log(f"answer failed: {f'exit {out.returncode}' if out.returncode else 'bad output'}")
        return None
    return doc


def main(argv=None):
    no_send = bool(os.environ.get("AGENT_OPS_NO_SEND"))
    start = _child("check" if no_send else "start", timeout=START_S)   # start repairs once
    if start is None:
        return 1
    if "error" in start:                      # a transient read error: exit 1, launchd retries
        log(str(start["error"])[:100])
        return 1
    if "off" in start:                        # Slack deliberately off: exit 0, no restart
        log(str(start["off"])[:100])
        return 0
    if not isinstance(start.get("identity"), str) or not start["identity"]:
        log("answer failed: bad output")
        return 1
    if no_send:                               # config checked above; no token, no connection
        log("AGENT_OPS_NO_SEND set; not connecting")
        return 0
    if start.get("repaired"):
        log(f"repaired {start['repaired']} answered ask(s) at start")
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    _Listener(get_url=lambda: _open_url(slack_secret.secret_token(
                  start["identity"], name=APP_TOKEN_NAME, pattern=APP_TOKEN_RE)),
              connect=lambda url: _WebSocket(url, stop=stop),
              handle=lambda p: bool((_child("event", p) or {}).get("ok")),
              owner=start.get("owner"), stop=stop).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
