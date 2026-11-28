# Rendered by `capsule image render` from capsule.toml [image]. Do not edit by hand.
FROM capsule-web@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc

USER root
RUN apt-get -o Acquire::Check-Valid-Until=false update \
    && apt-get install -y --no-install-recommends fonts-liberation \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*
COPY package.json package-lock.json /opt/project-npm/
RUN cd /opt/project-npm && /usr/bin/env PATH=/usr/local/bin:/usr/bin:/bin npm ci --ignore-scripts --no-audit --no-fund --cache /tmp/npm-cache \
    && /bin/rm -rf /tmp/npm-cache && /bin/chmod -R a+rX /opt/project-npm

LABEL capsule-layer=project
USER worker
