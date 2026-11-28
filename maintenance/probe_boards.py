"""The boards the Lookout probe reads (row 478 D8: $CAPSULE_HOME, never the old system's home)."""
import os
from pathlib import Path


def kanban_dbs(home):
    """Every $CAPSULE_HOME/boards/*/kanban.db, sorted. `home` is the user HOME, used
    only for capsule.paths' own default when CAPSULE_HOME is unset."""
    root = os.environ.get("CAPSULE_HOME") or Path(home) / ".local" / "share" / "capsule"
    return sorted((Path(root) / "boards").glob("*/kanban.db"))
