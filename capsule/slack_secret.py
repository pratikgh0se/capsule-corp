"""What the Slack listener trusts with a secret (stdlib only, so the listener's import closure
stays this file and `capsule/__init__.py`): the token reader, the redirect refusal and the
log masking. `capsule.slack` re-exports these names."""
import os
import re
import subprocess
import urllib.request
from pathlib import Path

TOKEN_NAME = "SLACK_BOT_TOKEN"
SECRET_PROJECT = "capsule-corp"
REPO_SECRET_BIN = Path(__file__).resolve().parents[1] / "bin" / "capsule-secret"
MAX_RESPONSE = 65536
TOKEN_RE = re.compile(r"xoxb-[A-Za-z0-9-]+")
SECRET_RE = re.compile(r"(xox[a-z]|xapp)-[A-Za-z0-9-]+|(https?://)?hooks\.slack\.com/\S+")


class SendError(Exception):
    """One delivery failed; the row stays pending and the channel backs off."""


def secret_token(identity, *, secret_bin=None, timeout=30, name=TOKEN_NAME, pattern=None):
    """The token `name` (the bot token by default) from bin/capsule-secret at call time, read as
    `identity` (the config's, never an inherited CAPSULE_IDENTITY); `pattern` (a compiled regex)
    replaces the bot-token shape check, e.g. for the listener's `xapp-` token."""
    if not identity:
        raise SendError("slack token unavailable: no identity")
    exe = secret_bin or os.environ.get("CAPSULE_SECRET_BIN") or str(REPO_SECRET_BIN)
    env = dict(os.environ, CAPSULE_IDENTITY=identity)
    try:
        out = subprocess.run([exe, "get", SECRET_PROJECT, name], env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SendError(f"slack token unavailable: {type(exc).__name__}") from None
    token = out.stdout.decode("utf-8", "replace").strip()
    if out.returncode != 0 or not token:
        raise SendError(f"slack token unavailable: capsule-secret exit {out.returncode}")
    if pattern is not None:
        if not pattern.fullmatch(token):
            raise SendError("slack token unavailable: malformed")
        return token
    return check_token(token)


def check_token(token):
    if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
        raise SendError("slack token unavailable: malformed")
    return token


def default_opener():
    """An opener that refuses redirects: one would replay the bearer token to wherever it points,
    so the 3xx surfaces as an HTTPError (-> SendError)."""
    handler = urllib.request.HTTPRedirectHandler()
    handler.redirect_request = lambda *args: None
    return urllib.request.build_opener(handler).open
