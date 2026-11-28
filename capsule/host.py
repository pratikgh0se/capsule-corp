"""`$CAPSULE_HOME/host.json`: the host-level settings (public portable edition, P05a).

Precedence per key: env var > host.json > built-in default; an empty env var is unset. A missing
file is all defaults; an unknown key, a key set twice or a wrongly typed value is refused
(HostError), never ignored, and a refusal never echoes a value (it may be a secret pasted into the
wrong field). The file holds names and paths only. `launchd.label_prefix` and `secrets.backend`
default to None, shown as `auto`: the caller decides (bin/lib_prefix.sh falls back to dev.capsule,
bin/capsule-secret to its own default), so the check row never claims a value that is not in
effect. Shell callers: `capsule host get launchd.label_prefix --only`, empty meaning unset.
`capsule check` prints every effective value with its source.
"""
import copy
import json
import os
import re
import stat
import sys
from pathlib import Path

from capsule import answer_base, jobs_dir, paths

PREFIX_RE = re.compile(r"[A-Za-z0-9.-]+")     # as bin/lib_prefix.sh
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")  # a repo/board name: no space, glob, ../
_HOST, _TAIL = r"[A-Za-z0-9.-]+", r"[\w./~-]+"      # no user:token@, no shell metacharacters
URL_RE = re.compile(rf"https://{_HOST}(:\d+)?/{_TAIL}|git@{_HOST}:{_TAIL}"
                    rf"|ssh://(git@)?{_HOST}(:\d+)?/{_TAIL}")
OWNER_RE, ROLES = answer_base.OWNER_RE, answer_base.ROLES    # one source, in the protected base
BACKENDS = ("file", "infisical", "env")
AUTO = ("launchd.label_prefix", "secrets.backend")

# dotted key -> (default, kind, env var that beats the file or None); "?" = null is allowed
KEYS = {
    "repo": (str(paths.REPO), "path", None),
    "launchd.label_prefix": (None, "prefix?", "CAPSULE_LAUNCHD_PREFIX"),
    "secrets.backend": (None, "backend?", "CAPSULE_SECRETS_BACKEND"),
    "config_dir": (None, "path?", "CAPSULE_JOBS_DIR"),   # the jobs' runtime dir (env files)
    "vault.path": (None, "path?", "OBSIDIAN_VAULT_PATH"),
    "vault.skill_root": (None, "path?", "OBSIDIAN_SKILL_ROOT"),   # the vault checker's checkout
    # notes-vault features are opt-in (D254): they run only when vault.path (or the env var) is set
    "vault.decision_notes": ("Decisions/*decisions*.md", "glob", None),   # a Decisions/ note
    "vault.push_allow": ([
        "Ledger/", "Boards/", "Specs/", "Decisions/", "Architecture/", "Reviews/", "Projects/",
        "Reports/", "Inbox/", "Debugging/", "Dev Logs/", "Knowledge/", "Daily/", "Tasks/",
        "Brainstorms/", "Pitches/"], "strs", None),
    # owner decisions the ask gate may cite (files or globs; relative ones sit under vault.path)
    "diligence.ledgers": (["Decisions/*decisions*.md"], "strs", None),
    "observability.enabled": (True, "bool", None),
    "tailscale.serve": (False, "bool", None),
    # project lists (public-release prep, D30): the owner's own repos live here, not in code
    "repos": ({"capsule-corp": str(paths.REPO)}, "map", None),   # name -> checkout path
    "repo_urls": ({}, "urlmap", None),    # name -> git URL (release links, bootstrap clones)
    "quality.repos": (["capsule-corp"], "names", None),       # nightly quality rotation
    "secrets.projects": (["capsule-corp"], "names", None),    # secret-store project slugs
    "observability.components": ([], "names", None),  # extra coverage-matrix rows
    # the owner's name in decision-row text and author aliases
    "owner.name": ("owner", "owner", "CAPSULE_OWNER_NAME"),
    # the legal name on generated LICENSEs; "" = use owner.name
    "owner.copyright": ("", "owner", None),
    "boards.default": ("capsule-corp", "str", None),         # board a bare `capsule tick` serves
    "maintenance.scan_repos": ([], "names", None),           # repos the architect scan walks
    "maintenance.mirror_repos": ([], "names", None),         # repos whose docs mirror to the vault
}
SECTIONS = {k.split(".")[0] for k in KEYS if "." in k}
WANT = {"bool": "true or false", "backend": " or ".join(BACKENDS),
        "prefix": "letters, digits, . and -", "path": "an absolute path (or ~/...)",
        "str": "a non-empty string",
        "glob": "a non-empty relative glob (a top-level note in Decisions/)",
        "strs": "a list of non-empty strings",
        "owner": "a name: a letter first, then letters, digits, space . _ ' - (64 at most),"
                 " and not cos or opus",
        "names": "a list of plain names (letters, digits, . _ -)",
        "map": "an object of plain name -> absolute path",
        "urlmap": "an object of plain name -> https://host/path, git@host:path or"
                  " ssh://host/path (no credentials)"}


