# Rendered by `capsule image render` from capsule.toml [image]. Do not edit by hand.
FROM capsule-python@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb

USER root
RUN apt-get -o Acquire::Check-Valid-Until=false update \
    && apt-get install -y --no-install-recommends libpq-dev g++ \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/locks/requirements.txt
RUN /opt/venv/bin/pip install -q --no-cache-dir --only-binary=:all: --require-hashes -r /tmp/locks/requirements.txt \
    && chmod -R a+rX /opt/venv

LABEL capsule-layer=project
USER worker
