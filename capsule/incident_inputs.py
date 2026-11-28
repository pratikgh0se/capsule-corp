"""Korin's small input readers: the tail of a log or evidence file, and the `korin.since` date.
Nothing a blind tester wrote reaches Korin's input (D161): `host_wall.withheld` paths skipped."""
import datetime
import json
import os
import re
import sys
from pathlib import Path

from capsule import incident_blind
from capsule.runners import host_wall


def blind_role(role):
    """True for a blind tester or a blind run: role `blind-tester`, `blind_red:<card>`,
    `blind_runner:<card>`."""
    return str(role or "").startswith((host_wall.TESTER_ROLE, "blind_red:", "blind_runner:"))


def blind_incident(evidence, role=None):
    """True when the incident is on a blind role or any evidence path is withheld (a blind
    tester's spool, a `blind*` entry): Korin gets the ids and paths of such an incident, never a
    tail or an event's text (a tester's crash error rides in its `crashed` event)."""
    return blind_role(role) or any(host_wall.withheld(e) for e in evidence if isinstance(e, str))


def _kept(role, evidence):
    """The evidence an incident on a blind role may store: `event:<id>` and spool folders only,
    no file text or reason a run wrote (D161); other roles keep all of it."""
    def keep(item):
        spool = host_wall.spool_of(item) if isinstance(item, str) else None
        return str(item).startswith("event:") or (spool is not None
                                                  and spool == Path(os.path.realpath(item)))
    return [e for e in evidence if keep(e)] if blind_role(role) else evidence


def blind_safe(role, key, evidence):
    """(key, evidence) an incident may store. One on a blind tester or a blind run holds no free
    text from the run, for it would reach Korin's card and its fix card (D161): the key keeps its
    class, the text before the first `:` ("fatal: exit 1: <tester's line>" is "fatal"), and the
    evidence `_kept`. Any other role passes through unchanged."""
    if not blind_role(role):
        return key, evidence
    return str(key).split(":")[0].strip() or "blind", _kept(role, evidence)


BLIND_RUN_ERROR = "the run carries blind tests, so its error text is withheld"
_CLASS = re.compile(r"[a-z_]+(?:-[a-z_]+)*|timed out")
_EXIT = re.compile(r"exit (?:-?\d+|none)")


def scrub(evidence, key=None):
    """The record fields to overwrite for a blind incident (its root cause key can carry the
    tester's own error line), else nothing. What stays of `key` ("crash: validator: fatal: exit 1:
    <error line>") is its kind, role and error class and the exit code: Korin has no log to read,
    so those are all it can work from. Only a plain word (or "timed out") stays, never free text."""
    if not blind_incident(evidence):
        return {}
    parts = [p.strip() for p in str(key or "").split(":")]
    kept = []
    for part in parts[:3]:
        if not _CLASS.fullmatch(part):
            break
        kept.append(part)
    if len(kept) == len(parts[:3]) and len(parts) > 3 and _EXIT.fullmatch(parts[3]):
        kept.append(parts[3])
    return {"root_key": ": ".join([*kept, "withheld: an incident on a blind run"])}


def readable(evidence):
    """`evidence` without the paths Korin is refused (a blind spool or entry); none is named."""
    return [e for e in evidence if not (isinstance(e, str) and host_wall.withheld(e))]


RECORD_KEYS = ("id", "board", "task_id", "run_id", "kind", "opened_at", "root_key", "fingerprint",
               "repeat_of", "description")
WITHHELD_TEXT = "withheld: the run carries blind rows"


def record(inc):
    """The incident row as Korin's input and card hold it: `RECORD_KEYS`, with `root_key` scrubbed
    for a blind incident (which also holds no description)."""
    scrubbed = scrub(json.loads(inc["evidence"] or "[]"), inc["root_key"])
    row = {k: inc[k] if k in inc.keys() else None for k in RECORD_KEYS}    # description: old board
    return row | ({"description": None} if scrubbed else {}) | scrubbed


def _blind_run(root, row):
    return row["profile"] == host_wall.TESTER_ROLE or host_wall.is_tester_spool(
        Path(root) / row["run_id"], missing=False)


