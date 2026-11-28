#!/usr/bin/env python3
"""Licence inventory (G1), NOTICE generation and freshness (G5)."""
import json
import os
import re
import subprocess
import sys
from collections import namedtuple
from datetime import date
from fnmatch import fnmatchcase
from pathlib import Path

Component = namedtuple("Component", "name version license source")

# The copyleft policy, as classes rather than a list of packages (study item 7: a human
# writes the policy once, the gate enforces it forever).
ALLOWED = {"MIT", "BSD-2-Clause", "BSD-3-Clause", "Apache-2.0", "ISC",
           "PSF-2.0", "Python-2.0", "0BSD", "Unlicense", "CC0-1.0", "Zlib"}
# D240 (2026-11-13): weak share-alike (LGPL-*, MPL-*) is allowed in what ships; GPL-*/AGPL-*
# (every version and SPDX suffix) is denied; dev-only tools may use any licence
# (docs/licenses/dev-only.txt). The patterns below cover the whole families.
REVIEW = {"EPL-2.0", "CDDL-1.0"}
# Whole-id match only: an expression ("LGPL-2.1 AND GPL-2.0", "MPL-2.0;GPL-3.0") or junk
# ("MPL-") is unclassified and therefore denied.
_WEAK_ALLOWED_RE = re.compile(
    r"(?:LGPL-(?:2\.0|2\.1|3\.0)|MPL-(?:1\.0|1\.1|2\.0))"
    r"(?:-only|-or-later|\+|-no-copyleft-exception)?", re.IGNORECASE)
_STRONG_COPYLEFT_RE = re.compile(r"^A?GPL(?:-|$)", re.IGNORECASE)
DENIED = {"GPL-2.0", "GPL-3.0", "AGPL-3.0", "SSPL-1.0", "BUSL-1.1", "Elastic-2.0", "unknown"}

OPS_LICENSE_GATE = "OPS_LICENSE_GATE"
_ADVISORY_VALUES = ("", "0", "false", "off", "no")
UNKNOWN = "unknown"
# declared.json value for a component whose source is public but whose licence is not open
# source (Semgrep rules, ...). Neither allowed nor denied: scan lists it and the coverage
# check insists NOTICE names it, so it is a recorded decision rather than a silent one.
SOURCE_AVAILABLE = "source-available"

# `pip install -q --no-cache-dir "coverage>=7" "ruff==0.6.9" "playwright==${PW_VER}"`
# and the bare `pip install -q playwright` on the venv line.
_PIP_LINE_RE = re.compile(r"pip install\b(?P<rest>.*)$")
_PIN_RE = re.compile(r'"?(?P<name>[A-Za-z][A-Za-z0-9._-]*)'
                     r'(?:(?P<op>==|>=|~=)(?P<ver>\$\{[A-Za-z_][A-Za-z0-9_]*\}|[0-9][0-9A-Za-z.*+-]*))?"?')
_PIP_FLAGS = {"-q", "--no-cache-dir", "-U", "--upgrade", "install", "pip", "-m",
              "python3", "python", "--quiet", "-i", "--index-url"}
_GENERATED_RE = re.compile(r"^generated:\s*(\d{4})-(\d{2})-(\d{2})\s*$", re.MULTILINE)
# A licence id is untrusted text (it comes from a lockfile or pip-licenses JSON someone
# else wrote) and it becomes a filename - `MIT/X11`, `../x`, `..` must not turn into a
# path outside texts_dir.
_UNSAFE_FS_RE = re.compile(r"[^A-Za-z0-9._+-]")
_DOTDOT_RE = re.compile(r"\.\.+")


# pip-licenses prints the PyPI trove classifier when a package declares no SPDX id. Only the
# unambiguous ones are mapped; "BSD License" is read as BSD-3-Clause (every BSD variant is allowed).
_TROVE = {"MIT License": "MIT", "Apache Software License": "Apache-2.0",
          "BSD License": "BSD-3-Clause"}


