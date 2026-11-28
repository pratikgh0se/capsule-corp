"""The host wall (D161, D162): an operating-system refusal, not a prompt, that keeps a factory
session's Read, Glob and Grep out of the blind tests (reads only: no session has a host shell).

The implementer, reviewer, guide and Korin run `claude` on the Mac. With `redesign.blind_tests` at
`report` or `gate`, `apply` starts it under `sandbox-exec` with an allow-default profile that denies
`file-read*` (data, listing, `stat`) on parent folders, so a clone or spool made later is walled
too: the blind repo and `also_walled`, the board's `blind-work`, its spool root and the claude
config dir's `projects` and `jobs`. It allows back its own spool and, except for Korin, its own
`projects/<cwd>`; a `stat` of the spool root and `projects` stays allowed. Korin gets its
incident's evidence spools instead, minus a tester's or a validator's with `blind:` rows, and
inside them nothing named `blind*`. Every path is a realpath without `/System/Volumes/Data`.

`plan` decides from the board config whether a session is walled (unknown: `{"unavailable": why}`,
only `off` gives None). `apply` renders a per-start profile into the run's spool, proves with one
real `sandbox-exec` process that it refuses a probe file, every walled folder and what lies under
it, every allow inside one and (Korin) a `blind*` file in each spool it may read, and returns the
wrapped argv; else `WallUnavailable` and no session starts (`_ProbeTimeout`: a dropped start).
`HOLD_CLASSES`: refused for good. Every role is walled but `UNWALLED_ROLES`.
"""
import itertools
import json
import os
import re
import secrets
import sys
import subprocess
from pathlib import Path

from capsule.validator_report import is_blind     # the one `blind:` row matcher

TESTER_ROLE = "blind-tester"     # reads its own clone, keeps no transcript (claude.py)
UNWALLED_ROLES = ("validator", TESTER_ROLE)      # the validator has no read tools
HOST_ROLES = ("implementer", "reviewer", "guide", "korin", "security")   # run `claude` on the Mac
SANDBOX_EXEC = "/usr/bin/sandbox-exec"
PROFILE, PROBE = "hostwall.sb", "hostwall.probe"     # both in the run's own spool
PROBE_TIMEOUT = 15
MODES = ("off", "report", "gate")
DATA_VOLUME = "/System/Volumes/Data"
MAX_PROJECT_NAME = 200       # the CLI hashes a longer project folder name
WALL_RETRY = 300             # seconds a refused run waits before the tick tries again
ERROR_CLASS = "wall_unavailable"     # transient: the tick pauses the board and retries
LIGHT_CLASS = "wall_light_tier"      # a config conflict: the card is held with the reason
JOB_CLASS = "wall_job_refused"       # one job's own fault (a workspace path too long): held too
HOLD_CLASSES = (LIGHT_CLASS, JOB_CLASS)      # the card is held `capability`: no pause, no incident
LIGHT_REFUSED = ("the host wall cannot wrap a light-tier session: a seatbelt sandbox cannot nest"
                 " inside the wall. Set redesign.blind_tests off, or use the strong tier")
MAC_REFUSED = ("the host wall cannot wrap a Mac lane session: a seatbelt sandbox cannot nest"
               " inside the wall. Turn off blind tests through the host wall for this board, or"
               " run the card without the host wall")
