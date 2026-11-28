# capsule-web: the web stack layer (spec "Worker images", slice S10a, R11). Chains from the slim
# capsule-base by DIGEST (the capsule-base row in docker/IMAGES.lock), never a tag. Budget 3 GB.
# Build context is docker/ (`docker/build-base.sh web` builds it and records the capsule-web
# lock row).
#
# Pins (looked up 2026-10-29 on the web, not from memory):
#   - Node: node:26.12.1-trixie-slim, by the multi-arch index digest below (Docker Hub registry
#     manifest, library/node). Node 26.13.0 is the newest release and the line is LTS since
#     2026-10-28 (https://nodejs.org/dist/index.json), but the official 26.13.0 image is not
#     published yet; 26.12.1 is the newest published image on that line. Renovate moves this
#     digest to 26.13 once that image exists. Node arrives by multi-stage COPY (binary + npm),
#     so nothing here fetches from nodejs.org.
#   - npm packages: @playwright/test 1.64.1 (registry.npmjs.org), installed only from the
#     committed package-lock.json (integrity hashes) with `npm ci --ignore-scripts`.
#   - Python playwright 1.64.1 (PyPI, https://pypi.org/pypi/playwright/1.64.1/json, checked
#     2026-11-08): the same version as the node @playwright/test above, in a venv at /opt/playwright
#     (ui-check's default interpreter). It downloads no browser; it drives the Chromium below.
#   - Python tools (same pins as docker/stacks/python/Dockerfile, mirrored; a test keeps them equal):
#     coverage 7.17.0, diff-cover 10.6.0, hypothesis 6.170.9, mutmut 3.8.0, pytest 9.1.1, ruff 0.17.4.
#   - Chromium: Debian trixie's own `chromium` from the same dated apt snapshot as the base. No
#     Playwright browser download (that would need cdn.playwright.dev, which is not on the door-5
#     list in docs/EGRESS.md). Instead PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright holds the folders
#     stock Playwright looks for (link-browsers.js, revisions read from the installed packages): the
#     chromium_headless_shell-<rev> link goes to Debian's `chromium-headless-shell` (same version and
#     snapshot as `chromium`), so a default headless launch behaves like stock Playwright (no
#     /favicon.ico fetch, same canvas bytes), and chromium-<rev> goes to the full /usr/bin/chromium
#     (headless=False, channel='chromium'). A plain `chromium.launch()` works offline, node and python.
#     CHROMIUM_PATH below still works: `launch({ executablePath: process.env.CHROMIUM_PATH, args: ['--no-sandbox'] })`.
# Bump the capsule-base FROM digest here whenever the capsule-base row in the lock changes.
FROM node:26.12.1-trixie-slim@sha256:94816674c62774da0d099a947bf4613b9e1972a2733abd0d0cd80e795f8411d0 AS node

FROM capsule-base@sha256:5309972f8302c7ca3074f5339b462ada46a3d0fe99fb50ae240bd9965aae7d3b

USER root
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

RUN apt-get -o Acquire::Check-Valid-Until=false update \
    && apt-get install -y --no-install-recommends chromium chromium-headless-shell python3 python3-venv perl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY stacks/web/package.json stacks/web/package-lock.json /opt/capsule-web/
RUN cd /opt/capsule-web \
    && /usr/bin/env PATH=/usr/local/bin:/usr/bin:/bin npm ci --ignore-scripts --no-audit --no-fund --cache /tmp/npm-cache \
    && /bin/rm -rf /tmp/npm-cache \
    && /bin/chmod -R a+rX /opt/capsule-web \
    && ln -s /opt/capsule-web/node_modules/.bin/playwright /usr/local/bin/playwright \
    && printf 'PATH=/opt/playwright/bin:$PATH:/opt/project-npm/node_modules/.bin:/opt/capsule-web/node_modules/.bin\nexport PATH\n' >/etc/profile.d/web.sh
