#!/usr/bin/env python3
"""Capsule chat plugin helper: the Claude Code status line and the read-only commands.

    capsule_chat.py statusline   one line for Claude Code's statusLine (JSON session data on stdin)
    capsule_chat.py status       the factory screen (`python3 -m capsule status`)
    capsule_chat.py needs-you    only what waits on the user, from `status --json`

A thin wrapper over the Capsule CLI: it only reads (`status` verbs), never writes a board, never
prints secrets or internal ids, and always exits 0 with a short plain message when Capsule is
missing or down, because a non-zero exit would abort a slash command and blank a status line.
Python standard library only.
Design: docs/superpowers/specs/2026-10-29-capsule-chat-plugin-design.md
"""
import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

TIMEOUT_S = 8       # one CLI call; the status line must never hang the terminal
CACHE_TTL_S = 10    # the status line re-runs on every message; one CLI call per 10 s is enough
NOT_FOUND = "Capsule is not reachable from here. Set CAPSULE_REPO to the capsule-corp checkout."
UNAVAILABLE = "Capsule did not answer (status could not be read)."
NO_ANSWER = "no answer"
MARKETPLACE = "capsule-corp"
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


class CapsuleError(Exception):
    """Capsule could not be found or did not answer; the message is safe to show."""

    def __init__(self, message, short):
        super().__init__(message)
        self.short = short      # the status line's version of the message


def clean(text):
    """Card names and asks are untrusted text: no escape sequences or control characters."""
    return _CONTROL.sub(" ", str(text))


def _find_repo(cwd=None, env=None):
    """The capsule-corp checkout. CAPSULE_REPO is used alone when set (no fallback); otherwise the
    current directory or a parent that carries the Capsule marketplace file (a strong marker, so an
    unrelated folder's `python -m capsule` is never run)."""
    env = os.environ if env is None else env
    if env.get("CAPSULE_REPO"):
        repo = Path(env["CAPSULE_REPO"]).expanduser()
        return repo if (repo / "capsule" / "__main__.py").is_file() else None
    here = Path(cwd or os.getcwd()).resolve()
    return next((p for p in (here, *here.parents) if _is_checkout(p)), None)


def _is_checkout(p):
    market = p / ".claude-plugin" / "marketplace.json"
    if not (p / "capsule" / "statusview.py").is_file():
        return False
    with contextlib.suppress(OSError, ValueError, AttributeError):
        return json.loads(market.read_text()).get("name") == MARKETPLACE
    return False


def run_capsule(repo, args, runner=subprocess.run):
    """stdout of `python3 -m capsule <args>` run in `repo`; CapsuleError on any failure."""
    try:
        done = runner([sys.executable, "-m", "capsule", *args], cwd=repo, capture_output=True,
                      text=True, timeout=TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        raise CapsuleError(UNAVAILABLE, NO_ANSWER) from None
    if done.returncode != 0:
        raise CapsuleError(UNAVAILABLE, NO_ANSWER)
    return done.stdout


def _cache_file(repo, env):
    root = Path(env.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "capsule-chat"
    key = hashlib.sha1(f"{repo}|{env.get('CAPSULE_HOME', '')}".encode()).hexdigest()[:12]
    return root / f"status-{key}.json"


def snapshot(repo, env=None, runner=subprocess.run, now=None, cached=True):
    """`status --json` as a dict. With `cached`, a copy under 10 s old is reused (CPU: the status
    bar was switched off once for load)."""
    env, now = os.environ if env is None else env, now if now is not None else time.time()
    cache = _cache_file(repo, env)
    if cached:
        with contextlib.suppress(OSError, ValueError):
            if now - cache.stat().st_mtime < CACHE_TTL_S:
                return json.loads(cache.read_text())
    try:
        snap = json.loads(run_capsule(repo, ["status", "--json"], runner))
    except ValueError:
        raise CapsuleError(UNAVAILABLE, NO_ANSWER) from None
    if cached:
        with contextlib.suppress(OSError):
            cache.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            tmp = cache.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(snap))
            os.replace(tmp, cache)
    return snap


def _ago(since, now):
    if not since:
        return ""
    mins = max(0, int(now - since) // 60)
    if mins >= 2880:
        return f" · {mins // 1440}d"
    return f" · {mins // 60}h" if mins >= 120 else f" · {mins}m"


def _label(item):
    return clean(item["name"]) + (f" #{item['number']}" if item.get("number") else "")


def _statusline_text(snap):
    """One line: loop dot, then the four counts the status screen leads with."""
    down = [clean(b["board"]) for b in snap["boards"] if b["loop"] != "alive"]
    counts = (f"▶️ {len(snap['running'])} running · 🙋 {len(snap['needs_you'])} need you · "
              f"🧱 {len(snap['blocked'])} blocked · 🔴 {len(snap['attention']['open'])} problems")
    return f"🔴 loop down: {', '.join(down)} · {counts}" if down else f"🟢 {counts}"


def _needs_you_text(snap):
    """The waiting list, one plain line each; a note when a loop is down (the list may be stale)."""
    now, items = snap["generated_at"], snap["needs_you"]
    lines = [f"🙋 {_label(n)} · {clean(n['ask'])}{_ago(n.get('since'), now)}" for n in items]
    down = [clean(b["board"]) for b in snap["boards"] if b["loop"] != "alive"]
    head = f"🙋 NEEDS YOU ({len(items)})" if items else "Nothing needs you right now."
    note = [f"(loop down on {', '.join(down)}; this list may be out of date)"] if down else []
    return "\n".join([head, *lines, *note])


def _session_cwd(stdin):
    """The directory Claude Code is in, from the status line's JSON; None when absent."""
    try:
        data = json.loads(stdin.read()) if not stdin.isatty() else {}
    except (ValueError, OSError):
        return None
    cwd = (data.get("workspace") or {}).get("current_dir") or data.get("cwd")
    return cwd if isinstance(cwd, str) else None


def _render(verb, stdin, env, runner):
    repo = _find_repo(_session_cwd(stdin) if verb == "statusline" else None, env)
    if repo is None:
        raise CapsuleError(NOT_FOUND, "not found (set CAPSULE_REPO)")
    if verb == "status":
        return run_capsule(repo, ["status"], runner).rstrip("\n")
    snap = snapshot(repo, env, runner, cached=verb == "statusline")
    return _statusline_text(snap) if verb == "statusline" else _needs_you_text(snap)


def main(argv=None, stdin=None, out=None, env=None, runner=subprocess.run):
    argv, stdin, out = sys.argv[1:] if argv is None else argv, stdin or sys.stdin, out or sys.stdout
    env = os.environ if env is None else env
    verb = argv[0] if argv else ""
    if verb not in ("statusline", "status", "needs-you"):
        out.write("usage: capsule_chat.py statusline | status | needs-you\n")
        return 0
    try:
        text = _render(verb, stdin, env, runner)
    except CapsuleError as e:
        text = f"🔴 capsule: {e.short}" if verb == "statusline" else str(e)
    except (KeyError, TypeError, AttributeError):    # a snapshot of an unexpected shape
        text = f"🔴 capsule: {NO_ANSWER}" if verb == "statusline" else UNAVAILABLE
    except Exception:    # last resort: never a traceback in the terminal
        text = f"🔴 capsule: {NO_ANSWER}" if verb == "statusline" else UNAVAILABLE
    out.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
