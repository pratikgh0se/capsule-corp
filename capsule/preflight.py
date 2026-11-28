"""`python3 -m capsule check`: the read-only preconditions for a live run (pilot prep)."""
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import time
import tomllib
from pathlib import Path

from capsule import config, intent, paths, style_lint, tick_guard, workspace
from capsule import docker_door, host, keeper_watch
from capsule import db as capsule_db
from capsule.runners import docker


def _sums_ok(directory, pattern=None):
    """Problems with `directory`'s SHA256SUMS, one entry each: a listed file that no longer hashes to
    its pinned digest or is missing, and (with `pattern`) a file on disk the sums never list."""
    lines = (directory / "SHA256SUMS").read_text().split("\n")
    bad, listed = [], set()
    for line in filter(None, (l.strip() for l in lines)):
        digest, name = line.split(None, 1)
        name = name.strip()
        listed.add(name)
        try:
            ok = hashlib.sha256((directory / name).read_bytes()).hexdigest() == digest
        except OSError:
            bad.append(f"{name} (missing)")
            continue
        if not ok:
            bad.append(name)
    if pattern:
        bad += [f"{p.name} (not pinned)" for p in sorted(directory.glob(pattern)) if p.name not in listed]
    return bad


def _schemas_ok():
    # row 569
    return _sums_ok(paths.SCHEMAS, "*.json") + _sums_ok(paths.SCHEMAS / "rows", "*.json")


def _sidecar_state(db):
    """`-wal`/`-shm`/directory state for a `wal_health` FAIL detail - purely
    descriptive, never part of the decision: the decision is whatever `connect_ro_tolerant`'s
    real open just did."""
    wal = Path(str(db) + "-wal")
    shm = Path(str(db) + "-shm")
    wal_bytes = wal.stat().st_size if wal.is_file() else "absent"
    shm_state = "present" if shm.exists() else "absent"
    dir_state = "writable" if os.access(db.parent, os.W_OK) else "not writable"
    return f"-wal {wal_bytes} B, -shm {shm_state}, dir {dir_state}"


def _contract_rows():
    """The schema, intent-example and style-checklist rows (rows 454, 569, 570, 573)."""
    match, clean = "SHA256SUMS match", "SHA256SUMS match, lint clean"
    schemas = _schemas_ok()
    intents = _sums_ok(paths.INTENT, "*.jsonl") + intent.lint_all()     # row 454: second oracle
    style = _sums_ok(paths.REPO / "contracts") + style_lint.lint_file()  # rows 570, 573: checklist
    return [
        ("FAIL" if schemas else "ok", "schemas",
         f"SHA256SUMS mismatch: {schemas}" if schemas else match),
        ("FAIL" if intents else "ok", "intent",
         f"intent examples: {intents}" if intents else clean),
        ("FAIL" if style else "ok", "style", f"_style.md: {style}" if style else clean),
    ]


def _worker_image_row(cfg, home, run_cmd):
    """The `worker_image` row: the image this board runs, present locally or not. A missing
    image, or none recorded, names the build command."""
    image, source = docker.worker_image_source(home, cfg["board"])
    pinned = source.endswith("OPS_WORKER_IMAGE")   # only the env and ops.env pins override a record
    note = (" (OPS_WORKER_IMAGE override is active: the recorded image is not used)"
            if pinned else "")
    try:
        rc = docker_door.docker_run(["image", "inspect", image], run_cmd, capture_output=True,
                                    text=True, timeout=30).returncode if image else 1
    except Exception:
        rc = 1
    if rc == 0:
        return "ok", "worker_image", image + note
    if pinned:
        return "FAIL", "worker_image", f"{image} not present locally{note}"
    what = f"{image} not present locally" if image else "no worker image recorded"
    return ("FAIL", "worker_image",
            f"{what}: build it with {docker.build_command(home)} ({docker.BUILD_NOTE})")


def _tool_rows(cfg, home, run_cmd, which):
    """The docker, worker image, deps mount and claude rows."""
    strong = docker.StrongRunner()
    strong._run = run_cmd
    up = strong.available()
    out = [("ok" if up else "FAIL", "docker", "server reachable" if up
            else "docker not reachable (or AGENT_OPS_NO_DOCKER set): strong cards wait")]
    out.append(_worker_image_row(cfg, home, run_cmd))
    board = cfg["board"]
    # A generic image (the ops.env pin or the capsule-python row, not this board's own) when the
    # project's capsule.toml does not ask for exactly the python stack: say so (image unchanged).
    # A repo that is not a local directory cannot be read: warn as unchecked, never guess.
    proj = Path(str(cfg.get("repo_url")))
    source = docker.worker_image_source(home, board)[1]
    generic = {"ops.env OPS_WORKER_IMAGE": "the machine-wide pinned image",
               "capsule-python row": "the generic python image"}.get(source)
    if generic:
        local = proj.is_absolute() and proj.is_dir()
        try:
            stacks = tomllib.loads((proj / "capsule.toml").read_text("utf-8"))["image"]["stacks"]
        except (OSError, ValueError, KeyError, TypeError):
            stacks = None
        if not local or not docker.asks_generic(stacks):
            why = ("its capsule.toml [image] does not ask for stacks = [\"python\"]" if local
                   else "repo is not a local directory, cannot check capsule.toml")
            out.append(("info", "worker_image_fallback",
                        f"WARN board {board} runs {generic} (no "
                        f"{docker.project_image_name(board)} row in images.lock, and {why}): add "
                        f"[image] to the project's capsule.toml and run "
                        f"`python3 -m capsule image build --project DIR --board {board}`"))
    deps = Path.home() / docker.DEPS_MOUNT
    out.append(("ok" if deps.is_dir() else "FAIL", "deps_mount", str(deps)))
    claude = which("claude")
    out.append(("ok" if claude else "FAIL", "claude", claude or "claude not on PATH"))
    return out


