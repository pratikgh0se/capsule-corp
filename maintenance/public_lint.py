#!/usr/bin/env python3
"""Public-edition denylist lint (P02, spec "Public portable edition", Z2 oracle one of two).

Builds the public tree in a temp dir from release/public-paths.txt at a git ref (default HEAD),
greps it for personal or internal strings, prints `file:line: category: text` per hit and exits 1
on any hit (0 clean, 2 bad usage). A path the allowlist does not name is never public and never
scanned (fail closed). The tree is built from raw blobs (no git archive: no filters, no
export-subst), and a symlink is a hit when its target is absolute, leaves the tree, or passes
through another symlink. Only this file holds denylist words by design (SELF_EXEMPT).

This file holds only the generic rules (personal path, tailnet address, one vendor name). The
personal terms live in release/private-denylist.txt of the --repo checkout, which is NOT in the
allowlist and so never ships: one rule per line, tab-separated `category<TAB>regex<TAB>exempt
globs (comma, may be empty)<TAB>FILE=regex (optional, repeatable, each its own field: on that
file, lines matching regex are exempt)`; `#` starts a comment. The lint needs that file (missing:
exit 2, fail closed) unless --generic-only is given, which is for the public edition, where the
file does not exist.

  public_lint.py [--repo DIR] [--ref REF] [--allowlist FILE] [--denylist FILE | --generic-only]
                 [--list] [--extract DIR] [--summary] [--baseline FILE]
  --list prints the allowlisted file names (no scan).
  --summary prints one `category=count` line per category plus the total instead of every hit,
  and exits 0 (report mode, for CI over the real tree).
  --baseline FILE implies --summary and is a ratchet: FILE holds `category<TAB>count` lines (`#`
  comments); exit 1 when any category count is above its baseline (a missing category is 0).
  --extract DIR writes the public tree into DIR (no scan), for running its tests in isolation.
"""
import argparse
import os
import posixpath
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import unquote

DEFAULT_ALLOWLIST = "release/public-paths.txt"
DEFAULT_DENYLIST = "release/private-denylist.txt"
BINARY_ALLOWLIST = "release/binary-allowlist.txt"
PUBLIC_EXEMPT = "maintenance/public_lint_exempt.txt"
SELF_EXEMPT = ("maintenance/public_lint.py",)

# Generic rules, always on: (category, regex, exempt path globs). Case-insensitive.
RULES = (
    # the shipped exemption file spells /Users/ in its regexes; every other rule still scans it
    ("personal-path", r"/Users/", (PUBLIC_EXEMPT,)),
    ("tailnet", r"\.ts\.net|\b100\.\d+\.\d+\.\d+\b", ()),
)


def _file_field(field, where):
    """(file, regex) of one FILE=regex field; an empty regex or alternative would exempt a whole
    file, so it is refused. Raises ValueError."""
    name, eq, rx = field.partition("=")
    if not name or not eq or any(not part for part in rx.split("|")):
        raise ValueError(f"{where}: bad FILE field {field!r}: want FILE=regex, "
                         "no empty regex or alternative")
    try:
        re.compile(rx)
    except re.error as e:
        raise ValueError(f"{where}: bad FILE regex: {e}") from None
    return name, rx


def _load_denylist(path, need_rules=True):
    """(extra rules, line exemptions) from the private denylist. Raises OSError, ValueError."""
    rules, line_exempt = [], {}
    for n, raw in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        f = raw.split("\t")
        if len(f) < 2 or not f[0] or not f[1]:
            raise ValueError(f"{path}:{n}: want category<TAB>regex[<TAB>globs[<TAB>FILE=regex...]]")
        try:
            re.compile(f[1])
        except re.error as e:
            raise ValueError(f"{path}:{n}: bad regex: {e}")
        exempt = tuple(g.strip() for g in f[2].split(",") if g.strip()) if len(f) > 2 else ()
        rules.append((f[0], f[1], exempt))
        for field in f[3:]:
            if field:
                name, rx = _file_field(field, f"{path}:{n}")
                old = line_exempt.get((name, f[0]))
                line_exempt[(name, f[0])] = re.compile(f"{old.pattern}|{rx}" if old else rx, re.I)
    if need_rules and not rules:
        raise ValueError(f"{path}: no denylist rules")
    return tuple(rules), line_exempt


