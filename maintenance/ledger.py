#!/usr/bin/env python3
"""ledger.py - the append-only per-card milestone history."""
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import board      # noqa: E402
from capsule import legacy_names  # noqa: E402
import events     # noqa: E402
import vault_conf  # noqa: E402
import vault_git  # noqa: E402

LEDGER_V = 1
LEDGER_REL = "Ledger/"
SUMMARY_CAP = 200
OVERSIZE = 500            # a card past this many entries is a 4.2 bug, not traffic

# Row 44's nine kinds, and nothing else. Widening is a separate diff.
KINDS = ("created", "dispatched", "review_requested", "changes_requested",
         "approved", "merged", "blocked", "parked", "abandoned")

# The kinds an old-system task_events row backs.
LEGACY_KINDS = frozenset({"review_requested", "changes_requested", "blocked"})

# Flat, scalar, allowlisted per kind. An unlisted key is dropped - what stops a
# caller stuffing a whole `show --json` (or a transcript) into the vault.
PAYLOAD_KEYS = {
    "created":           ("spec_slug", "role", "parent_id", "risk", "intake_key"),
    "dispatched":        ("impl_id", "lead_id", "verify_id", "spec_slug"),
    "review_requested":  ("reviewer", "round"),
    "changes_requested": ("round", "reason"),
    "approved":          ("pr", "via"),
    "merged":            ("pr", "lead_id", "sha"),
    "blocked":           ("reason_kind",),
    "parked":            ("n",),
    "abandoned":         ("via",),
}

_BOARD_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
# The canonical task-id pattern - real old-system ids are
# lowercase hex.
_TASK_RE = board.TASK_ID_RE

_PENDING = 0


def _refuse_real_vault_under_test(p):
    """Raise when `p` IS the real vault and we are under the suite's own
    AGENT_OPS_NO_SEND marker - however the path got there: an explicit argument, an
    exported OBSIDIAN_VAULT_PATH, or the default."""
    real = vault_conf.host_json_vault()
    if os.environ.get("AGENT_OPS_NO_SEND") and real is not None and \
            p.resolve(strict=False) == real.resolve(strict=False):
        raise RuntimeError("refusing to touch the real vault under test: "
                           "set OBSIDIAN_VAULT_PATH to a temp dir")


def vault_root(vault=None):
    """The vault, or None when there is nothing to write to."""
    env = os.environ.get("OBSIDIAN_VAULT_PATH")
    if vault:
        p = Path(vault)
    elif env:
        p = Path(env)
    else:
        return None
    _refuse_real_vault_under_test(p)
    return p if p.is_dir() else None


def path_for(board, task_id, *, vault=None):
    """`<vault>/Ledger/<board>/<task_id>.jsonl`, or None with no vault."""
    if not _BOARD_RE.fullmatch(str(board or "")):
        raise ValueError(f"bad board name {board!r}")
    if not _TASK_RE.fullmatch(str(task_id or "")):
        raise ValueError(f"bad task id {task_id!r}")
    root = vault_root(vault)
    if root is None:
        return None
    return root / "Ledger" / str(board) / f"{task_id}.jsonl"


def _truncate(text):
    """Scrubbed, one line, capped at SUMMARY_CAP on a word boundary. Run
    summaries are model output - the only sensitive-content vector here."""
    t = " ".join(events._clean(str(text or "")).split())
    if len(t) <= SUMMARY_CAP:
        return t
    cut = t[:SUMMARY_CAP]
    sp = cut.rfind(" ")
    if sp > SUMMARY_CAP // 2:
        cut = cut[:sp]
    return cut.rstrip() + "…"


def _payload(kind, payload):
    out = {}
    for k in PAYLOAD_KEYS.get(kind, ()):
        if not payload or k not in payload:
            continue
        v = payload[k]
        if v is None:
            continue
        if isinstance(v, str):
            out[k] = events._clean(v)
        elif isinstance(v, (int, float, bool)):
            out[k] = v
        # anything else (dict, list) is dropped: payload is flat scalars only
    return out


def make_entry(board, task_id, kind, *, source, dedupe, summary="",
               legacy_event_id=None, payload=None, ts=None):
    """The 4.1 entry. Pure: no I/O, no clock unless `ts` is omitted."""
    if kind not in KINDS:
        raise ValueError(f"unknown ledger kind {kind!r} (known: {', '.join(KINDS)})")
    seed = f"{board}|{task_id}|{kind}|{dedupe}"
    return {
        "v": LEDGER_V,
        "id": hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32],
        "ts": float(ts if ts is not None else time.time()),
        "board": str(board),
        "task_id": str(task_id),
        "kind": kind,
        "source": str(source),
        legacy_names.LEDGER_EVENT_KEY: (int(legacy_event_id) if legacy_event_id is not None
                                        else None),
        "summary": _truncate(summary),
        "payload": _payload(kind, payload),
    }


def key(entry):
    """(board, task_id, legacy_event_id) when non-null, else (board, task_id, id) - row
    49."""
    hid = entry.get(legacy_names.LEDGER_EVENT_KEY)
    tail = ("h", int(hid)) if hid is not None else ("i", str(entry.get("id") or ""))
    return (str(entry.get("board") or ""), str(entry.get("task_id") or "")) + tail


def keys(entries):
    return {key(e) for e in entries}


def read_with_errors(board, task_id, *, vault=None):
    """`(entries, bad_line_numbers)` - what `read` returns, plus the 1-based numbers of
    the lines it dropped."""
    try:
        p = path_for(board, task_id, vault=vault)
    except (ValueError, RuntimeError):
        return [], []
    if p is None or not p.exists():
        return [], []
    out, bad = [], []
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return [], []
    for n, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            bad.append(n)
            continue
        if isinstance(row, dict):
            out.append(row)
        else:
            bad.append(n)
    return out, bad


def read(board, task_id, *, vault=None):
    """Every parseable entry, in file order. A corrupt line is skipped, never
    fixed: the file is append-only and 5 reports what does not parse."""
    return read_with_errors(board, task_id, vault=vault)[0]


def append(board, task_id, entry, *, vault=None):
    """The LedgerHook (board.LEDGER_HOOK_SIG)."""
    global _PENDING
    try:
        p = path_for(board, task_id, vault=vault)
        if p is None:
            return None
        existing = read(board, task_id, vault=vault)
        if key(entry) in keys(existing):
            return None
        if len(existing) >= OVERSIZE:
            events.log("card", "ledger", f"ledger over {OVERSIZE} entries: {board}/{task_id}",
                       board=str(board), task_id=str(task_id), severity="warn",
                       payload={"event": "ledger_oversize", "state": None})
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        _PENDING += 1
        return f"Ledger/{board}/{task_id}.jsonl"
    except (OSError, ValueError, RuntimeError, TypeError) as exc:
        print(f"ledger: not written ({exc})", file=sys.stderr)
        return None


def pending():
    return _PENDING


def push_pending(*, vault=None, git_runner=None):
    """One batched push per bridge tick (controller ruling 5)."""
    global _PENDING
    n = _PENDING
    try:
        root = vault_root(vault)
    except RuntimeError as exc:
        print(f"ledger: not pushed ({exc})", file=sys.stderr)
        return False
    if root is None:
        return False
    if not n and not (root / LEDGER_REL).is_dir():
        return False
    msg = f"ledger: {n} entries" if n else "ledger: sync"
    ok = vault_git.push(root, [LEDGER_REL], msg, runner=git_runner)
    if ok:
        _PENDING = 0
    return ok
