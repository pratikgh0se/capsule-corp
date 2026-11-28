# mutation-check: targets-predate-range ff625e94 -- fixture rename only, no behaviour pinned ::
#   RealTree.test_the_scan_is_not_vacuous
"""Egress contract test (spec 2026-09-09-retention-egress-design.md section 5,
docs/EGRESS.md): data leaves this laptop through exactly three doors - git
pushes (merge_gate.py / vault_git.py), Slack (the outbox sender), and the not-yet-
built encrypted backup. Everything else that could open an outbound
connection must resolve to a host on ALLOWED_HOSTS, and every `git`
push/fetch/pull or `gh` call must live in one of the files that are those
doors (or their supporting scripts - fork-sync, the compose/backup bundle
scripts).

`find_egress(root)` is a plain-text line scanner, not a proof (same spirit as
maintenance/tests/test_contracts.py's `_kanban_argv_lines` - "a tripwire
against the easy regression, not a proof"). It takes `root` so a test can
point it at a temp tree with a planted `https://evil.example.com` and assert
the failure fires; the real tree must come back clean.

Known limitation: a URL built by concatenation from a non-allowlisted
variable is caught only as `<unresolved>`; a literal on the same line wins.
A curl/wget/nc line or a urllib.request/http.client/requests./
socket.create_connection call with no `https?://` literal on its own line
(joined across a shell backslash continuation or a Python call's own parens - see
`_join_shell_continuation`/`_join_py_call`) is reported as host `<NAME>`
only when a single bare identifier sits where the target belongs and NAME
resolves to a literal via `_resolve_name`'s fixed idiom set; anything else -
including a concatenation or f-string expression built from a name that
does not resolve - is reported as `<unresolved>` and fails `classify()`.
That is deliberate: an unrecognised construction is a place to fix (rename
to a literal, or add the name with its default asserted), not a hole the
scanner quietly waves through.
"""
import os, re, shutil, sys, tempfile, tokenize, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from capsule.tests import tmpclean  # noqa: E402
tmpclean.install()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("AGENT_OPS_NO_SEND", "1")

ROOT = Path(__file__).resolve().parents[2]

# --------------------------------------------------------------------- data

ALLOWED_HOSTS = {
    "127.0.0.1", "localhost", "host.docker.internal",
    "github.com", "api.github.com",
    "raw.githubusercontent.com", "hc-ping.com",
    # bin/capsule-ui-up's local front-door names (Host header and echoed URLs, same machine)
    "cc.grafana.localhost", "cc.secrets.localhost",
}

# api.telegram.org is NOT allowed anywhere (D229: Telegram is retired); the planted-host test
# below pins that the scanner reports it as off the allowlist.

# The status/base-URL vars a curl/wget call or a urllib.request/http.client/
# requests./socket.create_connection call is allowed to target with no
# literal of its own on the line: the literal lives elsewhere in this same
# file, reachable from NAME by _resolve_name's fixed idiom chain, and
# classify() asserts that literal resolves to an allowed host before
# passing the hit. Global names apply to every file; PER_FILE_VAR_ALLOW
# below adds names meaningful only in one specific file.
VAR_ALLOW = {"INFISICAL_API_URL", "CAPSULE_SECRET_STATUS_URL",
             "INFISICAL_STATUS_URL", "GRAFANA_URL",
             # deploy.sh's grafana health-check base URL - `${AGENT_OPS_GRAFANA_URL:=http://127.0.0.1:3000}`
             # resolves via the bash-default idiom to the literal 127.0.0.1 default.
             "AGENT_OPS_GRAFANA_URL"}

