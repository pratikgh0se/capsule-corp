#!/usr/bin/env bash
# lib_prefix.sh - the one place that resolves the launchd label prefix of the capsule-* jobs
# (D261, public portable edition P03/P05). Source it, then:
#   CAPSULE_LAUNCHD_PREFIX="$(capsule_launchd_prefix)" || exit 1
# Resolution order: env CAPSULE_LAUNCHD_PREFIX > host.json launchd.label_prefix (read through
# `capsule host get`, which owns the validation) > dev.capsule. A host that keeps older job labels
# sets {"launchd": {"label_prefix": "<its prefix>"}} in $CAPSULE_HOME/host.json; nothing is detected
# from the plists installed. An invalid value (env or host.json) is refused; a reader that cannot run
# at all (no python3, one older than 3.11; CAPSULE_PY names the interpreter to use) is an error, never a
# guess; so is a resolved prefix with no installed jobs while another prefix's jobs are installed
# (host.json not set before deploy), and a prefix that comes only from the dev.capsule default while jobs
# are installed under two or more prefixes (ambiguous). Env is explicit and skips these guards.
_CAPSULE_LIB_REPO="$(cd -P "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
capsule_launchd_prefix() {
  # LC_ALL=C: under bash 3.2 + UTF-8 the range A-Z is locale-ordered and accepts e-acute, sharp s, fullwidth A.
  local LC_ALL=C p="${CAPSULE_LAUNCHD_PREFIX:-}" err out rc=0 args=()
  if [ -n "$p" ]; then
    case "$p" in *[!A-Za-z0-9.-]*) echo "invalid CAPSULE_LAUNCHD_PREFIX: use letters, digits, . and -" >&2; return 1 ;; esac
    printf '%s\n' "$p"; return 0
  fi
  if [ -z "${CAPSULE_HOME:-}" ] && [ -z "${AGENT_OPS_NO_SEND:-}" ] && [ -n "${AGENT_OPS_HOME:-}" ]; then
    args=(--capsule-home "$AGENT_OPS_HOME/.local/share/capsule")
  fi
  # The reader needs Python 3.11+ (capsule's floor, which also has -P). A stock Mac python3 is 3.9: a caller
  # that knows a newer interpreter (capsule-tick: the one its plist runs) passes it as CAPSULE_PY.
  local py="${CAPSULE_PY:-python3}"
  "$py" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1 || {
    echo "capsule_launchd_prefix: the host.json reader cannot run: it needs Python 3.11+ ($py is missing or older); refusing to guess the prefix, set CAPSULE_PY to a 3.11+ python or CAPSULE_LAUNCHD_PREFIX" >&2; return 1; }
  err="$(mktemp)" || { echo "capsule_launchd_prefix: mktemp failed; refusing to guess the prefix" >&2; return 1; }
  out="$(cd "$_CAPSULE_LIB_REPO" && PYTHONPATH="$_CAPSULE_LIB_REPO" "$py" -s -P -m capsule host get launchd.label_prefix --only ${args[@]+"${args[@]}"} 2>"$err")" || rc=$?
  if [ "$rc" -ne 0 ]; then
    if grep -q '^capsule host: ' "$err"; then
      grep '^capsule host: ' "$err" | tail -n 1 | sed 's/^capsule host: /invalid launchd prefix in host.json: /' >&2
    else
      echo "capsule_launchd_prefix: the host.json reader cannot run (python3 missing or too old?); refusing to guess the prefix, set CAPSULE_LAUNCHD_PREFIX" >&2
    fi
    rm -f "$err"; return 1
  fi
  rm -f "$err"
  local dflt=0; [ -n "$out" ] || dflt=1
  p="${out:-dev.capsule}"
  # Guard (as board_tick's _prefix_problem): jobs installed under another prefix and none under this one
  # means host.json is not set yet; deploying would install a second set of jobs.
  local la="${CAPSULE_LA_DIR:-${LAUNCH_AGENTS_DIR:-${AGENT_OPS_HOME:-$HOME}/Library/LaunchAgents}}" f n other="" mine=0 seen=" "
  for f in "$la"/*.capsule-*.plist; do
    [ -e "$f" ] || continue
    n="${f##*/}"; n="${n%%.capsule-*}"
    case "$seen" in *" $n "*) ;; *) seen="$seen$n " ;; esac
    if [ "$n" = "$p" ]; then mine=1; elif [ -z "$other" ]; then other="$n"; fi
  done
  # Prefix from the default alone (no env, no host.json) and jobs under two or more prefixes: ambiguous.
  if [ "$dflt" = 1 ] && [ "$(printf '%s\n' $seen | wc -l | tr -d ' ')" -ge 2 ]; then
    echo "capsule_launchd_prefix: ambiguous: Capsule jobs are installed under more than one prefix (${seen# }); set launchd.label_prefix in host.json or CAPSULE_LAUNCHD_PREFIX" >&2
    return 1
  fi
  if [ "$mine" = 0 ] && [ -n "$other" ]; then
    echo "capsule_launchd_prefix: Capsule jobs here use prefix $other, none use $p; set launchd.label_prefix in host.json (or CAPSULE_LAUNCHD_PREFIX=$other)" >&2
    return 1
  fi
  printf '%s\n' "$p"
}

# capsule_ollama_label <repo>: label of the ollama-serve job. deploy.sh installs launchd/templates/<label>.plist under
# its literal name, so the label is that file name minus .plist. Fallback order: no ollama template -> the prefix of the
# first overlay template -> the machine prefix. More than one ollama template: warn on stderr, take the first.
capsule_ollama_label() {
  local f first="" n=0 p
  for f in "$1"/launchd/templates/*.ollama-serve.plist; do
    [ -e "$f" ] || continue
    f="${f##*/}"; [ -n "$first" ] || first="${f%.plist}"; n=$((n+1))
  done
  [ "$n" -gt 1 ] && echo "capsule_ollama_label: WARNING more than one ollama-serve template; using $first" >&2
  if [ -n "$first" ]; then printf '%s\n' "$first"; return 0; fi
  for f in "$1"/launchd/templates/*.obsidian-*.plist; do
    [ -e "$f" ] || continue
    f="${f##*/}"; printf '%s.ollama-serve\n' "${f%%.obsidian-*}"; return 0
  done
  p="$(capsule_launchd_prefix)" || return 1
  printf '%s.ollama-serve\n' "$p"
}