def _canon(name):
    """PEP 503 name: `diff_cover` (pip-licenses) and `diff-cover` (Dockerfile) are one."""
    return re.sub(r"[-_.]+", "-", str(name)).lower()


def _norm_license(raw):
    """SPDX-ish id, or `unknown`. pip-licenses writes "UNKNOWN"; npm may write
    nothing at all or a `{"type": ...}` object in an ancient lockfile."""
    if isinstance(raw, dict):
        raw = raw.get("type") or ""
    text = str(raw or "").strip()
    if not text or text.upper() in ("UNKNOWN", "NONE", "NULL", "SEE LICENSE IN LICENSE"):
        return UNKNOWN
    return _TROVE.get(text, text)


def parse_dockerfile_pins(text):
    """Components from every `pip install` line in a Dockerfile. Licence is
    always `unknown` - a Dockerfile declares nothing - so declared.json is what
    turns these into an answer."""
    out = []
    for raw in re.sub(r"\\\n", " ", text or "").splitlines():
        m = _PIP_LINE_RE.search(raw)
        if not m:
            continue
        for token in re.split(r"&&|;|\|", m.group("rest"))[0].split():
            token = token.strip("\\").strip()
            if not token or token in _PIP_FLAGS or token.startswith("-"):
                continue
            pin = _PIN_RE.fullmatch(token)
            if not pin:
                continue
            ver = pin.group("ver")
            if ver and pin.group("op") in (">=", "~="):
                ver = pin.group("op") + ver
            out.append(Component(pin.group("name"), ver or "unpinned", UNKNOWN, "Dockerfile"))
    return out


def parse_pip_licenses(text):
    """`pip-licenses --format=json` output, when a human has run it and saved
    it under docs/licenses/. Never invoked from here - no pip call, no network."""
    try:
        rows = json.loads(text)
    except Exception:
        return []
    if not isinstance(rows, list):
        return []
    return [Component(_canon(str(r.get("Name", "")).strip()),
                      str(r.get("Version", "")).strip() or "unpinned",
                      _norm_license(r.get("License")), "pip-licenses")
            for r in rows if isinstance(r, dict) and r.get("Name")]


def parse_npm_lock(text, *, source="package-lock.json"):
    """`package-lock.json` v2/v3 `packages` map, stdlib-parsed - the `npm ls --json`
    shape without needing npm on PATH."""
    try:
        data = json.loads(text)
    except Exception:
        return []
    packages = data.get("packages") if isinstance(data, dict) else None
    if not isinstance(packages, dict):
        return []
    out = []
    for path, meta in packages.items():
        if not path or not isinstance(meta, dict) or meta.get("dev"):
            continue
        name = path.split("node_modules/", 1)[-1]
        out.append(Component(name, str(meta.get("version") or "unpinned"),
                             _norm_license(meta.get("license")), source))
    return out


def parse_declared(text):
    """docs/licenses/declared.json: `{"<name>": "<SPDX id>"}`. One human
    decision per component, written down once."""
    try:
        data = json.loads(text)
    except Exception:
        return {}
    return {str(k): _norm_license(v) for k, v in data.items()} if isinstance(data, dict) else {}


def apply_declared(components, declared):
    """Fill an `unknown` licence from declared.json. Never overrides a licence
    the component actually declared - a human note must not be able to launder
    a GPL dependency into MIT."""
    declared = declared or {}
    return [c._replace(license=declared[c.name]) if c.license == UNKNOWN and c.name in declared else c
            for c in components]


def inventory(*, sources, declared=None):
    """Merged, deduped, name-sorted component list. `sources` is a list of
    `(label, text, parser)`; later sources win only when they carry a real
    licence and the incumbent does not."""
    best = {}
    for _label, text, parser in sources:
        for c in parser(text):
            prev = best.get(c.name)
            if prev is None:
                best[c.name] = c
                continue
            # A later source wins only where it is more specific.
            better_licence = prev.license == UNKNOWN and c.license != UNKNOWN
            better_version = prev.version == "unpinned" and c.version != "unpinned"
            if better_licence or better_version:
                best[c.name] = c._replace(
                    license=c.license if c.license != UNKNOWN else prev.license,
                    version=c.version if c.version != "unpinned" else prev.version)
    return sorted(apply_declared(list(best.values()), declared), key=lambda c: c.name.lower())


