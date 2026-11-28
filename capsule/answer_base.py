"""The answer path's whole base (PROTECTED, stdlib only): the few functions and constants that
`capsule.slack_answer` and `capsule.human` need to decide whether a Slack answer applies. Every
name here is moved unchanged from its old module (paths, db, spool, lookout, slack,
brainstorm_intake), which re-exports it; there is nothing new to maintain. Keeping the decision
path on protected, stdlib-level code means a worker edit to any open module cannot change it."""
import json
import os
import re
import sqlite3
import tempfile
import time
from pathlib import Path

# from paths (REPO itself stays in paths: tests patch paths.REPO)
SCHEMAS = Path(__file__).resolve().parents[1] / "contracts" / "schemas"


# the owner-name rules, one source for `capsule.host` and the stdlib reader in `capsule.human`
OWNER_RE = re.compile(r"[A-Za-z](?:[A-Za-z0-9 ._'-]{0,62}[A-Za-z0-9._'-])?")   # a name, not a role
ROLES = ("cos", "opus")      # agent authors: never the owner's name (they would read as owner)


def default_home():
    """The home when nothing names one: ~/.local/share/capsule."""
    return Path.home() / ".local" / "share" / "capsule"


def home(override=None):
    """override > CAPSULE_HOME > (AGENT_OPS_NO_SEND: a temp dir, never the real home) > the
    default. Every test module and the lander gate set the marker, so a run that skipped
    tmpclean (`discover -p <file>`, mutation_check) still cannot write the live board DB.
    Mirrors maintenance/events.py state_root() (row 609), including its trade-off: that temp
    dir (<tmp>/capsule-home-no-send) is shared by every run that skipped tmpclean."""
    explicit = override or os.environ.get("CAPSULE_HOME")
    if explicit:
        return Path(explicit)
    if os.environ.get("AGENT_OPS_NO_SEND"):
        return Path(tempfile.gettempdir()) / "capsule-home-no-send"
    return default_home()


def board_dir(board, home_=None):
    return (home_ or home()) / "boards" / board


def board_db(board, home_=None):
    return board_dir(board, home_) / "kanban.db"


def config_path(home_=None, board=None):
    """`board` -> `boards/<board>/config.json`, else the legacy `home/config.json`."""
    if board is not None:
        return board_dir(board, home_) / "config.json"
    return (home_ or home()) / "config.json"


def resolve_config(home_=None, board=None):
    """The per-board config file when `board` names one that exists, else the legacy path."""
    if board is not None and config_path(home_, board).is_file():
        return config_path(home_, board)
    return config_path(home_)


# from db
def connect_ro_tolerant(path):
    """`file:...?mode=ro`, tolerant of a WAL this process cannot map (a read-only
    checkout, or a board owned by another user) by retrying `immutable=1` - but only
    when the `-wal` file is absent or empty (row 470; moved here from
    `unblocked._connect_ro` in row 466 `preflight.py`'s `wal_health`
    check needs exactly this same tolerant open, so there is one reader everyone
    shares, not a second module predicting what the real one would do - row 466
    review found that prediction wrong for the commonest case, an idle board whose
    `-wal`/`-shm` are both gone after a clean close)."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1)
        conn.row_factory = sqlite3.Row
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
        return conn
    except sqlite3.OperationalError:
        if not _wal_empty_or_absent(path):
            raise
        conn = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True, timeout=1)
        conn.row_factory = sqlite3.Row
        return conn


def _wal_empty_or_absent(path):
    """True only when `path`'s own `-wal` file is absent or empty (row 470):
    `immutable=1` makes sqlite ignore the WAL outright, so falling back to it
    while the WAL still holds real committed data would silently read a stale
    snapshot instead of failing loud."""
    wal = Path(str(path) + "-wal")
    return not (wal.is_file() and wal.stat().st_size > 0)


# from spool
def human_dir(home, board):
    """Where a human's own interventions land (row 420): `capsule/human.py`'s writer
    and `report.interventions`'s reader both use this path, nothing else touches it."""
    return Path(home) / "boards" / board / "human"


# from lookout
BLOCK_OPEN = ("blocked", "block_loop_detected", "dependency_wait", "decision_requested",
              "first_screen_hold", "parked_by_human", "parked", "failure_limit",
              "contradiction_loop")
BLOCK_CLOSE = {"unblocked": "unblocked", "completed": "completed", "failure_reset": "reset",
               "archived": "archived", "promoted": "promoted",
               "first_screen_ok": "screen_answered", "first_screen_fix": "screen_answered"}
# blocks.accept's own close - `request_review {"by": "accept"}`
# (human's `accept` verb and the guide's ask-owner auto-accept both go through
# blocks.accept) - closes the open block the same as `unblock` does.
ACCEPT_CLOSE_KIND = "request_review"


# from slack
def block(cfg):
    """The board's `slack` block when Slack is on (present and `enabled` is not false)."""
    blk = cfg.get("slack") if isinstance(cfg, dict) else None
    if not isinstance(blk, dict) or blk.get("enabled", True) is False:
        return None
    return blk


def _allowed(cfg_slack):
    """Where a post may go. `slack.allowed_channels` when set (validate() requires it to
    cover every configured channel, so an empty list is a config error);
    absent, the channels already in `slack.channels`, so an existing install keeps working
    and anything else (a thread or edit target outside the config) is refused."""
    listed = cfg_slack.get("allowed_channels")
    if listed is not None:
        return {c.strip() for c in listed}
    return {c.strip() for c in cfg_slack["channels"].values() if isinstance(c, str)}


# from brainstorm_intake
SLACK_ID_RE = re.compile(r"[A-Z][A-Z0-9]{2,20}\Z")


def _host_ids(home):
    """(slack_team_id, slack_app_id) from the host config, or None when either is missing."""
    try:
        cfg = json.loads(config_path(home).read_text())
    except (OSError, ValueError):
        return None
    ids = tuple(cfg.get(k) for k in ("slack_team_id", "slack_app_id")) if isinstance(cfg, dict) \
        else ()
    return ids if len(ids) == 2 and all(isinstance(i, str) and SLACK_ID_RE.match(i)
                                        for i in ids) else None


def source_ok(home, payload):
    """True when the payload's (team, app) are the host's; False when they differ; None when the
    host config does not name them (the caller decides)."""
    ids = _host_ids(home)
    if ids is None:
        return None
    team = payload.get("team_id") or (payload.get("team") or {}).get("id")
    return (team, payload.get("api_app_id")) == ids


# AGENT_OPS_NO_SEND capture of the answer path's Slack replies (tests read it instead of Slack)
def capture(board, home_, channel, thread_ts, text, kind):
    """Append one JSON line {channel, thread_ts, text, kind, ts} (mode 0600): the reply that
    would have been posted. kind is "answered" or "refused"."""
    target = board_dir(board, home_) / "slack-replies-no-send.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    rec = {"channel": channel, "thread_ts": thread_ts, "text": text, "kind": kind,
           "ts": int(time.time())}
    fd = os.open(target, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")
