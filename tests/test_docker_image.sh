#!/usr/bin/env bash
# Builds the worker image and checks the quality tooling is importable.
# Skips (exit 0, prints SKIP) when docker is not available.
set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fails=0
check() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; fails=$((fails+1)); fi; }

# Static, no docker (row 526, card t_1758e0bd item 5): tests/test_capsule_tick_sh.sh needs `pkill` and `ps`
# (procps) in the worker image; the apt-get install list in docker/base/Dockerfile must name it.
check "the base Dockerfile installs procps (pkill and ps for tests/test_capsule_tick_sh.sh)" \
  'sed -n "/apt-get install/,/rm -rf \/var\/lib\/apt\/lists/p" "$REPO/docker/base/Dockerfile" | grep -qE "(^|[[:space:]])procps([[:space:]\\\\]|$)"'

# Static, no docker (spec ui-check-web): ui-check ships in the web stack, and only there.
WEB="$REPO/docker/stacks/web/Dockerfile"
check "web Dockerfile copies ui-check and ui_guard.py to /usr/local/bin" \
  'grep -Eq "^COPY base/ui-check base/ui_guard.py /usr/local/bin/" "$WEB" && grep -q "chmod 755 /usr/local/bin/ui-check" "$WEB"'
check "the COPY sources exist" '[ -f "$REPO/docker/base/ui-check" ] && [ -f "$REPO/docker/base/ui_guard.py" ]'
PWV="$(sed -n 's/.*"playwright==\([0-9.]*\)".*/\1/p' "$WEB")"
NODEV="$(sed -n 's/.*"@playwright\/test": "\([0-9.]*\)".*/\1/p' "$REPO/docker/stacks/web/package.json")"
LOCKV="$(grep -A1 '"node_modules/@playwright/test": {' "$REPO/docker/stacks/web/package-lock.json" | sed -n 's/.*"version": "\([0-9.]*\)".*/\1/p')"
check "python playwright is pinned to the node @playwright/test version (py $PWV, node $NODEV, lock $LOCKV)" \
  '[ -n "$PWV" ] && [ "$PWV" = "$NODEV" ] && [ "$PWV" = "$LOCKV" ]'
check "web stack skips browser downloads and points PLAYWRIGHT_BROWSERS_PATH at symlinks to the apt chromium" \
  'grep -q "^ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright$" "$WEB" && grep -q "^ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1" "$WEB" \
   && grep -q "node link-browsers.js" "$WEB" && [ -f "$REPO/docker/stacks/web/link-browsers.js" ] && grep -q "^ENV CHROMIUM_PATH=/usr/bin/chromium" "$WEB"'
check "web stack build fails unless a stock chromium.launch() works as the worker user (python and node)" \
  'tail -n 11 "$WEB" | grep -q "^USER worker" && grep -qE "^RUN python3 -c .*chromium.launch\(\)" "$WEB" && grep -qE "^ +&& node -e .*chromium.launch\(\)" "$WEB"'
check "web stack: the headless-shell link targets the apt chromium-headless-shell binary, chromium-<rev> the full chromium, and the build checks for no favicon request" \
  'grep -q "^ENV HEADLESS_SHELL=/usr/bin/chromium-headless-shell$" "$WEB" && grep -q "link-browsers.js .*\"\$CHROMIUM_PATH\" \"\$HEADLESS_SHELL\"" "$WEB" \
   && grep -q "target: shellTarget" "$REPO/docker/stacks/web/link-browsers.js" && grep -q "target: fullTarget" "$REPO/docker/stacks/web/link-browsers.js" \
   && grep -q "favicon.ico" "$REPO/docker/stacks/web/headless-check.js" && grep -q "headless-check.js" "$WEB"'
check "no second browser download in the web stack (no playwright install, no cdn.playwright.dev fetch)" \
  '! grep -vE "^[[:space:]]*#" "$WEB" | grep -Eq "playwright[[:space:]]+install|cdn\.playwright\.dev"'
check "ui-check default interpreter exists in the web stack (/opt/playwright venv)" \
  'grep -q "UI_CHECK_PYTHON:-/opt/playwright/bin/python" "$REPO/docker/base/ui-check" && grep -q "python3 -m venv /opt/playwright" "$WEB" && grep -q "chromium chromium-headless-shell python3 python3-venv" "$WEB"'
check "ui-check launches CHROMIUM_PATH, which the web stack sets" \
  'UC="$REPO/docker/base/ui-check"; grep -vE "^[[:space:]]*#" "$UC" | grep -q "environ.get(\"CHROMIUM_PATH\")" \
   && grep -vE "^[[:space:]]*#" "$UC" | grep -q "chromium.launch(.*executable_path=chromium" \
   && grep -q "^ENV CHROMIUM_PATH=" "$WEB"'
check "base and python stacks carry no browser, node or playwright" \
  '! grep -vE "^[[:space:]]*#" "$REPO/docker/base/Dockerfile" "$REPO/docker/stacks/python/Dockerfile" | grep -Eiq "chromium|playwright|nodejs|npm|/usr/local/bin/node"'
