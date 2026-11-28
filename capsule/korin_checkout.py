"""Korin runs in `korin.main_checkout`, so that checkout must be the board's own repo (spec
2026-10-29-app-incidents-design.md:191-192). `refuse` is the one check `Tick._korin_dispatch` calls
first. It compares two sets of `workspace.repo_url_sha256` hashes: the checkout's own path and its
origin, against `repo_url` and (a local-path `repo_url` only) that repo's origin. With no shared
member no Korin run starts, and the outbox says so once per pair of hash lists. A notice never
names a URL."""
import subprocess
from pathlib import Path

from capsule import outbox, workspace
from capsule.db import guard_get, guard_set, txn

GUARD = "korin_checkout:gap"


def _git(path, *args):
    try:
        res = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True,
                             timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ""
    return res.stdout.strip() if res.returncode == 0 else ""


def _hashes(path, *, own):
    """Hashes for a local repo path: its own resolved path (when `own`) and its origin. A path that
    is not the top of a git repo gives none (a plain folder inside another repo is not a repo)."""
    real = Path(path).expanduser().resolve()
    top = _git(real, "rev-parse", "--show-toplevel")
    if not top or Path(top).resolve() != real:
        return set()
    origin = _git(real, "config", "--get", "remote.origin.url")
    found = {workspace.repo_url_sha256(origin)} if origin else set()
    return found | {workspace.repo_url_sha256(str(real))} if own else found


def refuse(conn, cfg, ks, now):
    """True when the Korin dispatch must not run: the sets of hashes for `ks['main_checkout']` and
    for the board's `repo_url` share no member. Pages once per pair of sorted hash lists; a match
    clears the mark. The notice and the mark commit together, so a failed notice sends no mark."""
    have = _hashes(ks["main_checkout"], own=True)
    want = {workspace.repo_url_sha256(cfg["repo_url"])}
    if Path(str(cfg["repo_url"])).expanduser().is_dir():
        want |= _hashes(cfg["repo_url"], own=False)
    mark, version = guard_get(conn, GUARD)
    if have & want:
        if mark is not None:
            guard_set(conn, GUARD, None, expect_version=version, now=now)
        return False
    pair = {"checkout": sorted(have), "repo_url": sorted(want)}
    if mark != pair:
        path = str(ks["main_checkout"])
        body = (f"capsule: Korin runs are refused. {path} is not a git repo, or its origin does"
                " not match this board's repo_url. Set korin.main_checkout"
                " to this board's repo checkout.")
        with txn(conn):
            outbox.notify(conn, "incident", "Korin run refused: checkout is not the board repo",
                          fields=[("checkout", path), ("config key", "korin.main_checkout")],
                          page=True, now=now, file_body=body)
            guard_set(conn, GUARD, pair, expect_version=version, now=now)
    return True
