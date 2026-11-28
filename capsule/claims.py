"""Card pre-flight claim check (D84, D44). On every dispatch, a card whose `File set:` block names
a protected path parks (row 395) before the claim. Before a card's first dispatch, a plain script
(no model call) checks that the repo paths its text puts in backticks exist at the clone's HEAD,
read from git only (never the working tree). A card that names something missing goes to the
guide, on the runaway pattern (`runaway.py`), instead of costing a worker run. Warn-safe: nothing
parseable, or a repo git cannot read, never blocks a dispatch."""
import json
import re
import subprocess
import sys

from capsule import blocks, outbox, risk, workspace
from capsule.db import event, transition, txn

MAX_REPORTED = 10
GIT_TIMEOUT = 30
_TICKED = re.compile(r"`([^`\n]+)`")
_PATH = re.compile(r"^(?:\./)?([\w.\-]+(?:/[\w.\-]+)+/?)(?::\d+(?:-\d+)?|::\S+|#\S+)?$")
_EXT = re.compile(r"\.\w{1,5}$")
# a line that says it makes the thing: that path is not a claim it exists, on any line of the card
_CREATES = re.compile(r"\b(creat\w*|new|add\w*|build\w*|built|rewrit\w*|writ\w*|introduc\w*"
                      r"|generat\w*|tests? first)\b", re.I)


def _line_paths(line):
    """The repo paths a line's backticked tokens claim (a file with an extension, or `dir/`)."""
    found = [m.group(1) for tok in _TICKED.findall(line) if (m := _PATH.match(tok.strip()))]
    return [p for p in found if ".." not in p.split("/") and (p.endswith("/") or _EXT.search(p))]


def _claims(text):
    """Paths the card claims exist: every one it names, less any it says it creates anywhere
    (and a folder it creates a path inside of)."""
    lines = text.splitlines()
    made = {p.rstrip("/") for line in lines if _CREATES.search(line) for p in _line_paths(line)}
    return [p for p in dict.fromkeys(p for line in lines for p in _line_paths(line))
            if not any(m == p.rstrip("/") or m.startswith(p.rstrip("/") + "/") for m in made)]


def _git(repo, args, stdin=None):
    run = subprocess.run(["git", "-C", str(repo), *args], input=stdin, capture_output=True,
                         text=True, check=True, timeout=GIT_TIMEOUT)
    return run.stdout


def _claims_mismatch(card_text, repo):
    """Claimed paths absent at `repo`'s HEAD, e.g. `["capsule/gone.py (not in the repo)"]`, asked
    of git (`ls-tree`, `cat-file --batch-check`: an object lookup that never opens a file or
    follows a symlink). Only a path whose first folder is in the repo counts: scratch, vault and
    other-repo paths are not this repo's claims. No claims: `[]` without running git; a repo git
    cannot read raises, so the caller decides what warn-safe means."""
    paths = _claims(card_text)
    if not paths:
        return []
    top = set(_git(repo, ["ls-tree", "--name-only", "-z", "HEAD"]).split("\0"))
    paths = [p for p in paths if p.split("/")[0] in top]
    if not paths:
        return []
    asked = "".join(f"HEAD:{p.rstrip('/')}\n" for p in paths)
    seen = _git(repo, ["cat-file", "--batch-check"], asked).splitlines()
    gone = [f"{p} (not in the repo)" for p, line in zip(paths, seen) if line.endswith(" missing")]
    return gone[:MAX_REPORTED]


def _mismatch_for(conn, row, ws):
    """The card's missing paths; `[]` for a card that already ran or already went to the guide."""
    done = conn.execute("SELECT 1 FROM task_runs WHERE task_id=? LIMIT 1", (row["id"],)).fetchone()
    held = conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='claims'",
                        (row["id"],)).fetchone()
    return [] if done or held else _claims_mismatch(f"{row['title']}\n{row['body'] or ''}", ws)


def precheck(conn, row, ws, now):
    """True when the card was just parked (its file set names a protected path, row 395) or sent
    to the guide (triage, block kind `claims`) because its text names paths `ws` lacks. The
    claims check covers only a card with no run yet and no earlier claims hold: once the guide
    has ruled, the card is believed."""
    try:
        held = workspace.touches_protected(risk.file_set(row["body"]))
        bad = [] if held else _mismatch_for(conn, row, ws)
    except Exception as exc:
        why = f"{type(exc).__name__}: {exc}"
        print(f"capsule tick: claim pre-flight skipped for {row['id']} ({why})", file=sys.stderr)
        return False
    if held:
        return blocks.park(conn, row["id"], f"the file set names {', '.join(held)}: a protected "
                           "path no worker may change (row 395); amend the `File set:` block "
                           "with amend --body-file, then reset, or close the card", now=now)
    if not bad:
        return False
    with txn(conn):
        if not transition(conn, row["id"], "triage", frm=("ready",), claim=True,
                          block_kind="claims", block_recurrences=0):
            return False
        event(conn, row["id"], "claims", {"missing": bad, "pass": True}, now=now)
        outbox.notify(conn, "blocked", f"{row['id']} held before dispatch: the guide checks what"
                      f" its text names ({len(bad)} not in the repo)",
                      thread_key=row["id"], now=now,
                      file_body=f"capsule: {row['id']} held before dispatch: not in the repo: "
                                f"{'; '.join(bad)[:900]}; the guide rules")
    return True


def packet(conn, ev):
    """What the guide rules on: the card's text and each path the repo does not have."""
    card = conn.execute("SELECT title, body FROM tasks WHERE id=?", (ev["task_id"],)).fetchone()
    return {"kind": "claims", "role": "claims", "missing": json.loads(ev["payload"])["missing"],
            "card": f"{card['title']}\n{card['body'] or ''}"}