_SPDX_TOKEN_RE = re.compile(r"\s*(?:(\()|(\))|([A-Za-z0-9][A-Za-z0-9.+-]*))")
_SPDX_WORDS = ("AND", "OR", "WITH")


# Real SPDX exception ids that pair with an allowed base (LGPL, MPL, Apache, MIT, BSD, UPL);
# source (checked 2026-11-13): https://spdx.org/licenses/exceptions-index.html. Ids that only
# follow a GPL base are left out: that base is denied anyway. After WITH only these are
# accepted; a licence id (GPL-3.0) or an unknown name leaves the term unresolved (False).
_SPDX_EXCEPTIONS = frozenset(x.lower() for x in (
    "LLVM-exception", "Swift-exception", "mxml-exception", "SHL-2.0", "SHL-2.1",
    "LGPL-3.0-linking-exception", "OCaml-LGPL-linking-exception", "WxWindows-exception-3.1",
    "Qt-LGPL-exception-1.1", "Digia-Qt-LGPL-exception-1.1", "Nokia-Qt-exception-1.1",
    "FLTK-exception", "Universal-FOSS-exception-1.0", "OCCT-exception-1.0", "LLGPL",
    "Qwt-exception-1.0", "Spelling-Provider-LGPL-exception", "GStreamer-exception-2005",
    "GStreamer-exception-2008", "PCRE2-exception", "fmt-exception", "Google-Patent-WebM",
    "Simple-Library-Usage-exception"))


def _id_allowed(lic):
    return lic in ALLOWED or bool(_WEAK_ALLOWED_RE.fullmatch(lic))


def _spdx_tokens(text):
    """Tokens of an SPDX expression, or None when anything is not a paren or an id."""
    out, pos = [], 0
    while text[pos:].strip():
        m = _SPDX_TOKEN_RE.match(text, pos)
        if not m:
            return None
        out.append(m.group(m.lastindex))
        pos = m.end()
    return out


def _atom(toks, i):
    if i >= len(toks) or toks[i] == ")" or toks[i].upper() in _SPDX_WORDS:
        raise ValueError(i)
    if toks[i] != "(":
        return _with_term(toks, i)
    ok, i = _or(toks, i + 1)
    if i >= len(toks) or toks[i] != ")":
        raise ValueError(i)
    return ok, i + 1


def _with_term(toks, i):
    """An id, or `id WITH exception`: the base licence decides, but only a known exception
    id is accepted after WITH (anything else leaves the term unresolved -> False)."""
    # Per the SPDX grammar WITH binds to a single licence id, so "(A OR B) WITH X" is
    # unparseable and denied on purpose.
    ok = _id_allowed(toks[i])
    if i + 1 < len(toks) and toks[i + 1].upper() == "WITH":
        j = i + 2
        if j >= len(toks) or toks[j] in ("(", ")") or toks[j].upper() in _SPDX_WORDS:
            raise ValueError(j)
        return ok and toks[j].lower() in _SPDX_EXCEPTIONS, j + 1
    return ok, i + 1


def _and(toks, i):
    ok, i = _atom(toks, i)
    while i < len(toks) and toks[i].upper() == "AND":
        more, i = _atom(toks, i + 1)
        ok = ok and more
    return ok, i


def _or(toks, i):
    ok, i = _and(toks, i)
    while i < len(toks) and toks[i].upper() == "OR":
        more, i = _and(toks, i + 1)
        ok = ok or more
    return ok, i


def _expr_allowed(text):
    """SPDX semantics over the single-id lists: OR needs any allowed alternative, AND
    every part. Unparseable is False (fail closed)."""
    toks = _spdx_tokens(text)
    try:
        ok, end = _or(toks, 0) if toks else (False, 0)
    except (ValueError, RecursionError):
        return False
    return ok and end == len(toks)


