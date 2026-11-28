#!/usr/bin/env python3
"""absence.py - how long nobody has approved anything, and what the pipeline
does about it (spec docs/superpowers/specs/
2026-09-10-continuity-and-data-safety-design.md section 7.5, decision row 137,
[[Study - The founder's week at Capsule Corp]] sections 6.4 and 7.3).

    TIER 1  <48 h    CONTINUE   cards run to the gate; nothing merges
    TIER 2  2-7 d    DRAIN      no new specs, no new dispatch; finish what is
                                in flight; every custodial job keeps running
    TIER 3  >7 d     PARK       page once, weekly heartbeat, `capsule park`

The tiers exist because absence fails by ACCUMULATION, not breakage (study
6.1): specs nobody approved, findings nobody triaged, branches nobody merged,
against a codebase that has not moved. DRAIN therefore stops *starting* work
and never stops heal/backup/restore-verify/obs-probe/cleanup - "during an
absence the custodial jobs matter more and the productive jobs matter less".
This is emphatically NOT the old system's `pause`, which would stop exactly the wrong
half.

THE PREDICATE, and why it is narrower than the study's wording. The study says
"no approval stamp written AND no controller session started for N days". The
first half is on disk and exact: every file under $MERGE_GATE_STATE/approvals/
was written by a bin/ verb the owner ran (approve-release.sh -> approvals/release/
<repo>-<tag>.json; approve-spec.sh -> approvals/spec/<slug>.json). The second
half is not readable without ~/.claude, so it is an explicit file instead -
state/human-seen, written by `capsule park` and by hand (`absence.py seen`, or
plain `touch`). Consequence, written into the RUNBOOK in these words: a
terminal session that approves nothing does not reset the clock. That is
deliberate - a session that approves nothing has not unblocked any of the
things these tiers are about.

Everything is behind OPS_ABSENCE_PROTOCOL. Off - the default - every reader
returns tier 1 and no caller changes behaviour.

CLI:  absence.py state | tier | hold <1|2|3> | clear | seen
"""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import events  # noqa: E402
import verdicts  # noqa: E402  - ops_env() is the shared flag-reading idiom

V = 1
CONTINUE, DRAIN, PARK = 1, 2, 3
NAMES = {CONTINUE: "continue", DRAIN: "drain", PARK: "park"}
DEFAULT_GATE_STATE = Path.home() / ".local" / "state" / "capsule-corp" / "merge-gate"
DRAIN_AFTER_S = 48 * 3600
PARK_AFTER_S = 7 * 86400
_ON = {"1", "true", "yes", "on"}


def enabled(env=None):
    """Anything unrecognised is OFF: a typo must not stop the pipeline."""
    env = env if env is not None else verdicts.ops_env()
    return str(env.get("OPS_ABSENCE_PROTOCOL") or "").strip().lower() in _ON


def _approvals_dir():
    """Same override-or-refuse rule as merge_gate.state_dir(): under
    AGENT_OPS_NO_SEND with no MERGE_GATE_STATE, falling through to the real
    ~/.local/state path is always a bug (the 2026-09-07 incident class)."""
    override = os.environ.get("MERGE_GATE_STATE")
    if not override and os.environ.get("AGENT_OPS_NO_SEND"):
        raise RuntimeError("refusing to touch the default state dir under test: "
                            "set MERGE_GATE_STATE explicitly")
    return Path(override or DEFAULT_GATE_STATE) / "approvals"


def _state_dir():
    """Same override-or-refuse rule as merge_gate.state_dir(): under
    AGENT_OPS_NO_SEND with no AGENT_OPS_STATE_DIR, falling through to the real
    ~/.local/state path is always a bug (the 2026-09-07 incident class)."""
    override = os.environ.get("AGENT_OPS_STATE_DIR")
    if not override and os.environ.get("AGENT_OPS_NO_SEND"):
        raise RuntimeError("refusing to touch the default state dir under test: "
                            "set AGENT_OPS_STATE_DIR explicitly")
    return Path(override or events.state_root())


def _seen_path():
    return _state_dir() / "human-seen"


def _hold_path():
    return _state_dir() / "absence.json"


def last_human_ts():
    """Newest mtime across every approval stamp and state/human-seen, or None
    when neither has ever existed. None is tier 1, never tier 3."""
    newest = None
    d = _approvals_dir()
    try:
        for p in d.rglob("*"):
            if p.is_file():
                m = p.stat().st_mtime
                newest = m if newest is None or m > newest else newest
    except OSError:
        pass
    try:
        m = _seen_path().stat().st_mtime
        newest = m if newest is None or m > newest else newest
    except OSError:
        pass
    return newest


def _held():
    """A hand-set tier, written by `capsule park` or `absence.py hold`. An
    unreadable, malformed or unknown-v file reads as absent - never as a tier -
    because the failure mode of a wrong tier is a pipeline that stops when
    nobody asked it to."""
    try:
        d = json.loads(_hold_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict) or d.get("v") != V:
        return None
    t = d.get("held")
    return t if t in NAMES else None


def state(now=None):
    now = time.time() if now is None else now
    last = last_human_ts()
    held = _held()
    if last is None:
        computed, age_h = CONTINUE, None
    else:
        age = max(0.0, now - last)
        age_h = round(age / 3600.0, 1)
        computed = PARK if age > PARK_AFTER_S else DRAIN if age > DRAIN_AFTER_S else CONTINUE
    t = held or computed
    if not enabled():
        t = CONTINUE
    return {"v": V, "tier": t, "name": NAMES[t], "computed": computed,
            "held": held, "since": last, "age_h": age_h,
            "days": None if age_h is None else int(age_h // 24),
            "enabled": enabled()}


def tier(now=None):
    return state(now)["tier"]


def block_reason(now=None):
    """The string the dispatcher stores on a queued card. None on tier 1 and
    whenever the flag is off, so an off flag changes no behaviour and no
    message."""
    s = state(now)
    if s["tier"] == CONTINUE:
        return None
    d = s["days"]
    return (f"absence: {s['name']} day {d if d is not None else '?'}"
            f" - no approval for {d if d is not None else '?'}d")


def brief_line(now=None):
    """One line for report.sh / status.sh, or None on tier 1 / flag off - the
    absent-entirely rule the status.sh blocks follow: no heading
    either, when there is nothing to say."""
    s = state(now)
    if s["tier"] == CONTINUE:
        return None
    return (f"absence day {s['days']}: tier {s['tier']} ({s['name']})"
            f"{' - held by hand' if s['held'] else ''}; custodial jobs still running")


def _write_hold(t):
    p = _hold_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"v": V, "held": t, "ts": time.time()}), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


def hold(t):
    if t not in NAMES:
        raise ValueError(f"tier must be one of {sorted(NAMES)}")
    _write_hold(t)


def clear():
    try:
        _hold_path().unlink()
    except OSError:
        pass


def seen():
    p = _seen_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch()
    os.chmod(p, 0o600)
    os.utime(p, None)


def main(argv):
    cmd = (argv[0] if argv else "state")
    if cmd == "state":
        print(json.dumps(state(), indent=1, default=str))
    elif cmd == "tier":
        print(tier())
    elif cmd == "hold":
        hold(int(argv[1]))
        print(f"absence: held at tier {argv[1]}")
    elif cmd == "clear":
        clear()
        print("absence: hold cleared")
    elif cmd == "seen":
        seen()
        print("absence: clock reset")
    else:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