BLIND_PROBE = "blind-hostwall-probe"     # + `-<tag>`: made under a Korin-readable spool, per start
MAX_PROBED, MAX_DEEP = 50, 5     # a `meta` folder: children the start-up check tries, and of each
MAX_DEPTH, MAX_WALK = 6, 200      # levels and paths of the breadth-first walk of a non-meta folder
TIMEOUT_CLASS = "wall_probe_timeout"  # the start-up check ran out of time: this start is dropped
# The start-up proof is ONE process under the profile (a process per path took 25-42 s on a real
# board). Paths come on stdin, NUL-separated. Exit 3: a walled path was read. 5: a path was not
# refused (or the probe could not be tried). 4: the own profile did not read. The first `n` paths
# are files this start made, the probe first (missing is a failure); the rest may vanish.
PROBE_PY = r"""
import errno, os, sys
sb, n = sys.argv[1], int(sys.argv[2])
paths = [os.fsdecode(p) for p in sys.stdin.buffer.read().split(b"\0") if p]
def state(path):      # a socket or device fails open() with something else: stat it too
    for attempt in (os.listdir, lambda p: os.close(os.open(p, os.O_RDONLY | os.O_NONBLOCK)),
                    os.lstat):
        try:
            attempt(path)
            return "open"
        except OSError as exc:
            if exc.errno in (errno.EPERM, errno.ENOENT):
                return "refused" if exc.errno == errno.EPERM else "gone"
    return "other"
for i, path in enumerate(paths, 1):
    got = state(path)
    if got == "open":
        sys.exit(3)
    if got != "refused" and not (got == "gone" and i > n):
        sys.exit(5)
try:
    open(sb, "rb").read(1)
except OSError:
    sys.exit(4)
"""


class WallUnavailable(Exception):
    """The wall cannot be applied, so the session must not start (the tick retries it later)."""
    error_class = ERROR_CLASS
    retry = True
    wait = WALL_RETRY          # seconds the run's `retry_after` is set ahead

    def __init__(self, why):
        super().__init__(" ".join(str(why).split()))     # one line: worker._error_line keeps one


class _ProbeTimeout(WallUnavailable):
    """The start-up check ran out of time: no verdict. This start is dropped, no try spent, the
    board not paused; the next pass retries."""
    error_class = TIMEOUT_CLASS
    wait = 0


class LightTierRefused(WallUnavailable):
    """A light-tier session with a wall planned: the card is held."""
    error_class = LIGHT_CLASS
    retry = False


class JobRefused(WallUnavailable):
    """A refusal that belongs to this one job (its workspace): the card is held."""
    error_class = JOB_CLASS
    retry = False


REFUSALS = {LIGHT_CLASS: LightTierRefused, JOB_CLASS: JobRefused}


def _canon(path):
    """`path` resolved, without the data-volume prefix (a deny with it misses the plain name)."""
    real = os.path.realpath(os.path.expanduser(str(path)))
    return real[len(DATA_VOLUME):] if real.startswith(DATA_VOLUME + "/") else real


def _lit(path):
    """A path as an SBPL string literal (a control character refused)."""
    text = str(path)
    if any(ord(c) < 32 for c in text):
        raise ValueError(f"control character in profile path {text!r}")
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _esc(text):
    """`text` as a literal inside an SBPL `#"..."` regex (no quote can be escaped there)."""
    if '"' in text or any(ord(c) < 32 for c in text):
        raise ValueError(f"unsafe character in profile regex path {text!r}")
    return re.sub(r"([.\[\](){}\\*+?|^$])", r"\\\1", text)


def spool_of(path):
    """The run spool (`<home>/boards/<board>/spool/<run>`) holding `path`, or None."""
    if not os.path.isabs(os.path.expanduser(str(path))):
        return None       # a message that merely mentions a spool path is not a path
    parts = Path(_canon(path)).parts
    for i in range(2, len(parts) - 1):
        if parts[i] == "spool" and parts[i - 2] == "boards":
            return Path(*parts[:i + 2])
    return None


def _blind_rows(node):
    """True when a job's inputs hold a `blind:<id>` check row (blind test text)."""
    if isinstance(node, dict):
        return is_blind(node.get("cmd", "")) or any(map(_blind_rows, node.values()))
    return isinstance(node, list) and any(map(_blind_rows, node))


def is_tester_spool(spool, missing=True):
    """True when `spool` carries blind tests (a tester's: its workspace is a clone under
    `blind-work`; or a validator's with `blind:` rows), or when that cannot be told. `missing` is
    the answer for a spool with no job.json; an unreadable or malformed one is always True."""
    try:
        job = json.loads((Path(spool) / "job.json").read_text())
        clone = "blind-work" in Path(_canon(job["workspace"])).parts
        return clone or bool(_blind_rows(job.get("inputs")))
    except FileNotFoundError:
        return missing
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return True