class HostError(ValueError):
    """host.json (or an env override) is unreadable, has an unknown key, or a bad value."""


def _got(value):
    return "null" if value is None else f"a {type(value).__name__}"


def _need(ok, key, base, kind, value, where):
    if not ok:
        null = " or null" if kind.endswith("?") else ""
        raise HostError(f"{where}: {key} must be {WANT[base]}{null}, got {_got(value)}")
    return True


def _check(key, kind, value, where):
    """The value as used, or HostError naming the key, the wanted shape and the TYPE got."""
    base = kind.rstrip("?")
    if value is None and kind.endswith("?"):
        return None
    text = isinstance(value, str) and value != ""
    if base == "names":
        ok = isinstance(value, list) and all(
            isinstance(v, str) and NAME_RE.fullmatch(v) for v in value)
        return _need(ok, key, base, kind, value, where) and list(value)
    if base == "strs":
        ok = isinstance(value, list) and all(isinstance(v, str) and v for v in value)
        return _need(ok, key, base, kind, value, where) and list(value)
    if base == "map":
        ok = (isinstance(value, dict) and all(
            NAME_RE.fullmatch(k) and isinstance(v, str) and os.path.isabs(os.path.expanduser(v))
            for k, v in value.items()))
        return _need(ok, key, base, kind, value, where) and {
            k: os.path.expanduser(v) for k, v in value.items()}
    if base == "urlmap":
        ok = (isinstance(value, dict) and all(
            NAME_RE.fullmatch(k) and isinstance(v, str) and URL_RE.fullmatch(v)
            for k, v in value.items()))
        return _need(ok, key, base, kind, value, where) and dict(value)
    ok = {"bool": isinstance(value, bool), "backend": value in BACKENDS,
          "prefix": text and PREFIX_RE.fullmatch(value) is not None,
          "path": text and os.path.isabs(os.path.expanduser(value)),
          "glob": text and not os.path.isabs(value) and not value.startswith("~"),
          "str": text,
          "owner": text and OWNER_RE.fullmatch(value) is not None
          and value.strip().lower() not in ROLES}[base]
    _need(ok, key, base, kind, value, where)
    return os.path.expanduser(value) if base == "path" else value


def _flatten(doc, where):
    """{dotted key: value}: nested sections or flat dotted keys; refuses a non-object file, an
    unknown key or section, a non-object section and a key given twice."""
    if not isinstance(doc, dict):
        raise HostError(f"{where}: must be a JSON object")
    flat = {}
    for name, value in doc.items():
        if name in KEYS:
            found = {name: value}
        elif name not in SECTIONS:
            raise HostError(f"{where}: unknown key {name}")
        elif not isinstance(value, dict):
            raise HostError(f"{where}: {name} must be an object, got {_got(value)}")
        else:
            bad = [s for s in value if f"{name}.{s}" not in KEYS]
            if bad:
                raise HostError(f"{where}: unknown key {name}.{bad[0]}")
            found = {f"{name}.{s}": v for s, v in value.items()}
        twice = sorted(flat.keys() & found.keys())
        if twice:
            raise HostError(f"{where}: {twice[0]} is set twice")
        flat.update(found)
    return flat


