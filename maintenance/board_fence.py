"""C29 write fence (decision rows 599/600, off-old-system plan risk R3)."""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import events  # noqa: E402
from capsule import paths  # noqa: E402

OWNED_MARKER = ".capsule-owned"
SKIP_STAMP = events.state_root() / "capsule-owned-skip.json"


def legacy_boards_dir():
    """The legacy boards dir, resolved by `paths.legacy_home` (honours LEGACY_KANBAN_HOME), the
    one resolver the `.capsule-owned` marker and the fence share."""
    return paths.legacy_home() / "kanban" / "boards"


def capsule_boards_dir():
    """Capsule's own boards dir (`paths.home()/boards`, CAPSULE_HOME aware), no old-system fallback.
    Only the read-only `boards` listing and the finding board-name check use it: the write-side
    callers stay on `legacy_boards_dir`, because the `.capsule-owned` fence lives only there."""
    return paths.home() / "boards"


def guard(boards_dir, name, verb, exc):
    """Raise `exc` (board.BoardFrozen) naming the marker when `name` is marked; else return."""
    if (Path(boards_dir) / name / OWNED_MARKER).exists():
        raise exc(f"{name}: refusing `{verb}`: {OWNED_MARKER} marker present, Capsule owns this "
                  f"board and the old copy is frozen; file with "
                  f"`python3 -m capsule intake {name} --title ... --body-file ...`")


def note_frozen(boards_dir):
    """Frozen board names (from the marker glob alone); prints one NOTE when any is frozen."""
    frozen = sorted(p.parent.name for p in Path(boards_dir).glob("*/" + OWNED_MARKER))
    if frozen:
        print(f"NOTE: sync skips capsule-owned (frozen) board(s): {', '.join(frozen)}")
    return frozen


def drop_frozen(cards, frozen):
    """`cards` without the frozen boards' cards."""
    return [c for c in cards if c.board not in frozen]


def unfrozen_names(boards_dir, frozen):
    """Boards holding a kanban.db that are not in `frozen`."""
    names = {p.name for p in Path(boards_dir).glob("*") if (p / "kanban.db").exists()}
    return sorted(names - set(frozen))


def capsule_owned_skip(board_dir, today=None, stamp=None):
    """True for a board marked `.capsule-owned` (the retired
    `capsule handover take` wrote it)."""
    if not (Path(board_dir) / OWNED_MARKER).exists():
        return False
    name = Path(board_dir).name
    today = today or time.strftime("%Y-%m-%d")
    stamp = Path(stamp or SKIP_STAMP)
    try:
        seen = json.loads(stamp.read_text())
    except (OSError, ValueError):
        seen = {}
    if seen.get(name) != today:
        events.log("card", "bridge", f"epic_sync: {name} is capsule-owned, dispatch skipped",
                   board=name, severity="warn", payload={"event": "capsule_owned_skip"})
        seen[name] = today
        try:
            stamp.parent.mkdir(parents=True, exist_ok=True)
            stamp.write_text(json.dumps(seen))
        except OSError as exc:
            print(f"board_bridge: skip stamp not written ({exc})", file=sys.stderr)
    return True
