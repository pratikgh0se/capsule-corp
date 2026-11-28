"""A failed docker command as text safe to store: its argv (a list, or a shell string) with each
mount's host source masked (`<masked>:target`; a named volume stays), plus a tail of docker's own
output with those sources masked too. The raw command stays with the caller (the tick's log).
`incident_blind.masked` calls `text`."""
import copy
import os
import re
import shlex
import subprocess

TAIL = 2000
_GLOBAL_VALUE = {"-H", "--host", "--config", "-c", "--context", "-l", "--log-level",
                 "--tlscacert", "--tlscert", "--tlskey"}
_FLAG = re.compile(r"(--volume|--mount)(?:=(.*))?\Z|(-[dituPq]*v)(.*)\Z", re.DOTALL)
_KEPT = {"type", "target", "destination", "dst"}


def _body(args):
    """Index of the first argument after the docker subcommand (`container run` is one; global
    options skipped); None when `args` is not a docker command."""
    if not args or os.path.basename(args[0]) != "docker":
        return None
    words, i = [], 1
    while i < len(args) and len(words) < 2:
        if args[i].startswith("-"):
            i += 2 if args[i] in _GLOBAL_VALUE else 1
            continue
        words.append(args[i])
        i += 1
        if words[0] != "container":
            break
    return min(i, len(args))


def _host(source):
    """True for a host path (`/..`, `~..`, `./..`, or any with a `/`); a named volume is not."""
    return source[:1] in ("/", "~", ".") or "/" in source


def _volume(spec, sources):
    """`SRC:DST[:opts]` as `<masked>:DST[:opts]`: the source is all before the last `:/DST`; a
    named volume or an anonymous one has no host source and stays."""
    parts = spec.split(":")
    last = max((i for i, p in enumerate(parts) if p.startswith("/")), default=0)
    source = ":".join(parts[:last])
    if not last or not _host(source):
        return spec
    sources.append(source)
    return "<masked>:" + ":".join(parts[last:])


def _mount(spec, sources):
    """`--mount` values as `<masked>:target` (every value but the type and target dropped) when
    any other value is a host path (`source`, `volume-subpath`, `volume-opt=device=..`, ...) or
    it is a bind; else it stays."""
    got = [part.partition("=") for part in spec.split(",")]
    paths = [val.partition("=")[2] if key == "volume-opt" and "=" in val else val
             for key, _, val in got if key not in _KEPT and val]
    host = [p for p in paths if _host(p)]
    if not host and ("type", "=", "bind") not in got:
        return spec
    sources.extend(host)
    target = next((v for k, _, v in got if k in _KEPT - {"type"} and v), None)
    return "<masked>" + (f":{target}" if target else "")


def _argv(args):
    """`(argv, sources)`: `args` with each mount spec (`-v`, `-v=`, `-v/h:/c`, `-dv`, `--volume`,
    `--mount`) masked, and every host source; None when it is not a docker command."""
    start = _body(args)
    if start is None:
        return None
    out, sources, pending = args[:start], [], None
    for arg in args[start:]:
        m = _FLAG.match(arg)
        if pending:
            arg, pending = _spec(pending, arg, sources), None
        elif m:
            name, sep, val = ((m[1], "=" if m[2] is not None else "", m[2] or "") if m[1] else
                              (m[3], "=" if m[4][:1] == "=" else "", m[4].removeprefix("=")))
            if val:
                arg = f"{name}{sep}{_spec(name, val, sources)}"
            elif not sep:
                pending = name
        out.append(arg)
    return out, sources


def _spec(flag, spec, sources):
    return (_mount if flag == "--mount" else _volume)(spec, sources)


def _hide(text, hide):
    """`text` with each key of `hide` (longest first, none shorter than 2 characters) replaced."""
    for old in sorted((o for o in hide if len(o) > 1), key=len, reverse=True):
        text = text.replace(old, hide[old])
    return text


def _tail(exc, hide):
    """The last `TAIL` characters of docker's stderr and output, `hide` applied before the cut."""
    parts = [p.decode("utf-8", "replace") if isinstance(p, bytes) else p
             for p in (getattr(exc, "stderr", None), getattr(exc, "output", None)) if p]
    return _hide("\n".join(parts), hide)[-TAIL:]


def _split(cmd):
    """A command (an argv, or a shell string: `shlex`, else whitespace) as a list of strings."""
    if isinstance(cmd, str):
        try:
            return shlex.split(cmd)
        except ValueError:
            return cmd.split()
    return [str(a) for a in cmd] if isinstance(cmd, (list, tuple)) else []


def text(exc, blind=None):
    """The message of a failed docker command (`CalledProcessError`, `TimeoutExpired`) with its
    mounts masked, then the tail of its output; None for any other exception. Every host mount
    source becomes `<masked>`, and each `blind` path (`{path: its mask}`) its mask, in both."""
    if not isinstance(exc, (subprocess.CalledProcessError, subprocess.TimeoutExpired)):
        return None
    cmd = getattr(exc, "cmd", None)
    got = _argv(_split(cmd))
    if got is None:
        return None
    shown = copy.copy(exc)
    shown.cmd = (shlex.join(got[0]) if isinstance(cmd, str) else got[0])
    tail = _tail(exc, {**{s: "<masked>" for s in got[1]}, **(blind or {})})
    return _hide(str(shown), blind or {}) + (f"\n{tail}" if tail else "")
