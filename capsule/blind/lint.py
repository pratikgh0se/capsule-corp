"""Blind tests as the tester left them (blind spec 3.4 step 1; R31): `collect` reads the tester's
clone from the host without following links (every file is opened with O_NOFOLLOW and read from
that descriptor), `problems` lints what it found and `stage` writes the
lint-approved files into the real blind repo as one commit on `card/<id>` on top of `main`.

The tester's clone is host-side but untrusted: its container wrote every file in it, so nothing
here executes it, follows a link out of it or trusts its `.git` pointer. The commit is built from
the collected bytes (git plumbing and a private index), so it holds `cards/<id>/` exactly as
linted and nothing else, whatever else the clone held."""
import ast
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

from capsule.blind import preflight

MAX_FILES = 50
MAX_BYTES = 200 * 1024
HARNESS_DIR = "harness"                 # the harness copy in the tester's clone; never landed
SKIP_DIRS = ("__pycache__", ".worker-exec")
ALLOWED_ROOTS = frozenset(sys.stdlib_module_names) | {"harness"}
COMPILE_TIMEOUT = 60
DYNAMIC_NAMES = frozenset({"exec", "eval", "compile", "__import__", "__builtins__"})
DYNAMIC_MODULES = frozenset({"importlib", "runpy", "imp", "pkgutil", "zipimport", "builtins"})
_BAD_NAME = re.compile(r"[\x00-\x1f\x7f]")
MOUNT_TABLE = re.compile(rb"mountinfo|mountstats|/etc/mtab|/proc/mounts|/proc/self/mounts")


def _entries(root):
    """(path relative to `root`, absolute path) of every file and link under it, never following
    a link; the harness copy, bytecode caches and the worker's scratch dir are left out."""
    for here, dirs, names in os.walk(root, followlinks=False):
        rel_dir = Path(here).relative_to(root).as_posix()
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS
                         and not (rel_dir == "." and d == HARNESS_DIR))
        for name in sorted(names) + [d for d in dirs if os.path.islink(os.path.join(here, d))]:
            yield (name if rel_dir == "." else f"{rel_dir}/{name}"), os.path.join(here, name)


def _read(path):
    """(bytes, size) of one regular file, opened without following a link and read from the open
    descriptor (a swap after the open cannot change what is read); raises OSError when the file
    cannot be opened, is not a regular file or is over the cap."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise OSError("not a regular file")
        data = handle.read(MAX_BYTES + 1)
    return data


def _classify(rel, mine):
    """The problem a file at `rel` is for the lint, whatever its content, else None."""
    if not rel.startswith(mine):
        return f"{rel}: outside {mine}"
    if _BAD_NAME.search(rel) or any(p.lower().startswith(".git") for p in rel.split("/")):
        return f"{rel!r}: a file name git or a person cannot use"
    return None


def _take(rel, path, mine):
    """(bytes, None) for a file the lint may read, else (None, the problem it is)."""
    unreadable = f"{rel}: cannot be read (a link, special file or no permission)"
    try:
        regular = stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return None, unreadable
    if not regular:
        return None, f"{rel}: not a regular file (a link or special file)"
    bad = _classify(rel, mine)
    if bad:
        return None, bad
    try:
        return _read(path), None
    except OSError:
        return None, unreadable


def collect(clone, card):
    """(files, problems): `files` maps `cards/<card>/...` paths to bytes for every regular file the
    tester left under its card folder; `problems` names everything else it left in the clone
    (outside that folder, a link or special file, a file it cannot read, over the caps). The shared
    harness copy, the `.git` pointer, bytecode caches and the worker's scratch dir are not the
    tester's output. Never raises for what the tester left behind (a mode 000 file is a problem)."""
    files, problems, seen, total = {}, [], 0, 0
    for rel, path in _entries(Path(clone)):
        if rel == ".git" or rel.endswith((".pyc", ".pyo")):
            continue
        data, problem = _take(rel, path, f"cards/{card}/")
        if problem:
            problems.append(problem)
            continue
        seen, total = seen + 1, total + len(data)
        if seen <= MAX_FILES and total <= MAX_BYTES:
            files[rel] = data
    if seen > MAX_FILES or total > MAX_BYTES:
        problems.append(f"over the caps: {MAX_FILES} files and {MAX_BYTES // 1024} KB"
                        f" in cards/{card}/")
        files = {}
    return files, problems