def clean_env():
    """os.environ without the variables that redirect git to another repo, index, object store
    or config (a hook or wrapper may set them); every git call here and in public_export uses it."""
    drop = {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE", "GIT_CEILING_DIRECTORIES",
            "GIT_PREFIX", "GIT_QUARANTINE_PATH"}
    return {k: v for k, v in os.environ.items()
            if k not in drop and not k.startswith(("GIT_CONFIG", "GIT_AUTHOR_", "GIT_COMMITTER_"))}


def glob_re(glob):
    """`*` stays in one directory, `**` crosses directories, trailing `/` is a whole directory."""
    if glob.endswith("/"):
        glob += "**"
    out, i = "", 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif glob.startswith("**", i):
            out += ".*"
            i += 2
        elif glob[i] == "*":
            out += "[^/]*"
            i += 1
        elif glob[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(glob[i])
            i += 1
    return re.compile(out + r"\Z")


def git(repo, *args, input=None):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True,
                          env=clean_env(), input=input).stdout


def _public_files(repo, ref, allowlist):
    inc, exc = [], []
    for raw in Path(allowlist).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            (exc if line.startswith("!") else inc).append(glob_re(line.lstrip("!").strip()))
    if not inc:
        raise ValueError(f"{allowlist}: no allowlist entries")
    names = git(repo, "ls-tree", "-r", "--name-only", "-z", ref).decode().split("\0")
    return sorted(n for n in names if n and any(r.match(n) for r in inc)
                  and not any(r.match(n) for r in exc))


def _file_hits(name, text, rules, line_exempt):
    hits = []
    for n, line in enumerate(text.splitlines(), 1):
        for cat, rx, ex in rules:
            if any(r.match(name) for r in ex) or not rx.search(line):
                continue
            keep = line_exempt.get((name, cat))
            if not (keep and keep.search(line)):
                hits.append((name, n, cat, line.strip()[:160]))
    return hits


LINK_RE = re.compile(r"\]\(\s*<?([^)\s>]+)|<a\s[^>]*?href\s*=\s*[\"']([^\"']+)", re.I)


def _link_findings(name, text, shipped):
    """private-link hits: a relative link in a shipped markdown file whose target (resolved against
    the file's folder) is not a shipped file or a folder holding one. http(s), mailto, other
    schemes and #anchors are ignored."""
    hits = []
    for n, line in enumerate(text.splitlines() if name.endswith(".md") else (), 1):
        for m in LINK_RE.finditer(line):
            ref = m.group(1) or m.group(2)
            if ref.startswith(("#", "//")) or re.match(r"[A-Za-z][A-Za-z0-9+.-]*:", ref):
                continue
            path = unquote(ref.split("#")[0].split("?")[0])
            if not path:
                continue
            base = "" if path.startswith("/") else posixpath.dirname(name)
            tgt = posixpath.normpath(posixpath.join(base, path.lstrip("/")))
            held = tgt in shipped or any(f.startswith(tgt + "/") for f in shipped)
            if tgt != "." and not held:
                hits.append((name, n, "private-link",
                             f"link to a file the public release does not ship: {ref[:100]}"))
    return hits


def _extract(repo, ref, files, tree):
    """Write the named files of `ref` into tree from raw blobs: no filters, no export-subst, no
    export-ignore. Modes are set explicitly (100755 executable, 120000 symlink, else 644); a
    gitlink (submodule) or any other entry is not written and _scan reports it."""
    want = set(files)
    entries = {}
    for rec in git(repo, "ls-tree", "-r", "-z", "--full-tree", ref).split(b"\0"):
        meta, _, name = rec.decode().partition("\t")
        if name in want:
            mode, typ, sha = meta.split()
            if typ == "blob":
                entries[name] = (mode, sha)
    names = sorted(entries)
    out = git(repo, "cat-file", "--batch",
              input="".join(entries[n][1] + "\n" for n in names).encode())
    pos = 0
    for n in names:
        eol = out.index(b"\n", pos)
        size = int(out[pos:eol].split()[2])
        data, pos = out[eol + 1:eol + 1 + size], eol + 2 + size
        dst = Path(tree) / n
        dst.parent.mkdir(parents=True, exist_ok=True)
        if entries[n][0] == "120000":
            os.symlink(data.decode("utf-8", "surrogateescape"), dst)
        else:
            dst.write_bytes(data)
            dst.chmod(0o755 if entries[n][0] == "100755" else 0o644)
    _trim_sums(tree, names)