def tick_text(conn, exc, root, task_id, run_id=None, cfg=None):
    """`str(exc)`, unless a run it may concern carries blind rows or is a blind tester's: then a
    fixed phrase, for the message may quote them and a `tick_error` line reaches the implementer's
    and Korin's inputs. That run is `run_id`, else any validator or tester run of the card (`root`:
    the spool root). The exception's type name stays with the caller. The runner's blind-mount
    refusal is always `incident_blind.masked` (its path can sit under blind material, whatever
    runs the card has); so is a failed docker run's argv (`masked`: each mount `<masked>:target`);
    the raw path or command goes to one stderr line, the tick's log, and nowhere else. The blind
    repo (of `cfg`) and `root` in docker's output are `path-<digest>`."""
    if incident_blind.refusal(exc):
        path = incident_blind.mount_path({"error": f"RuntimeError: {exc}"})
        print(f"capsule tick: {incident_blind.BLIND_CHECK}: card {task_id}: {path}",
              file=sys.stderr)
        return incident_blind.masked(exc)
    blind = incident_blind.roots(root, cfg)         # the board's blind repo and spool root
    docker_text = incident_blind.logged(exc, task_id, blind)   # a failed docker run: masked
    if docker_text:
        return docker_text
    rows = conn.execute(
        "SELECT run_id, profile FROM task_runs WHERE run_id IS NOT NULL AND "
        + ("run_id=?" if run_id else "task_id=? AND profile IN ('validator', ?)"),
        (run_id,) if run_id else (task_id, host_wall.TESTER_ROLE)).fetchall()
    if any(_blind_run(root, r) for r in rows):
        return WITHHELD_TEXT
    return str(exc)


def spawn_error(conn, exc, root, task_id, run_id, cfg=None):
    """`Type: text` of a review run's spawn failure, the text withheld for a blind run."""
    return f"{type(exc).__name__}: {tick_text(conn, exc, root, task_id, run_id, cfg)}"


def run_text(run_json, sp, profile):
    """A finished run's run.json with the text of a run that carries blind rows (or a blind tester)
    withheld: the worker does it too, but the tick reads a file any process may have written."""
    if not run_json or not _blind_run(sp.parent, {"profile": profile, "run_id": sp.name}):
        return run_json
    error = run_json.get("error")
    return {**run_json, "denial_reason": None,
            "error": f"{run_json.get('error_class')}: {BLIND_RUN_ERROR}" if error else None}


def run_row(row, root):
    """A `task_runs` row as a dict, its `error` withheld when the run carries blind rows (or is a
    blind tester's): the review stage reads it into a hold reason and a retry event, and Korin's
    inputs tail both. A row without a `run_id` or a `profile` key has no spool to read."""
    out = dict(row)
    probe = {"profile": out.get("profile"), "run_id": out.get("run_id") or ""}
    if out.get("error") and _blind_run(root, probe):
        out["error"] = f"{out.get('error_class') or 'error'}: {BLIND_RUN_ERROR}"
    return out


def ids_only(context):
    """A decision's `context` cut to its "Criterion N" lines: no excerpt, no test id (D17)."""
    return "\n".join(re.sub(r"^(Criterion [^:]*):.*", r"\1", ln)
                     for ln in str(context or "").splitlines() if ln.startswith("Criterion "))


def outbox_tail(tail):
    """`tail` of the outbox log with each decision.v1 line's `context` cut to `ids_only`; a line
    holding "decision.v1" that is not JSON is withheld. The log file itself is not changed."""
    out = []
    for ln in str(tail).splitlines():
        if "decision.v1" in ln:
            try:
                doc = json.loads(ln[ln.index("{"):])
                ln = json.dumps({**doc, "context": ids_only(doc.get("context"))})
            except (ValueError, AttributeError, TypeError):
                ln = "withheld: blind excerpts"
        out.append(ln)
    return "\n".join(out)


def event_line(conn, r):
    """One board event as Korin's evidence line. A `decision_requested` of a card that carries
    blind tests keeps its criterion numbers only: the given / expect / observed
    excerpts go to the human (D17), never to an agent."""
    payload = r["payload"] or ""
    if r["kind"] == "decision_requested" and conn.execute(
            "SELECT 1 FROM task_events WHERE task_id=? AND kind LIKE 'blind\\_%' ESCAPE '\\'",
            (r["task_id"],)).fetchone():
        try:
            doc = json.loads(payload)
            doc["context"] = ids_only(doc.get("context"))
            payload = json.dumps(doc)
        except (ValueError, AttributeError):
            payload = "withheld: blind excerpts"
    return f"{r['id']} {r['task_id']} {r['kind']} {payload}"


def first_tail(candidates, chars):
    """`{"path", "tail"}` for the first regular file among `candidates` (symlinks, directories and
    withheld blind paths are skipped): its last `chars` characters, reading at most 4 x `chars`
    bytes from its end (errors='replace'). None when no candidate is readable."""
    for p in map(Path, candidates):
        if p.is_symlink() or not p.is_file() or host_wall.withheld(p):
            continue
        try:
            with open(p, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                fh.seek(max(0, fh.tell() - 4 * chars))
                text = fh.read(4 * chars).decode("utf-8", errors="replace")
                return {"path": str(p), "tail": text[-chars:]}
        except OSError:
            continue
    return None


def since_ts(since):
    """`korin.since` (ISO date, read as UTC midnight) as epoch seconds; None when unset."""
    if not since:
        return None
    day = datetime.datetime.fromisoformat(str(since))
    return int(day.replace(tzinfo=datetime.timezone.utc).timestamp())