def classify(component):
    """"allowed" | "review" | "denied". Anything unclassified is denied: a
    licence nobody has looked at is not evidence that it is fine."""
    lic = component.license
    if lic == SOURCE_AVAILABLE:
        return SOURCE_AVAILABLE
    if lic in ALLOWED or _WEAK_ALLOWED_RE.fullmatch(lic) or _expr_allowed(lic):
        return "allowed"
    if _STRONG_COPYLEFT_RE.match(lic):
        return "denied"
    if lic in REVIEW:
        return "review"
    return "denied"


def gate_blocking(env):
    """True when a review/denied component fails CI (`OPS_LICENSE_GATE=1`).
    Absent means advisory - the mandate rule for every new gate."""
    raw = (env if env is not None else os.environ).get(OPS_LICENSE_GATE)
    return raw is not None and str(raw).strip().lower() not in _ADVISORY_VALUES


def scan_lines(components):
    """G1's output: one line per component, then a summary. Pure."""
    counts = {"allowed": 0, "review": 0, "denied": 0, SOURCE_AVAILABLE: 0}
    lines = []
    for c in components:
        verdict = classify(c)
        counts[verdict] += 1
        lines.append(f"  {c.name} {c.version} {c.license} {verdict} ({c.source})")
    lines.append(f"licences: {counts['allowed']} allowed, {counts['review']} review, "
                 f"{counts['denied']} denied, {counts[SOURCE_AVAILABLE]} source-available")
    return lines, counts


def _safe_license_filename(lic):
    """A licence id turned into a filename component, never a path."""
    safe = _DOTDOT_RE.sub("_", str(lic))
    safe = _UNSAFE_FS_RE.sub("_", safe)
    safe = safe.lstrip(".")
    return safe or "unknown"


SECTIONS_FILE = "notice-sections.json"


def _license_ids(components):
    """Every licence id named by the components, SPDX expressions split into their ids."""
    return {t for c in components for t in (_spdx_tokens(c.license) or [c.license])
            if t not in ("(", ")", SOURCE_AVAILABLE) and t.upper() not in _SPDX_WORDS}


def render_notice(components, *, generated, texts_dir=None, sources=None, sections=None):
    """docs/licenses/NOTICE.md. One table row per component (the spec's "one entry per
    component"; a table rather than a heading each, because a Node lockfile has
    hundreds), then one line per distinct licence saying whether its full text is on
    disk - because a bare URL is not compliant."""
    lines = [
        "# NOTICE - third-party components",
        "",
        f"generated: {generated.isoformat()}",
        "",
        "Generated by `maintenance/notice_gen.py --write` from the sources named in",
        "the Source or Upstream column. Capsule Corp's own code is MIT-licensed (`LICENSE`); the",
        "components below remain under their own licences and this file is their",
        "required attribution. **a bare URL is not compliant attribution for MIT,",
        "BSD or Apache** (legal study section 5.4). The licences below that have a",
        "copy on disk under `docs/licenses/texts/` are listed there as \"full text",
        "present\"; a licence listed as `TEXT MISSING` has no copy reproduced here",
        "yet.",
        "",
    ]
    if sources:
        lines.append("## Sources read")
        lines.append("")
        for s in sources:
            if s.get("external"):
                lines.append(f"- `{s['label']}` - read from a path outside this repository "
                              "(a sibling checkout); the row count above depends on what is "
                              "present on the machine that ran `--write`.")
            else:
                lines.append(f"- `{s['label']}`")
        lines.append("")
    if components:
        lines += [
            "| Component | Version | Licence | Class | Source |",
            "|---|---|---|---|---|",
        ]
        for c in components:
            lines.append(f"| {c.name} | {c.version} | {c.license} | {classify(c)} | {c.source} |")
        lines.append("")
    # sections: docs/licenses/notice-sections.json, a list of {title, intro, columns, rows,
    # outro} for what no lockfile or Dockerfile pin can supply (services, CI tools, ...).
    for sec in sections or []:
        lines += [f"## {sec['title']}", "", sec.get("intro", ""), ""]
        if sec.get("rows"):
            cols = sec["columns"]
            lines += ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
            lines += ["| " + " | ".join(r) + " |" for r in sec["rows"]] + [""]
        if sec.get("outro"):
            lines += [sec["outro"], ""]
    lines += ["## Full licence texts", ""]
    tdir = Path(texts_dir) if texts_dir else None
    on_disk = {f.stem for f in tdir.glob("*.txt")} if tdir and tdir.is_dir() else set()
    for lic in sorted(_license_ids(components) | on_disk):
        fname = _safe_license_filename(lic)
        present = bool(texts_dir) and (Path(texts_dir) / f"{fname}.txt").exists()
        lines.append(f"- {lic} - {'full text present' if present else 'TEXT MISSING'}"
                     f" (`docs/licenses/texts/{fname}.txt`)")
    lines += ["", "A `TEXT MISSING` line is a gap in attribution, not a formatting note:",
              "add the licence's full text before that component ships in anything.", ""]
    return "\n".join(lines)


