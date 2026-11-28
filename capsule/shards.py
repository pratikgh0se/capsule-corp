"""Board setting `contract_shards`: {"<contract command>": N} splits that command into N parts.

A suite too long for one command timeout runs as N commands, each a fixed share of the test
modules (`capsule.test_shard`), together covering every module once. The tick (contract_jobs) and
the lander's suites check both expand through here, so a card faces the same parts at review and
at landing. Each part is its own command with its own exit, so a failed or timed-out part fails
the whole result and the part is named (`# part 3/6`). Without the setting nothing changes."""
import re

DISCOVER = "python3 -m unittest discover "
_FINAL = re.compile(r"^part \d+/\d+: ran \d+ of (\d+) test modules$", re.M)


def _only_s_t(cmd):
    """True when the discover arguments are `-s DIR` and `-t TOP` (either order, once each)."""
    args = cmd[len(DISCOVER):].split()
    return len(args) == 4 and sorted(args[::2]) == ["-s", "-t"] and "-" not in (
        args[1][:1] + args[3][:1])


def validate(cfg):
    """Raise ValueError unless `contract_commands` is a list of strings and `contract_shards`
    (optional) is {discover command: int >= 2}."""
    cc = cfg.get("contract_commands")
    if not isinstance(cc, list) or not cc or not all(isinstance(x, str) for x in cc):
        raise ValueError("contract_commands: missing")
    shards = cfg.get("contract_shards", {})
    if not isinstance(shards, dict):
        raise ValueError("contract_shards: not an object")
    if shards and len(set(cc)) != len(cc):
        raise ValueError("contract_commands: duplicate entries with contract_shards set")
    for cmd, n in shards.items():
        if cmd not in cc:
            raise ValueError(f"contract_shards: {cmd!r} is not one of contract_commands")
        if not isinstance(cmd, str) or not cmd.startswith(DISCOVER) or not _only_s_t(cmd):
            raise ValueError(f"contract_shards: {cmd!r} is not `{DISCOVER.strip()}` with only"
                             " -s DIR and -t TOP")
        if not isinstance(n, int) or isinstance(n, bool) or n < 2:
            raise ValueError(f"contract_shards[{cmd!r}]: parts must be an integer >= 2")


def expand(cfg):
    """The board's `contract_commands`, each sharded command replaced by its N parts."""
    shards = cfg.get("contract_shards") or {}
    out = []
    for cmd in cfg["contract_commands"]:
        n = shards.get(cmd)
        if not n:
            out.append(cmd)
            continue
        rest = cmd[len(DISCOVER):]     # `-s DIR -t TOP`, which capsule.test_shard also takes
        out += [f"python3 -m capsule.test_shard {k} {n} {rest} # part {k}/{n}"
                for k in range(1, n + 1)]
    return out


def failures(parts, outputs):
    """One line per part of a sharded command that is missing, timed out, failed, or saw a
    different number of test modules than the others (a file made mid-run shifts the shares).
    `parts` is `expand(cfg)`; `outputs` the contract outputs ({cmd, exit, output}); [] when no
    command in `parts` is a part (no setting)."""
    rows = {o.get("cmd"): o for o in outputs or []}
    lines, totals = [], {}
    for cmd in (c for c in parts if " # part " in c):
        part, row = cmd.rsplit("# ", 1)[1], rows.get(cmd)
        if row is None:
            lines.append(f"{part} missing from the contract outputs (contract_shards)")
        elif row.get("exit") == 124:
            lines.append(f"{part} timed out (contract_shards)")
        elif row.get("exit") != 0:
            lines.append(f"{part} exited {row.get('exit')} (contract_shards)")
        else:
            found = _FINAL.findall(row.get("output") or "")
            if found:
                totals[part] = found[-1]
            else:
                lines.append(f"{part} did not report its module count (contract_shards)")
    if len(set(totals.values())) > 1:
        lines.append(f"parts saw different module counts {totals} (contract_shards)")
    return lines
