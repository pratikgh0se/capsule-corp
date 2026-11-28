"""Seatbelt profile renderer for the light runner tier (rows 373, 378, 395; spec §6.6).

`render` only produces profile text (a pure function of its arguments plus the worktree's
realpath), so the policy is reviewable before anything executes under it. `probe` is the one
helper that runs `sandbox-exec`, for the live tests.

Policy: default deny; read+write only under the worktree realpath (macOS `/tmp`, `/var` are
symlinks into `/private`, and the sandbox matches resolved paths); write-deny for every
`workspace.PROTECTED` path inside the worktree (row 395 layer 1); read-only for the vault scope
and a short list of system paths any process needs to start; network only to the endpoints given.
Seatbelt's network filter takes the host `*` or `localhost` only (a hostname or an IP literal is a
profile syntax error), so an endpoint is `localhost:<port>` (a host-side allow-list proxy); any
other form is refused rather than silently widened (`*:443` would allow every host).

Rename guard: a write-deny on `capsule/intent.py` alone lets a worker `mv capsule capsule_old`
and plant a new `capsule/`, so every proper ancestor directory of a fixed-prefix PROTECTED glob
is also denied for write, `$`-anchored: the directory itself cannot be renamed, unlinked or
removed, while creating its children stays allowed. Residual risk: `**/CLAUDE.md`,
`**/SOUL*.md` and `**/AGENTS.md` have no fixed prefix, so a worker can still rename a whole
directory that already holds one of those files; layer 2 (lander.py, tick.py) catches that.
Metadata (`stat`) is allowed everywhere for path lookup, so denies of secrets name
`file-read-metadata` too: a specific operation's allow beats a `file-read*` deny.
"""
import os
import re
import subprocess
import tempfile
from pathlib import Path

from capsule import workspace

# Reads any process needs to start (dyld, libc, tzdata, tool shims); everything else is denied.
SYSTEM_READ = ("/usr", "/bin", "/sbin", "/System", "/Library", "/private/etc", "/dev",
               "/private/var/db/dyld", "/private/var/db/timezone")
# Denied for read wherever they sit, even inside the worktree or the vault scope: secrets and
# the human's media. Home-relative entries; the `*.env` file patterns are built in `render`.
DENY_READ_HOME = (".config/capsule", "Movies")

_ENDPOINT = re.compile(r"^localhost:([0-9]{1,5})$")


def _q(path):
    """A path as an SBPL string literal."""
    s = str(path)
    if '"' in s or "\n" in s:
        raise ValueError(f"unsafe character in profile path: {s!r}")
    return '"' + s.replace("\\", "\\\\") + '"'


def _esc(text, ci=False):
    """Escape `text` for a POSIX ERE. `ci`: letters match either case (the usual macOS volume is
    case-insensitive and `touches_protected` casefolds, so `Contracts/` must match `contracts/`)."""
    return "".join(f"[{c.lower()}{c.upper()}]" if ci and c.isalpha() else
                   "\\" + c if c in ".[](){}\\*+?|^$" else c for c in text)


def _glob_regex(root, glob):
    """A PROTECTED glob under `root` as an anchored regex: `**/` any directory prefix (so a
    root-level file matches too), `/**` the directory and everything below, `*` anything."""
    head, tail = "", ""
    if glob.startswith("**/"):
        head, glob = "(.*/)?", glob[3:]
    if glob.endswith("/**"):
        tail, glob = "(/.*)?", glob[:-3]
    body = ".*".join(_esc(part, True) for part in glob.split("*"))
    return f'#"^{_esc(root)}/{head}{body}{tail}$"'


def _endpoint(ep):
    m = _ENDPOINT.match(ep)
    if not m or not 0 < int(m.group(1)) < 65536:
        raise ValueError(f"endpoint must be localhost:<port>, got {ep!r} "
                         "(seatbelt cannot filter by hostname or IP; use a localhost proxy)")
    return f'(allow network-outbound (remote tcp {_q(ep)}))'


