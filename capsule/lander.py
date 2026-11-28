"""The landing checks as a dry-run step (off-the-old-system plan C07a): what the merge keeper
runs by hand before a landing, as code. `run` reports pass/fail per check and NEVER merges,
pushes, resets or edits anything: the suites and gates run in a throwaway clone of the repo at
the card's commit, and the only writes are that clone's temp dir (removed again) and the
`origin/main` ref a fetch refreshes. Checks, in order: `fetch` (a failure waits), `sync` (the
commit is on the epic branch, the branch holds `origin/main`; a card behind main has its
commits (the tip and the earlier ones naming the card) replayed onto it by cherry-pick in the
throwaway clone and the new tip is the one checked and landed; a replay that conflicts waits and
names the files), `protected` (PROTECTED paths, matched case-insensitively, and case-colliding
paths hold for the owner's stamp; a changed file whose directory carries a SHA256SUMS must match
its line, else red), `stale_marker` (a targets-predate-range marker must name the range base or
an ancestor of it, else held), `gates`, `suites` (the board's `contract_commands`, held when
docker tests are needed), `blind` (R35, `gate` mode only: the card's blind tests run again on the
exact commit to push, `lander_blind.py`; a failure or a run that cannot be trusted holds the
card, a sandbox that is down waits) and `deploy` (advisory: reported, never red). A candidate's
own code runs only in the sandbox (runners/sandbox.py; a mac card's, on the host under the Mac
lane profile, the lane taken once by its session): no docker, a held card. Once a check fails or
waits the rest skip. `record` turns a verdict into the `landing_checked` event and the card's
state. C07b adds the merge, the push and the revert rule behind `lander.mode`.
"""
import argparse
import collections
import contextlib
import hashlib
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from capsule import blocks, config, db, incidents, lander_blind, lander_select, paths, seats
from capsule import shards, workspace
from capsule.db import event, txn
from capsule.lander_blind import step as blind_step
from capsule.runners import mac, sandbox

TAIL = 600
TIMEOUT = 1200   # seconds per command; at least tick.CONTRACT_TIMEOUT (a suite that passed review)
DETAIL = 1500      # a check's recorded detail: the unittest summary lines first, then the tail
# a unittest summary line, or the final exception line of a traceback
SUMMARY = re.compile(r"^(FAIL:|ERROR:|Ran |FAILED|[\w.]*(Error|Exception)\b)")
KEEPER = "merge-keeper"    # the owner the keeper stamps with bin/capsule-own (keeper-rules.md)
GATES = ("python3 tests/mutation_check.py --base {base} --head {head}",
         "python3 tests/size_check.py --base {base} --head {head}",
         "python3 tests/simplicity_check.py --base {base} --head {head}")
DEPLOY = "./deploy.sh --check"
GATE = re.compile(r"python3 (tests/\w+\.py)((?: .*)?)")   # the one gate shape run from trusted code
HOST_GATES = ("tests/size_check.py", "tests/simplicity_check.py", "tests/ruff_gate.py",
              "tests/portability_lint.py")   # text-only trusted gates; any other is sandboxed
# tests/mutation_check.py's marker shape; valid when its sha is the range base or an ancestor
MARKER = re.compile(r"^\+#\s*mutation-check:\s*targets-predate-range\s+([0-9a-fA-F]{7,40})\s+--",
                    re.M)
EXIT = {"checked": 0, "red": 1, "held": 2, "wait": 3}
ENV_RC = 75    # a gate that refused for the environment, not the code (tests/mutation_check.py)
WAITS_FOR_INCIDENT = 3    # waits on one commit that open an incident
ENV_WAIT = "environment error twice, no verdict on the card"   # opens that wait's detail
HOLDS = {"protected": "protected", "stale_marker": "stale_marker", "suites": "lander_wait_docker",
         "gates": "lander_wait_docker", "fetch": "lander_wait_fetch", "sync": "lander_wait_rebase",
         "blind": "lander_wait_docker",
         "suites:hold": "needs_docker_tests",    # name:status beats name (a sandbox down is a wait)
         "gates:env": "lander_wait_env", "gates:hold": "needs_docker_tests"}
BLIND_ROWS = ("blind", "mount_audit")   # the rows the landing's blind step holds on
BLIND_SKIP = ("stale_marker", "gates", "suites", "deploy")   # not run on the keeper's blind route
NOT_RUN = "not run: blind-only route"
Work = collections.namedtuple("Work", "path env deploy_env")
IDENT = ("-c", "user.name=capsule-lander", "-c", "user.email=lander@capsule.invalid")


def _git(repo, *args, timeout=120):
    try:
        done = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True,
                              timeout=timeout, env={**os.environ, "LC_ALL": "C",
                                                    "GIT_TERMINAL_PROMPT": "0"})
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 124, f"git {args[0]} did not finish: {exc}"
    return done.returncode, (done.stdout + done.stderr).strip()


def _tail(rc, out):
    out = out.strip()
    if rc in (0, None):      # None: the sandbox is unavailable and `out` says why
        return rc, out[-TAIL:]
    lines = [ln for ln in out.splitlines() if SUMMARY.match(ln)]   # FAILED summary stays ahead
    return rc, "\n".join(lines)[:DETAIL - TAIL - 100] + "\n--- tail ---\n" + out[-TAIL:]


