#!/usr/bin/env python3
"""Host-side approval stamps and the offline clone runner (what is left of the merge gate).

The per-card old-system pipeline (push, PR, approval, merge) and its terminal approval verb were
retired 2026-11-12 (D238): approval of an epic is the owner's GitHub merge of its PR. What stays
is the spec and release stamp readers/writers behind `bin/approve-spec.sh` and
`bin/approve-release.sh` (under `<MERGE_GATE_STATE>/approvals/`), the state-dir resolver, and the
helpers quality_gate, quality_nightly and release_gate import (`run_in_clone`, `read`,
`test_cmd_for`, `InfraError`). stdlib only.
"""
import datetime as _dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import deps  # noqa
import events  # noqa

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")

# A release stamp is keyed by a TAG, and SLUG_RE does not accept `v0.1.0`. Semver with a leading
# `v` and no build metadata: every accepted character should have a reason.
TAG_RE = re.compile(r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(-[0-9A-Za-z.-]+)?$")


# Deliberately outside every tree a worker container mounts (the four repos and the vault).
# The deployed config dir's maintenance entry is a symlink to the capsule-corp repo, which IS
# mounted, same uid: a worker could have forged an approval stamp there.
DEFAULT_STATE = Path.home() / ".local" / "state" / "capsule-corp" / "merge-gate"


class InfraError(Exception):
    """A tool the gate depends on failed (git, docker). Never a
    verdict about the card: the gate posts no comment, logs an error event and leaves the card
    alone so the next tick retries."""


def state_dir(create=False):
    """Host state root. `MERGE_GATE_STATE` lets tests point at a temp dir. Under
    `AGENT_OPS_NO_SEND` (the suite's marker) with no override, falling through to the real
    `~/.local/state/capsule-corp/merge-gate` is always a bug, as board_view._refuse_
    default_vault_under_test guards: approval stamps, `board.lock_path` and the gate worktrees
    all resolve through this function."""
    override = os.environ.get("MERGE_GATE_STATE")
    if not override and os.environ.get("AGENT_OPS_NO_SEND"):
        raise RuntimeError("refusing to touch the default state dir under test: "
                            "set MERGE_GATE_STATE explicitly")
    p = Path(override or DEFAULT_STATE)
    if create:
        p.mkdir(parents=True, exist_ok=True, mode=0o700)   # no worker may read or write it
    return p


def spec_approval_path(slug):
    """`<state>/approvals/spec/<slug>.json`: the spec-approval stamp `bin/approve-spec.sh` and the
    Intake `approve spec:` line both write. `~/.local/state` is outside every
    tree a worker container mounts, so a worker cannot forge a stamp."""
    if not SLUG_RE.fullmatch(str(slug or "")):
        raise ValueError(f"bad spec slug {slug!r}: must match {SLUG_RE.pattern}")
    return state_dir() / "approvals" / "spec" / f"{slug}.json"


def read_spec_approval(slug):
    """`{"ts": float, "via": str}` when a spec stamp exists, else None. Presence is the whole
    verdict: there is no later event for the stamp to be newer than, which makes writing it
    twice a no-op."""
    try:
        data = json.loads(spec_approval_path(slug).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        ts = float(data.get("ts") or 0)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    return {"ts": ts, "via": str(data.get("via") or "host")}


def write_spec_approval(slug, *, via="host", now=None):
    """Write the stamp atomically (temp file + `os.replace`) and
    return its path. Overwriting is deliberate: a second approval is the same verdict."""
    path = spec_approval_path(slug)
    state_dir(create=True)
    # 0700 on the stamp dir and 0600 on the stamp (bin/approve-release.sh does the same dance in
    # shell): a stamp is an approval, and nothing but the host reads or writes one.
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    for d in (path.parent.parent, path.parent):     # `mode=` only sets the leaf
        os.chmod(d, 0o700)
    ts = float(now if now is not None else _dt.datetime.now().timestamp())
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{slug}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"ts": ts, "text": "approve", "via": str(via)}, fh)
            fh.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def release_approval_path(repo, tag):
    """`<state>/approvals/release/<repo>-<tag>.json`: the third stamp, written by
    `bin/approve-release.sh`, under the same 0700 root as the spec stamp.

    The joined name is NOT uniquely parseable back into repo and tag (a repo slug may contain
    `-` and a tag begins with `v`) and never needs to be: callers pass `repo` and `tag`
    separately and this path is only built, never split. Nobody should write a splitter."""
    if not SLUG_RE.fullmatch(str(repo or "")):
        raise ValueError(f"bad repo slug {repo!r}: must match {SLUG_RE.pattern}")
    if not TAG_RE.fullmatch(str(tag or "")):
        raise ValueError(f"bad tag {tag!r}: must match {TAG_RE.pattern}")
    return state_dir() / "approvals" / "release" / f"{repo}-{tag}.json"


