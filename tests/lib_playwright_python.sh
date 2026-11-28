# Which interpreter has playwright + an installed Chromium (card rq_2c9581db). Lookup order, resolved
# ONLY here (tests drive it via the env var and a fake HOME, they never name an install path):
#   1. CAPSULE_PLAYWRIGHT_PYTHON, when set, is authoritative: usable -> chosen; unusable -> nothing is
#      chosen (the caller skips loudly, exit 77), it never falls back silently past an explicit setting.
#   2. else the host default $HOME/.local/share/capsule/playwright/bin/python, if usable.
#   3. else the worker image's venv (PLAYWRIGHT_IMAGE_PYTHON, default /opt/playwright/bin/python), if usable.
#   4. else `python3` on PATH, if it can import playwright and find Chromium.
#   5. else nothing: the caller skips loudly (exit 77) and names CAPSULE_PLAYWRIGHT_PYTHON.
# Usage: . tests/lib_playwright_python.sh; PY="$(playwright_python)" || { skip loudly }
playwright_usable() {
  [ -x "$1" ] || command -v "$1" >/dev/null 2>&1 || return 1
  "$1" - <<'PROBE' >/dev/null 2>&1
import os, sys
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    # The web stack image has no downloaded browser: ui-check launches CHROMIUM_PATH there.
    cp = os.environ.get("CHROMIUM_PATH", "")
    ok = os.path.isfile(cp) and os.access(cp, os.X_OK)
    sys.exit(0 if ok or os.path.exists(p.chromium.executable_path) else 1)
PROBE
}
playwright_python() {
  if [ -n "${CAPSULE_PLAYWRIGHT_PYTHON:-}" ]; then
    playwright_usable "$CAPSULE_PLAYWRIGHT_PYTHON" && printf '%s\n' "$CAPSULE_PLAYWRIGHT_PYTHON" && return 0
    return 1
  fi
  local cand
  for cand in "$HOME/.local/share/capsule/playwright/bin/python" "${PLAYWRIGHT_IMAGE_PYTHON:-/opt/playwright/bin/python}" python3; do
    playwright_usable "$cand" && printf '%s\n' "$cand" && return 0
  done
  return 1
}