def _trim_sums(tree, names):
    """Drop each SHA256SUMS line that names a file not exported, so the shipped sums verify."""
    have = set(names)
    for n in names:
        p = Path(tree) / n
        if posixpath.basename(n) != "SHA256SUMS" or p.is_symlink():
            continue
        def shipped(line, n=n):
            parts = line.strip().split(None, 1)   # like preflight `_sums_ok`: the name is the rest
            return len(parts) < 2 or posixpath.join(posixpath.dirname(n),
                                                    parts[1].strip()) in have
        p.write_text("".join(x for x in p.read_text().splitlines(True) if shipped(x)))


def _link_hits(tree, name, target):
    """A symlink must stay inside the tree by itself: not absolute, and (resolved in the extracted
    tree) not leaving it and not passing through another symlink."""
    if posixpath.isabs(target):
        return [(name, 1, "symlink", f"absolute target {target[:100]}")]
    base = os.path.realpath(tree)
    lexical = os.path.normpath(os.path.join(base, posixpath.dirname(name), target))
    if os.path.commonpath([base, lexical]) != base:
        return [(name, 1, "symlink", f"target leaves the tree: {target[:100]}")]
    if os.path.realpath(os.path.join(base, name)) != lexical:
        return [(name, 1, "symlink", f"target passes through another symlink: {target[:100]}")]
    return []