def check_freshness(notice_path, sources, *, today=None):
    """G5-lite: NOTICE.md exists and its `generated:` date is not older than the newest
    source mtime."""
    notice_path = Path(notice_path)
    if not notice_path.exists():
        return False, "notice: missing (run notice_gen.py --write)"
    m = _GENERATED_RE.search(notice_path.read_text(encoding="utf-8"))
    if not m:
        return False, "notice: stale (no `generated:` date)"
    gen = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    now = today if today is not None else date.today()
    newest = None
    for s in sources:
        p = Path(s)
        if p.exists():
            # Capped at `now`: a source's real mtime can outrun a caller's simulated
            # `today` (a fixture built on a fixed date, run for real on a later day) -
            # that must not make a hermetic test depend on the wall clock.
            d = min(date.fromtimestamp(p.stat().st_mtime), now)
            newest = d if newest is None or d > newest else newest
    if newest and gen < newest:
        return False, f"notice: stale (generated {gen.isoformat()}, sources changed {newest.isoformat()})"
    return True, f"notice: fresh (generated {gen.isoformat()})"


# What the repo pulls in that no lockfile lists: compose images, workflow actions, release-URL
# downloads, pip pins in workflows, Brewfile and bootstrap.lock. Each must be named in
# NOTICE.md (and every declared.json key too), so adding one without a NOTICE row shows up
# in `--scan`.
_FIND = {
    "compose image": ("observability/*.yml", re.compile(r"^\s*image:\s*([^\s@:#]+)", re.M)),
    "workflow action": (".github/workflows/*.yml",
                        re.compile(r"\buses:\s*([\w.-]+/[\w.-]+)[\w./-]*@")),
    "release download": (".github/workflows/*.yml",
                         re.compile(r"github\.com/([\w.-]+/[\w.-]+)/releases/download/")),
    "workflow pip pin": (".github/workflows/*.yml",
                         re.compile(r"(?<![\w.-])([A-Za-z][\w.-]*)==\d")),   # every pin
    "brew": ("Brewfile", re.compile(r'^(?:brew|cask)\s+"([^"@]+)', re.M)),
    "bootstrap brew": ("bootstrap.lock", re.compile(r'^BREW_(?:FORMULAE|CASKS)="([^"]*)"', re.M)),
}


def _named(name, text):
    """`name` appears in lower-cased `text` as a whole word (separators optional:
    `docker-desktop` finds `Docker Desktop`, `uv` does not find `uvicorn`)."""
    toks = [re.escape(t) for t in re.split(r"[\W_]+", name.lower()) if t]
    return bool(re.search(r"(?<![a-z0-9])" + r"[\W_]*".join(toks) + r"(?![a-z0-9])", text))


