"""Log size caps, check only (stall supervisor unit 4b, docs/architecture/components/
maintenance-jobs.md). `python3 -m capsule.log_caps` lists each long-lived log and says whether the
`cleanup` job (maintenance/cleanup.sh) has a size rule that names it. It deletes nothing: a new
delete rule needs a D204 decision. Exit 0 when each log has a cap, 1 when one has none."""
import sys
from pathlib import Path

from capsule import paths

# (name, where it lives under the Capsule home, the text a cleanup rule for it must contain)
LOGS = (("tick log", "logs/tick*.log", "tick*.log"),
        ("worker log", "<spool>/<run>/worker.log", "worker.log"),
        ("launchd out", "<config>/maintenance/logs/launchd-*.out", "*.out"),
        ("launchd err", "<config>/maintenance/logs/launchd-*.err", "*.err"),
        ("outbox.log", "boards/<board>/outbox.log", "outbox.log"),
        ("patrol.log", "patrol.log", "patrol.log"))


def check(script=None):
    """[(name, where, capped)]: `capped` is True when the cleanup script names the log."""
    text = Path(script or paths.REPO / "maintenance" / "cleanup.sh").read_text(encoding="utf-8")
    return [(name, where, needle in text) for name, where, needle in LOGS]


def main():
    rows = check()
    for name, where, capped in rows:
        print(f"{name}: {'capped' if capped else 'NO CAP'} ({where})")
    return 0 if all(c for _, _, c in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
