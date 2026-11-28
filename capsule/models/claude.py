"""The `claude -p` adapter, on the host subscription login (plan P1). Same
invocation shape as maintenance/run.sh:717 (`claude -p ... --output-format
json`) and the same rate-limit-window parse as run.sh:834-844, reimplemented
here in pure Python (no shell, no `date -j`) so it is portable and testable.
"""
import json
import os
import re
import subprocess
import time as _time
import uuid
from datetime import datetime, timedelta

from capsule import workspace as _workspace
from capsule.runners import host_wall

from . import Result, with_retries

# Subscription-only (row 380): never let an API key reach the subprocess env.
API_KEY_RE = re.compile(r"^(ANTHROPIC_API_KEY|.*_API_KEY)$")

RATE_LIMIT_RE = re.compile(r"hit your session limit|usage limit|rate limit", re.I)
# Bounded to an error-envelope `result`/stderr string, anchored to the CLI's
# own "API Error: 5xx" form where possible, so a UUID or a duration number
# elsewhere in the JSON envelope can never trip this.
TRANSIENT_RE = re.compile(r"API Error:\s*5\d\d|\boverloaded\b|\b529\b|\bECONNRESET\b|\btimed out\b", re.I)
# The CLI's own text when the host login is gone. A 401/403 is_error envelope that is not this
# line is `account` (billing, org, a bad key), see `_refused`.
AUTH_LINES = ("Not logged in · Please run /login",)
ACCOUNT_PAUSE = 1800    # seconds the account waits after a 401/403 refusal (billing, org)
AUTH_PAUSE = 300   # seconds the board waits before it tries again
RESET_RE = re.compile(r"resets\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)", re.I)
# Result.tokens key -> the envelope's `usage` field (row 584: cache writes are fresh spend).
USAGE_FIELDS = {"in": "input_tokens", "out": "output_tokens",
                "cache_read": "cache_read_input_tokens",
                "cache_write": "cache_creation_input_tokens"}


def _cli_schema(schema):
    return {k: v for k, v in schema.items() if k not in ("$schema", "$id")}


def build_argv(route, bundle, workspace, limits):
    argv = [
        "claude", "-p",
        "--model", route["model"],
        "--output-format", "json",
        # The CLI's schema validator rejects the JSON Schema meta keys ($schema draft URI, $id),
        # so hand it the structural schema only (live smoke 2026-09-26).
        "--json-schema", json.dumps(_cli_schema(limits["schema"])),
        # dontAsk (the default) denies every tool not pre-approved; a writer passes acceptEdits,
        # which accepts edits in cwd and --add-dir only
        "--permission-mode", limits.get("permission_mode", "dontAsk"),
        "--restricted",
        "--strict-mcp-config",
        "--max-turns", str(route["turn_budget"]),
        "--append-system-prompt", bundle["system"],
    ]
    if route.get("reasoning_effort"):
        argv += ["--effort", route["reasoning_effort"]]
    if route.get("fallback"):
        argv += ["--fallback-model", route["fallback"]]
    if "tools" in limits:
        argv += ["--tools", limits["tools"]]       # built-in tool names only
    if limits.get("allowed"):
        argv += ["--allowedTools", limits["allowed"]]
    for d in limits.get("add_dirs") or ():
        argv += ["--add-dir", d]
    if limits.get("mcp_config"):
        argv += ["--mcp-config", limits["mcp_config"]]
    if limits.get("resume"):  # the checkpoint resumes a session
        argv += ["--resume", limits["resume"]]
    if limits.get("session_id"):    # a brainstorm's first turn names its session (D229)
        argv += ["--session-id", limits["session_id"]]
    return argv


def _retry_after(text, now):
    m = RESET_RE.search(text)
    if not m:
        return now + 3600
    hour = int(m.group(1)) % 12
    if m.group(3).lower() == "pm":
        hour += 12
    minute = int(m.group(2) or 0)
    target = datetime.fromtimestamp(now).replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target.timestamp() <= now:
        target += timedelta(days=1)
    return int(target.timestamp())


def _lost_login(obj, stderr, rc):
    """True only on the CLI's own line, whole: an error envelope whose stripped `result` is a
    listed line, or (no envelope) a non-zero exit with that line alone on a stderr line. A text
    that merely quotes it is not a lost login."""
    if obj is not None:
        result = obj.get("result")
        return (bool(obj.get("is_error")) and isinstance(result, str)
                and result.strip() in AUTH_LINES)
    return rc != 0 and any(ln.strip() in AUTH_LINES for ln in stderr.splitlines())


def _refused(obj, stderr, rc):
    """The account is refused (billing, organisation), whatever the role: an error envelope with
    api status 401 or 403 that is not the lost-login line."""
    return (bool(obj.get("is_error")) and obj.get("api_error_status") in (401, 403)
            and not _lost_login(obj, stderr, rc))


def _finish(error_class, obj, stderr, rc, text, now):
    """(error_class, retry_after): a fatal run that is a lost login is `auth_required`, 5 min."""
    if error_class == "fatal" and _lost_login(obj, stderr, rc):
        return "auth_required", now + AUTH_PAUSE
    if error_class == "account":
        return error_class, now + ACCOUNT_PAUSE
    return error_class, _retry_after(text, now) if error_class == "rate_limit" else None


