"""docker_engine.py - the one Python table of Docker engines and the one resolution of which is
in use.

ENGINE is `orbstack` (default) or `desktop`. Resolution: env CAPSULE_DOCKER_ENGINE, then the
one-line file `docker-engine` in the maintenance state root (events.state_root()), then
`orbstack`. An empty value counts as unset; an unknown value raises ValueError (never a silent
fallback). The shell twin is lib_docker_engine.sh; change both tables together."""
import os
from pathlib import Path

ENGINES = {
    "desktop": {"bin": "/Applications/Docker.app/Contents/Resources/bin",
                "context": "desktop-linux",
                "title": "Docker Desktop", "app": "Docker", "app_path": "/Applications/Docker.app",
                "open_hint": "open -a Docker", "cask": "docker-desktop"},
    "orbstack": {"bin": "~/.orbstack/bin", "context": "orbstack",
                 "title": "OrbStack", "app": "OrbStack", "app_path": "/Applications/OrbStack.app",
                 "open_hint": "open -a OrbStack", "cask": "orbstack"},
}
DEFAULT = "orbstack"


def resolve(state_dir, env=None):
    """The engine's table row (bin expanded) plus its `name`. `state_dir` holds `docker-engine`."""
    env = os.environ if env is None else env
    name, src = (env.get("CAPSULE_DOCKER_ENGINE") or "").strip(), "CAPSULE_DOCKER_ENGINE"
    if not name:
        src = str(Path(state_dir) / "docker-engine")
        try:
            name = Path(src).read_text().strip()
        except OSError:
            name = ""
    name = name or DEFAULT
    if name not in ENGINES:
        raise ValueError(f"unknown Docker engine {name!r} (from {src}): use one of "
                         f"{', '.join(sorted(ENGINES))}")
    row = dict(ENGINES[name])
    row["bin"] = os.path.expanduser(row["bin"])
    return {"name": name, **row}