def _used_names(root):
    return [(kind, n.split("@")[0]) for kind, (glob, rx) in _FIND.items()
            for f in sorted(root.glob(glob)) for m in rx.findall(_read(f)) for n in m.split()
            if not n.startswith("capsule")]


def _coverage_gaps(root, declared, dockerfile, jsons):
    """Names the repo uses that NOTICE.md does not mention (declared.json keys included),
    plus a pip-licenses JSON whose version differs from the Dockerfile pin (stale evidence)."""
    notice = _read(root / "docs" / "licenses" / "NOTICE.md").lower()
    found = _used_names(root) + [("declared.json", k) for k in declared]
    gaps = [f"{kind} {n} is not named in NOTICE.md" for kind, n in dict.fromkeys(found)
            if not _named(n, notice)]
    resolved = {c.name: c.version for t in jsons for c in parse_pip_licenses(t)}
    for c in parse_dockerfile_pins(dockerfile):
        have = resolved.get(_canon(c.name), c.version)
        if c.version[:1].isdigit() and have != c.version:
            gaps.append(f"pip-licenses JSON has {c.name} {have}, Dockerfile pins {c.version}")
    return gaps


_PROVENANCE_RE = re.compile(
    r"(?i)\b(?:lifted|adapted|copied|vendored|ported) from\s+[\w.-]+/[\w.-]+")


def _tracked(root, dirs):
    """Tracked files under `dirs` (git ls-files; a plain walk when `root` is not a checkout)."""
    r = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--", *dirs],
                       capture_output=True, text=True, check=False)
    names = [x for x in r.stdout.split("\0") if x] if r.returncode == 0 else []
    return [root / x for x in names] or [f for d in dirs for f in sorted((root / d).rglob("*"))]


def _provenance_gaps(root):
    """Files whose first 15 lines say they were lifted/adapted/copied/vendored/ported from
    <owner>/<repo>, and everything under capsule/licenses/, need an entry in NOTICE.md
    (found by their path). Tracked text files only; binaries read as empty."""
    notice = _read(root / "docs" / "licenses" / "NOTICE.md")
    code = [f for f in _tracked(root, ("capsule", "maintenance", "bin", "docker", "observability"))
            if f.is_file() and "tests" not in f.parts]
    files = [f for f in code if _PROVENANCE_RE.search("\n".join(_read(f).splitlines()[:15]))]
    files += [f for f in sorted((root / "capsule" / "licenses").glob("*")) if f.is_file()]
    rel = [f.relative_to(root).as_posix() for f in dict.fromkeys(files)]
    return [f"{r} carries third-party provenance but is not named in NOTICE.md"
            for r in rel if r not in notice]


def _scan_doc_stale(root, *, today=None, max_days=90):
    """The newest docs/licenses/SCAN-YYYY-MM-DD.md must be younger than `max_days`."""
    names = [f.name for f in (root / "docs" / "licenses").glob("SCAN-*.md")]
    dates = [date(*map(int, m.groups())) for n in names
             if (m := re.fullmatch(r"SCAN-(\d{4})-(\d{2})-(\d{2})\.md", n))]
    if not dates:
        return ["scan doc: missing (docs/licenses/SCAN-YYYY-MM-DD.md)"]
    age = ((today or date.today()) - max(dates)).days
    return [f"scan doc: stale ({max(dates).isoformat()}, {age} days old, limit {max_days})"] \
        if age > max_days else []


def _repo_root():
    return Path(__file__).resolve().parents[1]


def _read(path):
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, ValueError):   # unreadable, or binary (UnicodeDecodeError)
        return ""


DEFAULT_NODE_LOCK = ""