# secrets_import.py's `api()` helper builds its request in three hops -
# `url = f"{base}{path}"`, `base = base or DEFAULT_BASE` (DEFAULT_BASE is
# the file's own localhost literal), `req = urllib.request.Request(url, ...)`
# - each hop a name _resolve_name can trace via one of its idioms.
PER_FILE_VAR_ALLOW = {
    "secrets_import.py": {"base", "url", "req"},
    # `HEALTH_URL="${UI_HEALTH_URL:-http://127.0.0.1/api/health}"`, the file's own localhost default
    "capsule-ui-up": {"HEALTH_URL"},
}
DEFAULT_LOCAL_HOSTS = ("localhost", "127.0.0.1")

# Door 1 (git push) and its supporting scripts: merge_gate.py/vault_git.py
# push; run.sh's fork-sync section fetches/pushes the upstream fork;
# deploy.sh/backup.sh/restore.sh push or pull a git bundle or the
# live repo as part of a deploy/backup, not agent output.
# bin/capsule-corp.zsh is the owner's interactive shell alias file (its own header
# says "Sourced from ~/.zshrc"): `ops-deploy` runs `git pull --ff-only` of
# the checkout when the owner types it at his own terminal, not unattended.
# bootstrap.sh is the operator-run first-install script: it `git clone`s the
# pinned repos onto a fresh Mac and prints "run 'gh auth login'" as advice to
# the human. Both of its `gh` hits are that shape - a `command -v gh`
# presence loop and an echoed instruction - not an unattended gh call, and it
# pushes nothing.
# new-repo.sh (A2, row 201) is the same shape as bootstrap.sh: the owner runs it
# by hand to create a product repo (design doc 2026-09-12-new-repo-verb-
# design.md §2 - "run by the owner ... the controller never runs it"), never
# unattended, and its own registry commit (maintenance/repos.json) never
# pushes capsule-corp.
GIT_GH_ALLOWED_FILES = {
    "merge_gate.py", "vault_git.py", "run.sh", "deploy.sh",
    "backup.sh", "restore.sh", "capsule-corp.zsh",
    "bootstrap.sh", "new-repo.sh",
}

URL_RE = re.compile(r'https?://[^/"\' )]+')
GIT_RE = re.compile(r'\bgit\s+(?:push|fetch|pull)\b')
GH_PY_RE = re.compile(r'[\[(]\s*["\']gh["\']')  # ["gh", "pr", ...] argv style
GH_SH_RE = re.compile(r'(^|[^A-Za-z0-9_.])gh(\s+\S)')  # `gh pr ...` in shell
# The `(?!\s*:?[-=])` negative lookahead after the *_BIN name keeps a bare
# tool-path default declaration - `: "${CURL_BIN:=curl}"`,
# `CURL_BIN="${CURL_BIN:-curl}"` - from counting as a trigger: that line
# names the binary, it does not call it.
CURL_TRIGGER_RE = re.compile(r'(^|[^A-Za-z0-9_])curl(\s|$)|\$\{?CURL_BIN\}?(?!\s*:?[-=])\b')
WGET_TRIGGER_RE = re.compile(r'(^|[^A-Za-z0-9_])wget(\s|$)|\$\{?WGET_BIN\}?(?!\s*:?[-=])\b')
NC_TRIGGER_RE = re.compile(r'(^|[^A-Za-z0-9_])nc(\s|$)|\$\{?NC_BIN\}?(?!\s*:?[-=])\b')
QUOTED_VAR_ARG_RE = re.compile(r'"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?(?:/[^"]*)?"')
# Only an actual call - not a bare `import urllib.request` or a
# `urlopen = urlopen or urllib.request.urlopen` alias, neither of which
# names a target - counts as a hit.
PY_LIB_CALL_RE = re.compile(
    r'urllib\.request\.(?:urlopen|Request)\('
    r'|http\.client\.\w+\('
    r'|requests\.\w+\('
    r'|socket\.create_connection\('
)

# The two root-level scripts: deploy.sh, and bootstrap.sh - the first-install
# script, which fetches Homebrew's official installer over
# raw.githubusercontent.com. That host is on ALLOWED_HOSTS, so scanning
# bootstrap.sh is what actually exercises it (nothing else in the tree uses
# it) rather than leaving it an unexercised entry.
ROOT_FILES = ("deploy.sh", "bootstrap.sh")