def _no_such_board(home, board):
    """The FAIL config row for a board with no per-board config.json AND no claim from the legacy
    `home/config.json` (its `board` field is this name: the pre-per-board layout);
    else None. A typo must not report some other board's legacy file as its own."""
    want = paths.config_path(home, board)
    if board is None or want.is_file():
        return None
    try:
        legacy = json.loads(paths.config_path(home).read_text()).get("board")
    except (OSError, ValueError, AttributeError):
        legacy = None
    if legacy == board:
        return None
    return ("FAIL", "config", f"no such board {board!r}: {want} is missing "
                              "(create it with `capsule board new`)")


def _slack_owner_row(home):
    """The `slack_owner_id` row: ok when set, WARN when unset (the owner may postpone it on
    purpose, so it never FAILs and never opens a dark-test incident). Never prints the value."""
    from capsule import slack_answer
    if slack_answer._owner(home):
        return "ok", "slack_owner_id", "set"
    return "WARN", "slack_owner_id", (
        "NOT SET (Slack button answers are refused until it is set; set slack_owner_id in "
        f"{paths.config_path(home)})")


def foreign_ledger(vault, home=None, env=None):
    """The first "Capsule Corp - <name> acts ledger.md" file name in the vault for a name other
    than "owner", only while owner.name is the default "owner"; None otherwise, and None with no
    vault or an unreadable one."""
    try:
        if not vault or host.owner_name(home, env) != "owner":
            return None
        found = sorted(p.name for p in Path(vault).glob("**/Capsule Corp - * acts ledger.md"))
    except (host.HostError, OSError):
        return None
    found = [f for f in found if f != "Capsule Corp - owner acts ledger.md"]
    return found[0] if found else None


def _owner_warning(home=None, env=None):
    """A warning line when owner.name is the default but the vault holds a "Capsule Corp - <name>
    acts ledger.md" note for another name (the owner forgot to set it); None otherwise, and None
    with no vault configured or readable."""
    try:
        vault = host.get("vault.path", home, env, only=True)
    except host.HostError:
        return None
    found = foreign_ledger(vault, home, env)
    if not found:
        return None
    return (f"owner.name is the default 'owner' but the vault has {found!r}: set owner.name "
            "in host.json (or CAPSULE_OWNER_NAME) to that name")


def run(home, *, board=None, run_cmd=subprocess.run, which=shutil.which):
    """[(level, name, detail)] for the board config under `home`."""
    cfg_path = paths.resolve_config(home, board)
    # row 517: independent of the config below
    out = [tick_guard.check_row(home, board=board, run_cmd=run_cmd)]
    out.append(host.check_row(home))      # P05a: host.json, independent of the board
    warn = _owner_warning(home)
    if warn:
        out.append(("info", "owner", f"WARN {warn}"))
    missing = _no_such_board(home, board)
    if missing:
        return out + [missing]
    try:
        cfg = config.load(cfg_path)
        out.append(("ok", "config", f"{cfg_path}: board {cfg['board']}, epics {cfg['epics']}"))
    except Exception as exc:
        return out + [("FAIL", "config", f"{cfg_path}: {exc}")]
    try:
        workspace._validate_repo_url(cfg.get("repo_url"))
        local = Path(cfg["repo_url"])
        if local.is_absolute() and not local.exists():
            raise ValueError(f"local repo {local} does not exist")
        out.append(("ok", "repo_url", cfg["repo_url"]))
    except Exception as exc:
        out.append(("FAIL", "repo_url", str(exc)))
    db = paths.board_db(cfg["board"], home)
    state = "exists" if db.exists() else "absent (board not created yet)"
    out.append(("info", "board_db", f"{db} {state}"))
    if db.exists():
        # the real open IS the check - no separate prediction of what
        # sqlite would do (the branch's own prediction missed the commonest failing
        # state, an idle board whose -wal/-shm are both gone after a clean close).
        try:
            conn = capsule_db.connect_ro_tolerant(db)
        except sqlite3.OperationalError as exc:
            out.append(("FAIL", "wal_health", f"{db}: {exc} ({_sidecar_state(db)})"))
            out.append(("FAIL", "migrations", "skipped: board db unreadable (wal_health above)"))
        else:
            out.append(("ok", "wal_health", f"{db}: opens read-only ({_sidecar_state(db)})"))
            try:
                pending = capsule_db.migrations_pending(conn)
            finally:
                conn.close()
            out.append(("FAIL" if pending else "ok", "migrations", "pending" if pending else "ok"))
    else:
        out.append(("info", "migrations", "no board db yet"))
    out += _tool_rows(cfg, home, run_cmd, which)
    keeper_log = os.environ.get("CAPSULE_KEEPER_LOG", keeper_watch.DEFAULT_LOG)
    out.append(keeper_watch.heartbeat_row(keeper_log, time.time(), home=home))
    out.append(_slack_owner_row(home))
    return out + _contract_rows()


def main(home, board=None):
    rows = run(home, board=board)
    for level, name, detail in rows:
        print(f"{level} {name}: {detail}")
    return 1 if any(level == "FAIL" for level, _, _ in rows) else 0
