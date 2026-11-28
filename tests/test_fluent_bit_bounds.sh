#!/usr/bin/env bash
# Docker-free pin: every Fluent Bit tail input carries a Mem_Buf_Limit, so a slow or
# failing Loki (HTTP 500 retries) cannot grow the container's memory without bound.
set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONF="${1:-$REPO/observability/fluent-bit/fluent-bit.conf}"
awk '
  function close_input() { if (inp && !lim) { print "FAIL input without Mem_Buf_Limit: " name; bad=1 } }
  /^\[/ { close_input(); inp=($0=="[INPUT]"); lim=0; name=""; if (inp) n++ }
  inp && $1=="Tag" { name=$2 }
  inp && $1=="Mem_Buf_Limit" && $2 ~ /^[0-9]+[KkMm][Bb]?$/ { lim=1 }
  END { close_input()
        if (n < 4) { print "FAIL expected 4 inputs, saw " n; bad=1 }
        if (!bad) print "PASS all " n " inputs bounded"
        exit bad }
' "$CONF"