FILE_GLOBS = (
    ("maintenance", "*.py"),
    ("maintenance", "*.sh"),
    ("bin", "*"),
    ("launchd/templates", "*.plist"),
)


def _candidate_files(root):
    root = Path(root)
    files = []
    for name in ROOT_FILES:
        p = root / name
        if p.is_file():
            files.append(p)
    for sub, pattern in FILE_GLOBS:
        d = root / sub
        if d.is_dir():
            files.extend(sorted(p for p in d.glob(pattern) if p.is_file()))
    return files


def _triple_quoted_lines(path):
    """Line numbers fully inside a multi-line triple-quoted Python string
    (module/function docstrings). board_view.py's `regenerate()` docstring
    says "`git pull --ff-only` failing means..." as prose about vault_git's
    behaviour, not an invocation - a real false positive found while
    building this scanner (board_view.py is not on GIT_GH_ALLOWED_FILES).
    Per the controller ruling ("docstrings are fine to include ... if that
    produces false positives in the real tree, exclude lines inside
    triple-quoted strings and say so") this excludes them; only multi-line
    string tokens count, so an ordinary one-line `"http://..."` constant is
    still scanned."""
    excluded = set()
    try:
        with tokenize.open(path) as f:
            for tok in tokenize.generate_tokens(f.readline):
                if tok.type == tokenize.STRING and tok.end[0] > tok.start[0]:
                    excluded.update(range(tok.start[0], tok.end[0] + 1))
    except (SyntaxError, tokenize.TokenError, UnicodeDecodeError, OSError):
        pass
    return excluded


def _host_of(url):
    host = url.split("://", 1)[1].split(":")[0]
    # A regex-pattern string literal like r"https://github\.com/\S+/pull/\d+"
    # (board.py PR_URL_RE, merge_gate.py's _PR_URL_RE, vault_git.py's remote
    # matcher) reads as raw source text with the backslash still in it -
    # unescape so "github\.com" compares equal to "github.com".
    return host.replace("\\", "")


def _how(line):
    if CURL_TRIGGER_RE.search(line):
        return "curl"
    if WGET_TRIGGER_RE.search(line):
        return "wget"
    if NC_TRIGGER_RE.search(line):
        return "nc"
    if GIT_RE.search(line):
        return "git"
    if GH_PY_RE.search(line) or GH_SH_RE.search(line):
        return "gh"
    m = PY_LIB_CALL_RE.search(line)
    if m:
        text = m.group(0)
        if "urllib.request" in text:
            return "urllib.request"
        if "http.client" in text:
            return "http.client"
        if "socket.create_connection" in text:
            return "socket.create_connection"
        if "requests." in text:
            return "requests"
    return "url"


def _join_shell_continuation(lines, idx, max_extra=6):
    """Join physical shell lines starting at 0-based `idx` while the growing
    snippet still ends in a `\\` line continuation - so a curl target that
    lands on the next physical line (a Grafana call that splits
    their URL onto a continuation line) is visible to the same scan."""
    snippet = lines[idx]
    i = idx
    while snippet.rstrip().endswith("\\") and i + 1 < len(lines) and (i - idx) < max_extra:
        i += 1
        snippet = snippet.rstrip()[:-1] + " " + lines[i]
    return snippet


def _join_py_call(lines, idx, start_offset, max_extra=8):
    """Join physical Python lines starting at 0-based `idx` while the open
    parens from `start_offset` onward in the growing snippet outnumber the
    close parens - so a call's first argument that lands on a continuation
    line (topics.py's `urllib.request.Request(\\n    TELEGRAM_API...)`) is
    visible to the same scan. Not brace/string aware - a heuristic over our
    own small, known files, not a parser."""
    snippet = lines[idx]
    depth = snippet[start_offset:].count("(") - snippet[start_offset:].count(")")
    i = idx
    while depth > 0 and i + 1 < len(lines) and (i - idx) < max_extra:
        i += 1
        nxt = lines[i]
        snippet += " " + nxt
        depth += nxt.count("(") - nxt.count(")")
    return snippet