def _ancestors(globs):
    """Proper ancestor directories (relative) of each glob's fixed prefix, in first-seen order:
    `capsule/blind/harness/**` gives `capsule` and `capsule/blind`; a name pattern such as
    `capsule/prompts/brainstorm*` guards its whole fixed directory, `capsule/prompts`, too. A glob
    that starts with a wildcard has no fixed prefix and gives none."""
    seen = []
    for g in globs:
        parts = g.split("/")
        fixed = parts[:next((i for i, p in enumerate(parts) if "*" in p), len(parts))]
        name_glob = len(fixed) == len(parts) - 1 and parts[-1] != "**"   # `dir/name*`: dir too
        for i in range(1, len(fixed) + name_glob):
            if "/".join(fixed[:i]) not in seen:
                seen.append("/".join(fixed[:i]))
    return seen


def guarded_dirs():
    """The directories (relative) the rename guard protects, parents before children (a parent is
    always listed before its child, as `_ancestors` emits each glob's prefixes shortest first).
    The guard also blocks creating one of these, so the light runner creates them first."""
    return _ancestors(workspace.PROTECTED)


def _denies(wt, home, deny_read):
    """The closing deny rules. They come last, so they beat every allow of the same operation
    above; `file-read-metadata` has its own global allow, so the secret denies name it too."""
    lines = [";; denies come last: same-operation allows above lose to them (row 395 layer 1)"]
    lines.extend(f"(deny file-write* (regex {_glob_regex(wt, g)}))" for g in workspace.PROTECTED)
    lines.append(";; rename guard: the directories above a protected path cannot be moved")
    lines.extend(f"(deny file-write* (regex {_glob_regex(wt, a)}))" for a in guarded_dirs())
    # x.env, .env, .env.local, a.env.local: unreadable, unwritable (so not renamable to a new name)
    env = [f'(regex #"{_esc(".env", True)}$")', f'(regex #"{_esc(".env.", True)}[^/]+$")']
    lines.append(";; env files: no read, no write (mv .env x would leak it)")
    lines.append("(deny file-read* file-read-metadata file-write* " + " ".join(env) + ")")
    denied = [f"(subpath {_q(os.path.join(home, p))})" for p in DENY_READ_HOME]
    denied += [f"(subpath {_q(os.path.realpath(p))})" for p in deny_read]
    lines.append(";; secrets and media: no read, no stat")
    lines.append("(deny file-read* file-read-metadata " + " ".join(denied) + ")")
    return lines


def _xcode_contents(dev):
    """`dev`'s parent, `Xcode.app/Contents`; the grants built on it need this shape."""
    if not dev.endswith(".app/Contents/Developer"):
        raise ValueError(f"not an Xcode developer dir (.app/Contents/Developer): {dev}")
    return os.path.dirname(dev)


def _mac_lines(developer_dir):
    """The Mac tier's extra rules (see `render`)."""
    dev = os.path.realpath(developer_dir)
    app = _xcode_contents(dev)          # Xcode.app/Contents
    shared, frameworks, plist = (_q(os.path.join(app, n))
                                 for n in ("SharedFrameworks", "Frameworks", "Info.plist"))
    return [";; MAC LANE ADDITIONS (D305): present only in the Mac tier's profile",
            f"(allow file-read* (subpath {_q(dev)}) (subpath {shared})"
            f" (subpath {frameworks}) (literal {plist}))",
            '(allow iokit-open-user-client (iokit-user-client-class "AGXDeviceUserClient"))',
            '(allow mach-lookup (global-name "com.apple.MTLCompilerService"))',
            ";; offscreen GPU render for Mac app cards (SwiftUI ImageRenderer, IOSurface)",
            '(allow iokit-open-user-client'
            ' (iokit-user-client-class "IOSurfaceRootUserClient"))']


