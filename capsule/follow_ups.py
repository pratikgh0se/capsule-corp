"""report.v1 `follow_ups`: contained work outside the card's file set becomes its own intake
card (inc_e1d965b7, row 456). The tick files them only on the route to review; a hold files none."""
import fnmatch
import itertools
import re

from capsule import dispatch, intake, review_scope, risk, workspace
from capsule.db import event

norm = review_scope.norm_finding_path


def _entries(text):
    """The paths in one `File set:` item: every word, commas as spaces (`a.py and b.py` names
    both), with backticks, `*` and trailing punctuation dropped."""
    return [w.strip("`*").rstrip(".,;:") for w in text.replace(",", " ").split()]


def _body_files(body):
    """Every path under a body `File set:` / `Files:` header, as bullets or one comma line, with no
    filter by directory or extension: the card's files are not only Capsule's own (`risk.file_set`
    keeps just those, for classification)."""
    lines = (body or "").splitlines()
    out = []
    for i, line in enumerate(lines):
        m = risk._FILESET_RE.match(line)
        if not m:
            continue
        rest = lines[i + 1:]
        if not (m.group(1) or "").strip():
            rest = list(itertools.dropwhile(lambda ln: not ln.strip(), rest))
        bullets = itertools.takewhile(risk._BULLET_RE.match, rest)
        items = [m.group(1) or "", *(risk._BULLET_RE.sub("", b, count=1) for b in bullets)]
        out += [e for item in items for e in _entries(item) if e]
    return out


def _own_files(conn, cfg, task, work_root):
    """The card's file_set column, else its body `File set:` paths plus the files this card's diff
    touched so far (a stray `Files: none` must not switch the diff off). `[]` when nothing is
    known (no start_sha yet), and then the guard cannot fire."""
    declared = dispatch.file_set(task)
    if declared:
        return declared
    ws = workspace.clone(cfg["repo_url"], task["branch_name"], work_root)
    return _body_files(task["body"]) + review_scope.open_round_touched(conn, task["id"], ws)


def _parents(entry):
    """`entry` and each directory above it: `a/b/c` -> `a`, `a/b`, `a/b/c`."""
    parts = entry.split("/")
    return ["/".join(parts[:n]) for n in range(1, len(parts) + 1)]


def _touches(np, own):
    """Whether the normalised follow_up path `np` may be in the card's own files. The whole repo,
    an absolute path and any `*`, `?`, `[^` or `[!` glob cannot be checked, so they count. A path
    whose only glob char is `[` (`pages/[id].tsx` is a real path) counts when it matches an own
    entry or one of its parent directories. A plain path counts when it matches an own entry (a
    glob or a file), holds one (a directory) or sits under one (an own directory entry)."""
    if np in ("", ".") or np.startswith("/") or re.search(r"[*?]|\[[!^]", np):
        return True
    return any(fnmatch.fnmatch(np, o) or o.startswith(np.rstrip("/") + "/") or np in _parents(o)
               or np.startswith(o.rstrip("/") + "/")
               or ("[" in np and any(fnmatch.fnmatch(q, np) for q in _parents(o))) for o in own)


def hold_own_files(conn, cfg, task, report, work_root):
    """`report`, or a copy whose `issues` name each follow_up that touches the card's own files
    (that is in-scope work, not a follow-up), so `Tick._route` holds it."""
    if not report.get("follow_ups"):
        return report
    own = [norm(f) for f in _own_files(conn, cfg, task, work_root)]
    issues = []
    for item in report["follow_ups"]:
        hit = [p for p in item["files"] if _touches(norm(p), own)]
        if hit:
            own_hit = ", ".join(hit)
            outside = [p for p in item["files"] if p not in hit]
            exit_ = (f"drop {own_hit} from its files and keep it for {', '.join(outside)}"
                     if outside else "drop the follow_up if it is optional")
            issues.append(f"follow_up {item['title']!r} names this card's own files "
                          f"({own_hit}): do it in this card, or {exit_}")
    return dict(report, issues=list(report["issues"]) + issues) if issues else report


def file_cards(conn, home, cfg, run, report, now):
    """One `follow_up` intake file and one `follow_up_filed` event per item. The fingerprint is
    source task + normalised title + sorted files, so a retried item folds into the open card
    (`intake._open_same`) and a different item naming the same files does not. Called only on the
    route to review: a checkpoint, ruling or contradiction report drops its follow_ups."""
    task_id = run["task_id"]
    for item in report.get("follow_ups") or []:
        title = " ".join(item["title"].lower().split())
        fp = f"follow_up:{task_id}:{title}:{','.join(sorted(item['files']))}"
        rid = intake.enqueue(home, cfg["board"], kind="follow_up", fingerprint=fp,
                             title=item["title"], body=item["body"], source_board=cfg["board"],
                             source_task=task_id, file_set=item["files"], landing="triage", now=now)
        event(conn, task_id, "follow_up_filed",
              {"intake_id": rid, "title": item["title"][:120], "files": item["files"]},
              run_pk=run["id"], run_id=run["run_id"], now=now)