def find_egress(root):
    """Scan maintenance/*.py, maintenance/*.sh, bin/*, deploy.sh and
    launchd/templates/*.plist for outbound-network call sites.

    Returns a list of (file, line, host, how) tuples, `file` relative to
    `root`. `host` is either a hostname/IP pulled from a `https?://` literal,
    or `<NAME>` when only a shell variable names the target (the literal
    lives in that variable's own default elsewhere in the file). Comment
    lines (`#` first non-space char) and multi-line Python docstrings are
    excluded; XML `<!DOCTYPE ...>` lines are excluded too - every plist's
    boilerplate PLIST DOCTYPE line carries a fixed apple.com DTD identifier
    that is parsed locally by launchd, never fetched, and is not egress.

    This does not attempt to resolve every possible network call (it is a
    line-literal scanner, not a data-flow analysis - see the module
    docstring); it is a tripwire for a NEW literal host or a NEW git/gh call
    site outside the allowed files, matching maintenance/tests/
    test_contracts.py's `_kanban_argv_lines` in spirit and in limits.
    """
    root = Path(root)
    hits = []
    for path in _candidate_files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        lines = text.splitlines()
        is_py = path.suffix == ".py"
        skip = _triple_quoted_lines(path) if is_py else set()
        rel = str(path.relative_to(root))

        for lineno, line in enumerate(lines, 1):
            if lineno in skip:
                continue
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "<!DOCTYPE" in line:
                continue

            # `curl`/`wget`/`nc` are shell invocations - a Python local
            # named `nc` (vault_jobs.py's `nc = nightly_commit(vault)`) is
            # not one, so the shell triggers only apply outside .py files,
            # and the Python call patterns only apply inside them.
            if is_py:
                shell_trig = None
                py_call = PY_LIB_CALL_RE.search(line)
            else:
                shell_trig = (CURL_TRIGGER_RE.search(line) or WGET_TRIGGER_RE.search(line)
                              or NC_TRIGGER_RE.search(line))
                py_call = None

            if shell_trig is not None or py_call is not None:
                idx = lineno - 1
                anchor = shell_trig if shell_trig is not None else py_call
                how = _how(line)
                if shell_trig is not None:
                    snippet = _join_shell_continuation(lines, idx)
                else:
                    snippet = _join_py_call(lines, idx, anchor.start())
                tail = snippet[anchor.end():]

                lit = URL_RE.search(tail)
                if lit:
                    hits.append((rel, lineno, _host_of(lit.group(0)), how))
                else:
                    if shell_trig is not None:
                        vm = QUOTED_VAR_ARG_RE.search(tail)
                        name = vm.group(1) if vm else None
                    else:
                        vm = re.match(r'\s*([A-Za-z_]\w*)', tail)
                        name = vm.group(1) if vm else None
                    hits.append((rel, lineno, f"<{name}>" if name else "<unresolved>", how))
            else:
                for m in URL_RE.finditer(line):
                    hits.append((rel, lineno, _host_of(m.group(0)), _how(line)))

            # git push/fetch/pull and gh, independent of any URL match above
            # (a merge_gate.py line can carry both, e.g. `what="git fetch"`).
            if GIT_RE.search(line):
                hits.append((rel, lineno, "git", "git"))
            if GH_PY_RE.search(line) or GH_SH_RE.search(line):
                hits.append((rel, lineno, "gh", "gh"))

    return hits


