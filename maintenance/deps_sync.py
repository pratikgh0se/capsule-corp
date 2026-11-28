"""deps_sync.py - host-side dependency sync (A5 task 3, decision row 217; design doc:
docs/superpowers/specs/2026-09-13-dep-cache-design.md section 3). Ported from deps_sync.sh
(D97, shell-to-Python pilot: docs/shell-to-python.md); maintenance/deps_sync.sh is now a
thin starter that execs this file, so every caller and launchd entry keeps the old path.

Populates the vendored offline cache the sandboxed profiles' offline pip/npm env points at,
from every repo's deps-allowlist.txt (found through maintenance/repos.json). This host
process is the ONLY thing in the whole build that reaches a package registry - the worker
sandbox stays offline, always (its pip/npm env is PIP_NO_INDEX=1 / npm_config_offline).

Usage: deps_sync.sh [--check] [--force]
  (no args)  parse every allowlist, then for each entry not already synced fetch it (one
             command per package: pip download for a pip: line, npm cache add for an npm:
             line), printing one line per package (ok / GOT / FAIL) and a tail count. A
             malformed allowlist line refuses the WHOLE run - exit 2, names
             <repo>/deps-allowlist.txt:<n> - before anything is fetched.
  --check    parse every allowlist and report what has not been synced yet. Fetches
             nothing. Exit 1 if anything is missing, 0 if the cache already covers every
             entry. (Like the shell it replaces, it still creates the cache dirs and the
             empty manifest if they are absent.)
  --force    re-fetch every allowlisted package regardless of the manifest or the artifact
             check - the manual escape hatch for a cache whose evidence itself is
             suspect.

Parsing is delegated to deps.py (same directory) so there is exactly one allowlist parser in
the whole build - this module never re-implements the line grammar.

Idempotency is a manifest PLUS an artifact check: what filename pip or npm write for a given
package (wheel tags, sdist extension, npm's cacache layout) is not a reliable "do we already
have this" test on its own, so every spec this script has successfully synced is ALSO
recorded, one per line, in <cache>/synced.txt - the exact "pip:<name>==<ver>" /
"npm:<name>@<ver>" text the allowlist itself uses. But the manifest can outlive the files
(disk pressure, a manual `rm`, a restored-from-backup cache dir without the wheels), so `ok`
and a clean `--check` only trust the manifest line when the artifact is ALSO still there: a
pip entry needs some file in <cache>/pip whose name starts with the package name (loose: pip's
own wheel/sdist naming normalizes case and `_`/`-` differently across packaging tools, so this
is a name match, not a byte-exact one); an npm entry needs <cache>/npm to be non-empty - no
portable per-package npm cache query survives across npm's own CLI versions, so this is
deliberately coarser than the pip check and only catches "the whole cache got wiped", not
"this one package is gone" - `--force` is the fallback for anything finer-grained. Never
deletes: dropping a package from an allowlist leaves its cache entry and its manifest line
alone (pruning is a separate, non-goal decision - design doc section 3/7).
"""
from __future__ import annotations

import contextlib
import io
import os
import pathlib
import subprocess
import sys

MAINT_DIR = os.path.dirname(os.path.abspath(__file__))  # logical path, as the .sh wrapper's pwd
sys.path.insert(0, MAINT_DIR)
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # repo root, real path

import deps  # noqa: E402  (the one allowlist parser)
import events  # noqa: E402
from capsule import host_registry  # noqa: E402

NAME = "deps_sync.sh"  # the user-facing name: messages and the event source keep the old spelling


# --help text, a plain string: python -OO strips docstrings, so nothing here parses __doc__.
USAGE = """Usage: deps_sync.sh [--check] [--force]
  (no args)  parse every allowlist, then for each entry not already synced fetch it (one
             command per package: pip download for a pip: line, npm cache add for an npm:
             line), printing one line per package (ok / GOT / FAIL) and a tail count. A
             malformed allowlist line refuses the WHOLE run - exit 2, names
             <repo>/deps-allowlist.txt:<n> - before anything is fetched.
  --check    parse every allowlist and report what has not been synced yet. Fetches
             nothing. Exit 1 if anything is missing, 0 if the cache already covers every
             entry. (Like the shell it replaces, it still creates the cache dirs and the
             empty manifest if they are absent.)
  --force    re-fetch every allowlisted package regardless of the manifest or the artifact
             check - the manual escape hatch for a cache whose evidence itself is
             suspect."""


def _split_spec(spec):
    """(name, version) of one allowlist spec; ("", "") for an unknown manager prefix."""
    if spec.startswith("pip:"):
        name, _, ver = spec[len("pip:"):].partition("==")
        return name, ver
    if spec.startswith("npm:"):
        # rsplit on the LAST '@': a scoped name (`@types/node@20.1.0`) has its own leading '@'.
        name, _, ver = spec[len("npm:"):].rpartition("@")
        return name, ver
    return "", ""


def _artifact_exists(cache, spec, name):
    """The manifest line alone is not proof the artifact is still there (module docstring)."""
    kind = spec.split(":", 1)[0]
    try:
        names = os.listdir(os.path.join(cache, kind)) if kind in ("pip", "npm") else []
    except OSError:
        return False
    if kind == "pip":
        return any(f.startswith(name) or f.startswith(name.replace("_", "-")) for f in names)
    return kind == "npm" and bool(names)


def in_manifest(manifest, spec):
    try:
        with open(manifest, encoding="utf-8", errors="replace") as f:
            return spec in f.read().split("\n")
    except OSError:
        return False


