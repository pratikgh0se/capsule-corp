"""`python3 -m capsule image render [--project DIR] [--lock FILE] [-o PATH]` (R10, worker-images
spec slice S10a): render the project layer's Dockerfile from `capsule.toml [image]`. A judge, not
a builder (Z2): it runs no docker and no network, and it refuses anything outside a short list.
Allowed keys: stacks (exactly one of STACKS, with its `capsule-<stack>@sha256:` row on a whole
line of the lock), apt (package names only; versions come from the base's dated snapshot),
lockfiles (python stack: `requirements*.lock|.txt`, a real file, never a symlink, whose every
line is `name==version` plus `--hash=sha256:` pins and nothing else, installed wheels-only with
`pip --require-hashes`; web stack: `package-lock.json` beside a `package.json`, both plain JSON
files, lockfileVersion 3 only, every lock key a nested `node_modules/...` path, every
dependency an exact version and every lock entry an exact version resolved from
registry.npmjs.org (no `..`) with a sha512 integrity hash, installed with `npm ci
--ignore-scripts` by absolute tools on a clean PATH) and
smoke (lines run later in a no-network container, never in the
Dockerfile). Every value is matched whole against a strict pattern before it can reach a line,
so no name or file can carry shell. The smoke denylist is a lint that catches honest mistakes;
the boundary is `--network=none` on the container that runs it. Exit 0 rendered,
1 refused (reasons on stderr, nothing written), 2 unreadable input."""
import json
import re
import shlex
import sys
import tomllib
from pathlib import Path

from capsule import paths

KEYS = ("stacks", "apt", "lockfiles", "smoke")
STACKS = ("python", "web")   # grows with the stacks that exist
LOCK = paths.REPO / "docker" / "IMAGES.lock"
ROW = re.compile(r"capsule-([a-z][a-z0-9-]*)@(sha256:[0-9a-f]{64})")
APT = re.compile(r"[a-z0-9][a-z0-9+.-]*[a-z0-9+]")   # a trailing `-` would remove
PIP_LOCK = re.compile(r"requirements[A-Za-z0-9._-]*\.(lock|txt)")
PIP_REQ = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9._,-]+\])?"      # name[extras]
                     r"==[A-Za-z0-9][A-Za-z0-9._+!-]*"                            # ==version
                     r"([ \t]+--hash=sha256:[0-9a-f]{64})+")                      # 1+ sha256 pins
COMMENT = re.compile(r"(^|[ \t])#.*")
ODD_CHAR = re.compile(r"[^\t\r\n\x20-\x7e]")   # pip's splitlines also breaks at FF, VT, FS, NEL
NOT_PINNED = "is not a pinned, hashed requirement"
NPM_LOCK, NPM_JSON = "package-lock.json", "package.json"
NPM_DEPS = ("dependencies", "devDependencies", "optionalDependencies")
NPM_EXACT = re.compile(r"\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?")      # no range, tag, URL or alias
NPM_TARBALL = re.compile(r"https://registry\.npmjs\.org/[A-Za-z0-9@._/-]+\.tgz")
_NPM_PART = r"node_modules/(@[A-Za-z0-9-][\w.~-]*/)?[A-Za-z0-9-][\w.~-]*"     # never `.`/`..`
NPM_KEY = re.compile(rf"{_NPM_PART}(/{_NPM_PART})*")        # a nested package path, nothing else
NPM_INTEGRITY = re.compile(r"sha512-[A-Za-z0-9+/]{86}==")
BAD_SMOKE = re.compile(r"://|\|\s*(ba|z|da)?sh\b|\b(pip3?|npm|apt|apt-get)\s+(install|i|add|ci)\b")
HEADER = "# Rendered by `capsule image render` from capsule.toml [image]. Do not edit by hand."
# Absolute tools and a clean PATH: a registry package may ship a bin named rm or chmod, and
# node_modules/.bin must never decide what this root-run RUN line executes (full build network).
NPM_RUN = ("RUN cd /opt/project-npm && /usr/bin/env PATH=/usr/local/bin:/usr/bin:/bin"
           " npm ci --ignore-scripts --no-audit --no-fund --cache /tmp/npm-cache \\\n"
           "    && /bin/rm -rf /tmp/npm-cache && /bin/chmod -R a+rX /opt/project-npm")
