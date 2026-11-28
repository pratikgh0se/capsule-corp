"""The jobs' runtime config dir (ops.env, observability.env, maintenance/ ...).

Order: env CAPSULE_JOBS_DIR, else host.json `config_dir`, else the default: ~/.config/capsule-corp
if it exists, else the legacy dir (LEGACY_JOBS_DIR) if it holds observability.env or ops.env, else
~/.config/capsule-corp.
A tiny protected module on purpose: the sandbox runner reads ops.env from here
(OPS_WORKER_IMAGE), so what decides this path must not be an unprotected, often-edited module
(host.py delegates here). Imports only answer_base. maintenance/lib_config.sh is the shell
spelling of the same rule."""
import json
import os
import stat
import sys
from pathlib import Path

from capsule import answer_base

# The pre-rename default, still read when it is the only one present (the D270 legacy-read
# pattern). maintenance/lib_config.sh and compose_live_config_dir in maintenance/lib_compose.sh
# mirror this rule.
LEGACY_JOBS_DIR = "~/.config/obsidian-second-brain"


def _from_host_json(home):
    """host.json `config_dir` (None when absent or null); ValueError on a bad file or value."""
    path = answer_base.home(home) / "host.json"
    try:
        if not stat.S_ISREG(path.stat().st_mode):    # a FIFO would block the read for ever
            raise ValueError("not a regular file")
        doc = json.loads(path.read_text())
    except FileNotFoundError:
        if path.is_symlink():
            raise ValueError("a link to a missing file") from None
        return None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(str(exc)) from exc
    if not isinstance(doc, dict):
        raise ValueError("not a JSON object")
    return doc.get("config_dir")


def _usable(value):
    """The value as a path string (a leading ~ expanded), or ValueError."""
    if not isinstance(value, str) or not value or not os.path.isabs(os.path.expanduser(value)):
        raise ValueError("config_dir must be an absolute path")
    return os.path.expanduser(value)


def config_dir(home=None, env=None):
    """The config dir as a Path. Never raises: a bad host.json or value gives the default and
    one stderr note that names the key, never a value."""
    env = os.environ if env is None else env
    try:
        value = env.get("CAPSULE_JOBS_DIR") or _from_host_json(home)
        if value is not None:
            return Path(_usable(value))
    except ValueError:
        print("host: config_dir not usable in host.json; using the default", file=sys.stderr)
    new = Path.home() / ".config" / "capsule-corp"
    legacy = Path(os.path.expanduser(LEGACY_JOBS_DIR))
    # os.path.isfile never raises (Path.is_file does on 3.11 for an unreadable dir).
    marked = any(os.path.isfile(legacy / name) for name in ("observability.env", "ops.env"))
    return legacy if not new.is_dir() and marked else new
