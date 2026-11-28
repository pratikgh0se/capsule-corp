"""The Slack channel: seven message classes, their fixed template, routing and the
sender (rows 398, 424, 425)."""
import http.client
import json
import os
import re
import time
import urllib.error
import urllib.request

from capsule import paths, slack_msgs
from capsule.answer_base import _allowed, block  # noqa: F401  moved, re-exported
from capsule.slack_secret import (  # noqa: F401  re-exported: the listener and callers use these
    MAX_RESPONSE, SECRET_RE, SendError, check_token, default_opener, secret_token)

# class -> glyph, in the order of row 425 (1), plus `needs_input` (rows 424, 425 (1)). The first
# matching prefix wins, so no glyph may be a prefix of another.
CLASSES = {
    "blocked": "\U0001F534",          # 🔴 needs you: approve / decide / fix
    "done": "✅",                 # ✅ merge, release, epic close
    "scheduled": "\U0001F5D3",        # 🗓 the brief and anything on a clock
    "running": "⚙️",        # ⚙️ activity, channel only
    "incident": "\U0001F6A8",         # 🚨 support page / alert
    "paused": "⏸",               # ⏸ budget or limit stop
    "needs_input": "\U0001F64B",      # 🙋 a hold that names what it needs: guide question,
                                       # review infra, stale spec - needs you, distinct from blocked
    "ask": "\U0001F4EC",              # 📬 routing v2: a decision for the owner (`cos ask`)
    "page": "\U0001F6D1",             # 🛑 routing v2: the factory is down or stuck
}
ROUTING_V2 = "v2"       # `slack.routing`: class routing (spec 2026-10-29); absent = today's
SUMMARY_GLYPH = "☀"     # ☀️ the `scheduled` digest body; v2: project channel
DEFAULT_CLASS = "running"             # an unglyphed line is activity, never a push
SUMMARY_MAX = 3000
SUMMARY_TAIL = "\n…truncated, see outbox.log"
MAX_FIELDS = 3
HEADLINE_MAX = 150
VALUE_MAX = 300
API_URL = "https://slack.com/api/chat.postMessage"
UPDATE_URL = "https://slack.com/api/chat.update"
DEFAULT_RETRY_AFTER = 30
DEBUG_LOG_MAX = 1_000_000
AUTH_ERRORS = ("invalid_auth", "token_revoked", "token_expired", "not_authed", "account_inactive")
TRANSIENT_ERRORS = ("internal_error", "fatal_error", "service_unavailable", "request_timeout")
MAX_RETRY_AFTER = 3600


class Rejected(SendError):
    """Slack refused this message for good (channel_not_found, not_in_channel, msg_too_long ...):
    the row goes dead and the channel is not held."""


class Deferred(Exception):
    """Slack said wait (HTTP 429 / `ratelimited`): hold the channel `retry_after` seconds."""

    def __init__(self, retry_after):
        super().__init__(f"slack rate limited, retry after {retry_after}s")
        self.retry_after = retry_after


def _one_line(text, limit):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def render(cls, headline, fields=(), reply=None, digest=False):
    """The fixed template. Fields are (key, value) pairs, at most three. A `scheduled` digest
    (`digest=True`) is the multi-line body: SUMMARY_GLYPH plus the whole body, capped at
    SUMMARY_MAX."""
    if cls == "scheduled" and digest:
        body = str(headline).strip()
        if not body:
            raise ValueError("scheduled: empty body")
        if len(body) > SUMMARY_MAX:
            body = body[:SUMMARY_MAX - len(SUMMARY_TAIL)].rstrip() + SUMMARY_TAIL
        return f"{SUMMARY_GLYPH} {body}"
    if cls == "ask" and "\n" in str(headline).strip():
        # The whole inbox message, built and linted by `capsule cos ask`, kept multi-line. That
        # verb is the only caller of this path: `outbox-send` refuses class `ask`, so no other
        # sender can reach the inbox with a free-form body. Over SUMMARY_MAX it is refused, not
        # cut: a cut message would lose its question and its answer line.
        body = str(headline).strip()
        if len(body) > SUMMARY_MAX:
            raise ValueError(f"ask: body over {SUMMARY_MAX} characters")
        return f"{CLASSES[cls]} {body}"
    if cls not in CLASSES:
        raise ValueError(f"unknown message class {cls!r}")
    fields = list(fields.items()) if isinstance(fields, dict) else list(fields)
    if len(fields) > MAX_FIELDS:
        raise ValueError(f"{cls}: at most {MAX_FIELDS} fields, got {len(fields)}")
    head = _one_line(headline, HEADLINE_MAX)
    if not head:
        raise ValueError(f"{cls}: empty headline")
    lines = [f"{CLASSES[cls]} {head}"]
    for key, value in fields:
        key = _one_line(key, 40)
        if not key or ":" in key:
            raise ValueError(f"{cls}: bad field key {key!r}")
        lines.append(f"{key}: {_one_line(value, VALUE_MAX)}")
    if reply:
        cmd = _one_line(reply, 400).replace("`", "'")     # a backtick would end the code span
        lines.append(f"reply: `{cmd}`")
    return "\n".join(lines)