def read_release_approval(repo, tag):
    """`{"ts": float, "via": str}` when a release stamp exists, else None. Presence is the whole
    verdict, as for a spec approval: no `gate:` comment exists for it to be newer than, so
    writing it twice is a no-op and an old stamp stays valid. A tag is immutable, so there is no
    `head` to bind."""
    try:
        data = json.loads(release_approval_path(repo, tag).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        ts = float(data.get("ts") or 0)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    return {"ts": ts, "via": str(data.get("via") or "host")}


def write_release_approval(repo, tag, *, via="host", now=None):
    """Write the third stamp atomically (the mktemp + replace dance bin/approve-release.sh uses) and
    return its path. Overwriting is deliberate: a second approval is the same verdict."""
    path = release_approval_path(repo, tag)
    state_dir(create=True)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    for d in (path.parent.parent, path.parent):     # `mode=` only sets the leaf
        os.chmod(d, 0o700)
    ts = float(now if now is not None else _dt.datetime.now().timestamp())
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{repo}-{tag}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"ts": ts, "text": "approve", "via": str(via)}, fh)
            fh.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def _default_sh(argv, cwd=None, timeout=None, input=None, env=None):
    kw = dict(cwd=cwd, capture_output=True, text=True, timeout=timeout, input=input, env=env)
    try:
        p = events.docker_run(argv[1:], **kw) if argv[0] == "docker" else subprocess.run(argv, **kw)
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    except OSError as exc:
        return 127, str(exc)
    return p.returncode, (p.stdout or "") + (p.stderr or "")



