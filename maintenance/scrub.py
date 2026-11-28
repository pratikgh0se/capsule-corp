#!/usr/bin/env python3
"""scrub.py - redact secret-shaped values before they reach the event spool,
Postgres, or the vault ledger.

One pattern list, `PATTERNS`, used by `scrub()` (redact) and
`contains_secret()` (detect, used by the `--check` CLI and the quality hook).
Fluent Bit's `scrub.lua` filter (Task 2) re-implements these same patterns in
Lua for log lines that never pass through Python; keep the two lists in sync
by hand - a unit test renders a fixture line through both and expects the
same redaction. Every Lua row uses the frontier pattern %f[%w_] for the
left-hand \b and writes its minimum-length floor out with string.rep (Lua has
no {n,} quantifier), so the two sides agree on both edges:

    telegram-token: ("%d%d%d%d%d%d+:" + a 20-char floor for "[%w_%-]")
                    -- min 6 digits; the floor after the colon is what keeps
                    -- a short suffix like ":10" from matching (see the
                    -- "telegram negative" fixture in test_scrub.py). It is
                    -- 20 rather than this file's 30 on purpose: a lower
                    -- floor can only over-redact.
    openai-key:     ("%f[%w_]sk%-" + a 20-char floor)  -- without the
                    frontier, "risk-free" and "task-list" redacted
    github-token:   ("%f[%w_]ghp_" + 30) / ("%f[%w_]github_pat_" + 30)
    jwt:            ("%f[%w_]eyJ" + three 10-char floors around "%.")
    infisical:      ("%f[%w_]st%." + a 20-char floor)  -- without the
                    frontier and floor, "test.db", "manifest.cafe" and
                    "host.docker.internal" redacted ([%x] accepts "d")
    kv-secret:      one explicit, case-insensitive literal pattern per key
                    name (password, passwd, token, client_secret, secret,
                    api_key, api-key, apikey), each written
                    "%f[%w_]([%w_]*<key>)=(%S+)" - the frontier for \b and
                    "[%w_]*" for this file's own "[A-Za-z0-9_]*" prefix - so
                    "x-api-key=...", "auth-token=..." and env-var-shaped
                    keys like "INFISICAL_CLIENT_SECRET=..." redact the value
                    while keeping the whole key

Spec: docs/superpowers/specs/2026-09-09-retention-egress-design.md section 4.
"""
import re
import sys

_PEM_BEGIN = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_PEM_END = re.compile(r"-----END [A-Z ]*PRIVATE KEY-----")


class _PemBlock:
    """A BEGIN..END private-key block, found in one linear pass: each END is
    searched from the end of its BEGIN, and a BEGIN with no END after it
    stops the scan (no later BEGIN can have one either), so the text is
    never rescanned. An unterminated BEGIN (a truncated record) is redacted
    to the end of the text, so the key body cannot leak. Same rule as
    scrub.lua's scrub_pem."""

    def _spans(self, text):
        pos = 0
        while True:
            begin = _PEM_BEGIN.search(text, pos)
            if begin is None:
                return
            end = _PEM_END.search(text, begin.end())
            if end is None:
                yield begin.start(), len(text)
                return
            yield begin.start(), end.end()
            pos = end.end()

    def search(self, text):
        return next(self._spans(text), None)

    def sub(self, repl, text):
        out, pos = [], 0
        for start, stop in self._spans(text):
            out += [text[pos:start], repl]
            pos = stop
        return "".join(out) + text[pos:] if out else text


# kind -> compiled pattern, checked in order. kv-secret is last: it is the
# broadest pattern and must not steal a match a more specific pattern (e.g.
# openai-key) would otherwise report first for the same span.
PATTERNS = [
    ("telegram-token", re.compile(r"(?<![\w:-])\d{6,}:[A-Za-z0-9_-]{30,}(?![\w-])")),
    ("openai-key",     re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("github-token",   re.compile(
        r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b")),
    ("jwt",            re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("infisical",      re.compile(r"\bst\.[a-f0-9-]{20,}\b")),
    ("aws-key",        re.compile(r"\bA[KS]IA[0-9A-Z]{16}\b")),
    ("slack-token",    re.compile(r"\bx(?:ox[abprs]|app)-[A-Za-z0-9-]{10,}")),
    ("google-key",     re.compile(r"\bAIza[0-9A-Za-z_-]{35}")),
    ("private-key",    _PemBlock()),
    # The `[A-Za-z0-9_]*` prefix (and the explicit "start or non-word-char"
    # lookbehind in place of `\b`, which would refuse to fire in the middle
    # of an identifier) is what catches env-var-shaped keys:
    # INFISICAL_CLIENT_SECRET=, TELEGRAM_BOT_TOKEN=, SERVICE_API_KEY=. The
    # whole key is kept in the replacement. Known cost: `mypassword=x`
    # redacts too - over-redacting an English word is the cheap direction.
    ("kv-secret",      re.compile(r"(?i)(?:(?<=^)|(?<=[^A-Za-z0-9_]))([A-Za-z0-9_]*(?:client_secret|password|passwd|token|api[_-]?key|secret))=(\S+)")),
    # `key: value` and JSON `"key": "value"`: the key contains token, secret,
    # password or api_key; an unquoted value needs 6+ characters so short
    # prose after a colon is left alone. Key and colon are kept.
    ("kv-colon",       re.compile(
        r"""(?i)((?<![A-Za-z0-9_-])"?(?=[A-Za-z0-9_-]*?"""
        r"""(?:token|secret|password|api[_-]?key))[A-Za-z0-9_-]+"?[ \t]*:[ \t]*)"""
        r"""(?:"((?!<redacted:)[^"]+)"|(?!<redacted:)([^\s,}"]{6,}))""")),
]

# a macOS home path collapses to `~/` (not a secret, so not in PATTERNS).
HOME_PATH = re.compile(r"(?<![\w./-])/Users/[A-Za-z0-9._-]+/")


def _kv_colon(m):
    value = '"<redacted:kv-secret>"' if m.group(2) is not None else "<redacted:kv-secret>"
    return m.group(1) + value


def scrub(text):
    """Return `text` with every secret-shaped substring replaced by
    `<redacted:kind>`. Non-str or empty input is returned unchanged.
    `kv-secret` keeps the `key=` prefix and replaces only the value."""
    if not isinstance(text, str) or not text:
        return text
    for kind, pattern in PATTERNS:
        if kind == "kv-secret":
            text = pattern.sub(lambda m: f"{m.group(1)}=<redacted:{kind}>", text)
        elif kind == "kv-colon":
            text = pattern.sub(_kv_colon, text)
        else:
            text = pattern.sub(f"<redacted:{kind}>", text)
    return HOME_PATH.sub("~/", text)


def contains_secret(text):
    """The kind of the first secret-shaped match in `text`, or None."""
    if not isinstance(text, str) or not text:
        return None
    for kind, pattern in PATTERNS:
        if pattern.search(text):
            return kind
    return None


def _check_stdin():
    hit = 0
    for lineno, line in enumerate(sys.stdin, start=1):
        kind = contains_secret(line)
        if kind:
            hit = 1
            print(f"scrub: {kind} at line {lineno}")
    return 1 if hit else 0


if __name__ == "__main__":
    if "--check" in sys.argv[1:]:
        sys.exit(_check_stdin())
    print(__doc__)
    sys.exit(0)