def _literal_host(text, name):
    """NAME's own `NAME = "https?://..."` assignment, host extracted with
    the same URL_RE (and so the same '/' / quote / space cutoff) used for
    every other literal in this scanner - not a wider slice, so
    `TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"`
    resolves to `api.telegram.org`, not `api.telegram.org/bot`."""
    m = re.search(r'\b' + re.escape(name) + r'\s*=\s*["\'](https?://[^"\']*)', text)
    if not m:
        return None
    um = URL_RE.match(m.group(1))
    return _host_of(um.group(0)) if um else None


def _resolve_name(text, name, seen=None):
    """Trace NAME back to the literal `https?://` URL it ultimately comes
    from, in this file's own text, via a fixed, small set of idioms this
    codebase actually uses:

      - a direct literal assignment (`DEFAULT_BASE = "http://localhost..."`,
        `TELEGRAM_API = "https://api.telegram.org/..."`);
      - a bash default (`${NAME:-default}`, `${NAME:=default}` or `NAME="${X:-default}"`), chaining
        through another VAR_ALLOW name when the default has no literal of
        its own (`CAPSULE_SECRET_STATUS_URL`'s default is
        `${INFISICAL_API_URL}/status`);
      - a Python fallback (`base = base or DEFAULT_BASE`);
      - a Python f-string built from one name (`url = f"{base}{path}"`);
      - a value assigned from a call's first argument
        (`req = urllib.request.Request(url, ...)`).

    This is not a data-flow engine - each idiom is one fixed regex over raw
    text, so a name built any other way (concatenation, an f-string with
    more than one name, an argument that is not the literal first token)
    resolves to None, which the caller treats as a violation. That is the
    documented limitation: a literal on the same line always wins; anything
    else has to be one of these exact shapes or it does not resolve."""
    seen = seen if seen is not None else set()
    if name in seen:
        return None
    seen.add(name)

    host = _literal_host(text, name)
    if host:
        return host

    m = (re.search(r'\$\{' + re.escape(name) + r'(?::?[-=])([^}]*)\}', text)
         or re.search(r'\b' + re.escape(name) + r'="\$\{\w+:?[-=]([^}]*)\}"', text))  # X="${Y:-d}"
    if m:
        default = m.group(1)
        for other in VAR_ALLOW:
            if other != name and other in default:
                h = _resolve_name(text, other, seen)
                if h:
                    return h
        for h in DEFAULT_LOCAL_HOSTS:
            if h in default:
                return h

    m = re.search(r'\b' + re.escape(name) + r'\s*=\s*' + re.escape(name) + r'\s+or\s+([A-Za-z_]\w*)', text)
    if m:
        return _resolve_name(text, m.group(1), seen)

    m = re.search(r'\b' + re.escape(name) + r'\s*=\s*f["\']\{(\w+)\}', text)
    if m:
        return _resolve_name(text, m.group(1), seen)

    m = re.search(r'\b' + re.escape(name) + r'\s*=\s*[\w.]+\(\s*(\w+)', text)
    if m:
        return _resolve_name(text, m.group(1), seen)

    return None


def classify(root, file, line, host, how):
    """Return (ok, reason) for one find_egress() hit."""
    root = Path(root)
    filename = Path(file).name

    if how in ("git", "gh"):
        if filename in GIT_GH_ALLOWED_FILES:
            return True, ""
        return False, f"{how} invocation outside the allowed door files"

    if host.startswith("<") and host.endswith(">"):
        name = host[1:-1]
        if name == "unresolved":
            return False, "no literal and no resolvable name on this line"
        allowed_names = VAR_ALLOW | PER_FILE_VAR_ALLOW.get(filename, set())
        if name not in allowed_names:
            return False, "unresolved variable target, not an allowed name for this file"
        text = (root / file).read_text(encoding="utf-8")
        resolved = _resolve_name(text, name)
        if resolved is None:
            return False, f"{name} does not resolve to a literal host in this file"
        if resolved in ALLOWED_HOSTS:
            return True, ""
        return False, f"{name} resolves to {resolved}, not on the allowlist"

    if host in ALLOWED_HOSTS:
        return True, ""
    return False, "host not on the allowlist"


