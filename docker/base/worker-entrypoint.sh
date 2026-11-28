#!/bin/sh
# U1 tier-1, card 2 (t_cd2e6a3b): the runner mounts a
# --tmpfs /var/tmp:rw,noexec,nosuid,size=256m on EVERY worker container
# unconditionally, which
# shadowed the old image-baked /var/tmp/worker-exec and made `mktemp -d` fail
# inside a real worker (experiment 1, row 357). /workspace is where the runner
# bind-mounts the card's worktree and is never
# tmpfs-shadowed in that mode, so the scratch dir moves there.
#
# This runs ONCE, at container start (the runner does
# `docker run -d --init ... <image> sleep infinity`, then `docker exec`s
# commands into the already-running container - an exec session never runs
# this script again, and only inherits the image's own ENV). That is why
# TMPDIR is baked in as an image ENV (see Dockerfile), not exported here:
# an entrypoint-local `export TMPDIR=...` would be invisible to every later
# `docker exec`.
#
# Best-effort by design: a worker must still start even when /workspace is
# not writable yet (no bind mount, wrong ownership, ...) - a missing scratch
# dir is degraded service, not a reason to refuse to start the container.
set -u

dir="${WORKER_EXEC_DIR:-/workspace/.worker-exec}"

if mkdir -p "$dir" 2>/dev/null; then
    # Ignore chmod failure: we may not own the dir if it was already
    # created (e.g. baked into the image at build time, or created by a
    # previous container using the same bind mount) by a different uid.
    chmod 1777 "$dir" 2>/dev/null || true
    # A bare "*" so the scratch dir never shows up in `git status`/
    # `git add -A` of the worktree - it lives INSIDE the git worktree
    # (the /workspace bind mount is the card's checkout). The image bakes
    # this file in at build time as root, mode 0644 (Dockerfile); a
    # non-root worker (--user uid:gid, no /workspace bind mount - image.yml
    # probe (c)) can never rewrite a root-owned file it doesn't own, so
    # check first whether a "*" line is already there and skip the write
    # entirely when it is. Best-effort either way: any residual error from
    # the write attempt itself (and its own stderr) is thrown away, never
    # printed - a worker must still start.
    if ! grep -qx '\*' "$dir/.gitignore" 2>/dev/null; then
        { printf '*\n' >"$dir/.gitignore"; } 2>/dev/null || true
    fi
else
    echo "worker-entrypoint: could not create $dir, continuing without it" >&2
fi

exec "$@"