def read(runner, argv, cwd=None, *, required=True, what=None, timeout=None):
    """A read-only tool call. Not audited (only writes are). Raises InfraError when the tool
    fails or returns nothing where a value is required; the caller turns that into an `error:`
    result, not a verdict. A runner that raises `subprocess.TimeoutExpired` is treated like a
    nonzero exit: an InfraError, never a raw traceback out of a verdict path."""
    label = what or " ".join(argv[:3])
    try:
        rc, out = (runner or _default_sh)(argv, cwd=cwd, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise InfraError(f"{label}: timed out after {timeout}s")
    if rc != 0:
        raise InfraError(f"{label}: rc {rc}: {str(out or '').strip()[:120]}")
    if required and not str(out or "").strip():
        raise InfraError(f"{label}: empty output")
    return str(out or "").strip()


def test_cmd_for(repo, env):
    return env.get("OPS_TEST_CMD_" + repo.replace("-", "_")) or "python3 -m unittest discover tests"


def run_in_clone(repo_root, ref, cmd, *, sh=None, image=None, timeout=1200, keep=None):
    """Run `cmd` in the worker image (network none) on a standalone clone of `ref` mounted at
    /wt (never its own host path: Docker Desktop 4.43.1 brings that up empty; a linked
    worktree does not work for the same reason). Returns
    (rc, output); rc 124 = timeout. Raises InfraError when docker itself failed (rc 125-127).
    The clone is deleted unless `keep` (a list) is given: then its path is appended and the
    caller owns the cleanup.

    The same `-v <cache>:/deps:ro` + offline pip/npm `-e` flags every sandboxed profile's
    docker_env carries are appended, so this offline re-run can install what the card's own
    container run could; `--network none` is never relaxed."""
    image = image or _bridge().bridge_image()
    runner = sh or _default_sh
    repo_root = Path(repo_root)
    base = state_dir(create=True) / "worktrees"
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    wt = Path(tempfile.mkdtemp(prefix="gate-worktree-", dir=str(base)))
    try:
        rc, sha = _default_sh(["git", "rev-parse", "--verify", f"{ref}^{{commit}}"], cwd=str(repo_root))
        sha = sha.strip()
        if rc != 0 or not sha:
            return 1, f"unknown ref {ref}: {sha[:200]}"
        warn = ""
        for argv in (["git", "clone", "-q", "--no-checkout", str(repo_root), str(wt)],
                     ["git", "-C", str(wt), "fetch", "-q", str(repo_root), sha],
                     ["git", "-C", str(wt), "checkout", "-q", "--detach", sha],
                     ["git", "-C", str(wt), "fetch", "-q", str(repo_root), "+refs/remotes/origin/*:refs/remotes/origin/*"]):
            rc, out = _default_sh(argv, cwd=str(repo_root))
            if rc != 0 and "refs/remotes" not in " ".join(argv):
                return 1, f"checkout of {ref} failed: {out.strip()[:200]}"
            if rc != 0:
                # The clone keeps repo_root's own refs/remotes/origin/*, so
                # diff-cover's --compare-branch may resolve to a stale base.
                warn = "warning: compare-branch may be stale\n"
        rc, out = runner(["docker", "run", "--rm", "--network", "none",
                          "--label", "capsule=1", "--label", "capsule.kind=run",
                          "-e", "GIT_CONFIG_COUNT=1", "-e", "GIT_CONFIG_KEY_0=safe.directory", "-e", "GIT_CONFIG_VALUE_0=*",
                          "-v", f"{wt}:/wt", "-w", "/wt"] + deps.docker_flags() +
                         [image, "sh", "-c", cmd], cwd=str(repo_root), timeout=timeout)
        if rc in (125, 126, 127):
            raise InfraError(f"docker: rc {rc}: {str(out or '').strip()[:120]}")
        return rc, warn + (out or "")
    finally:
        if keep is not None:
            keep.append(wt)
        elif wt.exists():
            shutil.rmtree(wt, ignore_errors=True)


def _bridge():
    import board_bridge as bb  # lazy: board_bridge imports this module inside main()
    return bb


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="merge gate: spec and release approval stamps")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("approve-spec", help="write the spec-approval stamp for one slug")
    s.add_argument("slug")
    s.add_argument("--specs-dir", default=None)
    r = sub.add_parser("approve-release", help="write the release-approval stamp for one repo and tag")
    r.add_argument("repo"); r.add_argument("tag")
    a = ap.parse_args(argv)
    if a.cmd == "approve-spec":
        # The one thing checkable host-side: the spec note exists; a slug with no note is the only
        # "not found" left. The vault is the configured one (vault_conf: $OBSIDIAN_VAULT_PATH, else
        # host.json vault.path); with none configured there is nowhere to look, so it says so.
        # bin/approve-spec.sh never passes --specs-dir, so production goes through this default.
        if a.specs_dir:
            specs = Path(a.specs_dir)
        else:
            import vault_conf
            vault = vault_conf.vault()
            if vault is None:
                print(vault_conf.NO_VAULT, file=sys.stderr)
                return 1
            specs = vault / "Specs"
        if not (specs / f"{a.slug}.md").exists():
            print(f"no spec note: {a.slug}", file=sys.stderr)
            return 1
        try:
            write_spec_approval(a.slug, via="terminal")
        except ValueError as exc:
            print(f"no spec note: {a.slug} ({exc})", file=sys.stderr)
            return 1
        events.log("card", "merge_gate", f"approved spec {a.slug}", slug=a.slug,
                   payload={"event": "approved", "state": "closed"})
        print(f"approved: {a.slug}")
        return 0
    if a.cmd == "approve-release":
        # No PR lookup: a release is bound to a tag, which is
        # immutable. release_gate.py's `tag` check refuses a tag that is not on origin and runs
        # after the stamp by design, so a stamp written a minute early is not a wasted approval.
        try:
            path = write_release_approval(a.repo, a.tag, via="terminal")
        except ValueError as exc:
            print(f"bad release stamp: {exc}", file=sys.stderr)
            return 1
        events.log("card", "merge_gate", f"approved release {a.repo} {a.tag}",
                   board=a.repo, payload={"event": "release_approved", "state": None})
        print(f"approved release: {a.repo} {a.tag} -> {path}")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