def _violations(root):
    out = []
    for file, line, host, how in find_egress(root):
        ok, reason = classify(root, file, line, host, how)
        if not ok:
            out.append(f"{file}:{line} {host} ({how}) - {reason}")
    return out


class RealTree(unittest.TestCase):
    """The enforcement test: nothing under maintenance/, bin/, deploy.sh or
    the plists reaches a host outside ALLOWED_HOSTS, and no git push/fetch/
    pull or gh call sits outside the door files. Do not add a host or a file
    to make this pass - a real hit here means the change under review is the
    thing that needs fixing (per the controller ruling)."""

    def test_real_tree_has_no_egress_violations(self):
        violations = _violations(ROOT)
        self.assertEqual(violations, [], "\n" + "\n".join(violations))

    def test_the_scan_is_not_vacuous(self):
        """The scan must not be able to pass by finding nothing anywhere -
        it should see the doors it is guarding, not just miss everything."""
        hits = find_egress(ROOT)
        hosts = {h for _, _, h, _ in hits}
        files_by_how = {}
        for file, _, _, how in hits:
            files_by_how.setdefault(how, set()).add(file)
        self.assertNotIn("api.telegram.org", hosts)
        self.assertIn("hc-ping.com", hosts)
        self.assertIn("github.com", hosts)
        # bootstrap.sh's Homebrew installer curl - the only user of this host
        # in the tree, and the reason bootstrap.sh is scanned at all.
        if (ROOT / "bootstrap.sh").exists():   # private tree only; not in the public release
            self.assertIn("raw.githubusercontent.com", hosts)
        self.assertIn("maintenance/run.sh", files_by_how.get("git", set()))
        self.assertIn("bin/new-repo.sh", files_by_how.get("gh", set()))

    def test_hc_ping_lives_in_exactly_one_file(self):
        """The dead-man ping is one door in one place (docs/EGRESS.md door 4).
        A second caller means a second thing to audit, and the host allowlist
        cannot tell them apart - so pin the file, the way the git door
        pins its files."""
        files = {f for f, _, h, _ in find_egress(ROOT) if h == "hc-ping.com"}
        self.assertEqual(files, {"maintenance/run.sh"})