def _binary_allowed(text):
    """Compiled globs from the text of release/binary-allowlist.txt (one per line, `#` comments)."""
    return [glob_re(ln.strip()) for ln in text.splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")]


def _decode(data):
    """Text of a file, or None when it is binary: a NUL in the first 8 KB, or bytes that are not
    UTF-8 (a UTF-16 byte-order mark makes it text, NULs and all)."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            return None
    if b"\0" in data[:8192]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _scan(tree, files, rules=RULES, line_exempt=None, binary_allow=()):
    """Hits over every named file of the extracted tree, symlinks included (the target text is
    scanned and never followed). `rules` is (category, regex, exempt globs) tuples. A binary file
    (see _decode) is a `binary` hit unless a `binary_allow` glob matches its path."""
    hits, line_exempt = [], line_exempt or {}
    rules = [(c, re.compile(rx, re.I), [glob_re(g) for g in ex]) for c, rx, ex in rules]
    shipped = set(files)
    for name in files:
        p = Path(tree) / name
        if ".git" in name.split("/"):
            hits.append((name, 1, "git-path", "a .git path is never public"))
        elif p.is_symlink():
            target = os.readlink(p)
            hits += _link_hits(tree, name, target) + _file_hits(name, target, rules, line_exempt)
        elif not p.is_file():
            hits.append((name, 1, "not-a-blob", "not a regular file or symlink (submodule?)"))
        elif name not in SELF_EXEMPT:
            text = _decode(p.read_bytes())
            if text is not None:
                hits += _file_hits(name, text, rules, line_exempt)
                hits += _link_findings(name, text, shipped)
            elif not any(r.match(name) for r in binary_allow):
                hits.append((name, 1, "binary", f"binary file not on the allowlist; to allow it, "
                             f"add its path to {BINARY_ALLOWLIST}"))
    return hits


def load_rules(repo, denylist=None, generic_only=False):
    """(rules, line_exempt): the generic rules plus the private denylist (default: release/
    private-denylist.txt of `repo`). A missing file is a ValueError: fail closed. `generic_only`
    skips the file, for the public edition, which does not have it."""
    shared = Path(repo) / PUBLIC_EXEMPT  # shipped line exemptions for the built-in rules
    line_exempt = {}
    if shared.is_file():
        found, line_exempt = _load_denylist(shared, need_rules=False)
        bad = sorted({c for c, _rx, _ex in found if c != "personal-path"})
        if bad:
            raise ValueError(f"{shared}: only personal-path lines are allowed in a shipped "
                             f"exemption file, found {', '.join(bad)}")
    if generic_only:
        return RULES, line_exempt
    path = Path(denylist) if denylist else Path(repo) / DEFAULT_DENYLIST
    if not path.is_file():
        raise ValueError(f"private denylist {path} is missing; refusing (fail closed)")
    extra, private_exempt = _load_denylist(path)
    for key, rx in private_exempt.items():
        old = line_exempt.get(key)
        line_exempt[key] = re.compile(f"{old.pattern}|{rx.pattern}", re.I) if old else rx
    return RULES + extra, line_exempt


def _run(a):
    """(files, hits); hits is None for --list and --extract.
    Raises OSError, ValueError, CalledProcessError."""
    with tempfile.TemporaryDirectory() as tmp:
        al = a.allowlist or str(Path(tmp) / "allowlist.txt")
        if not a.allowlist:
            Path(al).write_bytes(git(a.repo, "show", f"{a.ref}:{DEFAULT_ALLOWLIST}"))
        files = _public_files(a.repo, a.ref, al)
        if a.list:
            return files, None
        tree = Path(a.extract) if a.extract else Path(tmp) / "tree"
        tree.mkdir(parents=True, exist_ok=True)
        _extract(a.repo, a.ref, files, tree)
        if a.extract:
            return files, None
        rules, line_exempt = load_rules(a.repo, a.denylist, a.generic_only)
        try:
            allow = _binary_allowed(git(a.repo, "show", f"{a.ref}:{BINARY_ALLOWLIST}").decode())
        except subprocess.CalledProcessError:
            allow = []   # no file at that ref: every binary is refused
        return files, _scan(tree, files, rules, line_exempt, allow)


def _load_baseline(path):
    """{category: count} from a baseline file. Raises OSError, ValueError."""
    base = {}
    lines = [r for r in Path(path).read_text(encoding="utf-8").splitlines()
             if r.strip() and not r.lstrip().startswith("#")]
    for raw in lines:
        cat, _, n = raw.partition("\t")
        if cat in base:
            raise ValueError(f"duplicate category {cat!r}")
        base[cat] = int(n)
    return base


def _summary(by, nfiles, baseline):
    """Print counts per category and the total; with a baseline file return 1 when any category
    is above its baseline, else 0."""
    try:
        base = _load_baseline(baseline) if baseline else {}
    except (OSError, ValueError) as e:
        print(f"public_lint: bad baseline {baseline}: {e}", file=sys.stderr)
        return 2
    over = []
    for cat in sorted(set(by) | set(base)):
        n = by.get(cat, 0)
        note = f" (baseline {base.get(cat, 0)})" if baseline else ""
        print(f"{cat}={n}{note}")
        if baseline and n > base.get(cat, 0):
            over.append(cat)
    print(f"public_lint: {nfiles} public files, total={sum(by.values())}")
    if over:
        print(f"public_lint: ABOVE BASELINE: {', '.join(over)}", file=sys.stderr)
    return 1 if over else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default=".")
    ap.add_argument("--ref", default="HEAD")
    ap.add_argument("--allowlist", default=None,
                    help="file path; default: the copy committed at --ref")
    ap.add_argument("--denylist", default=None,
                    help="private denylist file; default: release/private-denylist.txt in --repo")
    ap.add_argument("--generic-only", action="store_true",
                    help="skip the private denylist (public edition, which has none)")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--summary", action="store_true",
                    help="per-category counts and total instead of every hit; exit 0")
    ap.add_argument("--baseline", default=None, metavar="FILE",
                    help="ratchet: implies --summary; exit 1 if a category is above FILE's count")
    ap.add_argument("--extract", default=None, metavar="DIR")
    a = ap.parse_args(argv)
    if a.generic_only and a.denylist:
        ap.error("--generic-only and --denylist exclude each other")
    try:
        files, hits = _run(a)
    except (OSError, ValueError, subprocess.CalledProcessError) as e:
        detail = (getattr(e, "stderr", None) or b"").decode().strip() or e
        print(f"public_lint: cannot build the public tree: {detail}", file=sys.stderr)
        return 2
    if hits is None:
        print("\n".join(files) if a.list else f"public_lint: extracted {len(files)} files")
        return 0
    by = {}
    for h in hits:
        by[h[2]] = by.get(h[2], 0) + 1
    if a.summary or a.baseline:
        return _summary(by, len(files), a.baseline)
    for name, n, cat, text in hits:
        print(f"{name}:{n}: {cat}: {text}")
    detail = ", ".join(f"{k}={v}" for k, v in sorted(by.items()))
    print(f"public_lint: {len(files)} public files, {len(hits)} hits"
          + (f" ({detail})" if detail else ""))
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