def parse(stdout, stderr, rc, *, now=None):
    now = _time.time() if now is None else now
    text = f"{stdout}\n{stderr}"  # only for the no-JSON branch (nothing to isolate a `result` field from)

    try:
        obj = json.loads(stdout) if stdout else None
        if not isinstance(obj, dict):
            obj = None
    except ValueError:
        obj = None

    report = None
    tokens = dict.fromkeys(USAGE_FIELDS, 0)
    session_id = None

    if obj is not None:
        session_id = obj.get("session_id")
        usage = obj.get("usage") or {}
        tokens = {key: usage.get(field, 0) for key, field in USAGE_FIELDS.items()}
        report = obj.get("structured_output")
        result_field = obj.get("result")
        if report is None and isinstance(result_field, str):
            try:
                parsed = json.loads(result_field)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict):
                report = parsed

        is_error = bool(obj.get("is_error"))
        # Rate-limit/transient text only ever comes from an error envelope's own
        # `result` string (never `structured_output`, never a success `result`)
        # plus stderr - a worker's report that happens to mention "rate limit"
        # must never be misread as one.
        error_text = stderr + "\n" + (result_field if isinstance(result_field, str) else "")

        if obj.get("subtype") == "error_max_turns":
            error_class = "max_turns"
        elif (rc != 0 or is_error) and RATE_LIMIT_RE.search(error_text):
            error_class = "rate_limit"
        elif is_error and TRANSIENT_RE.search(error_text):
            error_class = "transient"
        elif _refused(obj, stderr, rc):
            error_class = "account"
        elif is_error or rc != 0:
            error_class = "fatal"
        else:
            error_class = None
    else:
        # Unparsable/empty stdout is never a guess: rate_limit if the reset
        # text is there (rc != 0 - a clean exit never carries that message),
        # fatal otherwise, even with rc 0.
        if rc != 0 and RATE_LIMIT_RE.search(text):
            error_class = "rate_limit"
        else:
            error_class = "fatal"

    error_class, retry_after = _finish(error_class, obj, stderr, rc, text, now)
    raw = stdout if stdout else stderr  # never silently drop stderr
    return Result(report, rc, tokens, 1, error_class, retry_after, session_id, raw, stderr)


def _clean_env(limits=None, workspace=None):
    """The subprocess env: no API key (row 380), and any git the CLI runs on the host is
    pinned to the tick's git dir for this clone with hooks and fsmonitor off.

    `CAPSULE_WORKER=1` marks this as a dispatched worker's own
    process, not the owner's interactive session: a user-level Claude Code hook (the
    `capsule unblocked` SessionStart/Stop pair, and any future one) checks this before
    injecting context or blocking a stop, since a worker judged against a turn budget and a
    structured-output contract must never have its own tick-owned session steered by a hook
    meant for the owner's terminal.
    """
    env = _workspace.hardening_env({k: v for k, v in os.environ.items() if not API_KEY_RE.match(k)})
    env["CAPSULE_WORKER"] = "1"
    if (limits or {}).get("git_dir"):
        env["GIT_DIR"], env["GIT_WORK_TREE"] = limits["git_dir"], str(workspace)
    return env


class ClaudeAdapter:
    """Adapter over the `claude` CLI (spec §8.4). `route` supplies
    model/turn_budget/fallback; per-call `limits` supplies schema/tools/
    mcp_config/resume.
    """

    def __init__(self, route):
        self.route = route

    def run(self, role, bundle, workspace, limits):
        def call():
            argv = build_argv(self.route, bundle, workspace, limits)
            sid = None
            if limits.get("new_session") and not (limits.get("resume") or limits.get("session_id")):
                sid = str(uuid.uuid4())     # a fresh id per attempt: a retry never reuses one
                argv += ["--session-id", sid]
            if role == host_wall.TESTER_ROLE:    # its transcript would hold what it read
                argv.append("--no-session-persistence")
            if limits.get("wall"):    # host wall (D161): a planned wall, or no session at all
                try:
                    argv = host_wall.apply(argv, limits["wall"])
                except host_wall.WallUnavailable as exc:
                    retry = int(_time.time()) + exc.wait if exc.retry else None
                    return Result(None, 1, {"in": 0, "out": 0, "cache_read": 0}, 1,
                                  exc.error_class, retry, None, f"host wall unavailable: {exc}")
            try:
                proc = subprocess.run(
                    argv,
                    input=bundle["prompt"],
                    capture_output=True,
                    text=True,
                    cwd=workspace,
                    timeout=limits.get("timeout", 3600),
                    env=_clean_env(limits, workspace),
                )
            except FileNotFoundError as exc:
                return Result(None, 127, {"in": 0, "out": 0, "cache_read": 0}, 1, "fatal", None, None, str(exc))
            except subprocess.TimeoutExpired as exc:
                # A hung subprocess produced no valid JSON. Return exit 124 and
                # class `fatal` (never retried). `raw` is the one line that
                # names the cap; the partial stdout and stderr go in `stderr`.
                out = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
                err = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
                partial = out + err
                head = f"model call timed out after {exc.timeout:g}s"
                zero = {"in": 0, "out": 0, "cache_read": 0}
                return Result(None, 124, zero, 1, "fatal", None,
                              limits.get("resume") or limits.get("session_id") or sid,
                              head, partial)
            return parse(proc.stdout, proc.stderr, proc.returncode)

        return with_retries(call)