def _sh(cmd, cwd, timeout, env):
    try:
        done = subprocess.run(cmd, shell=True, cwd=str(cwd), text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"
    except OSError as exc:
        return 127, f"could not run: {exc}"
    return _tail(done.returncode, done.stdout)


# Mirrors DOCKER_PATHS in tests/run_shell_suites.sh (a test pins the two equal).
DOCKER_PATHS = (r'^(docker/|observability/|maintenance/.*\.sql$|.*_pg\.sh$'
                r'|tests/lib_docker_optin\.sh$|tests/test_(docker_image|fluent_bit_scrub)\.sh$)')


def _sync(repo, commit, base, branch, board=(None, None)):
    if _git(repo, "merge-base", "--is-ancestor", commit, f"refs/heads/{branch}")[0] != 0:
        return "fail", f"{commit[:12]} is not on {branch}"
    if _git(repo, "merge-base", "--is-ancestor", base, commit)[0] != 0:
        return "wait", "needs rebase onto origin/main"   # a wait, never a counted failure (G2c)
    if _carries_closed(repo, *board, commit, base):
        return "wait", "replay: a closed card's commits are below the card"   # replayed alone
    return "pass", f"{commit[:12]} on {branch}, holds {base[:12]}"


def _card_commits(repo, commit, base, task_id, after=None):
    """The card's commits to replay, oldest first, ending in `commit`: those on base..commit whose
    message names the card the way the runners write it (`<title> (<task_id>, capsule run N)`),
    merges and other cards' commits left out. With `after` (the card commit its last landing
    replayed) only the commits that are not `after` or its ancestors count: what that landing
    carried is on main already, possibly changed since. `commit` itself is always last, so a
    message the runner truncated or a hand edit costs only the earlier commits, never the tip.
    None when git cannot list them (the caller waits)."""
    mine = []
    if task_id:
        rc, out = _git(repo, "rev-list", "--reverse", "--topo-order", "--no-merges",
                       "--fixed-strings", "--grep", f"({task_id}, capsule run",
                       f"{base}..{commit}", *([f"^{after}"] if after else []))
        if rc:
            return None
        mine = out.split()
    return [c for c in mine if c != commit] + [commit]


def _patch_ids(repo, *revs):
    """The verbatim patch ids (`git patch-id --verbatim`: whitespace counts, which `git cherry`
    ignores) of the non-merge commits `revs` select; empty when git could not say."""
    try:
        log = subprocess.run(["git", "log", "-p", "--no-merges", "--no-color", "--no-ext-diff",
                              "--no-textconv", *revs], cwd=str(repo),
                             capture_output=True, timeout=120)
        ids = subprocess.run(["git", "patch-id", "--verbatim"], cwd=str(repo), input=log.stdout,
                             capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return set()
    return {ln.split()[0] for ln in ids.stdout.decode(errors="replace").splitlines()
            if ln.strip()} if log.returncode == ids.returncode == 0 else set()


def _skip_same_patch(repo, work, base, commit, limit, rc, out):
    """(rc, out) of a replay after skipping the commits whose patch is already on `base`, word for
    word (same `git patch-id --verbatim`): rebased or re-applied there, then changed again by
    main, so the pick conflicts though the card's change is in. A patch that differs only in
    whitespace is a different change and still conflicts. Any other stop comes back as it was; a
    skipped commit is dropped and the rest are picked, at most `limit` times. Nothing is skipped
    while main's side of the range holds a revert: the equal commit may be the one reverted, and
    that change is wanted again."""
    main = None
    for _ in range(limit):
        if not rc:
            break
        got, stuck = _git(work, "rev-parse", "--verify", "CHERRY_PICK_HEAD")
        if got:
            break
        if main is None:
            reverted = _git(repo, "log", "-1", "--fixed-strings", "--grep", "This reverts commit",
                            "--format=%H", f"{commit}..{base}")[1]
            main = set() if reverted else _patch_ids(repo, f"{commit}..{base}")
        if not _patch_ids(repo, "--no-walk", stuck) & main:
            break
        rc, out = _git(work, *IDENT, "cherry-pick", "--skip")
    return rc, out


def _tip_resolves(work, base, commit, earlier):
    """True when the tip alone applies onto `base` and every file the `earlier` picks changed
    ends with the tip's own content (where main left it unchanged since the lane's last merge of
    main): a by-hand merge between the card's commits made the earlier picks conflict though the
    tip's tree holds the resolution. Run after every pick conflicted; on True the clone holds it."""
    _git(work, "cherry-pick", "--abort")
    if _git(work, "checkout", "-q", "--detach", base)[0] != 0:
        return False
    if _git(work, *IDENT, "cherry-pick", "--empty=drop", "--end-of-options", commit)[0] != 0:
        return False
    files = set()
    for pick in earlier:   # -z keeps names with spaces whole; --no-renames lists both sides
        rc, out = _git(work, "diff-tree", "--no-commit-id", "--name-only", "--no-renames", "-r",
                       "-z", pick)
        if rc:
            return False
        files.update(name for name in out.split("\0") if name)
    names = sorted(files)
    if not names or _git(work, "--literal-pathspecs", "diff", "--quiet", commit, "HEAD", "--",
                         *names)[0] == 0:
        return True
    # the resolution sits in the merge commit the replay leaves out: take the tip's content
    mb = _git(work, "merge-base", commit, base)
    if mb[0] != 0 or _git(work, "--literal-pathspecs", "diff", "--quiet", mb[1], base, "--",
                          *names)[0] != 0:
        return False
    for name in names:
        have = _git(work, "cat-file", "-e", f"{commit}:{name}")[0] == 0
        step = ("checkout", commit) if have else ("rm", "-q", "--ignore-unmatch")
        if _git(work, "--literal-pathspecs", *step, "--", name)[0] != 0:
            return False
    moved = _git(work, "rev-parse", "HEAD")[1] != _git(work, "rev-parse", base)[1]
    return _git(work, *IDENT, "commit", "-q", *["--amend"] * moved, "-C", commit)[0] == 0


def _replay(repo, commit, base, task_id=None, after=None):
    """(new sha or None, status, detail): the card's commits cherry-picked onto `base` in a
    throwaway clone, the result fetched back into `repo` as objects only (no ref moves). The
    card's commits are `commit` plus the earlier ones on base..commit whose message names
    `task_id` (a reworked card is its original plus the fix-up), oldest first, minus `after` and
    its ancestors (see `_card_commits`); no other card's commit and no epic history comes along.
    With no `task_id`, or no earlier match, that is `commit` alone. A conflict names the commit
    and the files; a merge commit as `commit`, or a commit list git could not give, is a wait
    that says why. A change already on `base` is dropped from the replay (a commit with the same
    patch id too, even when main changed those lines again), and a replay with nothing left is
    `base` itself."""
    if len(_git(repo, "rev-list", "--parents", "-n1", commit)[1].split()) > 2:
        return None, "wait", "needs rebase onto origin/main: the card commit is a merge"
    picks = _card_commits(repo, commit, base, task_id, after)
    if picks is None:
        return None, "wait", "needs rebase onto origin/main: could not list the card's commits"
    with _clone(repo, commit, True) as work:
        ready = work is not None and _git(work.path, "fetch", "-q", "--end-of-options",
                                          str(repo), base)[0] == 0
        if not ready or _git(work.path, "checkout", "-q", "--detach", base)[0] != 0:
            return None, "wait", "needs rebase onto origin/main: could not replay"
        rc, out = _git(work.path, *IDENT, "cherry-pick", "--empty=drop", "--end-of-options",
                       *picks)
        rc, out = _skip_same_patch(repo, work.path, base, commit, len(picks), rc, out)
        if rc:
            files = _git(work.path, "diff", "--name-only", "--diff-filter=U")[1].split()
            got, head = _git(work.path, "rev-parse", "--verify", "CHERRY_PICK_HEAD")
            stuck = f"of {head[:12]} " if got == 0 else ""   # git's error text is not a sha
            why = f"{stuck}conflicts in {', '.join(files)}" if files else out[-300:]
            if not (got == 0 and len(picks) > 1
                    and _tip_resolves(work.path, base, commit, picks[:-1])):
                return None, "wait", f"needs rebase onto origin/main: replay {why}"
        new = _git(work.path, "rev-parse", "HEAD")[1]
        if new == _git(work.path, "rev-parse", base)[1]:
            return base, "pass", f"{commit[:12]} is already on origin/main (empty replay)"
        if _git(repo, "fetch", "-q", "--end-of-options", str(work.path), new)[0] != 0:
            return None, "wait", "needs rebase onto origin/main: could not keep the replay"
    if rc:            # every pick conflicted and the tip alone resolved it (`_tip_resolves`)
        return new, "pass", f"tip {commit[:12]} replayed alone onto {base[:12]} as {new[:12]}"
    return new, "pass", f"{len(picks)} commit(s) up to {commit[:12]} replayed onto {base[:12]} " \
                        f"as {new[:12]}"


def _sums_problems(repo, commit, files):
    """A changed file whose directory carries a SHA256SUMS must match its line."""
    out = []
    for f in files:
        folder = str(Path(f).parent)
        sums = f"{folder}/SHA256SUMS" if folder != "." else "SHA256SUMS"
        if f == sums or _git(repo, "cat-file", "-e", f"{commit}:{f}")[0] != 0:
            continue
        rc, text = _git(repo, "show", f"{commit}:{sums}")
        if rc != 0:
            continue
        lines = dict(reversed(l.split(None, 1)) for l in text.splitlines() if l.strip())
        body = subprocess.run(["git", "show", f"{commit}:{f}"], cwd=str(repo),
                              capture_output=True).stdout
        if lines.get(Path(f).name, "").strip() != hashlib.sha256(body).hexdigest():
            out.append(f"{f}: no matching line in {sums}")
    return out


def _protected(repo, commit, base):
    # --no-renames lists both sides of a rename; -z leaves odd names unquoted
    listing = _git(repo, "diff", "--name-only", "--no-renames", "-z", base, commit)[1]
    files = [f for f in listing.split("\0") if f]
    flagged = workspace.touches_protected(files)
    bad = _sums_problems(repo, commit, flagged)
    if bad:
        return "fail", "; ".join(bad)
    # APFS is case-insensitive: two tree paths differing only in case collide on the Mac checkout
    tree = _git(repo, "ls-tree", "-r", "--name-only", "-z", commit)[1].split("\0")
    seen = collections.Counter(p.casefold() for p in tree if p)
    clash = sorted(f for f in files if seen[f.casefold()] > 1)
    if flagged or clash:
        why = [f"protected paths need the owner's stamp: {', '.join(flagged)}"] if flagged else []
        if clash:
            why.append(f"case-colliding paths: {', '.join(clash)}")
        return "hold", "; ".join(why)
    return "pass", "no protected path in the range"


def _stale_marker(repo, commit, base):
    diff = _git(repo, "diff", "-U0", base, commit)[1]
    stale = []
    for short in MARKER.findall(diff):   # mutation_check: the base itself or any ancestor of it
        full = _git(repo, "rev-parse", "--verify", "-q", f"{short}^{{commit}}")[1]
        if not full or _git(repo, "merge-base", "--is-ancestor", full, base)[0] != 0:
            stale.append(short)
    if stale:
        return "hold", ("targets-predate-range marker is not the base or an ancestor of it: "
                        + ", ".join(stale))
    return "pass", "no stale marker"


def target(cfg):
    """The branch this board lands on (`lander.branch`, default main): `origin/<it>` is every
    place the lander once read `origin/main`."""
    return ((cfg or {}).get("lander") or {}).get("branch") or "main"


def _fetch(repo, fetch, branch="main"):
    from capsule import day_branch      # it imports this module
    if not fetch:
        return "pass", "not fetched (fetch off)"
    rc, out = day_branch.fetch(repo, branch)
    return (("pass", f"fetched origin {branch}") if rc == 0
            else ("wait", f"fetch failed: {out[-300:]}"))


def _trusted_gate(cmd, clone):
    """`python3 tests/<name>.py [args]` rewritten to run that script from the trusted checkout
    (paths.REPO, the checkout the lander itself runs from) on the candidate via `--repo`, so a
    worker's edit of its own gate script never runs on the host. Returns (command, problem,
    boxed); a gate outside HOST_GATES (it may run the candidate's tests) is in its sandbox form
    (/trusted, /workspace). A matching gate the trusted checkout lacks fails closed: (None, why,
    False), and the candidate's script never runs. Any other shape (`bash tests/x.sh`, `echo ok`)
    is config-only: (None, None, False), and it runs as written, in the sandbox."""
    m = GATE.fullmatch(cmd)
    if not m:
        return None, None, False
    if not (paths.REPO / m[1]).is_file():
        return None, f"gate script missing from trusted checkout: {m[1]}", False
    boxed = m[1] not in HOST_GATES
    root, repo = (sandbox.TRUSTED, sandbox.WORKSPACE) if boxed else (paths.REPO, clone)
    return (f"python3 {shlex.quote(f'{root}/{m[1]}')}{m[2]} --repo {shlex.quote(str(repo))}",
            None, boxed)


def _env_retry(gate, first, work, timeout, again, flaky):
    """A sandboxed trusted gate refused for the environment (ENV_RC, `first` its output): run it
    once more in a fresh copy of the commit (`again()` starts a new sandbox session), so state
    left by an earlier boxed gate neither causes nor cures it. Green: (0, the note) and `gate` is
    appended to `flaky`. A second refusal asks the host clone, which no container writes, about the
    paths it named only: dirty is the commit's own doing, red; clean or none named, a wait (none
    named: its detail is the gate's `environment error:` line). Returns (rc, tail)."""
    first = "\n".join(ln for ln in first.splitlines() if "--allow-dirty" not in ln)
    rc, out = again()(gate, timeout)
    rc, tail = _tail(rc, out)
    if rc == 0:
        flaky.append(gate)
        return 0, f"environment error, then green on retry (flaky): {first}"
    if rc != ENV_RC:
        return rc, tail
    named = {ln.strip() for ln in out.splitlines() if ln.startswith("  ") and ln.strip()}
    # read-tree first: a stat-cache status misses a CRLF file; paths compare as git's quoted text
    if named and _git(work.path, "read-tree", "HEAD")[0] == 0:
        code, status = _git(work.path, "-c", "core.quotepath=true", "status", "--porcelain",
                            "--no-renames", "--untracked-files=no")
        # `_git` strips the output, so the first line may lack its leading space: no `line[3:]`
        dirty = [ln for ln in status.splitlines() if ln.split(None, 1)[1:] and
                 ln.split(None, 1)[1] in named]
        if code == 0 and dirty:
            return 1, ("environment error twice, and the commit itself leaves a dirty checkout:\n"
                       + "\n".join(dirty[:5]) + f"\n{first}")
    own = re.search(r"^environment error:.{,200}", out, re.M)
    why = own[0] if own and not named else "the host clone is clean (stale share?)"
    return None, f"{ENV_WAIT}: {why}. Unblock to check again.\n{first}"


def _commands(cmds, work, timeout, subst=(), deploy=False, gates=False, box=None, again=None,
              flaky=None, rows=None):
    """Run `cmds`. The deploy check, and the trusted gate scripts that only read text, run on the
    host from the trusted checkout (see `_trusted_gate`). Everything else runs the candidate's
    code, the board's `contract_commands` included: in the sandbox `box` (runners/sandbox.py) or
    not at all; an unavailable sandbox makes the check wait. A boxed trusted gate that exits
    ENV_RC is retried by `_env_retry` (`again`, a new session; `flaky`, a list that learns the
    gates that passed only then). `rows`, a list, gets one {cmd, exit, output} per command run
    (for `shards.failures`). The clone root reads `<clone>` in a detail."""
    if work is None:
        return "fail", "could not clone or check out the commit"
    seen, status = [], "pass"
    for cmd in cmds:
        for old, new in subst:
            cmd = cmd.replace(old, new)
        gate, problem, boxed = _trusted_gate(cmd, work.path) if gates else (None, None, False)
        if problem:
            rc, tail = 1, problem
        elif deploy or (gate and not boxed):
            env = work.deploy_env if deploy else work.env
            rc, tail = _sh(gate or cmd, paths.REPO, timeout, env)
        else:
            rc, tail = _tail(*box(gate or cmd, timeout))
            if gate and rc == ENV_RC and again:    # the gate could not judge: not the card's fault
                rc, tail = _env_retry(gate, tail, work, timeout, again, flaky)
        for root in sorted({str(work.path), str(work.path.resolve())}, key=len, reverse=True):
            tail = tail.replace(root, "<clone>")
        seen.append(f"$ {cmd}\n{tail}")
        if rows is not None:
            rows.append({"cmd": cmd, "exit": rc, "output": tail})
        if rc != 0:
            status = ("hold" if tail.startswith(sandbox.NO_IMAGE) else "wait") if rc is None \
                else "fail"
            break
    return status, "\n".join(seen)[-DETAIL:]


@contextlib.contextmanager
def _audit_guard(conn, cfg, card, rows):
    """Around a lander container's whole session: a mount audit refusal inside it ends the session
    with one more row, `mount_audit` (`lander_blind.audit_hold`'s hold), instead of a crash."""
    try:
        yield
    except RuntimeError as exc:
        status, detail = lander_blind.audit_hold(conn, cfg, card, exc)
        rows.append({"name": "mount_audit", "status": status, "detail": detail})


@contextlib.contextmanager
def _clone(repo, head, load):
    """A throwaway clone detached at `head` (None when `load` is false or git fails), with the
    env of the trusted host commands pinned inside the same temp dir: a fake vault, no sends, its
    own TMPDIR, HOME and state dir (common-rules). Candidate code runs in the sandbox instead."""
    tmp = Path(tempfile.mkdtemp(prefix=paths.LANDER_PREFIX))
    try:
        work = None
        if load:
            path = tmp / paths.CLONE_DIR
            ok = _git(tmp, "clone", "-q", "--no-checkout", str(repo), str(path))[0] == 0
            ok = ok and _git(path, "checkout", "-q", "--detach", head)[0] == 0
            for d in ("vault", "home", "state", "t"):
                (tmp / d).mkdir()
            real_uv = os.environ.get("UV_CACHE_DIR") or os.path.expanduser("~/.cache/uv")
            pin = {"OBSIDIAN_VAULT_PATH": str(tmp / "vault"), "AGENT_OPS_NO_SEND": "1",
                   "TMPDIR": str(tmp / "t"), "CAPSULE_OPS_PY": "python3"}
            inherited = {k: v for k, v in os.environ.items()
                         if k not in ("RUN_DOCKER_TESTS", "CAPSULE_DOCKER_AUTO_BASE")}
            env = {**inherited, **pin, "HOME": str(tmp / "home"),
                   "AGENT_OPS_STATE_DIR": str(tmp / "state"), "UV_CACHE_DIR": real_uv}
            work = Work(path, env, {**inherited, "TMPDIR": str(tmp / "t")}) if ok else None
        yield work
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _deploy(cmds, work, timeout):
    """Advisory only, from the trusted checkout: deploy.sh sets REPO from its own directory, so in
    the throwaway clone it would red every card. The gating `--check` is landing._deploy."""
    status, detail = _commands(cmds, work, timeout, deploy=True)
    return ("pass" if status == "pass" else "advisory"), detail


def _docker_diff(repo, base, head):
    """True when base...head touches a docker path (a failed diff counts as none, loudly)."""
    code, out = _git(repo, "diff", "--name-only", "--no-renames", f"{base}...{head}")
    return code == 0 and any(re.match(DOCKER_PATHS, f) for f in out.splitlines())


def _suites(work, suites, needs_docker, timeout, box, docker_diff=False, parts=None):
    """`suites` are the board's commands (the needs-docker match); `parts` is what runs, given
    only when the board sets `contract_shards` (then every part's count line is required)."""
    if docker_diff or any(c in suites for c in needs_docker):    # the sandbox has no docker socket
        return "hold", "needs docker tests: land by keeper"
    sharded, parts, rows = parts is not None, suites if parts is None else parts, []
    status, detail = _commands(parts, work, timeout, box=box, rows=rows)
    if bad := (sharded and status == "pass" and shards.failures(parts, rows)):
        return "fail", "; ".join(bad)[:DETAIL]
    if status != "fail" or work is None or "timed out after" in detail:   # no 2nd 1200s run
        return status, detail
    rows = []
    again, second = _commands(parts, work, timeout, box=box, rows=rows)   # one retry: a flake
    if sharded and again == "pass" and (bad := shards.failures(parts, rows)):
        return "fail", "; ".join(bad)[:DETAIL]
    if again == "pass":
        return "pass", f"red, then green on retry (flaky): {detail}"[:DETAIL]
    return again, second


def _verdict(rows):
    status = {r["status"] for r in rows}
    if "fail" in status:
        return "red", None
    why = next((_why(r) for r in rows if r["status"] in ("hold", "wait")), None)
    return ("held" if "hold" in status else "wait" if "wait" in status else "checked"), why


def _why(row):
    """The reason name of a held or waiting row; a `blind` or `mount_audit` hold carries its plain
    reason first in its detail."""
    if row["name"] == "gates" and ENV_WAIT in row["detail"]:
        return HOLDS["gates:env"]
    if row["name"] in BLIND_ROWS and row["status"] == "hold":
        return row["detail"].split(": ")[0]       # the plain reason leads the detail
    return HOLDS.get(f"{row['name']}:{row['status']}") or HOLDS[row["name"]]


def _boxes(stack, work, cfg, span, home=None, runner=None, card=(None, None)):
    """(box, again, first) over sandbox sessions on `stack`; `box(cmd, timeout)` runs in the
    newest one. `again()`: a fresh container and copy for the commands after a retry (a mac
    card: its one). `first`: the first session (a mac one holds the lane lock, `lock`). `card`:
    (conn, task id), to tell a mac card."""
    mac_card = mac.card_is_mac(*card)
    runs = [stack.enter_context(mac.checks(work and work.path, cfg, span, runner, home, mac_card))]

    def again():
        if not mac_card:
            runs.append(stack.enter_context(sandbox.session(work.path, cfg, span, runner, home)))
        return runs[-1]
    return (lambda cmd, secs: runs[-1](cmd, secs)), again, runs[0]


def _result(rows, head, card, base_sha, first=None):
    """The dict `run` returns: the verdict of `rows`, the commit checked, the card's own commit and
    `lane` (a `mac.LaneWait` if the card's mac session could not run)."""
    verdict, why = _verdict(rows)
    return {"verdict": verdict, "hold": why, "commit": head, "card_commit": card,
            "base": base_sha, "checks": rows, "lane": getattr(first, "lane", None)}


def _carries_closed(repo, conn, task_id, head, base):
    """True when base..head holds a commit of another card a person closed (work that did not
    land): the lane already holds main, so a fast-forward push of `head` would carry it to main
    and into this card's revert list. The card is then replayed alone, as a lane behind main is."""
    if conn is None or not task_id:
        return False
    log = _git(repo, "log", "--no-merges", "--format=%B", f"{base}..{head}")[1]
    return any(other != task_id and blocks.was_closed(conn, other)
               for other in set(re.findall(r"\((t_\w+), capsule run [^)\s]+\)", log)))


def run(repo, branch, *, cfg, commit=None, base=None, fetch=True, timeout=TIMEOUT,
        task_id=None, after=None, home=None, conn=None, runner=None, skip=()):
    """The verdict dict: verdict (checked | red | held | wait), hold (the reason name of a held or
    waiting card), commit, base and one {name, status, detail} row per check. `task_id` names the
    card's commits for a replay; `after` (called after the fetch, only when a replay is needed)
    gives the card commit its last landing replayed, or None. With `conn` and `redesign.
    blind_tests` = `gate` the blind tests run too (`lander_blind`, on `runner`); `skip` names the
    checks not run: their rows read `skipped` (`NOT_RUN`), never `pass` (the keeper's route)."""
    repo, lc, rows, t0, tgt = Path(repo), cfg.get("lander") or {}, [], time.monotonic(), target(cfg)

    def named(text):         # on a day board every message names the branch, not origin/main
        text = text if tgt == "main" else re.sub(r"origin/main(?![\w/.-])", f"origin/{tgt}", text)
        return paths.collapse_clone(text)       # and the clone root reads <clone>

    def go(name, fn, *args, **kw):
        if name in skip:
            rows.append({"name": name, "status": "skipped", "detail": NOT_RUN})
        elif any(r["status"] in ("fail", "wait") for r in rows):   # fail closed
            rows.append({"name": name, "status": "skipped", "detail": "an earlier check failed"})
        else:
            status, detail = fn(*args, **kw)
            rows.append({"name": name, "status": status, "detail": named(detail)[:DETAIL]})

    go("fetch", _fetch, repo, fetch, tgt)
    head = _git(repo, "rev-parse", "--verify", commit or f"refs/heads/{branch}")[1]
    if base:
        base_sha = _git(repo, "rev-parse", "--verify", base)[1]
    else:
        from capsule import day_branch
        fresh = bool(fetch) and rows[-1]["status"] == "pass"     # no merge on stale refs
        base_sha, status, detail = day_branch.base(repo, tgt, merge=fresh)
        if status == "wait":     # the catch-up merge could not be made: no checks on an empty base
            rows.append({"name": "sync", "status": status, "detail": named(detail)[:DETAIL]})
            return _result(rows, head, head, "")
    go("sync", _sync, repo, head, base_sha, branch, (conn, task_id))
    card, lane = head, None
    if rows[-1]["status"] == "wait":     # behind main: replay the card onto it, check that
        new, status, detail = _replay(repo, head, base_sha, task_id, after and after())
        rows[-1] = {"name": "sync", "status": status, "detail": named(detail)[:DETAIL]}
        head = new or head
    go("protected", _protected, repo, head, base_sha)
    go("stale_marker", _stale_marker, repo, head, base_sha)
    load = not any(r["status"] in ("fail", "wait") for r in rows)
    gates = lc.get("gates", GATES)
    blind = lander_blind.plan(conn, cfg, task_id, head == base_sha)
    span = 0 if skip else timeout * (len(gates) + 2 * len(shards.expand(cfg)))   # suite: twice
    with _audit_guard(conn, cfg, task_id, rows), _clone(repo, head, load) as work, \
            contextlib.ExitStack() as stack:
        box, again, lane = _boxes(stack, work, cfg, span, home, runner, (conn, task_id))
        subst, flaky = (("{base}", base_sha), ("{head}", head)), []
        go("gates", _commands, gates, work, timeout, subst, gates=True, box=box, again=again,
           flaky=flaky)
        rows[-1].update({"flaky": True} if flaky else {})     # a flag, not a match on the output
        go("suites", _suites, work, cfg["contract_commands"], lc.get("needs_docker", ()),
           timeout, box, docker_diff=_docker_diff(repo, base_sha, head),
           parts=lander_select.parts(cfg, lc, work, base_sha, head))
        if blind:     # selection never skips the blind gate or its no-blind-tests hold
            blind_step(go, rows, blind, conn, cfg, work, timeout, t0 + span, runner, home, lane)
        go("deploy", _deploy, [lc.get("deploy_check", DEPLOY)], work, timeout)
    return _result(rows, head, card, base_sha, lane)


def record(conn, task_id, result, *, limit, run_id=None, now=None, board=None):
    """Append the `landing_checked` event; red goes back to ready as a counted failure, held and
    waiting cards are blocked. A green verdict changes nothing else (dry-run). The event and the
    card's move are one transaction, so the card is never claimable between them. `board` names
    the board for a repeat-wait incident (none: the db's directory name) and a hold's commands."""
    checks = [{**c, "detail": paths.collapse_clone(c["detail"])} for c in result["checks"]]
    first = next((r for r in checks if r["status"] in ("fail", "hold", "wait")), None)
    reason = f"landing check {first['name']}: {first['detail']}" if first else ""
    with txn(conn):
        event(conn, task_id, "landing_checked",
              {"verdict": result["verdict"], "hold": result["hold"], "commit": result["commit"],
               "card_commit": result.get("card_commit"), "base": result["base"],
               "checks": checks}, run_id=run_id, now=now)
        for row in result["checks"]:
            if row.get("flaky") or (row["name"] == "suites"
                                    and row["detail"].startswith("red, then green")):
                event(conn, task_id, "landing_flaky", {"detail": row["detail"]},
                      run_id=run_id, now=now)
        if result["verdict"] == "red":
            blocks.record_failure(conn, task_id, reason=reason, limit=limit, run_id=run_id,
                                  now=now)
        elif result["verdict"] in ("held", "wait"):
            kind = "needs_input" if result["verdict"] == "held" else "transient"
            db.transition(conn, task_id, "ready", frm=("done",))   # `block` takes review, not done
            # `cause` is what `blocks.lander_wait` trusts: no worker verb can set it
            why = f"{result['hold']}: {reason}"[:1800] + (
                f"; {lander_blind.WAIT_NEXT}" if kind == "transient" else "")
            terms = {"reason": why, "offer": "unblock|reset",
                     "cause": result["hold"] if kind == "transient" else None,
                     **_blind_terms(conn, task_id, result["checks"], board)}
            blocks.block(conn, task_id, kind=kind, run_id=run_id, now=now, **terms)
            if kind == "transient":
                _repeat_wait(conn, task_id, result, now, board)


def _repeat_wait(conn, task_id, result, now, board):
    """A third wait on one card commit opens a `review_infra` incident, whoever unblocks it: a
    repeat has a signal. `commit` is the replayed one (new at every check of a card behind
    main), so the card's own commit is what counts."""
    card = result.get("card_commit") or result["commit"]
    seen = conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='landing_checked' AND "
        "json_extract(payload, '$.verdict')='wait' AND "
        "COALESCE(json_extract(payload, '$.card_commit'), json_extract(payload, '$.commit'))=?",
        (task_id, card)).fetchone()[0]
    if seen >= WAITS_FOR_INCIDENT:
        # `boards/<board>/kanban.db`: the directory name, when the caller has no board to say
        board = board or Path(conn.execute("PRAGMA database_list").fetchone()[2]).parent.name
        incidents.open_incident(
            conn, board=board, kind="review_infra", task_id=task_id, now=now,
            key=f"lander check keeps waiting: {result['hold']}",
            evidence=[f"{seen} waits on {card[:12]}: {result['hold']}"])


def _blind_terms(conn, task_id, rows, board):
    """`blocks.block` arguments of a landing whose first held row is a blind one: its detail (the
    plain reason) with the verbs that lift the hold, the verbs as the offer, and the validator's
    own `cause` for that reason (so a person's `accept` after THIS hold, and only that, is
    honoured, and an older release never survives a newer hold). `accept` is offered only for a
    reason a release can lift (`lander_blind.RELEASABLE`) and when no test failed. Else {}."""
    first = next((r for r in rows if r["status"] in ("fail", "hold", "wait")), None)
    if first is None or first["name"] not in BLIND_ROWS or first["status"] != "hold":
        return {}
    reason = first["detail"].split(": ")[0]
    accept = reason in lander_blind.RELEASABLE and not any(
        r["name"].startswith("blind:") for r in rows)
    exits = lander_blind.exits(board or "<board>", task_id, accept)
    terms = {"reason": f"{first['detail']}; {exits}"[:2000],
             "offer": "accept|amend|reset|close" if accept else "amend|reset|close"}
    task = conn.execute("SELECT body FROM tasks WHERE id=?", (task_id,)).fetchone()
    if task is not None:
        terms["cause"] = lander_blind.cause(task, reason)
    return terms


def guard(conn, now):
    """G3, the keeper's side: reasons not to `merge --ff-only` (empty list: go). A fresh
    `lander.busy` flag, or a landing this lander pushed that is not yet verified or reverted."""
    busy, _ = db.guard_get(conn, "lander.busy")
    why = ["lander.busy is held"] if (
        busy and busy.get("busy") and now - busy["at"] < seats.LANDER_TTL) else []
    for key, blob in conn.execute("SELECT key, value FROM guard_state WHERE key LIKE ?",
                                  ("lander.landed:%",)):
        if json.loads(blob).get("state") in ("pushed", "revert_owed"):
            why.append(f"{key} is in state {json.loads(blob)['state']}")
    return why


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["guard"]:      # the keeper's pre-ff check: `python3 -m capsule.lander guard`
        gp = argparse.ArgumentParser(prog="lander guard", description="Exit 1 if landing.")
        gp.add_argument("--board", required=True)
        gp.add_argument("--home")
        g = gp.parse_args(argv[1:])
        home = Path(g.home) if g.home else None
        path, cfg_file = paths.board_db(g.board, home), paths.config_path(home, g.board)
        why, taken = [], False        # read-only: no DB or no guard_state = no lander ever ran
        if path.exists():       # no non-empty -wal: immutable opens create no -wal/-shm sidecars
            live = Path(f"{path}-wal").is_file() and Path(f"{path}-wal").stat().st_size > 0
            conn = db.connect_ro_tolerant(path) if live else sqlite3.connect(
                f"file:{path}?mode=ro&immutable=1", uri=True)
            try:
                taken = conn.execute("SELECT 1 FROM sqlite_master WHERE name='guard_state'"
                                     ).fetchone() is not None
                why = guard(conn, int(time.time())) if taken else []
            finally:
                conn.close()
        cfg = json.loads(cfg_file.read_text()) if cfg_file.is_file() else {}   # bad JSON: raises
        if not taken and set(cfg.get("lander") or ()) - {"branch"}:   # a configured lander (the
            # landing branch alone is not one) must have a guard_state
            why = ["lander configured but no guard_state"]
        print("\n".join(f"lander guard: STOP, {w} ({path})" for w in why)
              or f"lander guard: clear ({'' if taken else 'board not taken: '}{path})")
        return 1 if why else 0
    ap = argparse.ArgumentParser(description="Dry-run landing checks (never merges or pushes).")
    ap.add_argument("--repo", required=True)
    ap.add_argument("--branch", required=True)
    ap.add_argument("--config", help="JSON with contract_commands and optional lander{}")
    ap.add_argument("--board", help="the board's own config and database, instead of --config")
    ap.add_argument("--home")
    ap.add_argument("--task", help="the card (with --board): in gate mode its blind tests run too")
    ap.add_argument("--blind-only", action="store_true",
                    help="the keeper route: only the blind step decides the exit code")
    ap.add_argument("--commit")
    ap.add_argument("--base")      # default origin/<lander.branch>, origin/main when unset
    ap.add_argument("--no-fetch", action="store_true")
    a = ap.parse_args(argv)
    home = Path(a.home) if a.home else None
    if not (a.config or a.board):
        ap.error("--config or --board is required")
    if a.board and not a.task:
        return _refuse("--task is required with --board: the card whose blind tests are checked")
    cfg = json.loads((paths.config_path(home, a.board) if a.board else Path(a.config)).read_text())
    conn = db.connect(paths.board_db(a.board, home)) if a.board else None
    if conn is None and config.redesign(cfg)["mac_runner"]:    # no board: the tier is unknown
        return _refuse("the Mac lane is on: use --board, so a Mac card is never checked in docker")
    if a.blind_only:      # no suites, gates or deploy check: the keeper runs those itself
        if why := _refusal(a, conn, cfg):
            return _refuse(why)
    result = run(a.repo, a.branch, cfg=cfg, commit=a.commit, base=a.base, fetch=not a.no_fetch,
                 task_id=a.task, conn=conn, skip=BLIND_SKIP if a.blind_only else ())
    print(json.dumps(result, indent=1))
    if not a.blind_only:
        return EXIT[result["verdict"]]
    code, line = _blind_verdict(result)
    print(f"lander --blind-only: {line}", file=sys.stderr)
    return code


def _refuse(why):
    print(f"lander: refused: {why}", file=sys.stderr)
    return 1


def _refusal(a, conn, cfg):
    """Why `--blind-only` must not run for these arguments, in plain words, or None: it needs a
    board and a card of it, a board in gate mode, a commit that is the branch's tip, and a branch
    that holds origin/main (the check tests exactly the commit asked, never a replay of it)."""
    if conn is None:
        return "--blind-only needs --board and --task"
    if conn.execute("SELECT 1 FROM tasks WHERE id=?", (a.task,)).fetchone() is None:
        return f"{a.task} is not a card of board {a.board}"
    if config.redesign(cfg)["blind_tests"] != "gate":
        return f"board {a.board} does not run blind tests in gate mode: there is no blind step"
    tip = _git(a.repo, "rev-parse", "--verify", f"refs/heads/{a.branch}")
    asked = _git(a.repo, "rev-parse", "--verify", f"{a.commit or a.branch}^{{commit}}")
    if tip[0] or asked[0] or tip[1] != asked[1]:
        return f"{a.commit or a.branch} is not the tip of branch {a.branch}"
    base = a.base or f"origin/{target(cfg)}"
    if _fetch(a.repo, not a.no_fetch, target(cfg))[0] != "pass":
        return f"{base} could not be fetched"
    if _git(a.repo, "merge-base", "--is-ancestor", base, tip[1])[0] != 0:
        return f"{a.branch} is behind {base}: rebase it first (this check never tests a replay)"
    return None


def _blind_verdict(result):
    """(exit code, one plain line) of the keeper route. 0 only when a `blind` row says pass, or
    the card truly has no blind tester (the line says which); every other case is non-zero."""
    rows = result["checks"]
    code = _blind_exit(rows)
    first = next((r for r in rows if r["status"] in ("fail", "hold", "wait")), None)
    blind = next((r for r in rows if r["name"] == "blind"), None)
    if code:
        return code, f"not landable: {first['name']} {first['status']}: {first['detail']}"
    if blind is not None:
        return 0, f"the blind step passed: {blind['detail']}"
    if result["commit"] == result["base"]:
        return 1, "the commit is already on origin/main: no blind step ran"
    return 0, "this card gets no blind tester and has no blind tests: there is nothing to run"


def _blind_exit(rows):
    """The keeper route's exit code: 2 when the blind step held the card, else 1 for a failed row
    of any check and 3 for a waiting one; 0 when neither. Other checks' holds (protected paths, a
    suite that needs docker) are the keeper's own business here."""
    if any(r["status"] == "hold" and r["name"].split(":")[0] in BLIND_ROWS for r in rows):
        return 2
    return 1 if any(r["status"] == "fail" for r in rows) else 3 * any(
        r["status"] == "wait" for r in rows)


if __name__ == "__main__":
    sys.exit(main())
