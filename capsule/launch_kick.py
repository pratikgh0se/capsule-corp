"""The live `launchctl kickstart` probe (the tick and the patrol pass it as `kickstart`)."""
import os
import subprocess


def kickstart(label):
    """Live probe: `launchctl kickstart -k` of one label; True when it ran (never under
    AGENT_OPS_NO_LAUNCHCTL=1)."""
    if os.environ.get("AGENT_OPS_NO_LAUNCHCTL") == "1":
        return True
    try:
        r = subprocess.run([os.environ.get("CAPSULE_LAUNCHCTL", "launchctl"), "kickstart", "-k",
                            f"gui/{os.getuid()}/{label}"], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0
