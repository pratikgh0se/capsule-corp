"""obs_probe_util.py - pure helpers of obs_probe (percentile, error fingerprint, docker mem
parse), split out of obs_probe.py (card t_53c4b297; no behaviour change). obs_probe
re-exports them, so `obs_probe.fingerprint` etc. keep working."""
import re
import hashlib


def _percentile(sorted_vals, p):
    """Nearest-rank percentile over an already-sorted list. None on empty."""
    if not sorted_vals:
        return None
    idx = min(len(sorted_vals) - 1, int(round(p * (len(sorted_vals) - 1))))
    return sorted_vals[idx]


_HEX_RE = re.compile(r"\b[0-9a-f]{7,}\b", re.I)
_DIGIT_RE = re.compile(r"\d+")


def fingerprint(error):
    """An 8-hex digest of an error's SHAPE, not its text. Pids, epochs, task
    ids and shas differ on every run; without normalising them the same
    failure three times looks like three different failures, which is exactly
    the signal M010 wants to count. Never the raw text and never the digest
    itself - only the per-fingerprint repeat count (`error_repeats`) ships as
    a value; the digest stays in-memory as a grouping key and the text stays
    in the board db."""
    t = _HEX_RE.sub("#", str(error or "").lower())
    t = _DIGIT_RE.sub("#", t)
    return hashlib.sha1(" ".join(t.split())[:120].encode("utf-8")).hexdigest()[:8]