def _only(doc, key, where):
    """{key: raw value} for one dotted key, nested or flat; {} when absent."""
    section, _, leaf = key.partition(".")
    if not leaf:        # a top-level key such as config_dir
        return {key: doc[key]} if key in doc else {}
    nested = doc.get(section)
    if section in doc and not isinstance(nested, dict):
        raise HostError(f"{where}: {section} must be an object, got {_got(nested)}")
    found = [doc[key]] if key in doc else []
    if isinstance(nested, dict) and leaf in nested:
        found.append(nested[leaf])
    if len(found) > 1:
        raise HostError(f"{where}: {key} is set twice")
    return {key: found[0]} if found else {}


def _read(home, only=None):
    """({dotted key: raw value}, path); a missing file is ({}, path). With `only`, the file must
    still be a readable JSON object, but only that key is looked at (the rest is ignored)."""
    path = paths.home(home) / "host.json"
    try:
        if not stat.S_ISREG(path.stat().st_mode):   # a FIFO would block the read for ever
            raise HostError(f"{path}: not a regular file")
        text = path.read_text()
    except FileNotFoundError:
        if path.is_symlink():
            raise HostError(f"{path}: a link to a missing file")
        return {}, path
    except OSError as exc:
        raise HostError(f"{path}: {exc}")
    except UnicodeDecodeError:
        raise HostError(f"{path}: not UTF-8 text")
    try:
        doc = json.loads(text)
        if only is not None and isinstance(doc, dict):
            return _only(doc, only, str(path)), path
        return _flatten(doc, str(path)), path
    except json.JSONDecodeError as exc:
        raise HostError(f"{path}: not valid JSON ({exc})")


def effective(home=None, env=None):
    """[(key, value, source)] for every key; source is "env NAME", "host.json" or "default"."""
    env = os.environ if env is None else env
    raw, path = _read(home)
    out = []
    for key, (default, kind, env_name) in KEYS.items():
        if env_name and env.get(env_name):
            source = f"env {env_name}"
            out.append((key, _check(key, kind, env[env_name], source), source))
        elif key in raw:
            out.append((key, _check(key, kind, raw[key], str(path)), "host.json"))
        else:
            out.append((key, copy.deepcopy(default), "default"))
    return out


def get(key, home=None, env=None, only=False):
    """One effective value; KeyError for a key host.json does not define. `only`: problems in
    other keys are ignored (bin/capsule-secret must not stop over a typo elsewhere)."""
    if key not in KEYS:
        raise KeyError(key)
    env = os.environ if env is None else env
    raw, path = _read(home, key if only else None)
    default, kind, env_name = KEYS[key]
    if env_name and env.get(env_name):
        return _check(key, kind, env[env_name], f"env {env_name}")
    return _check(key, kind, raw[key], str(path)) if key in raw else copy.deepcopy(default)


def get_or_default(key, home=None, env=None):
    """`get` for scheduled jobs: a bad or unreadable host.json never stops them. Only this key is
    looked at; on a HostError the public default is used and one stderr note names the key (never
    a value)."""
    try:
        return get(key, home, env, only=True)
    except HostError:
        print(f"host: {key} not usable in host.json; using the default", file=sys.stderr)
        default = KEYS[key][0]
        return copy.deepcopy(default)


DEFAULT_DECISION_NOTE = "Decisions/System design decisions.md"


def decision_note(vault, home=None, env=None):
    """Vault-relative decision log (newest top-level Decisions/ note matching
    `vault.decision_notes`), else the neutral default."""
    root = Path(vault) if vault else None
    pat = get_or_default("vault.decision_notes", home, env)
    try:
        found = sorted(p for p in root.glob(pat) if p.parent == root / "Decisions") if root else []
    except (ValueError, NotImplementedError, OSError):    # an unusable glob never stops a tick
        print("host: vault.decision_notes cannot be searched; using the default", file=sys.stderr)
        found = []
    return str(found[-1].relative_to(root)) if found else DEFAULT_DECISION_NOTE


