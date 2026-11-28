"""A runner that lacks a Python module is not the card's failure (inc_adf55f49). The implementer
cannot fix it, so every round fails and burns the failure cap, and the card goes to triage with no
hint of the cause. This module finds the line `ModuleNotFoundError: No module named '<name>'` in a
validator run's contract outputs. The round ends in one `capability` hold that names the module
and the remedy, and no failure counts, unless the card caused it. The card caused it when `<name>`
matches a tracked path of the clone (module or package, any depth), or when the card's diff against
its base adds an import of `<name>` or adds `<name>` to a dependency file."""
import re
import subprocess
from pathlib import Path

from capsule import blocks, validator_report, workspace
from capsule.blind import gate as blind_gate
from capsule.db import txn

_LINE = re.compile(r"ModuleNotFoundError: No module named '([^']+)'")
_DEPENDENCY_FILES = ("requirements", "pyproject.toml", "setup.py", "setup.cfg", "Pipfile",
                     "package.json")


def _reported(spool_root, validator_run):
    """Top-level names that the non-blind contract outputs report as missing modules."""
    found = set()
    for row in blind_gate.outputs(spool_root, validator_run) or []:
        lines = [] if row.get("blind") else str(row.get("output") or "").splitlines()
        hits = [_LINE.fullmatch(line.strip()) for line in lines]
        found.update(h.group(1).split(".")[0] for h in hits if h)
    return found


def _tracked(ws):
    """Every module or package name that a tracked .py file of the clone `ws` can import as."""
    out = workspace._git("ls-files", "-z", ws=Path(ws)).stdout
    names = set()
    for path in filter(None, out.split("\0")):
        parts = Path(path).parts
        if parts[-1].endswith(".py"):     # only Python files name a module or a package
            names.update(parts[:-1])
            names.add(Path(parts[-1]).stem)
    return names


def _added(ws, base):
    """The lines that the card's diff against `base` adds, as (file, line) pairs."""
    diff = workspace._git("diff", "--no-ext-diff", "--no-renames", "-U0", base, ws=Path(ws)).stdout
    path, rows = "", []
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[4:].removeprefix("b/")
        elif line.startswith("+"):
            rows.append((path, line[1:]))
    return rows


def _card_caused(name, ws, base):
    """True when `name` is a tracked path of the clone, or the card adds an import of it or lists
    it in a dependency file. Any read error counts as the card's."""
    imp = re.compile(rf"^\s*(import|from)\s+{re.escape(name)}\b")
    try:
        return name in _tracked(ws) or any(
            imp.match(text) or (name in text and any(d in path for d in _DEPENDENCY_FILES))
            for path, text in _added(ws, base))
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError):
        return True     # git or its text could not be read: count the failure, as before


def gate_hold(tick, args, infra, infra_cause, failures, run_id, now):
    """True when the round is held: the blind gate's hold (`blind_gate.hold`), else a capability
    hold for a module the runner lacks. `args` is (conn, cfg, task, latest, ws, base)."""
    conn, cfg, task, latest, ws, base = args
    spool = tick._spool_root(cfg)
    if blind_gate.hold(conn, cfg, spool, task, latest["validator"], run_id, now, infra, infra_cause,
                       failures):
        return True
    reported = _reported(spool, latest["validator"])
    names = sorted(n for n in reported if not _card_caused(n, ws, base))
    if not failures or not names:
        return False
    reason = ("The validator runner has no Python module: " + ", ".join(names) + ". The "
              "implementer cannot fix this, so the round did not count as a failure. Remedy: add "
              "the module to the runner image, or give the tests a skip guard on the import.")
    reason = validator_report.hold_reason(reason, failures)
    with txn(conn):
        blocks.block(conn, task["id"], kind="capability", reason=reason, run_id=run_id,
                     now=now)
    return True