def one_line(text):
    """The same message flattened for the one-line `file` channel (outbox.log)."""
    return " | ".join(line for line in text.splitlines() if line)


def classify(body):
    for cls, glyph in CLASSES.items():
        if body.startswith(glyph):
            return cls
    return DEFAULT_CLASS


# row 425 (1): falls back to `blocked`'s channel when absent. `ask` and `page` have no channel
# key of their own: both go to `inbox` (row 637), then `needs_input`'s, then `blocked`'s, so a
# page is never lost.
INBOX_CLASSES = ("ask", "page")
OPTIONAL_CHANNELS = ("needs_input", "inbox", "brainstorm")
BRAINSTORM_GLYPH = "\U0001F4AD"    # 💭 a Capsule-started brainstorm opener (D229): its own channel


def validate(cfg_slack):
    """`slack` config block: {"channels": {class: channel}, "identity": str, "enabled"?:
    bool}."""
    if not isinstance(cfg_slack, dict):
        raise ValueError("slack: not an object")
    channels = cfg_slack.get("channels")
    if not isinstance(channels, dict):
        raise ValueError("slack.channels: missing")
    keys = [c for c in CLASSES if c not in INBOX_CLASSES] + ["inbox", "brainstorm"]
    for cls in keys:
        val = channels.get(cls)
        if cls in OPTIONAL_CHANNELS and val is None:
            continue
        if not isinstance(val, str) or not val.strip():
            raise ValueError(f"slack.channels.{cls}: missing")
    extra = set(channels) - set(keys)
    if extra:
        raise ValueError(f"slack.channels: unknown class {sorted(extra)[0]!r}")
    if not isinstance(cfg_slack.get("enabled", True), bool):
        raise ValueError("slack.enabled: not a boolean")
    if not isinstance(cfg_slack.get("board_prefix", False), bool):
        raise ValueError("slack.board_prefix: not a boolean")
    allowed = cfg_slack.get("allowed_channels")
    if allowed is not None and not (isinstance(allowed, list) and all(
            isinstance(c, str) and c.strip() for c in allowed)):
        raise ValueError("slack.allowed_channels: must be a list of channel names or ids")
    if allowed is not None:
        left_out = {c.strip() for c in channels.values() if isinstance(c, str)} - set(
            c.strip() for c in allowed)
        if left_out:
            raise ValueError(f"slack.allowed_channels: leaves out {sorted(left_out)[0]!r}; "
                             "set `slack.enabled` false to turn Slack off")
    if "owner_ids" in cfg_slack or "owner_id" in cfg_slack:    # D225: the one owner is host-level
        raise ValueError("slack.owner_ids: not allowed in a board config; set `slack_owner_id` in"
                         " the host config (docs/architecture/components/outbox-notifications.md)")
    if cfg_slack.get("routing", ROUTING_V2) != ROUTING_V2:
        raise ValueError(f"slack.routing: must be {ROUTING_V2!r} or absent")
    ident = cfg_slack.get("identity")
    if not isinstance(ident, str) or not ident.strip():
        raise ValueError("slack.identity: missing")
    return cfg_slack


def is_v2(blk):
    """True for a `slack` block (or None) that sets `routing: "v2"`."""
    return isinstance(blk, dict) and blk.get("routing") == ROUTING_V2


def route(cfg_slack, body):
    channels = cfg_slack["channels"]
    if body.startswith(BRAINSTORM_GLYPH):
        if not channels.get("brainstorm"):
            raise Rejected("slack.channels.brainstorm is not set")
        return channels["brainstorm"]
    cls = classify(body)
    if body.startswith(SUMMARY_GLYPH):     # the morning summary: v2 sends it to the project channel
        cls = "done" if is_v2(cfg_slack) else "blocked"
    if cls in INBOX_CLASSES:
        cls = "inbox"
    if cls in OPTIONAL_CHANNELS and not channels.get(cls):
        # row 425 (1): no dedicated channel yet (absent, null or empty), fall back - falsiness,
        # not just key presence; a page or an ask tries `needs_input` first
        cls = "needs_input" if cls != "needs_input" and channels.get("needs_input") else "blocked"
    return channels[cls]


