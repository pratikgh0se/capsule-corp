#!/usr/bin/env python3
"""Policy-document presence gate (G6)."""
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path

# The contract with docs/policies/.
REQUIRED = ("terms.md", "privacy-notice.md", "security.txt",
            "support-policy.md", "subprocessors.md")

OPS_POLICY_GATE = "OPS_POLICY_GATE"
# Every new flag defaults OFF: an explicit on-list, not "anything that isn't a known
# off-value", so a typo'd/unrecognized value (e.g. "maybe") leaves the gate advisory
# instead of silently turning it blocking.
_ON_VALUES = ("1", "true", "on", "yes")
WARN_DAYS = 30
_EXPIRES_RE = re.compile(r"^\s*expires:\s*(\d{4}-\d{2}-\d{2})", re.IGNORECASE | re.MULTILINE)
_HEADING_RE = re.compile(r"^#{1,6}\s")
# A modest margin below the smallest real policy file (security.txt strips to 220 chars
# - 200 is about 9% below that, not an order of magnitude) and an order of magnitude
# above any plausible placeholder ("## Coming soon\n\n We will write this." strips to 34
# chars).
STUB_FLOOR = 200


def gate_blocking(env):
    """True when a finding fails CI (`OPS_POLICY_GATE=1`)."""
    raw = (env if env is not None else os.environ).get(OPS_POLICY_GATE)
    return raw is not None and str(raw).strip().lower() in _ON_VALUES


def parse_expires(text):
    """The `Expires` date from an RFC 9116 file, or None. RFC 9116 makes the
    field mandatory, so None is a finding, never a pass."""
    m = _EXPIRES_RE.search(text or "")
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d").date()
    except ValueError:
        return None


def _is_stub(text):
    """A body that is only the draft banner, blank lines, or headings does not count as
    present: a presence gate satisfiable by a stub or a one-line placeholder ("##
    Coming soon") enforces nothing."""
    body = [l.strip() for l in (text or "").splitlines()
            if l.strip() and not l.strip().startswith(">") and not _HEADING_RE.match(l.strip())]
    return len("".join(body)) < STUB_FLOOR


def check(policy_dir, *, today=None, env=None):
    """`(ok, lines)`. Pure apart from reading the directory."""
    today = today or date.today()
    blocking = gate_blocking(env)
    tag = "FAILED" if blocking else "advisory"
    policy_dir = Path(policy_dir)
    if not policy_dir.is_dir():
        return True, ["policies: skipped (docs/policies not created yet)"]

    lines, findings = [], []
    for name in REQUIRED:
        p = policy_dir / name
        if not p.exists():
            findings.append(f"policies: {tag} - {name} missing")
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        if _is_stub(text):
            findings.append(f"policies: {tag} - {name} is empty apart from the banner")
            continue
        if name == "security.txt":
            exp = parse_expires(text)
            if exp is None:
                findings.append(f"policies: {tag} - security.txt has no parseable Expires (RFC 9116)")
            else:
                left = (exp - today).days
                if left < 0:
                    findings.append(f"policies: {tag} - security.txt EXPIRED {exp.isoformat()} "
                                    f"({-left} days ago)")
                elif left <= WARN_DAYS:
                    lines.append(f"security.txt: expires in {left} days ({exp.isoformat()}) - "
                                 f"see the refresh checklist item in docs/RUNBOOK.md")
                else:
                    lines.append(f"security.txt: Expires {exp.isoformat()} ({left} days left)")

    if findings:
        return (not blocking), findings + lines
    return True, [f"policies: ok ({len(REQUIRED)} files)"] + lines


def main(argv=None, env=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    env = os.environ if env is None else env
    root = Path(__file__).resolve().parents[1]
    if not argv and not (root / "release" / "public-paths.txt").exists():
        # Public tree: the export drops the private policy documents, so there is nothing to check.
        print("policies: skipped (public tree: the policy documents are private and not exported)")
        return 0
    target = Path(argv[0]) if argv else root / "docs" / "policies"
    ok, lines = check(target, env=env)
    for line in lines:
        print(line)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
