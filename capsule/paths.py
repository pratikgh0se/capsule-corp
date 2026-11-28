"""Where Capsule keeps its state. CAPSULE_HOME overrides the default (row 389: no personal path)."""
import json
import os
import re
from pathlib import Path

from capsule import legacy_names
from capsule.answer_base import (  # noqa: F401  moved to answer_base, re-exported
    SCHEMAS, board_db, board_dir, config_path, default_home, home, resolve_config)

REPO = Path(__file__).resolve().parents[1]
INTENT = REPO / "contracts" / "intent"      # intent examples (row 454)
BOARD_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def board_name_problem(name):
    """Why `name` cannot be a board name, or None when it can."""
    if BOARD_NAME.fullmatch(name) and ".." not in name and name not in ("new", "tick"):
        return None     # `new` and `tick` are `capsule board`'s own words
    return ("the board name must be letters, digits, . _ - (max 64), no `..`, and not `new` "
            "or `tick`")


def image_record(home_=None):
    """What `docker/build-base.sh` built on this machine: `<ref>@sha256:<digest>` rows, outside
    any git clone, so the tick (it runs from the harness clone) and `capsule check` share it."""
    return (home_ or home()) / "images.lock"


def outbox_log(home_=None, board=None):
    """`board` -> its own `boards/<board>/outbox.log`, else the legacy `home/outbox.log`."""
    if board is not None:
        return board_dir(board, home_) / "outbox.log"
    return (home_ or home()) / "outbox.log"


def backup_dir():
    """Where `human ... amend` backs a board up before writing (row 452's one-off, made a verb).
    `CAPSULE_BACKUP_DIR` overrides the default so tests never touch the real $HOME (mirrors
    maintenance/backup.sh's own `BACKUP_DIR` override for the nightly tarball)."""
    return Path(os.environ.get("CAPSULE_BACKUP_DIR") or Path.home() / "Backups" / "capsule")


def unblocked_cache():
    """Where the `capsule unblocked --json` detector (sibling branch capsule/unblocked, row 470)
    caches its last result. `CAPSULE_UNBLOCKED_CACHE` overrides it so tests never touch the real
    one; capsule/metrics.py's exporter step tolerates the file being absent."""
    return Path(os.environ.get("CAPSULE_UNBLOCKED_CACHE") or Path.home() / ".cache" / "capsule" / "unblocked.json")


def legacy_home():
    """Legacy board home: LEGACY_KANBAN_HOME, else the old home folder under $HOME (its name lives
    in legacy_names.py, D270); `board_fence` reads it to find the boards dir."""
    return Path(os.environ.get("LEGACY_KANBAN_HOME")
                or Path.home() / legacy_names.LEGACY_HOME_DIRNAME)


def claude_projects_dir():
    """Where the `claude` CLI persists its own session transcripts, one `<slug-of-
    cwd>/<session_id>.jsonl` per session."""
    return Path(os.environ.get("CAPSULE_CLAUDE_PROJECTS_DIR") or Path.home() / ".claude" / "projects")


def queue_dir(repo_name, home_=None):
    """Row 470 `capsule unblocked`'s own branch-queue/ownership markers
    live under `$CAPSULE_HOME/queue/<repo_name>/...`, never inside the repo's own
    tree - a marker committed to a branch would inherit through a merge and silently
    apply to every later branch that carries that commit forward."""
    return (home_ or home()) / "queue" / repo_name


# The temp roots, and the lander's throwaway clone (`<temp>/lander-*/c`), named once for the
# lander, the incident fingerprint and the rerun brief.
TEMP_ROOT = r"(?:/private)?/(?:var/folders|tmp)"
LANDER_PREFIX, CLONE_DIR = "lander-", "c"
# a temp root only at the start of a path token, never mid-path or inside a URL
TEMP = r"(?<![\w.:/>-])" + TEMP_ROOT + r"(?:(?<=folders)/[^/\s]+/[^/\s]+(?:/T)?)?"
_CLONE_PATH = re.compile(TEMP + "/" + LANDER_PREFIX + r"[^/\s]*/" + CLONE_DIR + r"(?![^/\s])")


def collapse_clone(text):
    """`text` with the lander's throwaway clone root (`<temp>/lander-*/c`) written `<clone>`, so a
    landing detail names `<clone>/capsule/x.py:1`, never a temp dir. Other text is untouched."""
    return _CLONE_PATH.sub("<clone>", text)


PG_CONTAINER = "capsule-corp-postgres"  # the ops Postgres container name


KEEPER = "merge-keeper"     # capsule/lander.py KEEPER: the owner the keeper stamps its claims with


def keeper_claims(owners_files, queue, now):
    """(reason, age_s, beat_key) for each keeper claim: a merge-keeper row in one of the
    `owners_files` (age from its heartbeat, else the file's mtime; `beat_key` is `branch@beat` for
    a numeric heartbeat, else ""), then each marker under `queue` whose state is `landing` (the
    one predicate of landing.py, docker_idle.py and the engine restart). Any age: fails closed."""
    for owners in owners_files:
        try:
            rows = json.loads(owners.read_text())
            stamp = owners.stat().st_mtime
        except (OSError, ValueError):
            continue
        for branch, row in sorted(rows.items()) if isinstance(rows, dict) else ():
            if isinstance(row, dict) and row.get("owner") == KEEPER:
                beat = row.get("heartbeat")
                numeric = isinstance(beat, (int, float))
                yield (f"{KEEPER} holds {branch}", now - (beat if numeric else stamp),
                       f"{branch}@{int(beat)}" if numeric else "")
    for marker in sorted(queue.rglob("*.json")):
        try:
            landing = json.loads(marker.read_text()).get("state") == "landing"
            age = now - marker.stat().st_mtime
        except (OSError, ValueError, AttributeError):
            continue
        if landing and marker.name != "owners.json":
            yield f"marker {marker.name} is in state landing", age, ""
