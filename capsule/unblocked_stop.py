"""The Stop hook's own change-detection gate (row 558): block only when the
unowned/unblocked set (its `(item, blocker)` pairs) changed since the last block
issued, or 30 minutes passed unchanged - never merely because the count is nonzero."""
import json
import os
import re
import sys
import time
from pathlib import Path

from capsule import paths

RENAG_INTERVAL_S = 30 * 60    # row 558: "at most once per 30 minutes if unchanged"


def unowned_pairs(doc):
    """Sorted `(item, blocker)` pairs for every unblocked-and-unowned entry in a
    `capsule unblocked --json` document (mirrors `unblocked.Item.unowned` without importing
    that module - this only ever sees the detector's already-rendered JSON)."""
    if not isinstance(doc, dict) or not isinstance(doc.get("items"), list):
        return []
    return sorted((str(i.get("item", "")), str(i.get("blocker") or ""))
                  for i in doc["items"]
                  if isinstance(i, dict) and i.get("state") == "unblocked" and not i.get("owner"))


def _is_seat(key, claim, now, hours):
    """A fresh implementer claim on a branch (a card-id `t_<hex>` is an alias, never a seat)."""
    return (claim["owner"] == "implementer" and not re.fullmatch(r"t_[0-9a-f]+", key)
            and 0 <= now - claim["heartbeat"] < hours * 3600)


def _busy_seats(queue_dir, hours, now=None):
    """`(all_busy, build_busy)`: seats over every `queue_dir/*/owners.json` (an unreadable
    file holds none), and how many of them are `build/*` branches."""
    now = time.time() if now is None else now
    keys = []
    for path in Path(queue_dir).glob("*/owners.json"):
        try:
            claims = json.loads(path.read_text())
            keys += [k for k, c in claims.items() if _is_seat(k, c, now, hours)]
        except Exception:
            continue
    return len(keys), sum(k.startswith("build/") for k in keys)


def _remember(path, payload, now):
    """Write `payload` plus `blocked_at` to `path` atomically; never raising. On failure one
    stderr line names the path and the error and the result is False (the caller decides whether
    a lost write means nagging again next turn or failing open)."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")    # per-pid, no collision
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps({**payload, "blocked_at": now}))
        tmp.replace(path)
    except OSError as e:
        print(f"capsule-unblocked-hook: cannot write {path}: {e}", file=sys.stderr)
        try:
            tmp.unlink(missing_ok=True)    # don't leave the tmp file behind on a failed rename
        except OSError as e2:
            print(f"capsule-unblocked-hook: cannot remove {tmp}: {e2}", file=sys.stderr)
        return False
    return True


def should_block(doc, *, state_path, now=None):
    """`(block, pairs)`: blocks when `state_path` is missing/corrupt, the pair set
    changed, or `0 <= now - blocked_at < RENAG_INTERVAL_S` is false (guards a
    future `blocked_at`)."""
    now = time.time() if now is None else now
    pairs = unowned_pairs(doc)
    path = Path(state_path)
    if not pairs:
        try:
            path.unlink(missing_ok=True)    # nothing to remember; the next appearance is news
        except OSError as e:
            print(f"capsule-unblocked-hook: cannot remove {path}: {e}", file=sys.stderr)
        return False, pairs
    try:
        old = json.loads(path.read_text())
        old_pairs = [tuple(p) for p in old["pairs"]]
        blocked_at = float(old["blocked_at"])
        if old_pairs == pairs and 0 <= now - blocked_at < RENAG_INTERVAL_S:
            return False, pairs
    except (OSError, ValueError, KeyError, TypeError):
        pass    # missing/corrupt state: fail loud, block below
    _remember(path, {"pairs": pairs}, now)    # a lost write still blocks (fail loud)
    return True, pairs


def _seats_block(state_path, now=None):
    """D155: the idle-builder-seats nag, on its own 30 min window in its own `state_path` (never
    the pair gate's file, so neither can reset the other). Blocks when the state is missing or
    corrupt, or `0 <= now - blocked_at < RENAG_INTERVAL_S` is false. An unreadable state counts as
    not fresh, so the block is followed by a rewrite; if that write fails too the nag fails open
    (False, with the stderr line), since it could never be rate limited and would re-block every
    stop."""
    now = time.time() if now is None else now
    path = Path(state_path)
    try:
        fresh = 0 <= now - float(json.loads(path.read_text())["blocked_at"]) < RENAG_INTERVAL_S
    except (OSError, ValueError, KeyError, TypeError):
        fresh = False    # missing/corrupt/unreadable state: rewritten below
    if fresh:
        return False
    if _remember(path, {}, now):
        return True
    print("capsule-unblocked-hook: builder seats nag skipped (state not writable)", file=sys.stderr)
    return False


def main(argv):
    """The hook's stop gate: `<detector json, or "" if the detector failed> <queue dir> <heavy cap>
    <max age hours> <seats 0|1>`; prints the block decision or nothing. The row 558 pair gate runs
    only with a detector report (empty or non-JSON output counts as a failed detector: the pair
    gate is skipped and its state left alone) and while some seat is free, and drops its line once
    every implementer seat is busy. With seats=1 (D155) a separate gate adds a BUILDER SEATS line
    while fewer than `cap` `build/*` seats are busy."""
    doc = None
    if argv[1]:
        try:
            doc = json.loads(argv[1])
        except ValueError:
            print("capsule-unblocked-hook: detector output is not JSON; skipping the unowned "
                  "items check", file=sys.stderr)
    cap = int(argv[3])
    busy_all, busy_build = _busy_seats(argv[2], float(argv[4]))
    lines = []
    if doc is not None and busy_all < cap:
        blocked, pairs = should_block(doc, state_path=paths.home() / "unblocked-stop.json")
        if blocked:
            lines.append(f"UNBLOCKED WORK: {len(pairs)} unowned items - dispatch or state the "
                         "blocker per item")
    if argv[5] == "1" and busy_build < cap and _seats_block(
            paths.home() / "unblocked-seats-stop.json"):
        lines.append(f"BUILDER SEATS: {busy_build}/{cap} busy - start independent builds "
                     "(D154) or state why not")
    if lines:
        print(json.dumps({"decision": "block", "reason": "\n".join(lines)}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