APT_RUN = ("RUN apt-get -o Acquire::Check-Valid-Until=false update \\\n"
           "    && apt-get install -y --no-install-recommends {names} \\\n"
           "    && apt-get clean \\\n"
           "    && rm -rf /var/lib/apt/lists/*")


def _strs(image, key, problems):
    value = image.get(key, [])
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return value
    problems.append(f"{key}: must be a list of strings")
    return []


def _bad_pip_line(project, name):
    """(line, reason) for the first line of the lock that may not be rendered, or None.
    Continuation lines are joined and comments dropped as pip does; anything else (an option, an
    index, a URL, `@`, `-r`, an unhashed or unpinned name) fails the whole-line match. Only
    printable ASCII, tab and line ends are read at all: pip splits on more separators than we
    would (FF, VT, FS, NEL...), and an option after one could hide inside what we call a comment."""
    text = (project / name).read_text(encoding="utf-8")
    odd = ODD_CHAR.search(text)
    if odd:
        return text.count("\n", 0, odd.start()) + 1, "has a control or non-ASCII character"
    logical, start = [], None
    for number, raw in enumerate(text.splitlines(), 1):
        line = COMMENT.sub("", raw)
        start = start or number
        if line.endswith("\\"):
            logical.append(line[:-1])
            continue
        joined = "".join(logical + [line]).strip(" \t")
        if joined and not PIP_REQ.fullmatch(joined):
            return start, NOT_PINNED
        logical, start = [], None
    return (start, NOT_PINNED) if start else None      # a continuation cut off at the end


def _json_file(project, name):
    """(parsed, None) for a plain JSON file of the project, else (None, why)."""
    path = project / name
    if path.is_symlink() or not path.is_file():
        return None, f"{name} is not a plain file in the project (missing or a symlink)"
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except (OSError, ValueError):
        return None, f"{name} cannot be read as JSON"


def _npm_spec_problems(pkg):
    """package.json dependencies must be exact versions: no range, tag, URL, path or alias."""
    out = []
    for section in NPM_DEPS:
        deps = pkg.get(section, {}) if isinstance(pkg, dict) else {}
        for name, spec in (deps.items() if isinstance(deps, dict) else [(section, None)]):
            if not (isinstance(spec, str) and NPM_EXACT.fullmatch(spec)):
                out.append(f"lockfiles: {NPM_JSON} {section} {name!r} is not an exact version")
    return out


def _npm_entry_ok(entry):
    return (isinstance(entry, dict) and not entry.get("link")
            and NPM_EXACT.fullmatch(str(entry.get("version", "")))
            and NPM_TARBALL.fullmatch(str(entry.get("resolved", "")))
            and ".." not in str(entry.get("resolved", ""))
            and NPM_INTEGRITY.fullmatch(str(entry.get("integrity", ""))))


def _npm_problems(project):
    """[reason] for a package.json and package-lock.json pair that `npm ci` may not be run on."""
    (pkg, why), (lock, why2) = _json_file(project, NPM_JSON), _json_file(project, NPM_LOCK)
    problems = [f"lockfiles: {w}" for w in (why, why2) if w]
    if problems:
        return problems
    problems = _npm_spec_problems(pkg)
    entries = lock.get("packages") if isinstance(lock, dict) else None
    if not isinstance(entries, dict) or lock.get("lockfileVersion") != 3 or "dependencies" in lock:
        return problems + [f"lockfiles: {NPM_LOCK} needs lockfileVersion 3 with packages and "
                           "no legacy dependencies key"]
    problems += [f"lockfiles: {NPM_LOCK} key {key!r} is not a node_modules/ package path"
                 for key in entries if key and not NPM_KEY.fullmatch(key)]
    return problems + [f"lockfiles: {NPM_LOCK} entry {key!r} is not an exact version from "
                       "registry.npmjs.org with a sha512 integrity hash"
                       for key, e in entries.items() if key and not _npm_entry_ok(e)]


