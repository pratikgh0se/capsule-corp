"""Out-of-set reviewer findings (row 509 Zenkai Z7: a frozen file may
not grow, new logic goes into new modules)."""
import fnmatch
import json
import posixpath
import re
import subprocess
import sys

from capsule import dispatch, lease, outbox, paths, workspace
from capsule.db import event, txn


def norm_finding_path(path):
    """`file` normalised before it is matched against `file_set`/`touched`: a leading
    `./` and `..`-free redundancy (`a//b`, `a/./b`) stripped via
    `posixpath.normpath`, so a trivially- reshaped path cannot dodge the match."""
    p = (path or "").strip()
    if not p or p.startswith("/"):
        return p
    return posixpath.normpath(p if not p.startswith("./") else p[2:])


def _out_of_set_findings(findings, file_set, touched):
    """`findings` split into `(kept, moved)`: a finding is a move CANDIDATE when it is
    self-tagged `class: out_of_set`, or names a file outside the card's declared
    `file_set` (no file_set declared: nothing is a path-based candidate, only a self-
    tagged one; an absolute path never is, matched via `norm_finding_path`).
    A `safety: true` finding is never a candidate: it keeps failing the round."""
    touched_norm = {norm_finding_path(t) for t in touched}
    kept, moved = [], []
    for f in findings:
        path = norm_finding_path(f.get("file", ""))
        absolute = path.startswith("/")
        in_set = absolute or not file_set or any(
            fnmatch.fnmatch(path, norm_finding_path(pattern)) for pattern in file_set)
        candidate = (f.get("class") == "out_of_set" or not in_set) and not f.get("safety")
        (moved if candidate and path not in touched_norm else kept).append(f)
    return kept, moved


def mark_out_of_set(task, docs, touched):
    """The card's declared `file_set` and the round's own `touched` set
    (`_out_of_set_findings`) decide which reviewer findings move from `findings` to
    `out_of_set` in place - `Tick._content_findings`/`_findings` only ever read
    `findings`, so a moved finding is reported (returned here) but never fails the
    round on its own."""
    doc = docs.get("reviewer")
    findings = doc.get("findings") if doc else None
    if not findings:
        return []
    kept, moved = _out_of_set_findings(findings, dispatch.file_set(task), touched)
    if moved:
        doc["findings"] = kept
        doc["out_of_set"] = (doc.get("out_of_set") or []) + moved
        # nothing in-set survived: never fails the card
        if not kept and doc.get("verdict") != "pass":
            doc["verdict"] = "pass"
    return moved


def note_out_of_set(conn, task, docs, touched, now):
    """One `out_of_set_findings` event plus one outbox line, only when `mark_out_of_set` moved
    anything - a no-op otherwise (reported, but never counted toward `Tick._verdict`'s
    `failures`)."""
    out_of_set = mark_out_of_set(task, docs, touched)
    if out_of_set:
        with txn(conn):
            event(conn, task["id"], "out_of_set_findings", {"findings": out_of_set}, now=now)
            outbox.notify(conn, "running", f"{task['id']} {len(out_of_set)} reviewer finding(s) out of set",
                          file_body=(f"capsule: {task['id']} {len(out_of_set)} reviewer finding(s) named a"
                                     " file this round's diff never touched - moved to out_of_set,"
                                     " reported, not counted against the round"),
                          thread_key=task["id"], now=now)


def open_round_touched(conn, task_id, ws):
    """`workspace.changed` since this card's own `start_sha`, read straight off its
    task_runs row (never `Tick._start_sha`, which raises with no row yet) - `[]` when
    none exists (its very first implementer dispatch; `Tick._open_findings` returns
    `[]` for that case too, so there is nothing to compute this for)."""
    start_sha = lease.pinned_start_sha(conn, task_id)
    if start_sha is None:
        return []
    try:
        return workspace.changed(ws, start_sha)
    except (TypeError, KeyError, ValueError):
        return []


def judged_reviewer_doc(task, doc, touched):
    """A copy of the reviewer's document with out-of-set findings already moved:
    `intent.check` (`Tick._close`) must judge the same shape `Tick._verdict` will
    later score, never the raw one - a schema-valid `verdict: pass` reviewer document
    whose `findings` still carries a since-moved out-of-set item would otherwise trip
    `pass-with-findings` for nothing left to fix."""
    out = dict(doc)
    mark_out_of_set(task, {"reviewer": out}, touched)
    return out


def judged_document(conn, cfg, task, role, report, work_root):
    """The document `Tick._close` hands `intent.check`: `report` itself for any
    role but reviewer; for reviewer, out-of-set findings already moved on a copy
    (`judged_reviewer_doc`) - `report` stays untouched (`Tick._close` writes it to `handoff`)."""
    if role != "reviewer":
        return report
    ws = workspace.clone(cfg["repo_url"], task["branch_name"], work_root)
    return judged_reviewer_doc(task, report, open_round_touched(conn, task["id"], ws))


def reviewer_round(conn, task_id, since=0):
    """The 1-based review round about to start for `task_id`, for the reviewer's `round`
    job input (row 551, under row 540): `Tick._reviewed_impl`'s own predicate (a held
    run a human `accept` sends to review counts too, row 445)."""
    return len(round_impl_ids(conn, task_id, since))