check "bin/ui-check is a thin wrapper that execs docker/base/ui-check (one source)" \
  '[ "$(wc -l <"$REPO/bin/ui-check")" -lt 15 ] && grep -q "docker/base/ui-check" "$REPO/bin/ui-check" && ! grep -q "sync_playwright" "$REPO/bin/ui-check"'
check "deploy.sh syncs no bin/ui-check copy, and the wrapper's target exists" \
  '! grep -vE "^[[:space:]]*#" "$REPO/deploy.sh" | grep -q "ui-check" && [ -x "$REPO/bin/ui-check" ] && [ -x "$REPO/docker/base/ui-check" ]'
[ "$fails" -eq 0 ] || { echo "test_docker_image: static check failed"; exit 1; }

. "$REPO/tests/lib_docker_optin.sh"
docker_optin test_docker_image || exit 77
IMG="${1:-}"
docker_ready test_docker_image || exit 1

# U1 tier-1 card 2 (t_cd2e6a3b): the SAME security/tmpfs args the worker runner
# applies to every worker container (its docker environment's
# _BASE_SECURITY_ARGS and _RUN_TMPFS_NOEXEC), defined once here so the probes below prove the fix
# under the actual constraints a real worker runs under instead of a bare
# `docker run` that could silently drift out of sync.
WORKER_SECURITY_ARGS=(
  --cap-drop ALL
  --cap-add DAC_OVERRIDE
  --cap-add CHOWN
  --cap-add FOWNER
  --security-opt no-new-privileges
  --tmpfs /tmp:rw,nosuid,size=512m
  --tmpfs /var/tmp:rw,noexec,nosuid,size=256m
  --tmpfs /run:rw,noexec,nosuid,size=64m
)

host_scratch=""
cleanup() { [ -z "$host_scratch" ] || rm -rf "$host_scratch"; }
trap cleanup EXIT
# The image under test is the python stack (it carries the base): pass its ref, or build it
# first with `docker/build-base.sh base && docker/build-base.sh python`.
[ -n "$IMG" ] || IMG="capsule-python:local"
docker image inspect "$IMG" >/dev/null 2>&1 || { echo "FAIL $IMG not built: run docker/build-base.sh base && docker/build-base.sh python"; exit 1; }
out="$(docker run --rm --label capsule=1 --label capsule.kind=test --network none "$IMG" python3 -c 'import coverage, diff_cover; print("ok")' 2>&1)"
check "coverage, diff_cover importable" '[ "$out" = "ok" ]'
out="$(docker run --rm --label capsule=1 --label capsule.kind=test --network none "$IMG" sh -c 'pkill -V && ps --version' 2>&1)"
check "procps (pkill, ps) on PATH" 'grep -q procps <<<"$out"'
out="$(docker run --rm --label capsule=1 --label capsule.kind=test --network none "$IMG" diff-cover --version 2>&1)"
check "diff-cover on PATH" 'grep -qE "[0-9]+\.[0-9]+" <<<"$out"'
out="$(docker run --rm --label capsule=1 --label capsule.kind=test --network none "$IMG" python3 -m ruff --version 2>&1)"
check "ruff --version" 'grep -qE "[0-9]+\.[0-9]+" <<<"$out"'
out="$(docker run --rm --label capsule=1 --label capsule.kind=test --network none "$IMG" python3 -c 'import hypothesis; print("ok")' 2>&1)"
check "hypothesis importable" '[ "$out" = "ok" ]'
out="$(docker run --rm --label capsule=1 --label capsule.kind=test --network none "$IMG" mutmut --version 2>&1)"
check "mutmut on PATH" 'grep -qE "[0-9]+\.[0-9]+" <<<"$out"'
# mutmut 3.x has no runner of its own - it drives pytest in-process, so a
# worker image with mutmut and no pytest cannot run a single mutant.
out="$(docker run --rm --label capsule=1 --label capsule.kind=test --network none "$IMG" pytest --version 2>&1)"
check "pytest --version (mutmut's runner)" 'grep -qE "[0-9]+\.[0-9]+" <<<"$out"'
out="$(docker run --rm --label capsule=1 --label capsule.kind=test --network none "$IMG" sh -c 'command -v node chromium playwright' 2>&1)"
check "the python stack carries no Node, Chromium or Playwright (they live in the web stack)" '[ -z "$out" ]'

# R3/row 201, fix wave 1 review pt 9, moved by U1 tier-1 card 2 (t_cd2e6a3b):
# prove the noexec-/tmp fix for real, on the actual image CI builds
# (.github/workflows/image.yml), not just by reading the Dockerfile, and
# under the SAME args a real worker container runs under (WORKER_SECURITY_ARGS
# above), not a bare `docker run` - the original probe used a bare
# `--tmpfs /tmp:noexec` and missed that the runner ALSO tmpfs-shadows /var/tmp,
# where the old fix lived (experiment 1, row 357).
host_scratch="$(mktemp -d)"

