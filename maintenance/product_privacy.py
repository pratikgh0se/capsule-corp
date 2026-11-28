#!/usr/bin/env python3
"""Gates G8 and G9 over the product repos, asserted from capsule-corp."""
import re
import subprocess
from pathlib import Path

FIRST_PARTY = ("localhost", "127.0.0.1", "0.0.0.0", "[::1]")

_LOG_DEF_RE = re.compile(r"^([ \t]*)def\s+log_message\s*\(", re.MULTILINE)
_ADDRESS_RE = re.compile(r"client_address|X-Forwarded-For|REMOTE_ADDR|X-Real-IP", re.IGNORECASE)
_URL_RE = re.compile(r"https?://([A-Za-z0-9._:\-\[\]]+)")
_QUIET_BODY = {"pass", "return", "return None", "..."}


def _log_message_body_is_quiet(text, lines, match):
    """The non-blank, non-comment, non-docstring statements in one
    `log_message` def's body, tested against `_QUIET_BODY`."""
    indent = len(match.group(1))
    start = text[: match.start()].count("\n")
    body, in_doc = [], False
    for raw in lines[start + 1:]:
        if not raw.strip():
            continue
        cur = len(raw) - len(raw.lstrip())
        if cur <= indent:
            break                      # dedented out of the function
        line = raw.strip()
        if in_doc:
            if line.endswith('"""') or line.endswith("'''"):
                in_doc = False
            continue
        if line.startswith('"""') or line.startswith("'''"):
            # a one-line docstring closes on the same line
            in_doc = not (len(line) > 3 and (line.endswith('"""') or line.endswith("'''")))
            continue
        if line.startswith("#"):
            continue
        body.append(line)
    return bool(body) and all(b in _QUIET_BODY for b in body)


def suppresses_request_logging(text):
    """True when `log_message` is overridden and EVERY such definition's body does
    nothing but pass."""
    text = text or ""
    matches = list(_LOG_DEF_RE.finditer(text))
    if not matches:
        return False
    lines = text.splitlines()
    return all(_log_message_body_is_quiet(text, lines, m) for m in matches)


def reads_client_address(text):
    """1-based line numbers where the source touches the peer address, by any
    of its four names. Comment-only lines do not count - a mention is not a
    read - but anything else does, including a header lookup."""
    out = []
    for i, raw in enumerate((text or "").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if _ADDRESS_RE.search(line):
            out.append(i)
    return out


def _parse_host(raw):
    """Strip a trailing port and trailing sentence punctuation from one regex-captured
    host."""
    if raw.startswith("["):
        end = raw.find("]")
        bare = raw[: end + 1] if end != -1 else raw
    else:
        bare = raw.split(":", 1)[0]
    return bare.rstrip(".")


def outbound_hosts(text):
    """Sorted, deduped non-first-party hosts this source would contact, reading only the
    literal `http(s)://host` text on each line."""
    hosts = set()
    for raw in (text or "").splitlines():
        line = raw.strip()
        if line.startswith("#") or line.startswith("//"):
            continue
        for host in _URL_RE.findall(line):
            bare = _parse_host(host)
            if bare not in FIRST_PARTY:
                hosts.add(bare)
    return sorted(hosts)


def sibling_source(repo, ref, path):
    """`git show <ref>:<path>` from a sibling checkout, or None when the checkout, the
    ref, the file, or usable content is absent."""
    repo = Path(repo)
    if not (repo / ".git").exists():
        return None
    try:
        out = subprocess.run(["git", "-C", str(repo), "show", f"{ref}:{path}"],
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0 or not out.stdout:
        return None
    return out.stdout