def escape(text):
    """Slack mrkdwn control characters; `<answer>` must not become a link."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# Block Kit answer buttons (D225) on a `cos ask` post (class `ask`, outbox key `needs:ask-<key>`):
# Yes / No / Later. The button `value` is `ask:<key>:<choice>`, exactly what
# capsule/slack_answer.value() builds and answer() parses; the plain `text` still carries the whole
# message. No merge or approval button exists (spec 2026-10-29 section 7).
ASK_DEDUP_RE = re.compile(r"needs:ask-([0-9a-f]{16})\Z")
ASK_CHOICES = ("yes", "no", "later")
SECTION_MAX = 3000
ACTION_PREFIX = "cc_answer:"


BRAINSTORM_DEDUP_RE = re.compile(r"brainstorm:([0-9a-f]{16})\Z")
BRAINSTORM_BUTTONS = (("open", "Open the note"), ("queue", "Not now, queue it"))


def _brainstorm_blocks(dedup_key, text):
    """The handoff's two buttons (M203) under a Capsule-started opener (outbox key
    `brainstorm:<id>`); the click value is `bs:<id>:<open|queue>` (slack_answer)."""
    hit = BRAINSTORM_DEDUP_RE.match(str(dedup_key or ""))
    if not hit or len(text) > SECTION_MAX:
        return []
    buttons = [{"type": "button", "action_id": f"cc_brainstorm:{c}",
                "text": {"type": "plain_text", "text": label},
                "value": f"bs:{hit.group(1)}:{c}"} for c, label in BRAINSTORM_BUTTONS]
    return [{"type": "section", "text": {"type": "mrkdwn", "text": text[:SECTION_MAX]}},
            {"type": "actions", "elements": buttons}]


def _answer_blocks(cls, dedup_key, text):
    """A section with `text`, then one actions block of buttons; [] for anything but an ask
    whose outbox key carries a `cos ask` key, or a brainstorm opener."""
    if blocks := _brainstorm_blocks(dedup_key, text):
        return blocks
    hit = ASK_DEDUP_RE.match(str(dedup_key or ""))
    if cls != "ask" or not hit or len(text) > SECTION_MAX:     # too long: plain text only
        return []
    buttons = [{"type": "button", "action_id": f"{ACTION_PREFIX}{c}",
                "text": {"type": "plain_text", "text": c.capitalize()},
                "value": f"ask:{hit.group(1)}:{c}"} for c in ASK_CHOICES]
    return [{"type": "section", "text": {"type": "mrkdwn", "text": text[:SECTION_MAX]}},
            {"type": "actions", "elements": buttons}]


def retry_after(headers):
    try:
        val = int(str((headers or {}).get("Retry-After", "")).strip())
    except (TypeError, ValueError):
        return DEFAULT_RETRY_AFTER
    return min(max(val, 1), MAX_RETRY_AFTER)


class _AuthFailed(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _append_private(target, text):
    """Append to `target` at mode 0600, moving it to `.1` first once over DEBUG_LOG_MAX."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and target.stat().st_size > DEBUG_LOG_MAX:
        os.replace(target, target.with_name(target.name + ".1"))
    fd = os.open(target, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        os.fchmod(fh.fileno(), 0o600)
        fh.write(text)


class Sender:
    """`send(row)` for outbox rows on the `slack` channel. No sleeping; at most two HTTP calls
    (the second only to retry once with a re-read token after an auth error)."""

    def __init__(self, cfg_slack, *, token=None, urlopen=None, timeout=5, api_url=API_URL,
                 update_url=None, debug_log=None, home=None):
        self.cfg = validate(cfg_slack)
        self._token = token                       # a callable returning the token
        self._urlopen = urlopen or default_opener()
        self.timeout = timeout
        self.api_url = api_url
        self.update_url = update_url or (
            UPDATE_URL if api_url == API_URL else api_url.rsplit("/", 1)[0] + "/chat.update")
        self._cached = None
        self._debug_log = debug_log
        self._home = home

    def _debug(self, action, channel, detail=""):
        """One line per send, failure or refusal: time, action, channel, detail. Never the body
        or a secret; mode 0600, one `.1` rotation at DEBUG_LOG_MAX; a log that cannot be written
        never stops a send."""
        line = f"{int(time.time())}\t{action}\t{_one_line(channel, 80)}\t{_one_line(detail, 120)}"
        line = SECRET_RE.sub("***", line)
        target = self._debug_log or paths.home(self._home) / "slack-debug.log"
        try:
            _append_private(target, line + "\n")
        except (OSError, ValueError):       # a log failure never stops or fails a send
            return

    def _permit(self, channel):
        """Refuse (Rejected, logged) a channel that is not allowlisted."""
        if str(channel).strip() not in _allowed(self.cfg):
            self._debug("refused", channel, "not in the allowlist")
            raise Rejected(SECRET_RE.sub("***", f"slack channel {channel!r} is not allowlisted"))
        return channel

    def _call(self, url, payload):
        """`_api`, with a failure or a Slack refusal logged (masked) before it propagates."""
        try:
            return self._api(url, payload)
        except (SendError, Deferred, _AuthFailed) as exc:
            action = "rejected" if isinstance(exc, Rejected) else "failed"
            self._debug(action, payload["channel"], str(exc))
            raise

    def token(self):
        if self._cached is None:
            fetch = self._token or (lambda: secret_token(self.cfg["identity"]))
            self._cached = check_token(fetch())
        return self._cached

    def __call__(self, row):
        fresh = self._cached is None
        for _ in range(1 if fresh else 2):
            try:
                return self._post(row)
            except _AuthFailed as exc:
                self._cached = None               # a rotated token is re-read, once
                code = exc.code
        raise Rejected(f"slack error: {code} (token re-read)")

    def _post(self, row):
        """Post the row, or edit the message a keyed row names (`row["edit"]`: chat.update). An edit
        Slack refuses for good (message deleted, too old) falls back to one new post. A dict row
        gets `slack_channel` / `slack_ts` so the outbox can remember where it landed."""
        body = row["body"]
        text = escape(self.cfg.get("prefix", "") + body)
        edit = row.get("edit") if isinstance(row, dict) else None
        refused = False
        if edit:
            try:
                update = {"channel": self._permit(edit["channel"]), "ts": edit["ts"], "text": text}
                self._blocks(update, row, body, text, edit=True)
                ts = self._call(self.update_url, update)
            except Rejected:
                refused = True
            if not refused:
                self._debug("edited", edit["channel"])
                return self._landed(row, edit["channel"], ts or edit["ts"])
        thread = row.get("thread") if isinstance(row, dict) else None     # a reminder reply
        channel = self._permit(thread["channel"] if thread else route(self.cfg, body))
        payload = {"channel": channel, "text": text, "unfurl_links": False, "unfurl_media": False}
        if thread:
            payload["thread_ts"] = thread["ts"]
        self._blocks(payload, row, body, text)
        ts = self._call(self.api_url, payload)
        self._debug("sent", channel)
        return self._landed(row, channel, ts)

    @staticmethod
    def _blocks(payload, row, body, text, edit=False):
        """Answer buttons for a `cos ask` post (the `text` fallback is unchanged); a
        `✔` edit sends `blocks: []` so the buttons go away with the question."""
        cls = classify(body)
        dedup = row.get("dedup_key") if isinstance(row, dict) else None
        blocks = _answer_blocks(cls, dedup, text)
        if blocks:
            payload["blocks"] = blocks
        elif edit and body.startswith(slack_msgs.HANDLED) and ASK_DEDUP_RE.match(str(dedup or "")):
            payload["blocks"] = []

    @staticmethod
    def _landed(row, channel, ts):
        if isinstance(row, dict):
            row["slack_channel"], row["slack_ts"] = channel, ts
        return ts

    def _fetch(self, url, payload):
        try:
            req = urllib.request.Request(
                url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), method="POST",
                headers={"Content-Type": "application/json; charset=utf-8",
                         "Authorization": f"Bearer {self.token()}"})
            with self._urlopen(req, timeout=self.timeout) as resp:
                headers, raw = resp.headers, resp.read(MAX_RESPONSE)
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code == 429:
                raise Deferred(retry_after(exc.headers)) from None
            if exc.code in (401, 403):
                raise _AuthFailed(f"http {exc.code}") from None
            raise SendError(f"slack http {exc.code}") from None
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            reason = getattr(exc, "reason", None) or type(exc).__name__
            raise SendError(f"slack unreachable: {_one_line(reason, 120)}") from None
        except (ValueError, http.client.HTTPException) as exc:
            # e.g. http.client refusing a header value: its message would quote the token.
            raise SendError(f"slack request failed: {type(exc).__name__}") from None
        return headers, raw

    def _api(self, url, payload):
        headers, raw = self._fetch(url, payload)
        try:
            doc = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise SendError("slack: unreadable response") from None
        if not doc.get("ok"):
            err = _one_line(str(doc.get("error") or "unknown"), 80)
            if err == "ratelimited":
                raise Deferred(retry_after(headers))
            if err in AUTH_ERRORS:
                raise _AuthFailed(err)
            if err in TRANSIENT_ERRORS:
                raise SendError(f"slack error: {err}")
            raise Rejected(f"slack error: {err}")
        return doc.get("ts")
