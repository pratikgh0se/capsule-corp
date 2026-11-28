#!/usr/bin/env bash
# Docker-free pin: Loki's WAL disk-full threshold is raised above the 0.90 default, so a
# laptop disk that is 90% full does not make Loki reject every log push with HTTP 500.
set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONF="${1:-$REPO/observability/loki/loki.yaml}"
awk '
  /^[^ #]/ { ing=($1=="ingester:"); wal=0 }
  ing && $1=="wal:" { wal=1; next }
  ing && wal && $1=="disk_full_threshold:" { v=$2 }
  END { if (v == "" || v+0 < 0.95 || v+0 >= 1) { print "FAIL ingester.wal.disk_full_threshold=" v " (want 0.95..0.99)"; exit 1 }
        print "PASS disk_full_threshold=" v }
' "$CONF"
