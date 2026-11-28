"""deps.py - the vendored dependency cache: allowlist parser and the offline container
env (A5, decision row 217; design doc: docs/superpowers/specs/2026-09-13-dep-cache-
design.md)."""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path


class AllowlistReadError(OSError):
    """A deps-allowlist.txt that exists but cannot be read; the message names the file."""


class AllowlistError(ValueError):
    """A malformed allowlist line. Carries the 1-based line number so the
    caller (deps_sync.py through `plan`, or a direct parse_allowlist
    call) can report `<file>:<n>` without re-deriving it."""

    def __init__(self, line_no, line, reason):
        self.line_no = line_no
        self.line = line
        self.reason = reason
        super().__init__(f"line {line_no}: {reason}: {line!r}")


# pip:<name>==<version> / npm:<name>@<version> / npm:@scope/name@<version> - name and
# version are each restricted to [A-Za-z0-9._-] (a "anything
# but whitespace/separator" charset lets a leading `-` through, e.g. `pip:-rfoo==1.0`,
# which `pip download` parses as `-r <path>` - option injection, not a resolver
# grammar).
_IDENT = r"[A-Za-z0-9._-]+"
_PIP_RE = re.compile(rf"^pip:({_IDENT})==({_IDENT})$")
_NPM_SCOPED_RE = re.compile(rf"^npm:(@{_IDENT}/{_IDENT})@({_IDENT})$")
_NPM_PLAIN_RE = re.compile(rf"^npm:({_IDENT})@({_IDENT})$")
_RANGE_OPERATORS = (">=", "<=", "~=", "^", ">", "<", "!=")
_KNOWN_PREFIXES = ("pip:", "npm:")


def _dash_leading(name):
    """True if `name` (or, for a scoped npm name, the part after the `/`) starts with
    `-`: `-g`, `-rfoo`, `--config-settings` all
    read as an option flag to pip/npm, not a package name, once they reach `pip
    download`/`npm cache add`'s argv."""
    tail = name.split("/", 1)[-1] if name.startswith("@") else name
    return tail.startswith("-")


def parse_allowlist(text):
    """Parse one allowlist file's text into an ordered, deduplicated list of
    `"pip:<name>==<version>"` / `"npm:<name>@<version>"` /
    `"npm:@scope/name@<version>"` strings."""
    out = []
    seen = set()
    for i, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip("\n")
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not stripped.startswith(_KNOWN_PREFIXES):
            raise AllowlistError(i, stripped, "unknown manager prefix (want pip: or npm:)")
        if stripped.startswith(("pip:-e", "npm:-e")) or " -e " in stripped or stripped.endswith("-e"):
            raise AllowlistError(i, stripped, "-e (editable/URL) lines are refused")
        if "://" in stripped:
            raise AllowlistError(i, stripped, "URLs are refused")
        if "[" in stripped or "]" in stripped:
            # An extra passes every other check but is silently
            # unusable under `--no-deps` - the extra's own dependency is never fetched,
            # so the install fails later on a package the allowlist appeared to cover.
            raise AllowlistError(i, stripped,
                                 "extras (name[extra]) are refused - --no-deps never fetches the "
                                 "extra's own dependency; pin it as its own allowlist line")
        for op in _RANGE_OPERATORS:
            body = stripped.split(":", 1)[1] if ":" in stripped else stripped
            if op in body:
                raise AllowlistError(i, stripped, f"range operator {op!r} is refused - pin an exact version")
        if stripped.startswith("pip:"):
            m = _PIP_RE.match(stripped)
        else:
            m = _NPM_SCOPED_RE.match(stripped) or _NPM_PLAIN_RE.match(stripped)
        if not m:
            raise AllowlistError(i, stripped,
                                 "not a bare pip:<name>==<version>, npm:<name>@<version>, "
                                 "or npm:@scope/name@<version> line")
        name, version = m.group(1), m.group(2)
        if _dash_leading(name) or _dash_leading(version):
            raise AllowlistError(i, stripped,
                                 "name/version starts with '-' - would reach pip/npm as an option flag")
        if stripped not in seen:
            seen.add(stripped)
            out.append(stripped)
    return out


