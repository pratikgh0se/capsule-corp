"""M022: the card's `check_cmd` as one more contract command, behind the `check_gate` redesign
switch. It runs in the same strong container as `contract_commands`, its exit code and output reach
the validator as ordinary contract evidence, and a non-zero exit fails the round on the host, so a
validator that misreads the evidence cannot pass it. `check_expect` is a required substring of the
check's output (the redesign spec only says "expected result"; a substring is the v1 reading).
Switch off, or no `check_cmd`: nothing here adds a command, a key or a failure line.
The flake reruns (`flake.py`, R14) ride the same two calls: `commands` and `failures` return theirs
after the card check's, so `tick.py` keeps one call each. The blind run (R28) is not here: it is
the contract job's own second step (capsule/contract_jobs.py, `blind.run.Pending`)."""
from pathlib import Path

from capsule import base_red, config, flake, shards
from capsule.blind import gate as blind_gate


class CheckCmd(str):
    """A card's check command; carries its `check_expect` ('' for none)."""
    expect = ""


def commands(cfg, task, ws, conn):
    """The review's extra gate commands: the card's check (below), then the added tests' flake
    reruns (`flake.commands`, its own switch)."""
    from capsule.tick import Tick      # tick imports this module: resolve at call time
    return [*_check_commands(cfg, task),
            *flake.commands(cfg, ws, lambda: Tick._start_sha(conn, task["id"]))]


def _check_commands(cfg, task):
    """[the card's check as a CheckCmd], or [] with the switch off or no `check_cmd`. A mac card's
    check always counts (`_on`): the Linux container cannot test Mac work, so its host check is
    the card's only Mac proof."""
    cmd = (task["check_cmd"] or "").strip() if _on(cfg, _tier(task)) else ""
    if not cmd:
        return []
    check = CheckCmd(cmd)
    check.expect = (task["check_expect"] or "").strip()
    return [check]


def _tier(row):
    """The card's runner tier ('' for a row without one)."""
    return row["tier"] if "tier" in row.keys() else ""


def _on(cfg, tier):
    """True when the card check is a contract command: the switch is on, or a mac card."""
    return bool(config.redesign(cfg)["check_gate"] or tier == "mac")


def extra(cmd, out, tail):
    """Keys added to a contract output entry: {} for every ordinary command. For the check:
    `check` marks it for `failures`; a missing `check_expect` also appends a note to the kept tail
    so the validator sees why."""
    if not isinstance(cmd, CheckCmd):
        return {}
    if not cmd.expect or cmd.expect in (out or ""):
        return {"check": True}
    note = f"\ncheck_expect {cmd.expect!r} not found in the output"
    return {"check": True, "expect_missing": cmd.expect, "output": (out or "")[-tail:] + note}


def failures(cfg, conn, spool_root, validator_run):
    """The card-check failures (below) plus the flake-rerun ones (`flake.failures`) plus, in blind
    gate mode, one counts-and-ids line for the failing blind tests (R32: the failure comment, the
    events and the outbox never carry a test's given, expect or observed)."""
    return [*_check_failures(cfg, conn, spool_root, validator_run),
            *flake.failures(cfg, conn, spool_root, validator_run),
            *_shard_failures(cfg, spool_root, validator_run),
            *blind_gate.failures(spool_root, validator_run),
            *base_red.lines(spool_root, validator_run)]


def findings(cfg, conn, spool_root, validator_run):
    """What the next implementer round is told: `failures`, with each failing blind test as its
    own finding (id, given, expect, observed) in place of the counts line."""
    return [*_check_failures(cfg, conn, spool_root, validator_run),
            *flake.failures(cfg, conn, spool_root, validator_run),
            *_shard_failures(cfg, spool_root, validator_run),
            *blind_gate.failures(spool_root, validator_run, full=True),
            *base_red.lines(spool_root, validator_run)]


def _shard_failures(cfg, spool_root, validator_run):
    """`shards.failures` over the validator run's contract outputs: a part that did not pass
    fails the round whatever the validator wrote. [] without `contract_shards` or a run."""
    if validator_run is None or not cfg.get("contract_shards"):
        return []
    from capsule.tick import _read_json     # tick imports this module: resolve at call time
    job = _read_json(Path(spool_root) / validator_run["run_id"] / "job.json") or {}
    return shards.failures(shards.expand(cfg), (job.get("inputs") or {}).get("outputs"))


def _check_failures(cfg, conn, spool_root, validator_run):
    """One line per failed card check among the validator run's contract outputs (read from its
    job.json, the evidence the validator was given). [] with the switch off (and not a mac
    card), no validator run, or a card without a check_cmd; a card WITH one whose job.json holds
    no check entry (missing or unreadable file) is one "did not run" line,
    never a silent pass."""
    if validator_run is None or conn is None:      # no board connection: nothing to look up
        return []
    card = conn.execute("SELECT check_cmd, tier FROM tasks WHERE id=?",
                        (validator_run["task_id"],)).fetchone()
    if card is None or not _on(cfg, _tier(card)):
        return []
    if not (card["check_cmd"] or "").strip():
        return []
    outputs = blind_gate.outputs(spool_root, validator_run) or []
    if not any(o.get("check") for o in outputs):
        return [f"card check did not run: check_cmd `{card['check_cmd'].strip()}` has no result"
                " among the validator's contract outputs (card check, M022)"]
    lines = []
    for o in outputs:
        if o.get("check") and o["exit"] != 0:
            lines.append(f"check_cmd `{o['cmd']}` exited {o['exit']} (card check, M022)")
        elif o.get("expect_missing"):
            lines.append(f"check_expect {o['expect_missing']!r} not in its output of"
                         f" `{o['cmd']}` (card check, M022)")
    return lines
