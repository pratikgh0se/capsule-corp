"""Resolve and re-verify an operator identity for the `override` verb (row 454 item 4;
controller ruling below)."""
import os
import re
import stat
from pathlib import Path

NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _dir(identity_dir):
    if identity_dir is not None:
        return Path(identity_dir)
    # TEST HOOK ONLY: `$CAPSULE_IDENTITY_DIR` re-points the
    # trust root this module reads.
    env_dir = os.environ.get("CAPSULE_IDENTITY_DIR")
    return Path(env_dir) if env_dir else Path.home() / ".config" / "capsule" / "identities"


def verify(name, *, identity_dir=None):
    """The identity `name`, re-confirmed against its file right now, or `None`."""
    if not name or not NAME_RE.fullmatch(name):
        return None   # also refuses '/'- or '..'-bearing names: no path can even be built from them
    f = _dir(identity_dir) / f"{name}.env"
    try:
        st = f.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None   # a symlink (or anything else) at this path: refused, never followed
    if st.st_mode & 0o077:
        return None   # group/world readable or writable
    if st.st_uid != os.getuid():
        return None   # not owned by this process's own uid, even if the mode looks right
    return name


def resolve(*, env=None, identity_dir=None):
    """The CLI's own resolution: the identity `$CAPSULE_IDENTITY` names, or `None` when it names
    nothing with a real, correctly-permissioned identity file - never a guess, and the env var
    alone is never enough."""
    env = env if env is not None else os.environ
    return verify(env.get("CAPSULE_IDENTITY"), identity_dir=identity_dir)
