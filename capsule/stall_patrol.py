"""Stall class `patrol_agent_down` (docs/architecture/components/patrol.md): the patrol's agent
failed `DOWN_AFTER` calls in a row (its beat says `agent_down`). The patrol itself keeps running as
a script. `stall` registers `DETECTORS`. The beat is `<home>/patrol.beat`; a test passes `home` in
the probes."""
import json
from pathlib import Path

from capsule import paths

PHRASE = {"patrol_agent_down": ""}
TEXT = {"patrol_agent_down": "the patrol agent failed 3 calls in a row (the script still runs)"}


def _agent_down(conn, cfg, now, probes):
    home = Path((probes or {}).get("home") or paths.home())
    try:
        beat = json.loads((home / "patrol.beat").read_text())
    except (OSError, ValueError):
        return []
    if not isinstance(beat, dict) or not beat.get("agent_down"):
        return []
    return [{"class": "patrol_agent_down", "subject": cfg.get("board") or "board",
             "since": beat.get("agent_down_since") or beat.get("at") or now,
             "evidence": ["3 failed agent calls in a row"], "grace": 0, "l2_after": 0}]


DETECTORS = (_agent_down,)