def withheld(path):
    """True when Korin must not get `path`: it is in a blind tester's spool, or is or is under a
    `blind*` entry of a spool."""
    spool = spool_of(path)
    if spool is None:
        return False
    rel = Path(_canon(path)).relative_to(spool).parts
    return is_tester_spool(spool) or any(p.startswith("blind") for p in rel)


def _layout(spool):
    """(home, board) of a run spool, else `WallUnavailable`."""
    if len(spool.parts) < 5 or spool.parent.name != "spool" or spool.parents[2].name != "boards":
        raise WallUnavailable(f"{spool} is not <home>/boards/<board>/spool/<run>")
    return spool.parents[3], spool.parents[1].name


def _settings(home, board):
    """(mode, blind_tests block or None) from the board's config (stdlib only)."""
    for cfg_file in (home / "boards" / board / "config.json", home / "config.json"):
        if cfg_file.is_file():
            break
    else:
        raise WallUnavailable(f"no board config for {board} under {home}")
    cfg = json.loads(cfg_file.read_text())
    if not isinstance(cfg, dict) or cfg.get("board") != board:
        raise WallUnavailable(f"{cfg_file} is not the config of board {board}")
    redesign = cfg.get("redesign", {})
    mode = redesign.get("blind_tests", "off") if isinstance(redesign, dict) else None
    if mode not in MODES:
        raise WallUnavailable(f"{cfg_file}: redesign.blind_tests is not one of {MODES}")
    block = cfg.get("blind_tests")
    if mode != "off" and not (isinstance(block, dict) and isinstance(block.get("repo"), str)
                              and block["repo"]):
        raise WallUnavailable(f"{cfg_file}: blind tests are {mode} but blind_tests.repo is unset")
    return mode, block


def _project(cfgdir, workspace):
    """The config dir's own `projects/<cwd>` folder (the realpath cwd, non-alphanumerics made `-`);
    a name over 200 characters is hashed by the CLI, so it is refused."""
    if not isinstance(workspace, str) or not workspace:
        raise JobRefused("the job names no workspace")
    name = re.sub(r"[^a-zA-Z0-9]", "-", _canon(workspace))
    if len(name) > MAX_PROJECT_NAME:
        raise JobRefused(f"the workspace path is too long to name its claude project folder"
                         f" ({len(name)} > {MAX_PROJECT_NAME}): {workspace}")
    return cfgdir / "projects" / name


def _evidence_spools(root, inputs):
    """The board spools Korin's incident evidence names, minus any blind tester's."""
    found = []
    for item in (inputs or {}).get("evidence") or []:
        spool = spool_of(item) if isinstance(item, str) else None
        if spool and spool.parent == root and spool not in found and not is_tester_spool(spool):
            found.append(spool)
    return found