def _lock_problems(name, project, stack):
    if name == NPM_LOCK:
        if stack != "web":
            return [f"lockfiles: {name!r} needs the web stack"]
        return _npm_problems(project)
    if name == NPM_JSON:
        return [f"lockfiles: {name!r} is not a lockfile (list {NPM_LOCK}, keep it beside {name})"]
    if not PIP_LOCK.fullmatch(name):
        return [f"lockfiles: {name!r} is not a lockfile (requirements*.lock|.txt)"]
    if stack != "python":
        return [f"lockfiles: {name!r} needs the python stack"]
    path = project / name
    if path.is_symlink() or not path.is_file():
        return [f"lockfiles: {name} is not a plain file in the project (missing or a symlink)"]
    try:
        bad = _bad_pip_line(project, name)
    except (OSError, ValueError):
        return [f"lockfiles: {name} cannot be read as text"]
    return [f"lockfiles: {name} line {bad[0]} {bad[1]}"] if bad else []


def _smoke_problem(line):
    if not line.strip() or not line.isprintable():
        return "is empty or has an unprintable character"
    return "fetches, installs or pipes to a shell" if BAD_SMOKE.search(line) else None


def _validate(image, project, rows):
    """[reason] for everything the manifest may not say; empty means it is safe to render."""
    problems = [f"unknown key {k!r} (allowed: {', '.join(KEYS)})" for k in image if k not in KEYS]
    stacks, apt, locks, smoke = (_strs(image, k, problems) for k in KEYS)
    if len(stacks) != 1:
        problems.append("stacks: exactly one stack (a second needs a multi-stage COPY, not built)")
    problems += [f"stacks: {s!r} is not a built stack ({', '.join(STACKS)}) with a"
                 f" capsule-{s} row in the lock"
                 for s in stacks if s not in STACKS or s not in rows]
    problems += [f"apt: {a!r} is not a plain package name (no versions, urls or shell)"
                 for a in apt if not APT.fullmatch(a)]
    if len(set(locks)) != len(locks):
        problems.append("lockfiles: a file is listed twice")
    for name in dict.fromkeys(locks):
        problems += _lock_problems(name, project, stacks[0] if len(stacks) == 1 else "")
    reasons = [(s, _smoke_problem(s)) for s in smoke]
    return problems + [f"smoke: {s!r} {why}" for s, why in reasons if why]


def _dockerfile(stack, rows, apt, locks):
    out = [HEADER, f"FROM capsule-{stack}@{rows[stack]}", "", "USER root"]
    if apt:
        out.append(APT_RUN.format(names=" ".join(shlex.quote(a) for a in apt)))
    for name in locks:
        if name == NPM_LOCK:
            out += [f"COPY {NPM_JSON} {NPM_LOCK} /opt/project-npm/", NPM_RUN]
            continue
        path = shlex.quote(f"/tmp/locks/{name}")
        out += [f"COPY {name} /tmp/locks/{name}",
                "RUN /opt/venv/bin/pip install -q --no-cache-dir --only-binary=:all:"
                f" --require-hashes -r {path} \\\n    && chmod -R a+rX /opt/venv"]
    return "\n".join([*out, "", "LABEL capsule-layer=project", "USER worker", ""])


def _read(project, lock=None):
    toml = Path(project) / "capsule.toml"
    image = tomllib.loads(toml.read_text(encoding="utf-8")).get("image")
    if not isinstance(image, dict):
        raise ValueError("no [image] table")
    lines = (ln.strip() for ln in Path(lock or LOCK).read_text().splitlines())
    rows = dict(m.groups() for m in map(ROW.fullmatch, lines) if m)
    return image, rows


def render(image, project, rows):
    """(Dockerfile text or None, [reason]): the one judge `image render` and `image build` share."""
    problems = _validate(image, Path(project), rows)
    if problems:
        return None, problems
    return _dockerfile(image["stacks"][0], rows, image.get("apt", []),
                       image.get("lockfiles", [])), []


def main(args):
    try:
        image, rows = _read(args.project, args.lock)
    except (OSError, ValueError) as e:     # TOMLDecodeError is a ValueError
        print(f"capsule image: cannot read the manifest or the lock: {e}", file=sys.stderr)
        return 2
    text, problems = render(image, args.project, rows)
    if problems:
        print("\n".join(f"capsule image: refused: {p}" for p in problems), file=sys.stderr)
        return 1
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0