def _default_sources(root, inline_dockerfile=None, node_lock=None):
    """(sources for inventory(), source paths for freshness, `skipped (<reason>)` lines
    for any source that wasn't there, source metadata for render_notice's provenance
    block)."""
    lic = root / "docs" / "licenses"
    dockerfiles = [root / "docker" / d / "Dockerfile"
                   for d in ("base", "stacks/python", "stacks/web")]
    text = inline_dockerfile if inline_dockerfile is not None \
        else "\n".join(_read(p) for p in dockerfiles)
    sources = [("Dockerfile", text, parse_dockerfile_pins)]
    paths = list(dockerfiles)
    meta = [{"label": "Dockerfile", "external": False}]
    skips = []

    pip_files = sorted(lic.glob("*.pip-licenses.json"))
    if not pip_files:
        skips.append(f"pip-licenses: skipped (no {lic}/*.pip-licenses.json found)")
    for p in pip_files:
        sources.append((p.name, _read(p), parse_pip_licenses))
        paths.append(p)
        meta.append({"label": p.name, "external": False})

    in_repo = root / "docker" / "stacks" / "web" / "package-lock.json"
    use_repo = node_lock is None and in_repo.exists()
    lock_arg = node_lock if node_lock is not None else DEFAULT_NODE_LOCK
    lock = in_repo if use_repo else (Path(os.path.expanduser(lock_arg)) if lock_arg else None)
    if lock and lock.exists():
        label = f"{lock.resolve().parent.name}/{lock.name}"   # e.g. my-app/package-lock.json
        sources.append((lock.name, _read(lock),
                        lambda t, _s=label: parse_npm_lock(t, source=_s)))
        paths.append(lock)
        meta.append({"label": label, "external": not use_repo})
    else:
        skips.append(f"node-lock: skipped (not found: {lock_arg or '(empty --node-lock)'})")
    return sources, paths, skips, meta


def _parse_trivy_licenses(text):
    """`trivy --scanners license --format json` output -> [(path, licence)] from
    `Results[].Licenses[]`. ValueError when the text is not a trivy report."""
    data = json.loads(text)
    if not isinstance(data, dict) or "Results" not in data:
        raise ValueError("not a trivy report (no Results key)")
    return [(str(lic.get("FilePath") or lic.get("PkgName") or "?"), _norm_license(lic.get("Name")))
            for res in data["Results"] or [] for lic in res.get("Licenses") or []]


def _parse_accepted(text):
    """deep-scan-accepted.txt: `licence<TAB>glob<TAB>reason`, `#` comments."""
    rows = []
    for raw in (text or "").splitlines():
        if raw.strip() and not raw.lstrip().startswith("#"):
            f = (raw.split("\t") + ["", ""])[:3]
            rows.append(tuple(x.strip() for x in f))
    return rows


def _parse_dev_only(text):
    """dev-only.txt: one path glob per line, `#` comments (D240)."""
    return [r.strip() for r in (text or "").splitlines()
            if r.strip() and not r.lstrip().startswith("#")]


_STRONG_TEXT_RE = re.compile(
    r"(?<!Lesser )(?<!Library )(?<![a-z])A?GPL"
    r"|(?:GNU |Affero )(?:General )?Public Licen[cs]e"
    r"|(?<!Lesser )(?<!Library )General Public Licen[cs]e", re.IGNORECASE)


def _strong_denied(lic):
    """True when `lic` is not allowed and GPL/AGPL appears anywhere in its raw text
    (D240; LGPL and Lesser/Library General Public License do not count)."""
    text = " ".join(lic.split())
    return classify(Component("", "", lic, "")) == "denied" and bool(_STRONG_TEXT_RE.search(text))


def _deep_review(findings, accepted, dev_only=()):
    """One line per finding whose class is not allowed, that sits outside every
    dev-only glob (D240: dev tools may use any licence) and that no accepted
    (licence, glob) row covers. D240: an accept line never silences a finding denied
    for GPL/AGPL in a shipped path; it is listed with a note saying so."""
    out = []
    for path, lic in findings:
        if classify(Component(path, "", lic, "trivy")) == "allowed" \
                or any(fnmatchcase(path, g) for g in dev_only):
            continue
        covered = any(lic == a and fnmatchcase(path, g) for a, g, _r in accepted)
        if not covered:
            out.append(f"  {lic} {path}")
        elif _strong_denied(lic):
            out.append(f"  {lic} {path} (accept line ignored: GPL/AGPL in a shipped path "
                       "is never acceptable, D240)")
    return out