def _plan(role, spool, job):
    home, board = _layout(spool)
    mode, block = _settings(home, board)
    if mode == "off":
        return None
    tier = (job.get("limits") or {}).get("tier")
    if tier in ("light", "mac"):                  # a seatbelt tier: the Mac lane is held like light
        raise LightTierRefused(MAC_REFUSED if tier == "mac" else LIGHT_REFUSED)
    root = Path(_canon(spool.parent))
    cfgdir = Path(_canon(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"))
    extra = block.get("also_walled", [])
    if not isinstance(extra, list) or not all(isinstance(x, str) and x for x in extra):
        raise WallUnavailable("blind_tests.also_walled must be a list of non-empty strings")
    others = _evidence_spools(root, job.get("inputs")) if role == "korin" else []
    own = Path(_canon(spool))
    deny = [block["repo"], home / "boards" / board / "blind-work", root, cfgdir / "projects",
            cfgdir / "jobs", *extra]
    allow = [str(own), *map(str, others)]
    if role != "korin":      # Korin never resumes, and the cwd's folder holds others' transcripts
        allow.append(_canon(_project(cfgdir, job.get("workspace"))))
    metas = (str(root), _canon(cfgdir / "projects"))     # walled already: naming one is harmless
    for item in (i for i in map(_canon, extra) if i not in metas):   # a path the session must read
        if any(a == item or a.startswith(item + "/") for a in allow):     # cannot also be walled
            raise WallUnavailable(f"blind_tests.also_walled names {item}, which the session must"
                                  f" be able to read: remove it from also_walled")
    return {"mode": mode, "spool": str(own), "probe": str(own / PROBE),
            "deny": [_canon(p) for p in deny], "meta": [str(root), _canon(cfgdir / "projects")],
            "allow": allow, "blind": [str(own), *map(str, others)] if role == "korin" else []}


def plan(role, spool_dir, job=None):
    """The wall for one `role` session in `spool_dir` (`job`: its job.json), or None when it is not
    walled (an unwalled role, or `redesign.blind_tests` off). Else a dict of realpaths: `deny`,
    `meta` (stat only), `allow`, `blind` (allowed spools whose `blind*` entries stay walled),
    `spool` and `probe`. An unknown answer gives `{"unavailable": <why>}`, which `apply` refuses."""
    if role in UNWALLED_ROLES:
        return None
    try:
        return _plan(role, Path(spool_dir).absolute(), job or {})
    except WallUnavailable as exc:
        return {"unavailable": str(exc), "error_class": exc.error_class,
                "light": isinstance(exc, LightTierRefused)}
    except Exception as exc:    # deliberate: an unforeseen failure must still wall, never crash
        return {"unavailable": f"host wall plan failed: {type(exc).__name__}: {exc}"}


def render(wall):
    """The profile text for `wall`. The last matching rule wins: the allows follow the denies, and
    the `blind*` and probe denies come last."""
    def subpaths(paths):
        return " ".join(f"(subpath {_lit(p)})" for p in paths)
    return "\n".join([
        "(version 1)",
        ";; Capsule host wall (D161, D162), host_wall.py: never hand-edit. Last match wins.",
        "(allow default)",
        f"(deny file-read* {subpaths(wall['deny'])})",
        *(f"(allow file-read-metadata (literal {_lit(p)}))" for p in wall["meta"]),
        f"(allow file-read* {subpaths(wall['allow'])})",
        *(f'(deny file-read* (regex #"^{_esc(p)}/([^/]*/)*blind"))' for p in wall["blind"]),
        f'(deny file-read* (regex #"^{_esc(wall["probe"])}"))',
    ]) + "\n"


def _write(path, text):
    """Write `text` at `path` in one step (a renamed temp file). A symlink is refused."""
    if os.path.islink(path):
        raise OSError(f"{path} is a symlink")
    tmp = f"{path}.{secrets.token_hex(4)}.tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _children(folder, keep, limit):
    """Up to `limit` sorted entries of `folder` that are readable, not symlinks, and not in or
    around an allowed path (`keep`). A folder that vanished has none."""
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return []
    found = (os.path.join(folder, n) for n in names)
    return list(itertools.islice((p for p in found if os.access(p, os.R_OK) and not os.path.islink(
        p) and not any(p == a or a.startswith(p + "/") or p.startswith(a + "/") for a in keep)),
        limit))


def _walk(folder):
    """`folder` and what lies under it, breadth first (`MAX_DEPTH` levels, `MAX_PROBED` entries a
    folder, `MAX_WALK` paths). What those limits leave out is logged, one line: never a failure."""
    found = level = [folder]
    wide = full = 0
    for _ in range(MAX_DEPTH):
        kids = [_children(d, (), None) for d in level if os.path.isdir(d)]
        wide += sum(max(0, len(k) - MAX_PROBED) for k in kids)
        level = [p for k in kids for p in k[:MAX_PROBED]]
        full += max(0, len(level) - (MAX_WALK - len(found)))
        level = level[:MAX_WALK - len(found)]
        found = found + level
    deep = sum(os.path.isdir(d) and bool(_children(d, (), 1)) for d in level)
    if wide or full or deep:
        print(f"host wall: not probed under {folder}: {wide} past {MAX_PROBED} in a folder, "
              f"{full} past {MAX_WALK} paths, {deep} below level {MAX_DEPTH} (at least: what "
              f"lies under a cut entry is not counted)", file=sys.stderr)
    return found


def _must_refuse(wall, tag):
    """(files this start makes, paths) the profile must refuse: a `blind*` file (named with `tag`)
    under every spool Korin may read, each readable walled folder with what lies under it, and
    every allow inside one. Only `meta` folders (spool root, `projects`) hold allowed runs: there
    two levels are tried and allowed entries drop; any other is walked (`_walk`), an allow in it a
    leak."""
    found = []
    for folder in wall["deny"]:
        if not (os.path.isdir(folder) and os.access(folder, os.R_OK)):
            continue
        if folder not in wall["meta"]:
            found += _walk(folder) + [a for a in wall["allow"]
                                      if a.startswith(folder + "/") and os.path.exists(a)]
            continue
        kids = _children(folder, wall["allow"], MAX_PROBED)
        found += [folder, *kids]
        for kid in kids:
            found += _children(kid, wall["allow"], MAX_DEEP) if os.path.isdir(kid) else []
    return [f"{spool}/{BLIND_PROBE}-{tag}" for spool in wall["blind"]], found


def _prove(wall, profile):
    """Run the profile for real: this start's probe file and every `_must_refuse` path (one that
    vanished since is skipped) must fail with a permission refusal, and the profile must still read.
    Else `WallUnavailable`. The probe and `blind*` files get a per-start name and go after."""
    tag = secrets.token_hex(4)
    probe, (blind, found) = f"{wall['probe']}.{tag}", _must_refuse(wall, tag)
    made = [probe, *blind]
    try:
        for path in made:
            _write(path, "probe\n")
        done = subprocess.run([SANDBOX_EXEC, "-f", profile, sys.executable, "-I", "-c", PROBE_PY,
                               profile, str(len(made))], capture_output=True,
                              input=b"\0".join(os.fsencode(p) for p in (*made, *found)),
                              timeout=PROBE_TIMEOUT)
    finally:
        for path in made:
            Path(path).unlink(missing_ok=True)
    if done.returncode == 3:
        raise WallUnavailable(f"{profile} did not refuse a walled path: the wall does not work")
    if done.returncode == 5:
        raise WallUnavailable(f"{profile} lets a path inside a walled folder or a blind* name"
                              f" in a Korin spool through: the wall does not work")
    if done.returncode == 4:
        raise WallUnavailable(f"{profile} refuses the run's own spool: the wall over-blocks")
    if done.returncode:
        err = done.stderr.decode(errors="replace").strip()[:200]
        raise WallUnavailable(f"{SANDBOX_EXEC} refused the profile {profile} (exit"
                              f" {done.returncode}): {err}")


def apply(argv, wall):
    """`argv` run under the wall: `sandbox-exec -f <profile> <argv>`. The profile goes into the
    run's own spool under a per-start name (no two starts share a file) and is proved first; any
    failure is a `WallUnavailable` and removes it."""
    if "unavailable" in wall:
        raise REFUSALS.get(wall.get("error_class"), WallUnavailable)(wall["unavailable"])
    profile, proved = f"{Path(wall['spool']) / PROFILE}.{secrets.token_hex(4)}", False
    try:
        _write(profile, render(wall))
        _prove(wall, profile)
        proved = True
    except subprocess.TimeoutExpired as exc:     # a loaded machine, not a failed wall
        raise _ProbeTimeout(f"the start-up check did not finish in {PROBE_TIMEOUT} s") from exc
    except ValueError as exc:
        raise WallUnavailable(f"a path the wall must quote cannot be walled: {exc}") from exc
    except (OSError, KeyError, subprocess.SubprocessError) as exc:
        raise WallUnavailable(f"{SANDBOX_EXEC}: {type(exc).__name__}: {exc}") from exc
    finally:
        if not proved:      # a dropped start leaves no profile behind
            Path(profile).unlink(missing_ok=True)
    return [SANDBOX_EXEC, "-f", profile, *argv]