ENV NODE_PATH=/opt/project-npm/node_modules:/opt/capsule-web/node_modules
ENV PATH=/opt/playwright/bin:$PATH:/opt/project-npm/node_modules/.bin:/opt/capsule-web/node_modules/.bin
ENV CHROMIUM_PATH=/usr/bin/chromium

# ui-check (ui-check-web): Python Playwright, same version as the node one, in /opt/playwright.
# No browser download: node Playwright installs none here (--ignore-scripts, no cdn.playwright.dev)
# and PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 keeps it that way. PLAYWRIGHT_BROWSERS_PATH points at
# symlinks to the apt chromium (see the end of this file); ui-check passes CHROMIUM_PATH itself.
# The web stack is a superset of the python stack: the same tool pins as docker/stacks/python/Dockerfile
# (a test keeps them equal) go into this venv, so a Python app with browser tests (ruff, pytest,
# unittest, hypothesis, mutmut, coverage, diff-cover, ui_guard) runs on one image.
# /opt/playwright/bin leads PATH (ENV and web.sh above) so system python3 imports playwright and the tools.
RUN python3 -m venv /opt/playwright \
    && /opt/playwright/bin/pip install -q --no-cache-dir "playwright==1.64.1" \
         "coverage==7.17.0" "diff-cover==10.6.0" "hypothesis==6.170.9" \
         "mutmut==3.8.0" "pytest==9.1.1" "ruff==0.17.4" \
    && chmod -R a+rX /opt/playwright
ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
ENV HEADLESS_SHELL=/usr/bin/chromium-headless-shell
COPY stacks/web/link-browsers.js stacks/web/headless-check.js /opt/capsule-web/
# Stock-Playwright layout: chromium_headless_shell-<rev> -> the apt headless shell, chromium-<rev> ->
# the apt full chromium. The script fails the build when node and python Playwright disagree on a revision.
RUN cd /opt/capsule-web \
    && node link-browsers.js "$(find /opt/playwright/lib -path '*/playwright/driver/package/browsers.json')" "$CHROMIUM_PATH" "$HEADLESS_SHELL" \
    && chmod -R a+rX /opt/ms-playwright /opt/capsule-web/link-browsers.js /opt/capsule-web/headless-check.js
COPY base/ui-check base/ui_guard.py /usr/local/bin/
RUN chmod 755 /usr/local/bin/ui-check && chmod 644 /usr/local/bin/ui_guard.py
# ui_guard.py on the interpreter path so a card's tests/ui can `import ui_guard` (as the python stack does).
RUN cp /usr/local/bin/ui_guard.py "$(/opt/playwright/bin/python -c 'import site; print(site.getsitepackages()[0])')/ui_guard.py"
# The runner execs `sh -lc`; /etc/profile resets PATH for a login shell, so ENV PATH alone is
# lost there. /etc/profile.d/web.sh (sourced after the reset) puts the node tools back, AFTER
# the system dirs: a package bin named rm, chmod or env must never shadow a system tool.
# `npx playwright` only looks in the project's node_modules and the global bin (/usr/local/bin),
# not on PATH, so the playwright bin is linked there (no network: it must never fetch it).
# /opt/project-npm is where a rendered project layer installs the repo's own package-lock.json.

LABEL capsule-layer=stack-web
USER worker
# Build check, as the worker user and offline: a stock launch (no executablePath) must work in python and node,
# and the default headless launch must be the headless shell: a page from http.server gets no console error and
# no /favicon.ico request (full Chromium asks for it, the 404 is a console error).
RUN cd /opt/capsule-web && node headless-check.js
RUN python3 -c "from playwright.sync_api import sync_playwright as s; p=s().start(); b=p.chromium.launch(); pg=b.new_page(); pg.set_content('<p>ok</p>'); assert pg.inner_text('p')=='ok'; b.close(); p.stop()" \
    && node -e "const {chromium}=require('@playwright/test'); (async()=>{const b=await chromium.launch(); const pg=await b.newPage(); await pg.setContent('<p>ok</p>'); if(await pg.innerText('p')!=='ok') process.exit(1); await b.close();})().catch(e=>{console.error(e);process.exit(1)})"