# (a) + (b): with the runner args AND the /workspace bind mount the runner always
# adds for a real worker, `mktemp -d` must succeed, land under
# /workspace/.worker-exec, and produce a script that actually executes; and
# $TMPDIR must be exactly /workspace/.worker-exec. Experiment 1 showed an
# empty `$d` silently degrading `"$d/x"` to `/x` (or `/t.sh`-shaped paths),
# so the probe checks `mktemp`'s own exit status and that `$d` is non-empty,
# not just the final "ok".
out="$(docker run --rm --label capsule=1 --label capsule.kind=test --network none \
  --user "$(id -u):$(id -g)" \
  "${WORKER_SECURITY_ARGS[@]}" -v "$host_scratch:/workspace" "$IMG" sh -c '
    d="$(mktemp -d)"; rc=$?
    [ "$rc" -eq 0 ] && [ -n "$d" ] || { echo "FAIL_MKTEMP rc=$rc d=$d"; exit 1; }
    case "$d" in
      /workspace/.worker-exec/*) : ;;
      *) echo "FAIL_PATH:$d"; exit 1 ;;
    esac
    printf "#!/bin/sh\necho ok\n" >"$d/x" && chmod +x "$d/x" && "$d/x"
  ' 2>&1)"
check "mktemp -d under the runner args succeeds, lands under /workspace/.worker-exec, and executes" '[ "$out" = "ok" ]'

out="$(docker run --rm --label capsule=1 --label capsule.kind=test --network none \
  --user "$(id -u):$(id -g)" \
  "${WORKER_SECURITY_ARGS[@]}" -v "$host_scratch:/workspace" "$IMG" sh -c 'echo "$TMPDIR"' 2>&1)"
check "TMPDIR is /workspace/.worker-exec" '[ "$out" = "/workspace/.worker-exec" ]'

# (c) the same exec probe WITHOUT a /workspace bind mount - the image-baked
# /workspace/.worker-exec (created at build time) must work for a bare
# `docker run <image> <cmd>` too (merge_gate/quality_gate/tests call the
# image this way; not every caller has a workspace bind mount).
out="$(docker run --rm --label capsule=1 --label capsule.kind=test --network none \
  --user "$(id -u):$(id -g)" \
  "${WORKER_SECURITY_ARGS[@]}" "$IMG" sh -c '
    d="$(mktemp -d)"; rc=$?
    [ "$rc" -eq 0 ] && [ -n "$d" ] || { echo "FAIL_MKTEMP rc=$rc d=$d"; exit 1; }
    printf "#!/bin/sh\necho ok\n" >"$d/x" && chmod +x "$d/x" && "$d/x"
  ' 2>&1)"
check "mktemp -d executes with no /workspace bind mount (image-baked dir)" '[ "$out" = "ok" ]'

# (d) the entrypoint wrote the .gitignore into the bind-mounted (host) dir,
# not just inside the container - proves the ENTRYPOINT ran (not just image
# ENV) and that the scratch dir never shows up in `git status`/`git add -A`
# of the worktree that /workspace bind-mounts.
check "host workspace mount now has .worker-exec/.gitignore containing *" \
  '[ "$(cat "$host_scratch/.worker-exec/.gitignore" 2>/dev/null)" = "*" ]'

# Web stack, when its image is local (skipped otherwise): the browsers folder holds the Playwright
# layout and a stock launch (no executablePath) works offline as uid 1000.
WEBIMG="${CAPSULE_WEB_IMAGE:-capsule-web:local}"
if docker image inspect "$WEBIMG" >/dev/null 2>&1; then
  out="$(docker run --rm --label capsule=1 --label capsule.kind=test --network none --user 1000 "$WEBIMG" sh -lc '
    [ "$PLAYWRIGHT_BROWSERS_PATH" = /opt/ms-playwright ] && [ "$CHROMIUM_PATH" = /usr/bin/chromium ] || exit 3
    [ -x "$(ls /opt/ms-playwright/chromium_headless_shell-*/*/chrome-headless-shell)" ] && [ -x "$(ls /opt/ms-playwright/chromium-*/*/chrome)" ] || exit 4
    [ "$(readlink -f /opt/ms-playwright/chromium_headless_shell-*/*/chrome-headless-shell)" = "$(readlink -f /usr/bin/chromium-headless-shell)" ] || exit 5
    [ "$(readlink -f /opt/ms-playwright/chromium-*/*/chrome)" = "$(readlink -f /usr/bin/chromium)" ] || exit 6
    python3 -c "from playwright.sync_api import sync_playwright as s; p=s().start(); b=p.chromium.launch(); pg=b.new_page(); pg.set_content(\"<p>ok</p>\"); assert pg.inner_text(\"p\")==\"ok\"; b.close(); p.stop(); print(\"stock-ok\")"' 2>&1)"
  check "web image: browsers path layout exists and a stock python chromium.launch() works offline as uid 1000" '[ "$out" = "stock-ok" ]'
else
  echo "SKIP web image $WEBIMG not local"
fi

[ $fails -eq 0 ] && echo "test_docker_image: all passed" || { echo "test_docker_image: $fails failed"; exit 1; }
