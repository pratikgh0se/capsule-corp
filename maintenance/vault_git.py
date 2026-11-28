"""Vault git helpers: pull before reading a note a human may have edited on another
device, push right after the machinery writes one, so the phone's notes-app git plugin sees
decisions/brainstorms/mirrored docs as soon as the phone message arrives."""
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import events  # noqa: E402
from capsule import host  # noqa: E402

REMOTE = "origin"

# Row 46's GitHub boundary, cheap form (spec 4.4): push already takes an explicit path
# list and stages nothing else, so the boundary is a prefix check over `rels` plus a
# deny list.
# The allow prefixes are host.json `vault.push_allow` (neutral default in capsule/host.py).
PUSH_DENY = (".env", ".key", ".pem", "id_rsa", "Transcripts/", "state/", ".obsidian/")


def usable(vault, runner=None):
    """False when there is no vault to act on (none configured, or not a directory): the
    public entry points then do nothing. A runner (tests, dry runs) skips the directory check."""
    return bool(vault) and (runner is not None or Path(vault).is_dir())


def allowed(rel):
    """True when `rel` may be staged."""
    rel = str(rel or "").strip()
    if not rel or rel.startswith("/") or ".." in Path(rel).parts:
        return False
    # `./Boards/x.md` and `Boards/x.md` are the same path; the prefix check is a string
    # compare, so normalise through Path first (parked minor, spec 4).
    had_trailing_slash = rel.endswith("/") and rel not in ("/", "./")
    rel = Path(rel).as_posix()
    if had_trailing_slash and not rel.endswith("/"):
        rel += "/"
    parts = Path(rel).parts
    name = Path(rel).name
    for d in PUSH_DENY:
        if d.endswith("/"):
            if d.rstrip("/") in parts:
                return False
        elif name.endswith(d):
            return False
    return rel.startswith(tuple(host.get_or_default("vault.push_allow")))   # host.json folders


def _git(vault, args, runner=None, timeout=60):
    cmd = ["git", "-C", str(vault)] + list(args)
    if runner is not None:
        return runner(cmd)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _fail(what, res, vault):
    err = (getattr(res, "stderr", "") or getattr(res, "stdout", "") or "").strip()[:300]
    events.log("job", "vault_git", f"vault {what} failed: {err or 'rc ' + str(getattr(res, 'returncode', '?'))}",
               severity="warn", payload={"job": "vault_git", "event": f"vault_{what}_failed", "vault": str(vault)})
    return False


def no_send():
    return bool(os.environ.get("AGENT_OPS_NO_SEND"))


def pull(vault, runner=None):
    """`git pull --ff-only` so a note ticked on the phone is read, not the
    stale laptop copy. Returns True on success (or no-send)."""
    if (no_send() and runner is None) or not usable(vault, runner):
        return True
    try:
        # no refspec: pull the checked-out branch's configured upstream (origin/main
        # for the vault), never the remote's default-branch HEAD.
        res = _git(vault, ["pull", "-q", "--ff-only"], runner)
    except (OSError, subprocess.SubprocessError) as exc:
        return _fail("pull", type("R", (), {"stderr": str(exc), "returncode": -1})(), vault)
    return True if res.returncode == 0 else _fail("pull", res, vault)


def push(vault, paths, message, runner=None):
    """Stage only `paths` (relative to the vault), commit if anything is
    staged, push. Returns True when the remote has the commit (or nothing to
    commit, or no-send)."""
    if (no_send() and runner is None) or not usable(vault, runner):
        return True
    vault = Path(vault)
    wanted = [str(p) for p in paths if p]
    rels = [r for r in wanted if allowed(r)]
    dropped = [r for r in wanted if r not in rels]
    if dropped:
        events.log("job", "vault_git",
                   f"refusing to push {len(dropped)} path(s) outside the allowlist: "
                   f"{', '.join(dropped[:5])}",
                   severity="warn",
                   payload={"job": "vault_git", "event": "vault_push_denied",
                            "vault": str(vault), "n": len(dropped)})
    if not rels:
        return True
    try:
        res = _git(vault, ["add", "-A", "--"] + rels, runner)
        if res.returncode != 0:
            return _fail("add", res, vault)
        staged = _git(vault, ["diff", "--cached", "--quiet", "--"] + rels, runner)
        if staged.returncode == 0:
            return True  # nothing new for these paths
        res = _git(vault, ["commit", "-q", "-m", message, "--", *rels], runner)
        if res.returncode != 0:
            return _fail("commit", res, vault)
        res = _git(vault, ["push", "-q", REMOTE, "HEAD"], runner, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        return _fail("push", type("R", (), {"stderr": str(exc), "returncode": -1})(), vault)
    return True if res.returncode == 0 else _fail("push", res, vault)


_MACHINE_SUBJECT_RE = re.compile(r'^maint\([a-z-]+\): ')


def machine_commits_touching(repo, path, runner=None):
    """[(sha, subject), ...] for every commit in `repo`'s history that touches `path`
    AND was made by the pipeline itself, i.e. whose subject matches
    `^maint\\([a-z-]+\\): ` (C3/M054, spec 4.5)."""
    repo = Path(repo)
    real_vault = host.vault_path()
    if no_send() and real_vault and repo.resolve() == real_vault.resolve():
        raise RuntimeError(
            "refusing to read the real vault's git history under test: pass a "
            "fixture repo, not $OBSIDIAN_VAULT_PATH")
    res = _git(repo, ["log", "--format=%H%x1f%s", "--name-only", "--", str(path)], runner)
    out = []
    for line in (res.stdout or "").splitlines():
        if "\x1f" not in line:
            continue
        sha, subject = line.split("\x1f", 1)
        if _MACHINE_SUBJECT_RE.match(subject):
            out.append((sha, subject))
    return out


_WEB_CACHE = {}


def web_base(vault, runner=None):
    """https base for a file in the vault's origin repo (GitHub blob URL on the
    checked-out branch), or None when there is no usable remote. Telegram
    auto-links https URLs in plain text; custom-scheme links it does not."""
    if not usable(vault, runner):
        return None
    key = str(vault)
    if key in _WEB_CACHE:
        return _WEB_CACHE[key]
    base = None
    try:
        url = _git(vault, ["remote", "get-url", REMOTE], runner)
        branch = _git(vault, ["rev-parse", "--abbrev-ref", "HEAD"], runner)
        u = (url.stdout or "").strip(); b = (branch.stdout or "").strip() or "main"
        m = re.match(r"^(?:git@github\.com:|https://github\.com/)([^/]+)/([^/]+?)(?:\.git)?$", u)
        if url.returncode == 0 and m:
            base = f"https://github.com/{m.group(1)}/{m.group(2)}/blob/{b}"
    except (OSError, subprocess.SubprocessError):
        base = None
    _WEB_CACHE[key] = base
    return base