def round_impl_ids(conn, task_id, since=0):
    """The ids of the implementer runs that each open one review round, oldest first. A run a
    quiet-stop continue moved out of review (before any review run) opens none (rule 22)."""
    return [r[0] for r in conn.execute(
        "SELECT id FROM task_runs WHERE task_id=? AND profile='implementer' AND started_at>=?"
        " AND (outcome='completed' OR id IN (SELECT run_id FROM task_events WHERE task_id=?"
        " AND kind='request_review')) AND id NOT IN (SELECT json_extract(payload,'$.from_run')"
        " FROM task_events WHERE task_id=? AND kind='stall_action' AND json_extract(payload,"
        "'$.action')='continue' AND json_extract(payload,'$.from_run') IS NOT NULL)"
        " ORDER BY id", (task_id, since, task_id, task_id))]


_TEMP_PATH = re.compile(paths.TEMP + "(?=/)")
DETAIL_CAP = 1500


def _collapse_temp(text):
    """A landing detail without the throwaway clone's temp paths: `<clone>` for the lander's
    clone root, `<tmp>` for any other temp root (they mean nothing to the implementer)."""
    return _TEMP_PATH.sub("<tmp>", paths.collapse_clone(text))[:DETAIL_CAP]


def landing_lines(conn, task_id):
    """inc_f94ec5d3: the checks of the card's latest `landing_checked` that sent it back (fail,
    hold or wait), as rerun findings. None once an implementer run that started after the event
    has completed (it has answered them), and none for a `checked` verdict."""
    row = conn.execute("SELECT created_at, payload FROM task_events WHERE task_id=?"
                       " AND kind='landing_checked' ORDER BY id DESC LIMIT 1",
                       (task_id,)).fetchone()
    if row is None:
        return []
    doc = json.loads(row["payload"] or "{}")
    if doc.get("verdict") not in ("red", "held", "wait") or conn.execute(
            "SELECT 1 FROM task_runs WHERE task_id=? AND profile='implementer' AND"
            " outcome='completed' AND started_at>?", (task_id, row["created_at"])).fetchone():
        return []
    return [f"landing check {c['name']} ({c['status']}), after review: "
            f"{_collapse_temp(c['detail'])}"
            for c in doc.get("checks", []) if c["status"] in ("fail", "hold", "wait")]


def previous_out_of_set(conn, task, touched):
    """The most recent reviewer run's `out_of_set` findings, recomputed the same way
    `mark_out_of_set` does ('when present') - for the NEXT round's guide, as context
    on a rerun (this round's own reviewer runs in parallel with the guide, so it can
    never see its own findings, only a prior round's); never counted toward anything,
    never persisted."""
    row = conn.execute("SELECT handoff FROM task_runs WHERE task_id=? AND profile='reviewer'"
                       " AND handoff IS NOT NULL ORDER BY id DESC LIMIT 1", (task["id"],)).fetchone()
    if row is None:
        return []
    try:
        doc = json.loads(row["handoff"])
    except ValueError:
        return []
    return mark_out_of_set(task, {"reviewer": doc}, touched)


_REPORTED = set()   # (start_sha, fallback line) already on stderr: one line per card and reason


def bases(ws, start_sha):
    """`(parents, base)` for the review snapshot the mutation gate judges. M is the merge base of
    the lane HEAD and origin/main; when M is not an ancestor of the card's first-run commit
    (`start_sha`) the lane took main after it: `base` is then a synthetic commit (no ref)
    merging `start_sha` and M, so `base..HEAD` holds only this card's commits and merge
    resolutions, never main's new files or an earlier card's unlanded work, and `parents` is
    `(HEAD, base)` (the second keeps `base` reachable through the snapshot ref the gate
    fetches). Else, or when any git step fails (origin/main unresolved, a merge conflict; one
    stderr line per card and reason says why), `((start_sha,), start_sha)`, as before.
    origin/main is fetched first (one bounded, redacted fetch), so a stale ref never picks the
    base; a `start_sha` of None skips all of it."""
    if start_sha is None:
        return (None,), None
    try:
        return _synthetic_bases(ws, start_sha)
    except (RuntimeError, subprocess.TimeoutExpired, IndexError, OSError) as exc:
        line = " ".join(f"capsule tick: review base: {exc}, using start_sha".split())[:300]
        if len(_REPORTED) > 1000:
            _REPORTED.clear()
        if (start_sha, line) not in _REPORTED:
            _REPORTED.add((start_sha, line))
            print(line, file=sys.stderr)
        return (start_sha,), start_sha


def _fetch_main(ws):
    """One fetch of origin/main at `_git`'s normal timeout; a failure is redacted as `clone()`
    does (git's stderr can carry a credential in the remote URL)."""
    try:
        workspace._git("fetch", "--end-of-options", "origin", "main", ws=ws)
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        url = workspace._git("remote", "get-url", "origin", ws=ws).stdout.strip()
        raise workspace._redact_git_failure("fetch", exc, url) from None


def _synthetic_bases(ws, start_sha):
    def git(*args):
        return workspace._git(*args, ws=ws).stdout.strip()
    _fetch_main(ws)
    head = workspace.head(ws)
    mb = git("merge-base", head, git("rev-parse", "--verify", "refs/remotes/origin/main^{commit}"))
    if git("merge-base", mb, start_sha) == mb:
        return (start_sha,), start_sha
    tree = git("merge-tree", "--write-tree", start_sha, mb).splitlines()[0]
    base = git(*workspace.IDENTITY, "commit-tree", tree, "-p", start_sha, "-p", mb,
               "-m", "review base")
    return (head, base), base
