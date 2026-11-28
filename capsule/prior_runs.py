"""Prior run paths for a rerun or checkpoint restart (row 520)."""
import contextlib
import os
import shutil
from pathlib import Path

from capsule.runners import docker, host_wall

ROLES = ("implementer", "reviewer", "validator", "guide")
PRIOR_DIR = "prior"
# R33 (blind spec 4.4 item 3): a rerun worker never gets blind material through a prior run.
BLIND_DIRS = docker.BLIND_DIRS
BLIND_PROFILE = "blind-tester"


def _linked(path, sp):
    """True when `path` is a symlink, or sits behind one: it does not resolve to itself under the
    run's own spool dir `sp` (a link into the blind repo must never be copied as content)."""
    path, sp = Path(path), Path(sp)
    return path.is_symlink() or sp.resolve() not in (path.resolve(), *path.resolve().parents)


def _hides(role, sp):
    """True for a validator run whose spool has a `blind-out/`: its files are never copied."""
    return role == "validator" and os.path.lexists(Path(sp) / "blind-out")


def _blind(path, root):
    """True when `path`, symlinks resolved, is a run's blind / blind-out folder or under one
    (`root` is the spool root holding the run directories)."""
    real = Path(path).resolve()
    with contextlib.suppress(ValueError):
        if any(p in BLIND_DIRS for p in real.relative_to(Path(root).resolve()).parts[1:]):
            return True
    return docker._blind_mount(str(real), None)


def paths_for(conn, spool_root, task_id, exclude_run_id, limit=3, after_id=0):
    """[{run_id, role, started_at, paths}], newest first, for `task_id`'s
    implementer/review task_runs rows: the newest implementer run (if any) plus up to
    `limit - 1` newer other runs, so the round just reviewed never loses its own
    implementer report to review runs that happen to outnumber it. `after_id`: only runs
    with `task_runs.id` at or past it (the Opus restart sees only its own attempt, M155).
    A validator run that has a `blind-out/` (the blind run used its spool) contributes only its
    directory: its bundle carried the blind rows' output, which `report/`, `done/` and
    `worker.log` may hold, whatever the switch says today."""
    root = Path(spool_root)
    placeholders = ",".join("?" * len(ROLES))
    # Unbounded on purpose: the newest implementer run can sit arbitrarily far behind a run of
    # review runs, so the search for it cannot stop at `limit` rows in.
    rows = conn.execute(
        f"SELECT run_id, profile, started_at FROM task_runs WHERE task_id=? AND profile IN"
        f" ({placeholders}) AND profile!=? AND run_id IS NOT NULL AND run_id!=? AND id>=?"
        f" ORDER BY id DESC", (task_id, *ROLES, BLIND_PROFILE, exclude_run_id, after_id)).fetchall()
    # a validator run whose job carries blind rows is never copied: its report and log quote them
    rows = [r for r in rows if root.joinpath(r["run_id"]).is_dir()
            and not root.joinpath(r["run_id"]).is_symlink()
            and not _blind(root / r["run_id"], root)
            and not (r["profile"] == "validator"
                     and host_wall.is_tester_spool(root / r["run_id"], missing=False))]
    impl = next((r for r in rows if r["profile"] == "implementer"), None)
    cap_others = limit - 1 if impl is not None else limit
    others = [r for r in rows if r is not impl][:cap_others]
    chosen = sorted(([impl] if impl is not None else []) + others, key=rows.index)
    out = []
    for row in chosen:
        sp = root / row["run_id"]
        found, hide = [sp], _hides(row["profile"], sp)
        for sub in () if hide else ("report", "done"):
            found += sorted((sp / sub).glob("*.json"))
        log = sp / "worker.log"
        if log.is_file() and not hide:
            found.append(log)
        out.append({"run_id": row["run_id"], "role": row["profile"], "started_at": row["started_at"],
                    "paths": [str(p) for p in found if not _blind(p, root)
                              and not (p != sp and _linked(p, sp))]})
    return out


def _mirror(prior_runs, dest):
    """Copy `prior_runs`' files under `dest`/<run_id>/..., chmod'd read-only (0o444
    files, 0o555 dirs, set only once every copy is in place) - the original spool
    files are never touched, so an add-dir grant onto `dest` cannot mutate another
    run's evidence even under the implementer's `acceptEdits`
    (capsule/runners/docker.py)."""
    out = []
    for run in prior_runs:
        if run.get("role") == BLIND_PROFILE:
            continue
        sp = Path(run["paths"][0])   # paths_for always puts the run's own spool dir first
        run_dest = dest / run["run_id"]
        run_dest.mkdir(parents=True, exist_ok=True)
        copies = [str(run_dest)]
        for src in (Path(p) for p in run["paths"][1:] if not _hides(run.get("role"), sp)):
            if _blind(src, sp.parent) or _linked(src, sp):
                continue
            dst = run_dest / src.relative_to(sp)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            dst.chmod(0o444)
            copies.append(str(dst))
        for d in [run_dest] + [p for p in run_dest.rglob("*") if p.is_dir()]:
            d.chmod(0o555)
        out.append(dict(run, paths=copies))
    return out


def mount(inputs, sp, limits):
    """One call site for `Tick._launch`: mirrors `inputs["prior_runs"]` read-only under
    `sp/prior` and exposes only that one directory via `limits["add_dirs"]` (row
    520's mount decision) - a no-op, returning `inputs`/`limits` unchanged, when
    there is nothing to mirror."""
    prior = inputs.get("prior_runs")
    if not prior:
        return inputs, limits
    dest = Path(sp) / PRIOR_DIR
    inputs = dict(inputs, prior_runs=_mirror(prior, dest))
    return inputs, dict(limits, add_dirs=list(limits.get("add_dirs") or []) + [str(dest)])
