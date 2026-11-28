#!/usr/bin/env bash
# deps_sync.sh - thin starter (D97, docs/shell-to-python.md): the logic, the usage text and the
# design notes live in maintenance/deps_sync.py. This path stays so run.sh, the RUNBOOK and any
# launchd entry keep working. Usage: deps_sync.sh [--check] [--force]
set -u
MAINT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$MAINT_DIR/deps_sync.py" "$@"
