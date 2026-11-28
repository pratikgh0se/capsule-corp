"""`claude -r` resume pointers and their expiry (Capsule row 128, M085).

Five vault notes carry resume handles today and none carries an expiry. A
Claude Code transcript is not kept forever (~30 days), so a bare handle is a
pointer that reads as valid, is offered to the owner in a huddle or a brief, and
fails at the moment he tries to use it. The fix is a date beside every handle
and a Sunday pass that marks the ones that have passed.

Read-only: this module REPORTS. Row 50's rule (the reconciler reports, never
repairs) is the house style for vault maintenance, and the weekly job does the
marking under its own write guard.

Run directly against a vault to print a scan without going through the
weekly job:

    python3 maintenance/resume_pointers.py --scan [vault]

Prints one line per pointer: `<rel path>\t<handle>\t<expires or ->`. `vault`
defaults to the configured vault path.
"""
import datetime
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vault_conf  # noqa: E402

SESSION_RE = re.compile(r"claude\s+-r\s+`?([A-Za-z0-9_\-]{8,})`?")
EXPIRES_RE = re.compile(r"expires?:\s*(\d{4}-\d{2}-\d{2})", re.IGNORECASE)
TRANSCRIPT_DAYS = 30
_SKIP = {".obsidian", ".git", ".trash", "_trash", ".claude", "copilot",
         ".smart-env", ".claude-runs", "node_modules", "__pycache__", "_archive"}


def default_expiry(from_date):
    d = datetime.date.fromisoformat(str(from_date))
    return (d + datetime.timedelta(days=TRANSCRIPT_DAYS)).isoformat()


def scan(vault, today=None):
    """One dict per handle: {path, line, session, expires, state}.

    `state` is `live`, `expired` or `no-expiry`. A handle whose expiry cannot be
    parsed counts as `no-expiry`, not as live: the failure mode to avoid is a
    dead pointer that looks alive.
    """
    today = datetime.date.fromisoformat(str(today)) if today else datetime.date.today()
    out = []
    for md in sorted(Path(vault).rglob("*.md")):
        if any(part in _SKIP for part in md.relative_to(vault).parts):
            continue
        try:
            lines = md.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines, start=1):
            m = SESSION_RE.search(line)
            if not m:
                continue
            # The expiry may sit on the handle's own line or the one after it.
            window = line + " " + (lines[i] if i < len(lines) else "")
            e = EXPIRES_RE.search(window)
            if not e:
                state, expires = "no-expiry", None
            else:
                expires = e.group(1)
                try:
                    state = "expired" if datetime.date.fromisoformat(expires) < today else "live"
                except ValueError:
                    state, expires = "no-expiry", None
            out.append({"path": md.relative_to(vault).as_posix(), "line": i,
                        "session": m.group(1), "expires": expires, "state": state})
    return out


def summary(found):
    if not found:
        return "resume pointers: none"
    counts = {}
    for f in found:
        counts[f["state"]] = counts.get(f["state"], 0) + 1
    return "resume pointers: " + ", ".join(f"{n} {s}" for s, n in sorted(counts.items()))


def _main(argv):
    if not argv or argv[0] != "--scan":
        print(__doc__)
        return 2
    vault = argv[1] if len(argv) > 1 else vault_conf.vault()
    if vault is None:
        print(vault_conf.NO_VAULT, file=sys.stderr)
        return 0
    found = scan(vault)
    for f in found:
        print(f"{f['path']}\t{f['session']}\t{f['expires'] or '-'}")
    print(summary(found), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
