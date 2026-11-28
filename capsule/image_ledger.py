"""M137 host ledger of image requests: `<home>/image/requests.jsonl`, append-only, one JSON line
per event `{kind, at, day, ...}` with kind `filed` (a card was made), `try` (a build attempt
ended, `minutes` spent, `outcome`) or `override` (a human unblock lifted a D8 limit, Z4). It is
machine-wide (the build minutes belong to the laptop, not a board) and lives outside every repo;
appends hold an `fcntl` lock so two boards' ticks never interleave a line. A line that does not
parse is skipped. `day` is the local calendar day of `at`, the one every daily limit counts by."""
import fcntl
import json
import time
from pathlib import Path

# D8: requests filed per board per day, build tries per card, build minutes per day
DAILY_FILED, TRIES, DAY_MINUTES = 3, 2, 60


def path(home):
    return Path(home) / "image" / "requests.jsonl"


def day(now):
    return time.strftime("%Y-%m-%d", time.localtime(now))


def append(home, kind, now, **fields):
    p = path(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"kind": kind, "at": int(now), "day": day(now), **fields}) + "\n"
    with open(p, "a", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.write(line)
        fh.flush()


def rows(home, kind, *, today=None, **match):
    """The `kind` lines whose fields equal `match`; with `today` (a time), only that local day."""
    try:
        text = path(home).read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for ln in text.splitlines():
        try:
            doc = json.loads(ln)
        except ValueError:
            continue
        if (isinstance(doc, dict) and doc.get("kind") == kind
                and all(doc.get(k) == v for k, v in match.items())
                and (today is None or doc.get("day") == day(today))):
            out.append(doc)
    return out