def _import_problems(rel, source, own):
    """Imports in one test file that reach beyond the stdlib, the harness and the card's own
    folder (`own`: the module names its folder holds), and every way of loading code by name at
    run time (`exec`, `eval`, `compile`, `__import__`, `importlib`, `runpy` and the like)."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return []                       # py_compile says it better
    found = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Name, ast.Attribute)):
            name = node.id if isinstance(node, ast.Name) else node.attr
            if name == "__import__" or (isinstance(node, ast.Name) and name in DYNAMIC_NAMES):
                found.append(f"{rel}: uses {name} (code loaded at run time)")
            continue
        if isinstance(node, ast.Import):
            mods = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            mods = [] if node.level else [node.module or ""]
        else:
            continue
        for mod in mods:
            top = mod.split(".")[0]
            if top in DYNAMIC_MODULES:
                found.append(f"{rel}: imports {top} (code loaded at run time)")
            elif top not in ALLOWED_ROOTS and top not in own:
                found.append(f"{rel}: imports {top}, which is not the stdlib, the harness or"
                             " the card's own folder")
    return found


def _own_names(files, prefix):
    """The module names a test could import from the card's folder: a .py file or a folder directly
    under it. A plain file (`capsule`, `data.txt`) is not importable and is not one."""
    return {Path(rel[len(prefix):]).parts[0].removesuffix(".py") for rel in files
            if Path(rel[len(prefix):]).parts[0].endswith(".py") or len(Path(rel).parts) > 3}


def _shadow_problems(files, prefix):
    """A card file named after a stdlib module, the harness or a project package (`capsule`, `src`,
    ...) would be imported in its place: refuse it. Only the names a test could import count
    (a .py file or a folder directly under the card's folder)."""
    names = _own_names(files, prefix)
    taken = set(sys.stdlib_module_names) | {"harness"} | set(preflight.PRODUCTION_ROOTS)
    return [f"{prefix}{n}: named like {n!r}, a module the tests would import in its place"
            for n in sorted(names & taken)]


def _compile_problems(files):
    """`python3 -m py_compile` over the card's .py files, in a scratch directory outside the repo
    and the clone; one problem per file that does not compile."""
    names = sorted(rel for rel in files if rel.endswith(".py"))
    with tempfile.TemporaryDirectory(prefix="blind-lint-") as tmp:
        for rel in names:
            target = Path(tmp) / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(files[rel])
        def compiles(batch):
            try:
                return not subprocess.run([sys.executable, "-I", "-m", "py_compile", *batch],
                                          cwd=tmp, capture_output=True, timeout=COMPILE_TIMEOUT
                                          ).returncode
            except subprocess.TimeoutExpired:
                return False
        if not names or compiles(names):
            return []
        return [f"{rel}: does not compile" for rel in names if not compiles([rel])]


def examples(files, card):
    """({test id: example row}, problems) from the card's examples.jsonl: each row an object with
    a string `id` and a non-empty `source`."""
    text = files.get(f"cards/{card}/examples.jsonl")
    if text is None:
        return {}, ["examples.jsonl is missing"]
    rows, problems = {}, []
    for number, line in enumerate(text.decode("utf-8", "replace").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            problems.append(f"examples.jsonl line {number} is not JSON")
            continue
        ok = isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"]
        if not ok:
            problems.append(f"examples.jsonl line {number} has no test id")
        elif not (isinstance(row.get("source"), str) and row["source"].strip()):
            problems.append(f"example row {row['id']} has an empty source")
        else:
            rows[row["id"]] = row
    return rows, problems


def _header(files, card):
    """(HEADER.json as a dict, None), or (None, what is wrong with it)."""
    raw = files.get(f"cards/{card}/HEADER.json")
    if raw is None:
        return None, "HEADER.json is missing"
    try:
        head = json.loads(raw)
    except ValueError:
        return None, "HEADER.json is not JSON"
    return (head, None) if isinstance(head, dict) else (None, "HEADER.json is not an object")


def exempt_ids(files, card):
    """(full test ids, problem): the tester's `red_exempt` list in HEADER.json, the tests it asks
    to be allowed to pass on the base. Absent is an empty list; anything but a list of non-empty
    strings is a problem (and no ids)."""
    head, why = _header(files, card)
    listed = (head or {}).get("red_exempt", [])
    if why:
        return [], None                 # `_header_problems` reports a broken HEADER.json
    if isinstance(listed, list) and all(isinstance(t, str) and t for t in listed):
        return list(listed), None
    return [], "HEADER.json red_exempt is not a list of test ids"


def _header_problems(files, card, card_hash):
    head, why = _header(files, card)
    if why:
        return [why]
    bad = exempt_ids(files, card)[1]
    if bad:
        return [bad]
    if head.get("card_hash") != card_hash:
        return [f"HEADER.json card_hash {str(head.get('card_hash'))[:12]!r} is not the card's"
                f" current hash {card_hash[:12]!r}"]
    return []


def _test_names(data):
    """The `test*` method names defined in the classes of one source file ([] when it does not
    parse: py_compile says it better)."""
    try:
        tree = ast.parse(data)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return []
    methods = (m for c in ast.walk(tree) if isinstance(c, ast.ClassDef) for m in c.body)
    return [m.name for m in methods
            if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and m.name.startswith("test")]


def _duplicate_tests(py):
    """One problem per `test*` method name defined more than once in the card's .py files. The
    harness id is `<card>.<method>`, so two same-named tests (one hidden by a `load_tests` hook)
    share one id and the shown one stands for both."""
    seen = {}
    for rel, data in sorted(py.items()):
        for name in _test_names(data):
            seen.setdefault(name, []).append(rel.rsplit("/", 1)[-1])
    return [f"test {name} is defined {len(where)} times ({', '.join(dict.fromkeys(where))}): test"
            " names must be unique within a card, the id is the card id and the name"
            for name, where in sorted(seen.items()) if len(where) > 1]


def _mount_problems(files, prefix):
    """One problem per file of the card (but HEADER.json and examples.jsonl, which describe the
    tests) whose raw bytes hold `mountinfo`, `mountstats`, `/etc/mtab`, `/proc/mounts` or
    `/proc/self/mounts`. This catches accidents only: it is one regex over the bytes, linear in
    their size (`collect` empties `files` over the caps), so it cannot hang or raise on any input.
    A test that means to look builds the name at run time (or reads another file, or runs a
    command), and no static check sees that. The red check's mount shape (`red.py`) and its
    neutral checkout (`checkout.py`) hide the check from accidents and easy tricks too."""
    skip = (f"{prefix}HEADER.json", f"{prefix}examples.jsonl")
    return [f"{rel}: names the mount table; refused: a test may not look at the mount table, which"
            " shows where it runs (it must behave the same in the red check and in the real run)"
            for rel, data in sorted(files.items())
            if rel not in skip and MOUNT_TABLE.search(data)]


def problems(files, card, card_hash):
    """Plain-words lint problems of the collected `files` of card `card` whose current hash is
    `card_hash` (blind spec 3.4 step 1): HEADER.json carries that hash, examples.jsonl rows have a
    source, tests import only the stdlib, the harness and their own folder, every .py compiles,
    and the folder holds at least one test. [] means the files pass. The check that every test
    id has an example row needs the ids and is `missing_examples`."""
    prefix = f"cards/{card}/"
    py = {rel: data for rel, data in files.items() if rel.endswith(".py")}
    if not any(Path(rel).name.startswith("test") for rel in py):
        return [f"cards/{card}/ holds no test_*.py file"]
    own = _own_names(files, prefix)
    out = _header_problems(files, card, card_hash) + examples(files, card)[1]
    out += _shadow_problems(files, prefix) + _duplicate_tests(py)
    for rel, data in sorted(py.items()):
        out += _import_problems(rel, data, own)
    return out + _mount_problems(files, prefix) + _compile_problems(py)
