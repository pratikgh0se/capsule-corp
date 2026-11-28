"""The lander's selected-tests mode (decision D273): a landing runs only the tests the diff needs
(`tests/select_tests.py`, run from the trusted checkout; it only parses the candidate's files); the
nightly full run catches what that missed. Off unless a board sets `lander.select_tests: true`;
off, a FULL answer, a selector error or a timeout leave the board's commands and shards as they
were. Selection never touches the blind gate (`lander.run` runs it either way)."""
import subprocess
import sys

from capsule import paths, shards

DISCOVER = "python3 -m unittest discover -s "
KNOWN_DIRS = ("capsule.tests", "maintenance.tests", "tests")     # what the selector understands
TIMEOUT = 120


def _rewrite(cmds, tests):
    """`cmds` with each unittest discovery narrowed to the selected modules of its directory and
    each shell-suite runner given the selected shell suites; other commands are kept; a command
    left with nothing to run is dropped."""
    py = [t for t in tests if not t.endswith(".sh")]
    sh = [t for t in tests if t.endswith(".sh")]
    out = []
    for cmd in cmds:
        if cmd.startswith(DISCOVER):
            d = cmd[len(DISCOVER):].split()[0].strip("./").replace("/", ".")
            mods = [t for t in py if t.startswith(d + ".")]
            if d in KNOWN_DIRS:       # an unknown directory (tests/ui) is kept whole, never dropped
                cmd = f"python3 -m unittest {' '.join(mods)}" if mods else ""
        elif "run_shell_suites.sh" in cmd:
            cmd = f"{cmd} --only '{' '.join(sh)}'" if sh else ""
        if cmd:
            out.append(cmd)
    return out


def parts(cfg, lc, work, base, head):
    """What the suites check runs: the selected commands (a list), else the board's shard parts
    (or None). Any selector failure or timeout means the board's full commands, with one line on
    stderr."""
    plain = shards.expand(cfg) if cfg.get("contract_shards") else None
    if work is None or not lc.get("select_tests", False):
        return plain
    try:
        run = subprocess.run([sys.executable, str(paths.REPO / "tests" / "select_tests.py"),
                              "--repo", str(work.path), "--base", base, "--head", head],
                             capture_output=True, text=True, timeout=TIMEOUT)
    except (subprocess.SubprocessError, OSError) as exc:
        print(f"lander: test selection failed ({type(exc).__name__}); running the full suite",
              file=sys.stderr)
        return plain
    got = run.stdout.split()
    if run.returncode != 0 or not got or got == ["FULL"]:     # no output at all is a failure
        return plain
    return _rewrite(cfg["contract_commands"], [] if got == ["NONE"] else got)