class PlantedFixtures(unittest.TestCase):
    """The scanner must actually fire on a planted violation, in an isolated
    temp tree the real repo never sees."""

    def _tree(self):
        d = Path(tempfile.mkdtemp())
        (d / "maintenance").mkdir()
        (d / "bin").mkdir()
        (d / "launchd" / "templates").mkdir(parents=True)
        return d

    def test_a_planted_external_host_fails(self):
        d = self._tree()
        (d / "maintenance" / "offender.py").write_text(
            'URL = "https://evil.example.com/exfil"\n'
        )
        violations = _violations(d)
        self.assertEqual(len(violations), 1, violations)
        self.assertIn("offender.py:1 evil.example.com", violations[0])

    def test_a_planted_url_concatenation_from_an_unresolved_name_fails(self):
        """The bug this fix closes: a urllib.request call whose URL is built
        by concatenation from a variable that is not one of the allowed
        names must not be waved through - it has no literal on its own line
        for the scanner to check, so it must come back <unresolved>, not
        silently vouched for by some other host already found in the file."""
        d = self._tree()
        (d / "maintenance" / "offender.py").write_text(
            'import urllib.request\n'
            'GOOD = "http://localhost:9/ok"\n'
            'def leak(host):\n'
            '    urllib.request.urlopen("https://" + host + "/x")\n'
        )
        violations = _violations(d)
        self.assertEqual(len(violations), 1, violations)
        self.assertIn("offender.py:4", violations[0])
        self.assertIn("unresolved", violations[0])

    def test_a_planted_git_push_outside_the_door_files_fails(self):
        d = self._tree()
        (d / "maintenance" / "sneaky.sh").write_text(
            '#!/usr/bin/env bash\n'
            'git push origin HEAD\n'
        )
        violations = _violations(d)
        self.assertEqual(len(violations), 1, violations)
        self.assertIn("sneaky.sh:2 git", violations[0])

    def test_a_planted_telegram_host_fails_anywhere(self):
        d = self._tree()
        (d / "maintenance" / "other.py").write_text(
            'TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"\n'
        )
        violations = _violations(d)
        self.assertEqual(len(violations), 1, violations)
        self.assertIn("other.py:1 api.telegram.org", violations[0])

    def test_allowed_hosts_and_door_files_produce_no_violation(self):
        d = self._tree()
        (d / "maintenance" / "run.sh").write_text(
            '#!/usr/bin/env bash\n# fork-sync\ngit push -q origin HEAD\n'
        )
        (d / "bin" / "tool").write_text(
            '#!/usr/bin/env bash\ncurl -fsS http://127.0.0.1:3000/health\n'
        )
        self.assertEqual(_violations(d), [])

    def test_a_docstring_only_mention_does_not_fail(self):
        """Mirrors the real board_view.py false positive this scanner had to
        rule out: prose about `git pull` inside a docstring, in a file that
        is not a door file, must not be treated as an invocation."""
        d = self._tree()
        (d / "maintenance" / "prose.py").write_text(
            'def f():\n'
            '    """This talks about `git pull --ff-only` failing, as prose,\n'
            '    not as an invocation."""\n'
            '    return 1\n'
        )
        self.assertEqual(_violations(d), [])

    def test_doctype_boilerplate_does_not_fail(self):
        d = self._tree()
        (d / "launchd" / "templates" / "com.example.plist").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0"></plist>\n'
        )
        self.assertEqual(_violations(d), [])


# Door 5 (docs/EGRESS.md): the host-side image build fetches packages in. Build time only: these
# hosts are deliberately NOT in ALLOWED_HOSTS, so no runtime or maintenance code may use them.
DOOR5_HOSTS = {
    "registry-1.docker.io", "auth.docker.io", "production.cloudflare.docker.com",
    "snapshot.debian.org", "pypi.org", "files.pythonhosted.org", "registry.npmjs.org",
}
FROM_RE = re.compile(r'^\s*(?:FROM|COPY\s+--from=)\s*(?:--\S+\s+)*(\S+)', re.I | re.M)
# `# syntax=<image>` on a Dockerfile's first lines is a comment to Docker but BuildKit PULLS that
# image (the frontend), so it counts as an image the build fetches.
SYNTAX_RE = re.compile(r'^\s*#\s*syntax\s*=\s*(\S+)', re.I | re.M)


def images_in(text):
    """Image references a Dockerfile pulls (FROM, COPY --from and a `# syntax=` frontend), minus
    its own stage names."""
    stages = {a.lower() for a in re.findall(r'^\s*FROM\s+\S+\s+AS\s+(\S+)', text, re.I | re.M)}
    return [r for r in FROM_RE.findall(text) if r.lower() not in stages] + SYNTAX_RE.findall(text)


def build_files(root):
    root = Path(root) / "docker"
    files = sorted(root.glob("base/Dockerfile")) + sorted(root.glob("stacks/*/Dockerfile"))
    return files + sorted(root.glob("stacks/*/package-lock.json")) + [root / "build-base.sh"]


def build_hosts(root):
    """{(file, host)} for every URL host a build input names (Dockerfiles, build-base.sh, the npm
    lock's `resolved` URLs; comment lines are prose) plus an explicit registry host in a
    FROM, COPY --from or `# syntax=` image reference."""
    found = set()
    for f in build_files(root):
        if not f.is_file():
            continue
        name = f.relative_to(Path(root) / "docker").as_posix()
        full = f.read_text()
        text = "\n".join(ln for ln in full.splitlines() if not ln.lstrip().startswith("#"))
        found |= {(name, _host_of(m.group(0))) for m in URL_RE.finditer(text)}
        for ref in images_in(full):
            first = ref.split("/")[0]
            if "/" in ref and ("." in first or ":" in first):
                found.add((name, first))
    return found