def render(worktree, vault_read=(), endpoints=(), scratch=".worker-exec", *,
           home=None, deny_read=(), developer_dir=None):
    """The `(version 1)` seatbelt profile text for a card running in `worktree`.

    `vault_read`: extra read-only subpaths. `endpoints`: `localhost:<port>` strings; none means
    no network. `scratch`: a relative dir under the worktree (TMPDIR, row 357), checked here;
    the worktree allow already covers it.
    `home`: whose `~/.config/capsule` and `~/Movies` are denied (default the current user's);
    `deny_read`: further read-deny subpaths on top of the defaults.
    `developer_dir`: the Mac lane's profile (D305, contract B5): the light profile plus read-only
    toolchain grants, GPU and compiler-service access, and still NO network (no listening socket
    either: seatbelt's `local` filter does not limit the bind address). (1) read of the selected
    Xcode's Developer dir (toolchain, SDKs), its sibling SharedFrameworks (swift-driver dlopens
    llbuild.framework there), and `Contents/Frameworks` and `Contents/Info.plist` (xcrun and the
    /usr/bin shims fall back to `xcodebuild -find`, which needs them); all derive from
    `xcode-select -p`, never a fixed path. (2) iokit-open-user-client for the AGX GPU client only
    (SceneKit's Metal device). (3) mach-lookup of com.apple.MTLCompilerService only (without it
    renders come out empty). (4) iokit-open-user-client for IOSurfaceRootUserClient only (offscreen
    SwiftUI renders; without it the PNG is one flat colour).
    No `(allow default)`, no other mach-lookup. `endpoints` is the only
    way out of the no-network rule, as in the light profile; the closing denies still come last.
    None is the light profile, byte for byte.
    """
    wt = os.path.realpath(worktree)
    if os.path.isabs(scratch) or ".." in Path(scratch).parts or scratch in ("", "."):
        raise ValueError(f"scratch must be a relative dir inside the worktree, got {scratch!r}")
    home = os.path.realpath(home if home is not None else Path.home())
    lines = [
        "(version 1)",
        ";; Capsule light runner (rows 373, 378, 395). Rendered by capsule/runners/seatbelt.py;"
        " never hand-edit.",
        "(deny default)",
        ";; process basics, and the read-only system paths a process needs to start",
        "(allow process-exec* process-fork)",
        "(allow signal (target self))",
        "(allow sysctl-read)",
        "(allow file-read-metadata)",
        '(allow file-read-data (literal "/"))',
        "(allow file-read* " + " ".join(f"(subpath {_q(p)})" for p in SYSTEM_READ) + ")",
        '(allow file-write* (literal "/dev/null") (literal "/dev/dtracehelper"))',
        ";; the worktree (its scratch dir is inside it)",
        f"(allow file-read* file-write* (subpath {_q(wt)}))",
    ]
    if vault_read:
        lines.append(";; vault read scope")
        vaults = " ".join(f"(subpath {_q(os.path.realpath(v))})" for v in vault_read)
        lines.append(f"(allow file-read* {vaults})")
    if developer_dir:
        lines += _mac_lines(developer_dir)
    lines.append(";; network (row 373): only these endpoints")
    lines.extend(_endpoint(ep) for ep in endpoints)
    lines.extend(_denies(wt, home, deny_read))
    return "\n".join(lines) + "\n"


def probe(profile, script, timeout=15):
    """Run `/bin/sh -c script` under `profile` with the real `sandbox-exec`; returns the
    CompletedProcess. A missing `sandbox-exec` raises (never a silent pass, Z4)."""
    with tempfile.TemporaryDirectory() as d:
        sb = os.path.join(d, "profile.sb")
        Path(sb).write_text(profile, encoding="utf-8")
        return subprocess.run(["/usr/bin/sandbox-exec", "-f", sb, "/bin/sh", "-c", script],
                              capture_output=True, text=True, timeout=timeout)