def vault_path(home=None, env=None):
    """The configured notes vault as a Path, or None (the vault features are then off):
    the env override, else host.json `vault.path`. Never raises."""
    value = get_or_default("vault.path", home, env)
    return Path(value) if value else None


def config_dir(home=None, env=None):
    """The jobs' runtime config dir (ops.env, observability.env, maintenance/ ...) as a Path:
    env CAPSULE_JOBS_DIR, else host.json `config_dir`, else ~/.config/capsule-corp (or the legacy
    dir when only that holds observability.env or ops.env). Never raises.
    maintenance/lib_config.sh is the shell spelling of the same rule."""
    return jobs_dir.config_dir(home, env)    # the rule lives in the protected jobs_dir.py


def owner_name(home=None, env=None):
    """The configured owner name (`owner.name`, default "owner"); a bad host.json gives the
    default, like every scheduled-job read."""
    return get_or_default("owner.name", home, env)


def _show(key, value):
    if value is None:
        return "auto" if key in AUTO else "null"
    if isinstance(value, (list, dict)):
        return ",".join(value) or "none"        # names only: a map's paths stay out of a report
    return str(value).lower() if isinstance(value, bool) else str(value)


def describe(key, value, src):
    """One "key=value (source)" part that is safe to paste in a bug report: never the vault path
    or skill root (configured yes/no + directory exists/missing), the repo's basename only, the
    decision-note glob only when it is the public default (else "custom")."""
    if key == "config_dir":    # a path stays out of a pasted report
        return f"config_dir={'default' if value is None else 'set'} ({src})"
    if key in ("vault.path", "vault.skill_root"):
        has = ("no" if value is None else "yes, directory exists" if os.path.isdir(value)
               else "yes, directory missing")
        return f"{'vault' if key == 'vault.path' else 'vault skill_root'} configured={has} ({src})"
    if key == "vault.decision_notes" and value != KEYS[key][0]:
        return f"{key}=custom ({src})"
    if key == "repo":
        return f"repo={os.path.basename(str(value).rstrip('/'))} ({src})"
    if key in ("owner.name", "owner.copyright"):    # the name stays out of a pasted report
        return f"{key}={'default' if src == 'default' else 'set'} ({src})"
    return f"{key}={_show(key, value)} ({src})"


def check_row(home=None, env=None):
    """(level, name, detail) for `capsule check`: every effective value with its source."""
    try:
        rows = effective(home, env)
    except HostError as exc:
        return "FAIL", "host", str(exc)
    return "ok", "host", "; ".join(describe(k, v, src) for k, v, src in rows)


def add_parser(sub):
    p = sub.add_parser("host", help="host-level settings from $CAPSULE_HOME/host.json")
    hsub = p.add_subparsers(dest="host_cmd", required=True)
    hsub.add_parser("boards", help="the boards this host serves, space-separated"
                    ).add_argument("--capsule-home", default=None)
    g = hsub.add_parser("get", help="print one effective value (env > host.json > default);"
                                    " null and auto print an empty line")
    g.add_argument("key", help="a dotted key, e.g. launchd.label_prefix")
    g.add_argument("--capsule-home", default=None)
    g.add_argument("--only", action="store_true",
                   help="refuse only an unreadable file or a bad value for this key")


def handle(args, parser):
    try:
        value = get(args.key, args.capsule_home, only=args.only)
    except KeyError:
        known = ", ".join(KEYS)
        print(f"capsule host: unknown key {args.key!r} (known: {known})", file=sys.stderr)
        return 2
    except HostError as exc:
        print(f"capsule host: {exc}", file=sys.stderr)
        return 1
    if isinstance(value, list):
        value = " ".join(value)
    elif isinstance(value, dict):
        value = "\n".join(f"{k}\t{v}" for k, v in value.items())
    print("" if value is None else str(value).lower() if isinstance(value, bool) else value)
    return 0