class BuildDoor5(unittest.TestCase):
    """The image build is door 5 (docs/EGRESS.md): every host a build input names is on
    DOOR5_HOSTS, the list shares nothing with the runtime allowlist, and the doc names each host."""

    def test_every_build_host_is_on_the_door_5_list(self):
        seen = build_hosts(ROOT)
        self.assertTrue(seen, "the scan found no build hosts at all")
        self.assertEqual({h for _, h in seen} - DOOR5_HOSTS, set())

    def test_the_scan_sees_the_hosts_the_builds_use(self):
        self.assertIn("snapshot.debian.org", {h for _, h in build_hosts(ROOT)})

    def test_door_5_shares_no_host_with_the_runtime_allowlist(self):
        self.assertEqual(DOOR5_HOSTS & ALLOWED_HOSTS, set())

    def test_a_planted_host_in_a_dockerfile_or_lock_is_caught(self):
        d = Path(tempfile.mkdtemp(prefix="door5-"))
        self.addCleanup(shutil.rmtree, d, True)
        (d / "docker" / "stacks" / "x").mkdir(parents=True)
        (d / "docker" / "stacks" / "x" / "Dockerfile").write_text(
            "FROM ghcr.io/evil/img@sha256:" + "a" * 64 + "\nRUN true # https://evil.example/x\n")
        (d / "docker" / "stacks" / "x" / "package-lock.json").write_text(
            '{"resolved": "https://cdn.playwright.dev/chromium.zip"}')
        got = {h for _, h in build_hosts(d)}
        self.assertEqual(got, {"ghcr.io", "evil.example", "cdn.playwright.dev"})

    def test_the_doc_names_door_5_and_each_host(self):
        text = (ROOT / "docs" / "EGRESS.md").read_text()
        self.assertIn("Door 5, the image build", text)
        for host in DOOR5_HOSTS:
            self.assertIn(f"`{host}`", text)
        self.assertNotIn("`cdn.playwright.dev` (the", text.split("Not on the list")[0])

    def test_every_from_is_pinned_by_digest(self):
        for f in build_files(ROOT):
            if f.name == "Dockerfile":
                refs = images_in(f.read_text())
                self.assertTrue(refs, f)
                unpinned = [r for r in refs if "@sha256:" not in r]
                self.assertEqual(unpinned, [], f"{f}: {unpinned}")

    def test_a_syntax_directive_is_an_image_the_build_pulls(self):
        d = Path(tempfile.mkdtemp(prefix="door5-"))
        self.addCleanup(shutil.rmtree, d, True)
        (d / "docker" / "base").mkdir(parents=True)
        (d / "docker" / "base" / "Dockerfile").write_text(
            "# syntax=registry.example.net/frontend:1\nFROM debian@sha256:" + "a" * 64 + "\n")
        self.assertEqual(images_in((d / "docker" / "base" / "Dockerfile").read_text())[-1],
                         "registry.example.net/frontend:1")
        self.assertEqual({h for _, h in build_hosts(d)}, {"registry.example.net"})

    def test_a_copy_from_a_stage_name_is_not_an_image_but_an_unpinned_image_is(self):
        text = "FROM node:26 AS node\nFROM capsule-base@sha256:" + "a" * 64 + "\n"
        self.assertEqual(images_in(text + "COPY --from=node /a /b\n"),
                         ["node:26", "capsule-base@sha256:" + "a" * 64])
        self.assertEqual(images_in(text + "COPY --from=redis:7 /a /b\n")[-1], "redis:7")


if __name__ == "__main__":
    unittest.main()
