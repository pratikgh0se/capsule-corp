# capsule-base: the slim base layer of the worker images (spec "Worker images (slim
# base + per-project layers)", slice S10a). Stack and project layers chain from this
# one by digest. Budget 300 MB. Protected path: the maintainer or CoS only.
#
# Build context is docker/ (not docker/base/), so the entrypoint and ui_guard.py have
# one source file for both:
#   docker buildx build -f docker/base/Dockerfile docker     (docker/build-base.sh does this)
#
# Pins (looked up 2026-10-24, not from memory):
#   - FROM: Debian 13 "trixie" (current stable, 13.7), slim variant, dated tag
#     trixie-20261020-slim, pinned by the multi-arch index digest. Source:
#     https://github.com/docker-library/official-images/blob/master/library/debian
#   - apt: the dated snapshot below, so the same Dockerfile builds the same package set.
#     Bump the digest and the snapshot stamp together, never one alone.
# No runtime installs: nothing here is fetched by a running worker (workers run
# --network=none); the apt lists are deleted in the same layer they are created.
FROM debian:trixie-20261020-slim@sha256:62bd423aeee912639ca8ee681c2b762cb3e25137e0a92b20be455898a9f034ad

ARG SNAPSHOT=20261020T000000Z
# Snapshot sources replace the stock ones; apt still verifies the signed Release files
# (Valid-Until is off because snapshot Release files age out by design).
RUN rm -f /etc/apt/sources.list /etc/apt/sources.list.d/* \
    && printf 'deb http://snapshot.debian.org/archive/debian/%s/ trixie main\ndeb http://snapshot.debian.org/archive/debian-security/%s/ trixie-security main\n' "$SNAPSHOT" "$SNAPSHOT" >/etc/apt/sources.list \
    && apt-get -o Acquire::Check-Valid-Until=false update \
    && apt-get install -y --no-install-recommends \
         git sqlite3 jq curl ca-certificates procps \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

RUN git config --system --add safe.directory '*' \
    && git config --system user.name "Capsule Worker" \
    && git config --system user.email worker@capsule.localhost

# Non-root default user. The runner still passes --user <host uid>; stack layers
# that need to install things switch to USER root for their build steps and back.
RUN useradd --create-home --uid 1000 --shell /bin/bash worker

# ui_guard.py sits next to the entrypoint; the python stack puts it on the interpreter
# path (the base has no python).
COPY base/ui_guard.py /usr/local/bin/ui_guard.py
RUN chmod 644 /usr/local/bin/ui_guard.py

# Scratch dir and entrypoint (TMPDIR in the image ENV because `docker exec` sessions never
# run the entrypoint; see worker-entrypoint.sh).
RUN mkdir -p /workspace/.worker-exec \
    && chmod 1777 /workspace/.worker-exec \
    && printf '*\n' >/workspace/.worker-exec/.gitignore
ENV WORKER_EXEC_DIR=/workspace/.worker-exec
ENV TMPDIR=/workspace/.worker-exec
COPY base/worker-entrypoint.sh /usr/local/bin/worker-entrypoint
RUN chmod 755 /usr/local/bin/worker-entrypoint

LABEL capsule-layer=base
USER worker
WORKDIR /workspace
ENTRYPOINT ["/usr/local/bin/worker-entrypoint"]
