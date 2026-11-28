"""One host-side atomic file write that never follows a symlink a worker planted."""
import contextlib
import errno
import os
from pathlib import Path

_DIR = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_FILE = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)


class SymlinkRefused(OSError):
    """A path component or temp was a symlink (or not a directory): likely tampering."""


def _open(name, flags, dfd=None, mode=0o644):
    try:
        return os.open(name, flags, mode, dir_fd=dfd)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise SymlinkRefused(exc.errno, f"{exc.strerror}: {name}") from None
        raise


def _parts(root, path):
    try:
        parts = Path(path).relative_to(root).parts
    except ValueError:
        raise OSError(f"{path} is not under {root}") from None
    if ".." in parts:
        raise OSError(f"bad path {path} under {root}")
    return parts


def open_dir(root, path, create_dirs=False):
    """Open directory `path` (under `root`) by walking it with O_NOFOLLOW; returns an fd the
    caller closes. A symlinked component raises SymlinkRefused; `create_dirs` mkdirs by dir fd."""
    dfd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    for part in _parts(root, path):
        try:
            dfd = _step(dfd, part, create_dirs)
        except BaseException:
            os.close(dfd)
            raise
    return dfd


def _step(dfd, part, create_dirs):
    """Open child `part` of `dfd` (no symlink follow), closing `dfd` on success."""
    if create_dirs:
        with contextlib.suppress(FileExistsError):
            os.mkdir(part, 0o755, dir_fd=dfd)
    nxt = _open(part, _DIR, dfd)
    os.close(dfd)
    return nxt


def _temp(name, dfd, mode=0o644):
    """A fresh random O_EXCL|O_NOFOLLOW temp beside `name`: (temp name, fd)."""
    for _ in range(8):
        cand = f".{name}.{os.urandom(6).hex()}.tmp"
        with contextlib.suppress(FileExistsError):
            return cand, _open(cand, _FILE, dfd, mode)
    raise SymlinkRefused(errno.EEXIST, f"8 random temp names taken beside {name}: planted?")


def atomic_write(root, path, data, *, create_dirs=False, encoding="utf-8", mode=0o644):
    """Write `data` (str or bytes) to `path`, which must sit under `root`."""
    raw = data if isinstance(data, bytes) else str(data).encode(encoding)
    path = Path(path)
    if not _parts(root, path):
        raise OSError(f"bad path {path} under {root}")
    dfd = open_dir(root, path.parent, create_dirs)
    tmp = None
    try:
        tmp, fd = _temp(path.name, dfd, mode)
        try:
            fh = os.fdopen(fd, "wb")
        except BaseException:
            os.close(fd)
            raise
        with fh:
            fh.write(raw)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path.name, src_dir_fd=dfd, dst_dir_fd=dfd)
        tmp = None
    finally:
        if tmp is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp, dir_fd=dfd)   # only the temp this call created
        os.close(dfd)
    return len(raw)


def open_append(root, path):
    """Open `path` (under `root`) for binary append, created 0644 if absent; never follows a
    symlink at the leaf or in a parent (SymlinkRefused). The caller closes the file object."""
    path = Path(path)
    if not _parts(root, path):
        raise OSError(f"bad path {path} under {root}")
    dfd = open_dir(root, path.parent)
    try:
        fd = _open(path.name, os.O_WRONLY | os.O_CREAT | os.O_APPEND
                   | getattr(os, "O_NOFOLLOW", 0), dfd)
    finally:
        os.close(dfd)
    try:
        return os.fdopen(fd, "ab")
    except BaseException:
        os.close(fd)
        raise
