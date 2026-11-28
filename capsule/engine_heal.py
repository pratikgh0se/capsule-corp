"""The skip rule of a hung-engine hard restart (`StrongRunner._hard_restart`): the first reason
that forbids a restart, or "" when one may run. A skip prints one stderr line with its reason."""
import contextlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

from capsule import safe_write

HUNG = "the Docker engine is hung and a restart did not fix it"


def skip_reason(name, stamps, now, per_day, gap_s, keeper_busy, ask=None):
    """`stamps` are the restart times of the last 24 h; the reason is printed and returned.
    `ask` is (capsule home, engine bin dir): a cap skip then writes the CoS request."""
    why = ("the engine is not OrbStack" if name != "orbstack"
           else f"{len(stamps)} of {per_day} restarts used in 24 h" if len(stamps) >= per_day
           else f"the last restart is under {gap_s // 60} min old"
           if stamps and now - max(stamps) < gap_s
           else "the keeper is landing" if keeper_busy else "")
    if why:
        print(f"capsule: docker engine hung; restart skipped: {why}", file=sys.stderr)
        cap = name == "orbstack" and (len(stamps) >= per_day
                                      or (bool(stamps) and now - max(stamps) < gap_s))
        if ask and cap:
            with contextlib.suppress(OSError):
                _ask_cos(ask[0], why, stamps, now, ask[1])
    return why


ASK_GAP_S = 3600


def _run(cmd):
    """Stdout of a short probe (5 s), or "" when it fails or hangs."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _ask_cos(root, why, stamps, now, bin_dir):
    """A restart skipped by the cap (not "not OrbStack", not "the keeper is landing", which clear
    alone): at most once an hour write `<root>/needs-cos/engine-hung.json` for the chief of staff.
    The file holds the reason, the restart stamps and facts. True when it wrote the file."""
    root = Path(root)
    path = root / "needs-cos" / "engine-hung.json"
    with contextlib.suppress(OSError, ValueError, AttributeError, TypeError):
        if now - json.loads(path.read_text()).get("asked_at", 0) < ASK_GAP_S:
            return False
    ps = _run([f"{bin_dir}/docker", "ps", "-q", "--filter", "name=capsule"])
    facts = {"free_disk_gb": round(shutil.disk_usage(root).free / 1e9, 1),
             "orb_status": _run([f"{bin_dir}/orb", "status"]) or "orb status not answering",
             "capsule_containers": len(ps.split()) if ps else "docker not answering"}
    doc = {"kind": "engine_hung_cap", "asked_at": now, "reason": why,
           "restart_stamps": stamps, "facts": facts}
    safe_write.atomic_write(root, path, json.dumps(doc, indent=1), create_dirs=True)
    print("capsule: engine hung past the restart cap; asked the chief of staff", file=sys.stderr)
    return True
