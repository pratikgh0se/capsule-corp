"""legacy_gate.py - the one LEGACY_PRESENT switch and the one Capsule notice sender (C24b)."""
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def legacy_present():
    """Legacy branches run only with LEGACY_PRESENT=1 (default 0)."""
    return os.environ.get("LEGACY_PRESENT") == "1"


def outbox_send(message, cls):
    """Queue one notice through `capsule outbox-send` (the path heal.sh uses). True on a
    clean exit; never raises, so an outage degrades to "retry next tick"."""
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(
        [str(REPO)] + ([os.environ["PYTHONPATH"]] if os.environ.get("PYTHONPATH") else []))}
    try:
        r = subprocess.run([sys.executable, "-m", "capsule", "outbox-send", cls, message],
                           env=env, capture_output=True, timeout=30)
        return r.returncode == 0
    except Exception:
        return False