def fetch(cache, spec, name, ver):
    """Run the one fetch command for `spec` (pip/npm come from PATH); True on exit 0."""
    if spec.startswith("pip:"):
        cmd, env = ["pip", "download", "--no-deps", "--dest", os.path.join(cache, "pip"),
                    f"{name}=={ver}"], os.environ
    else:
        cmd, env = ["npm", "cache", "add", f"{name}@{ver}"], {
            **os.environ, "npm_config_cache": os.path.join(cache, "npm")}
    try:
        return subprocess.run(cmd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode == 0
    except OSError:  # pip/npm not on PATH: the shell's "command not found" (127) is a FAIL too
        return False


def _log_sync_event(repo, pkg):
    """One dep_cache "sync" row per package this run added to the cache. Never fails the job
    over trail plumbing (events.log's own contract), and stays quiet like the old
    `>/dev/null 2>&1 || true`. True if a row was written."""
    try:
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            events.log("dep_cache", NAME, f"deps-sync: got {pkg}", board=repo,
                       payload={"event": "sync", "repo": repo, "pkg": pkg})
    except Exception:
        return False
    return True


def _plan_specs(repos_json):
    """[(repo, spec)], the first-owning repo winning a dupe. Raises SystemExit(2) naming
    `BAD <repo>/deps-allowlist.txt:<n>: <line>` on stderr for a malformed allowlist."""
    out, seen = [], set()
    for r in deps.plan(repos_json):
        for e in r["entries"]:
            if e not in seen:
                seen.add(e)
                out.append((r["repo"], e))
    return out


def _record(manifest, spec):
    """Append `spec` to the manifest unless a line already says it. A manifest that cannot be
    written is one stderr line, not a stop: the package is still fetched (the shell's echo >>
    failed the same way and the loop went on)."""
    if in_manifest(manifest, spec):
        return
    try:
        with open(manifest, "a+b") as f:
            f.seek(0)
            body = f.read()
            lead = b"\n" if body and not body.endswith(b"\n") else b""
            f.write(lead + spec.encode("utf-8") + b"\n")
    except OSError as exc:
        print(f"{NAME}: {exc}", file=sys.stderr)


def _parse_flags(argv):
    """(check, force, rc): rc is None to carry on, or the exit code when the run is over already
    (-h, a bad argument). Arguments are judged in order, so `-h --bogus` answers -h and
    `--check --bogus` refuses."""
    check = force = False
    for a in argv:
        if a == "--check":
            check = True
        elif a == "--force":
            force = True
        elif a in ("-h", "--help"):
            print(USAGE)
            return check, force, 0
        else:
            print(f"{NAME}: unknown argument {a}", file=sys.stderr)
            return check, force, 2
    return check, force, None


def _sync_all(specs, cache, check, force):
    """The per-package loop: one ok / GOT / FAIL (or MISSING under --check) line each, then the
    tail count. Returns the exit code."""
    manifest = os.path.join(cache, "synced.txt")
    ok = got = fail = missing = 0
    for repo, spec in specs:
        name, ver = _split_spec(spec)
        if not force and in_manifest(manifest, spec) and _artifact_exists(cache, spec, name):
            if not check:
                print(f"ok {spec}")
                ok += 1
            continue
        if check:
            print(f"MISSING {spec}")
            missing += 1
        elif not spec.startswith(("pip:", "npm:")):
            # deps.py already validated every entry - unreachable, but fail loud, never skip.
            print(f"FAIL {spec} (unrecognized manager prefix)")
            fail += 1
        elif fetch(cache, spec, name, ver):
            _record(manifest, spec)
            print(f"GOT {spec}")
            got += 1
            _log_sync_event(repo, spec)
        else:
            print(f"FAIL {spec}")
            fail += 1
    if check:
        print(f"deps-sync --check: {missing} missing")
        return 0 if missing == 0 else 1
    print(f"deps-sync: {ok} ok, {got} got, {fail} failed")
    return 0 if fail == 0 else 1


def main(argv=None):
    check, force, rc = _parse_flags(list(sys.argv[1:] if argv is None else argv))
    if rc is not None:
        return rc
    repos_json = os.environ.get("AGENT_OPS_REPOS_JSON") or os.path.join(MAINT_DIR, "repos.json")
    if not os.environ.get("AGENT_OPS_REPOS_JSON") and not os.path.exists(repos_json):
        repos_json = host_registry.registry()      # no local registry file: host.json `repos`
    cache = str(deps.cache_root())
    try:
        specs = _plan_specs(repos_json)
    except SystemExit:
        return 2
    except deps.AllowlistReadError as exc:  # names the allowlist file itself
        print(f"{NAME}: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:  # a registry that cannot be read is not "nothing to sync"
        print(f"{NAME}: cannot read repos registry {repos_json}: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # an unreadable allowlist: passed through, nothing fetched
        print(f"{NAME}: {exc}", file=sys.stderr)
        return 2
    try:
        os.makedirs(os.path.join(cache, "pip"), exist_ok=True)
        os.makedirs(os.path.join(cache, "npm"), exist_ok=True)
        pathlib.Path(cache, "synced.txt").touch()  # like `touch`: a read-only manifest is fine
    except OSError as exc:  # the shell kept going past a failed mkdir/touch; so do we
        print(f"{NAME}: {exc}", file=sys.stderr)
    return _sync_all(specs, cache, check, force)


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)  # one progress line per package, as the shell did
    sys.exit(main())
