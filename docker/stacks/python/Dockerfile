# capsule-python: the python stack layer (spec "Worker images", slice S10a). Chains from the
# slim capsule-base by DIGEST (the capsule-base row in docker/IMAGES.lock), never a tag.
# Budget 1.5 GB. Build context is docker/ (`docker/build-base.sh python` builds it and
# records the capsule-python lock row).
#
# Pins (looked up 2026-10-26 on PyPI, not from memory): coverage 7.17.0, diff-cover 10.6.0,
# hypothesis 6.170.9, mutmut 3.8.0, pytest 9.1.1, ruff 0.17.4. Python is Debian trixie's own
# python3, from the same dated apt snapshot as the base. Bump the FROM digest here whenever
# the capsule-base row in the lock changes.
FROM capsule-base@sha256:5309972f8302c7ca3074f5339b462ada46a3d0fe99fb50ae240bd9965aae7d3b

USER root
RUN apt-get -o Acquire::Check-Valid-Until=false update \
    && apt-get install -y --no-install-recommends python3 python3-venv perl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Debian's python3 is externally managed, so the tooling lives in one venv on PATH.
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install -q --no-cache-dir \
         "coverage==7.17.0" "diff-cover==10.6.0" "hypothesis==6.170.9" \
         "mutmut==3.8.0" "pytest==9.1.1" "ruff==0.17.4" \
    && chmod -R a+rX /opt/venv \
    && printf 'PATH=/opt/venv/bin:$PATH\nexport PATH\n' >/etc/profile.d/venv.sh
ENV PATH=/opt/venv/bin:$PATH
# The runner execs `sh -lc`; /etc/profile resets PATH for a login shell, so ENV PATH alone is
# lost there. /etc/profile.d/venv.sh (sourced after the reset) puts the venv back first.

# ui_guard.py on the interpreter path so a card's tests/ui can `import ui_guard`.
RUN cp /usr/local/bin/ui_guard.py "$(/opt/venv/bin/python -c 'import site; print(site.getsitepackages()[0])')/ui_guard.py"

LABEL capsule-layer=stack-python
USER worker
