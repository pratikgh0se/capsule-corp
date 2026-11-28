#!/bin/sh
# install.sh - the stranger's entry point (public portable edition). A shim only: it finds
# (or, with your yes, installs) a python3 of 3.11+, then hands over to the real installer,
# capsule/install.py. All options go through:  ./install.sh [--dry-run] [--yes] [--capsule-home DIR]
#   --dry-run  print every step, change nothing      --yes  take the defaults, ask nothing
# Run it from a clone; never pipe it from the network. It never calls deploy.sh or bootstrap.sh.
set -u
REPO="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

if [ "$(uname -s)" != Darwin ]; then
  echo "install.sh: macOS only. On Linux see docs/SETUP.md, section 'Linux, manual'." >&2
  exit 2
fi

new_enough() { "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; }

PY="$(command -v python3 || true)"
if [ -z "$PY" ] || ! new_enough "$PY"; then
  dry=0; yes=0
  for a in "$@"; do
    case "$a" in --dry-run) dry=1 ;; --yes) yes=1 ;; esac
  done
  if [ "$dry" = 1 ]; then
    echo "WOULD brew install uv && uv python install 3.11 (no python3 of 3.11+ found)"
    exit 0
  fi
  command -v brew >/dev/null 2>&1 || { echo "install.sh: install Homebrew first: https://brew.sh" >&2; exit 1; }
  if [ "$yes" != 1 ]; then
    printf 'No python3 of 3.11+ found. Run: brew install uv && uv python install 3.11 ? [y/N] '
    read -r answer || answer=n
    case "$answer" in y|Y|yes) ;; *) echo "install.sh: stopped; install Python 3.11+ and rerun" >&2; exit 1 ;; esac
  fi
  brew install uv && uv python install 3.11 && PY="$(uv python find 3.11)" || exit 1
fi

PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}" exec "$PY" -P -m capsule install "$@"