def _iter_repo_allowlists(repos_json, strict=False):
    """Yield `(repo_name, allowlist_path, text)` for every repo in `repos_json`
    (maintenance/repos.json's own shape) that has a `deps-allowlist.txt` at its root
    - the raw text, unparsed, so a caller that needs the repo name alongside a parse
    failure (the CLI's `plan`, below) can report it. An unreadable registry yields nothing,
    unless `strict`: then its OSError/ValueError reaches the caller."""
    if isinstance(repos_json, list):      # the host.json registry (host_registry.registry())
        repos = repos_json
    else:
        try:
            with open(repos_json, encoding="utf-8") as f:
                repos = json.load(f)
        except (OSError, ValueError):
            if strict:
                raise
            return
    for r in repos:
        name = r.get("name")
        raw_path = r.get("path") or ""
        path = Path(raw_path).expanduser()
        allowlist_path = path / "deps-allowlist.txt"
        if not allowlist_path.is_file():
            continue
        try:
            text = allowlist_path.read_text(encoding="utf-8")
        except (OSError, ValueError) as exc:  # not the registry: say which file
            raise AllowlistReadError(f"cannot read allowlist {allowlist_path}: {exc}") from exc
        yield name, allowlist_path, text


def allowlists_for_repos(repos_json):
    """Yield `(repo_name, path, entries)` for every repo in `repos_json` that has a
    `deps-allowlist.txt` at its root - only a PRESENT, malformed allowlist raises
    (via parse_allowlist, propagated to the caller)."""
    for name, allowlist_path, text in _iter_repo_allowlists(repos_json):
        yield name, allowlist_path.parent, parse_allowlist(text)


def cache_root():
    """The host dependency cache root: `~/.local/share/capsule-corp/deps`,
    overridable by `AGENT_OPS_DEPS_CACHE` (tests; a non-default machine
    layout)."""
    override = os.environ.get("AGENT_OPS_DEPS_CACHE")
    return Path(override) if override else Path.home() / ".local" / "share" / "capsule-corp" / "deps"


# The offline env every sandboxed profile's `docker_env:` block carries and
# `merge_gate.run_in_clone` passes as `-e` flags (spec section 2d) - exactly these seven
# keys, exactly these values.
CONTAINER_ENV = {
    "PIP_NO_INDEX": "1",
    "PIP_FIND_LINKS": "/deps/pip",
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    "npm_config_cache": "/deps/npm",
    "npm_config_offline": "true",
    "npm_config_audit": "false",
    "npm_config_fund": "false",
}


def docker_flags():
    """The `-v <cache>:/deps:ro` + one `-e KEY=VAL` per CONTAINER_ENV key
    argv fragment, for `merge_gate.run_in_clone`'s `docker run` (spec section
    2c: "the same set as `-e` flags")."""
    flags = ["-v", f"{cache_root()}:/deps:ro"]
    for k, v in CONTAINER_ENV.items():
        flags += ["-e", f"{k}={v}"]
    return flags


# ------------------------------------------------------------------ plan: `plan()` is the ONE
# place deps_sync.py calls into this module (and `python3 -m deps plan <repos.json>` prints the
# same plan as JSON), so there is exactly one allowlist parser in the whole build - the sync
# never re-implements the line grammar.
def plan(repos_json):
    """[{repo, path, entries}] for every repo with an allowlist. A malformed line prints
    `BAD <repo>/deps-allowlist.txt:<n>: <line>` on stderr and raises SystemExit(2) before
    anything is returned. A missing or unreadable registry raises OSError/ValueError."""
    out = []
    for name, allowlist_path, text in _iter_repo_allowlists(repos_json, strict=True):
        try:
            entries = parse_allowlist(text)
        except AllowlistError as exc:
            print(f"BAD {name}/deps-allowlist.txt:{exc.line_no}: {exc.line}", file=sys.stderr)
            raise SystemExit(2)
        out.append({"repo": name, "path": str(allowlist_path.parent), "entries": entries})
    return out


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 2 or argv[0] != "plan":
        print("usage: python3 -m deps plan <repos.json>", file=sys.stderr)
        return 2
    try:
        repos = plan(argv[1])
    except SystemExit as exc:
        return exc.code
    except AllowlistReadError as exc:
        print(f"deps: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f"deps: cannot read repos registry {argv[1]}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(repos))
    return 0


if __name__ == "__main__":
    sys.exit(main())
