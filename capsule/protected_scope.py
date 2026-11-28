"""Host merges are not run edits (inc_b8a1d115, inc_f6317f0b; row 395). One reference tree R says
what the protected files should be with no worker edits: `start_sha` plus main when the host moved
the lane onto main (merge, fast-forward or rebase) after it, else `start_sha`. A protected path
whose working copy or index differs from R is flagged and restored to R, so a restored path clears
itself on the next verdict."""
from capsule import db, epic_clean, workspace

MAIN = "refs/remotes/origin/main"


def _git_ok(ws, *args):
    try:
        return workspace._git(*args, ws=ws).stdout.strip()
    except RuntimeError:
        return None


def _main_tree(ws, base, main):
    """(R, conflicted): `base` merged with main's tip `main`, and the paths that merge could not
    settle. Attributes come from `base` only (a worker's `.gitattributes` could pick
    `merge=union`); a git without `--attr-source` fails the call, which leaves R = `base`."""
    r = epic_clean._run(ws, f"--attr-source={base}", "merge-tree", "--write-tree", "--name-only",
                        "-z", base, main)
    if r.returncode > 1:
        return base, set()
    tree, *rest = r.stdout.decode().split("\0")
    return tree, set(rest[:rest.index("")] if "" in rest else rest)


def _host_copy(ws, base, main):
    """What settles a conflicted path: the earliest first-parent merge in base..HEAD that holds
    `main`, else main's own copy."""
    log = _git_ok(ws, "rev-list", "--merges", "--first-parent", "--reverse", "--topo-order",
                  f"{base}..HEAD") or ""
    ok = (_git_ok(ws, "merge-base", "--is-ancestor", main, c) is not None for c in log.split())
    return next((c for c, held in zip(log.split(), ok) if held), main)


def _off(ws, ref, keep):
    """Protected paths in `keep` whose working copy or index differs from `ref`."""
    index = workspace._git("diff", "--cached", "--name-only", "-z", "--no-renames",
                           "--end-of-options", ref, ws=ws).stdout.split("\0")
    paths = workspace.touches_protected(workspace.changed(ws, ref) + index)
    return [p for p in dict.fromkeys(paths) if keep(p)]


def split(ws, base):
    """Restore the run's protected-path changes and return their names."""
    workspace._validate_ref(base, what="base")
    main = _git_ok(ws, "merge-base", "HEAD", MAIN)
    ahead = main and _git_ok(ws, "merge-base", "--is-ancestor", main, base) is None
    tree, conflicts = _main_tree(ws, base, main) if ahead else (base, set())
    refs = [(tree, lambda p: p not in conflicts)]
    if conflicts:
        refs.append((_host_copy(ws, base, main), conflicts.__contains__))
    flagged = []
    for ref, keep in refs:
        paths = _off(ws, ref, keep)
        workspace.restore(ws, ref, paths)
        flagged += paths
    return flagged


def refuse(conn, task_id, paths, now):
    """One `protected_path_refused` event per refused path; returns the row-395 failure lines."""
    with db.txn(conn):
        for p in paths:
            db.event(conn, task_id, "protected_path_refused",
                     {"path": p, "task_id": task_id, "stage": "review"}, now=now)
    return [f"protected path changed (row 395): {p}" for p in paths]