def _deep_main(files, root, env):
    if not files:
        print("deep-scan: no report given")
        return 2
    findings = []
    for f in files:
        try:
            findings += _parse_trivy_licenses(Path(f).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            print(f"deep-scan: cannot read report {f}: {e}")
            return 2
    accepted = _parse_accepted(_read(root / "docs" / "licenses" / "deep-scan-accepted.txt"))
    dev_only = _parse_dev_only(_read(root / "docs" / "licenses" / "dev-only.txt"))
    new = _deep_review(findings, accepted, dev_only)
    if not new:
        print(f"deep-scan: clean ({len(findings)} licence finding(s), none new)")
        return 0
    out = [f"deep-scan: {len(new)} new finding(s)"] + new
    print("\n".join(out))
    if env.get("GITHUB_STEP_SUMMARY"):
        with open(env["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write("\n".join(out) + "\n")
    return 1


def main(argv=None, env=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    env = os.environ if env is None else env
    if "--deep-review" in argv:
        return _deep_main(argv[argv.index("--deep-review") + 1:], _repo_root(), env)
    mode, inline, root, node_lock = "--scan", None, _repo_root(), None
    # A `while` with an explicit index, not `enumerate`: a flag's value is consumed and
    # the index advanced past it, so a value that happens to look like a flag (`--node-
    # lock --scan`) is never re-scanned as one.
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--scan", "--write", "--check"):
            mode = a
        elif a == "--inline-dockerfile":
            i += 1
            inline = argv[i] if i < len(argv) else ""
        elif a == "--root":
            i += 1
            if i < len(argv):
                root = Path(argv[i])
        elif a == "--node-lock":
            i += 1
            node_lock = argv[i] if i < len(argv) else ""
        i += 1
    lic_dir = root / "docs" / "licenses"
    sources, paths, skips, meta = _default_sources(root, inline_dockerfile=inline, node_lock=node_lock)
    for s in skips:
        print(f"notice: {s}")
    declared = parse_declared(_read(lic_dir / "declared.json"))
    comps = inventory(sources=sources, declared=declared)
    notice = lic_dir / "NOTICE.md"
    sections = json.loads(_read(lic_dir / SECTIONS_FILE) or "[]")
    paths.append(lic_dir / SECTIONS_FILE)

    if mode == "--write":
        lic_dir.mkdir(parents=True, exist_ok=True)
        text = render_notice(comps, generated=date.today(), texts_dir=lic_dir / "texts",
                             sources=meta, sections=sections)
        tmp = notice.with_suffix(".md.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, notice)
        print(f"notice: wrote {notice.relative_to(root)} ({len(comps)} components)")
        return 0

    if mode == "--check":
        ok, line = check_freshness(notice, paths)
        print(line)
        problems = _provenance_gaps(root) + _scan_doc_stale(root)
        print("\n".join(f"notice: {x}" for x in problems) or "notice: provenance and scan doc ok")
        return 0 if (ok and not problems) or not gate_blocking(env) else 1
    return _scan_mode(root, comps, declared, sources, env)


def _scan_mode(root, comps, declared, sources, env):
    lines, counts = scan_lines(comps)
    names = {c.name for c in comps}
    lines += [f"  {k} - {v} {SOURCE_AVAILABLE} (declared.json)" for k, v in declared.items()
              if v == SOURCE_AVAILABLE and k not in names]
    gaps = _coverage_gaps(root, declared, sources[0][1],
                          [t for _l, t, f in sources if f is parse_pip_licenses])
    print("\n".join(lines + [f"licences: gap - {g}" for g in gaps]))
    bad = counts["denied"] + counts["review"] + len(gaps)
    if bad and not gate_blocking(env):
        print(f"licences: advisory - {bad} component(s) outside the allowlist "
              f"(OPS_LICENSE_GATE=1 to block)")
        return 0
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
