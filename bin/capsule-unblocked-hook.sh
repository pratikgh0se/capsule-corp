#!/usr/bin/env bash
# capsule-unblocked-hook.sh - Claude Code hooks around `python3 -m capsule unblocked`
# (row 470: no session or agent should ever have to guess whether there is unclaimed,
# unblocked work). Two modes:
#
#   session-start   prints a short context block (top 10 unowned items + counts) to
#                   stdout. Wired as a SessionStart hook.
#   stop            reads the Stop hook's JSON on stdin, runs the detector LIVE (never the
#                   cache). Blocks (prints
#                     {"decision":"block","reason":"UNBLOCKED WORK: <m> unowned items -
#                     dispatch or state the blocker per item"} (plus the BUILDER SEATS line, below)
#                   and exits 0) only when `capsule.unblocked_stop.should_block` (row 558)
#                   says the unowned (item, blocker) set changed since the last block, or 30
#                   min passed unchanged - never merely because UNOWNED > 0 (row 558: ~30
#                   turns in one evening restated an identical 25-item list before this
#                   fix). State: $CAPSULE_HOME/unblocked-stop.json, global across
#                   sessions/terminals; missing/corrupt state blocks (fail loud). Wired
#                   as a Stop hook; when its own JSON says `"stop_hook_active": true`
#                   (Claude Code already re-invoked it once for this stop) it exits 0
#                   silently - re-blocking on our own previous block would loop forever.
#
#                   Seats (owner, 2026-10-18): the UNBLOCKED line is dropped while every heavy
#                   seat is busy (a nudge with nowhere to dispatch is pure token cost).
#                   Usage: stop [--heavy-cap N] [--max-age-hours H] (defaults 5 and 3; cap
#                   was 3 until D155). A
#                   seat is a fresh `bin/capsule-own` implementer claim in any
#                   $CAPSULE_HOME/queue/*/owners.json; claims record only a stamp time (no pid),
#                   so "stale" means older than H hours (a future stamp is invalid). A run is its
#                   BRANCH claim; a card-id claim (`t_<hex>`) is an alias of the same run and never
#                   a seat (one claim per run: the branch). This 3h window is deliberately longer
#                   than unblocked.py's 40 min ownership heartbeat - do not align them. The merge-keeper has its own seat and
#                   never counts. Reviewers do not claim, so there is no review-seat signal.
#
#                   Builder seats (D155, D154 keeps 5-9 builders busy): busy = fresh implementer
#                   claims on `build/*` branches (same freshness rule). While busy < cap the
#                   block reason gains "BUILDER SEATS: <busy>/<cap> busy - start independent
#                   builds (D154) or state why not", and it blocks even with nothing unowned.
#                   Own rate limit: the seats line has its own 30 min window in its own state
#                   file ($CAPSULE_HOME/unblocked-seats-stop.json), never touched by the pair
#                   gate above (and vice versa); a stop with every build seat busy leaves it
#                   untouched. Only for a cwd under this checkout ($REPO): other repos' sessions
#                   never see the line nor use up the window. If the detector fails or times
#                   out the seat gate still runs alone and the pair gate's state is left alone.
#                   Logic: capsule/unblocked_stop.py main + should_block + _seats_block.
#
# Both modes act only when:
#   - the hook input's own `cwd` is under one of the configured repos
#     ($CAPSULE_UNBLOCKED_REPOS, colon-separated; default the host.json `repos` paths
#     (built-in: this checkout) plus `vault.path` (its env override wins)),
#     AND
#   - env `CAPSULE_WORKER` is not `1` (that marks this as a dispatched worker's own
#     process, set by `capsule/models/claude.py`'s `_clean_env` - a worker judged
#     against a turn budget and a structured-output contract must never have its own
#     tick-owned session steered by a hook meant for the owner's terminal).
# Outside either condition this script exits 0 having printed nothing at all - not even
# a warning, since "not one of my repos" / "this is a worker" is the normal case, not a
# failure.
#
# Both modes must finish in well under 3s and never fail the session on their own
# account: every internal error is a one-line warning on stderr and exit 0, never a
# non-zero exit and never a raw traceback. The session-start path's own detector result
# is cached for 60s under $CAPSULE_UNBLOCKED_CACHE (default ~/.cache/capsule/unblocked.json)
# so a SessionStart firing seconds after another one does not re-run the whole detector;
# the stop path never reads or writes that cache - a stop decision must always
# reflect the current state, not a snapshot from up to a minute ago.
#
# This script never writes anything besides its own cache file and the Stop gate's
# unblocked-stop.json (row 558), never touches a secret, and never blocks by itself - it
# prints Claude Code's own block/allow protocol and lets the harness decide. A crash in
# the stop gate itself is never silent - stderr is not redirected, and a nonzero exit warns.
set -uo pipefail
# Headless chief-of-staff runs (board-stall-gate.py sets BOARD_STALL_COS=1) get no nudges.
[ "${BOARD_STALL_COS:-}" = 1 ] && exit 0

REPO="$(cd -P "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
CACHE_FILE="${CAPSULE_UNBLOCKED_CACHE:-$HOME/.cache/capsule/unblocked.json}"
CACHE_TTL="${CAPSULE_UNBLOCKED_CACHE_TTL:-60}"
TIMEOUT_S="${CAPSULE_UNBLOCKED_TIMEOUT:-2}"
PY="${CAPSULE_UNBLOCKED_PYTHON:-python3}"
# CAPSULE_HOME rule (row 609; the same block is in every script that defaults it, = capsule/paths.py home()): explicit wins; under AGENT_OPS_NO_SEND a temp dir, never the live home.
# Known differences from tempfile.gettempdir(): TEMP/TMP are not read without TMPDIR; a missing or unwritable TMPDIR falls back to /tmp only in Python; only one trailing slash is stripped; a relative TMPDIR is not made absolute.
if [ -z "${CAPSULE_HOME:-}" ]; then
  if [ -n "${AGENT_OPS_NO_SEND:-}" ]; then CAPSULE_HOME="${TMPDIR:-/tmp}"; CAPSULE_HOME="${CAPSULE_HOME%/}/capsule-home-no-send"
  else CAPSULE_HOME="$HOME/.local/share/capsule"; fi
fi
# Repo list: explicit env wins, else host.json `repos` paths (built-in: this checkout) plus `vault.path` (its env override wins).
host_get() { (cd "$REPO" && PYTHONPATH="$REPO" CAPSULE_HOME="$CAPSULE_HOME" "$PY" -P -m capsule host get --only "$1" 2>/dev/null); }
if [ -n "${CAPSULE_UNBLOCKED_REPOS:-}" ]; then REPOS="$CAPSULE_UNBLOCKED_REPOS"
else REPOS="$({ host_get repos | cut -f2; host_get vault.path; } | grep . | paste -sd: -)"; fi

warn() { printf 'capsule-unblocked-hook: %s\n' "$*" >&2; }

# run_with_timeout SECS cmd... - stdout of cmd on success; empty stdout and a non-zero
# return on any failure or on timeout. No GNU coreutils dependency (macOS ships neither
# `timeout` nor `gtimeout` by default), just a poll loop on the child's pid.
run_with_timeout() {
    local secs="$1"; shift
    local out; out="$(mktemp "${TMPDIR:-/tmp}/capsule-unblocked.XXXXXX")" || return 1
    ("$@" >"$out" 2>/dev/null) &
    local pid=$! waited=0
    while kill -0 "$pid" 2>/dev/null; do
        sleep 0.1
        waited=$((waited + 1))
        if [ "$waited" -ge $((secs * 10)) ]; then
            kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null
            rm -f "$out"
            return 124
        fi
    done
    wait "$pid"; local rc=$?
    cat "$out"; rm -f "$out"
    return "$rc"
}

cache_is_fresh() {
    [ -f "$CACHE_FILE" ] || return 1
    local now mtime
    now="$(date +%s)" || return 1
    mtime="$(stat -c %Y "$CACHE_FILE" 2>/dev/null || stat -f %m "$CACHE_FILE" 2>/dev/null)" || return 1
    [ -n "$mtime" ] && [ $((now - mtime)) -lt "$CACHE_TTL" ]
}

# run_detector_live - the detector's raw --json output, always a fresh run, never the
# cache. Prints "" and returns 1 on any failure - callers treat that exactly like "the
# detector could not run", never a crash.
run_detector_live() {
    local out rc
    out="$(cd "$REPO" && run_with_timeout "$TIMEOUT_S" env PYTHONPATH="$REPO" "$PY" -P -m capsule unblocked --json)"
    rc=$?
    if [ "$rc" != 0 ] && [ "$rc" != 1 ]; then    # 0 = m==0, 1 = m>0 (both are a real report)
        return 1
    fi
    [ -n "$out" ] || return 1
    printf '%s' "$out"
    return 0
}

# get_report_cached - the detector's --json report, from the 60s cache when fresh, else
# a live run (itself cached for the next call). Session-start only (the stop path
# calls `run_detector_live` directly and never touches this cache at all).
get_report_cached() {
    if cache_is_fresh && [ -s "$CACHE_FILE" ]; then
        cat "$CACHE_FILE"
        return 0
    fi
    local out
    out="$(run_detector_live)" || return 1
    mkdir -p "$(dirname "$CACHE_FILE")" 2>/dev/null || true
    if printf '%s' "$out" >"${CACHE_FILE}.tmp" 2>/dev/null; then
        mv -f "${CACHE_FILE}.tmp" "$CACHE_FILE" 2>/dev/null || true
    fi
    printf '%s' "$out"
    return 0
}

# parse_hook_input JSON - two lines on stdout: the hook's own `cwd` (or empty), then
# "1"/"0" for `stop_hook_active`. Never raises past this function: unparsable input
# reads as "no cwd, not stop_hook_active" (the safe, do-nothing default).
parse_hook_input() {
    "$PY" -I - "$1" <<'PY' 2>/dev/null
import json, sys
try:
    doc = json.loads(sys.argv[1]) if sys.argv[1].strip() else {}
    if not isinstance(doc, dict):
        doc = {}
except Exception:
    doc = {}
print(doc.get("cwd") or "")
print("1" if doc.get("stop_hook_active") else "0")
PY
}

# cwd_is_allowed CWD [REPOS] - true iff CWD resolves under one of REPOS (default $REPOS; row 470). An empty or unresolvable CWD is never allowed - fail closed (do nothing), not
# fail open (act on a session this hook cannot actually place).
cwd_is_allowed() {
    local cwd="$1" repos="${2:-$REPOS}"
    [ -n "$cwd" ] || return 1
    local resolved
    resolved="$(cd -P "$cwd" 2>/dev/null && pwd -P)" || return 1
    local saved_ifs="$IFS"
    IFS=:
    local r rr
    for r in $repos; do
        IFS="$saved_ifs"
        rr="$(cd -P "$r" 2>/dev/null && pwd -P)" || continue
        case "$resolved" in
            "$rr") return 0 ;;
            "$rr"/*) return 0 ;;
        esac
        IFS=:
    done
    IFS="$saved_ifs"
    return 1
}

mode_session_start() {
    local stdin_json="$1"
    local report
    if ! report="$(get_report_cached)" || [ -z "$report" ]; then
        warn "could not run the detector; skipping session-start context"
        exit 0
    fi
    "$PY" -I - "$report" <<'PY' 2>/dev/null
import json, sys
try:
    doc = json.loads(sys.argv[1])
except Exception:
    sys.exit(0)
items = [i for i in doc.get("items", []) if i.get("state") == "unblocked" and not i.get("owner")]
print(f"capsule unblocked: UNBLOCKED {doc.get('unblocked', 0)} UNOWNED {doc.get('unowned', 0)}")
for i in items[:10]:
    blocker = i.get("blocker") or "-"
    print(f"- [{i.get('source')}] {i.get('item')} (blocker: {blocker})")
if len(items) > 10:
    print(f"... and {len(items) - 10} more")
PY
    exit 0
}

mode_stop() {
    local stdin_json="$1" stop_hook_active="$2" seats_here="$3"; shift 3
    if [ "$stop_hook_active" = "1" ]; then
        exit 0    # Claude Code already re-invoked us once for this stop - never loop
    fi
    local cap=5 hours=3
    while [ $# -gt 0 ]; do
        case "$1" in
            --heavy-cap) cap="${2:-}"; shift 2 || shift ;;
            --max-age-hours) hours="${2:-}"; shift 2 || shift ;;
            *) warn "unknown stop argument: $1"; shift ;;
        esac
    done
    case "$cap" in ''|0*|*[!0-9]*) warn "bad --heavy-cap '$cap'; using 5"; cap=5 ;; esac
    case "$hours" in ''|*[!0-9.]*) warn "bad --max-age-hours '$hours'; using 3"; hours=3 ;; esac
    local report
    if ! report="$(run_detector_live)" || [ -z "$report" ]; then    # never cached
        [ "$seats_here" = 1 ] || { warn "could not run the detector; not blocking stop"; exit 0; }
        warn "could not run the detector; checking builder seats only"
        report=""    # D155: a slow detector must not leave idle seats unflagged; "" skips the pair gate
    fi
    # row 558 + D155 logic lives in capsule/unblocked_stop.py main (Z7/Z8); cd into $REPO so `import
    # capsule` resolves. No 2>/dev/null, exit status not discarded - a crash is audible.
    (cd "$REPO" && PYTHONPATH="$REPO" "$PY" -P -m capsule.unblocked_stop "$report" "$CAPSULE_HOME/queue" "$cap" "$hours" "$seats_here"
    ) || warn "stop gate failed; not blocking stop"
    exit 0
}

main() {
    local mode="${1:-}"
    case "$mode" in
        session-start|stop) : ;;
        *) warn "usage: capsule-unblocked-hook.sh <session-start|stop>"; exit 0 ;;
    esac

    if [ "${CAPSULE_WORKER:-}" = "1" ]; then
        exit 0    # a dispatched worker's own process, never steered by this hook
    fi

    local stdin_json; stdin_json="$(cat)"    # stop's stdin is parsed, never discarded
    local parsed cwd stop_hook_active
    parsed="$(parse_hook_input "$stdin_json")"
    cwd="$(printf '%s\n' "$parsed" | sed -n '1p')"
    stop_hook_active="$(printf '%s\n' "$parsed" | sed -n '2p')"

    if ! cwd_is_allowed "$cwd"; then
        exit 0    # only act when the hook's own cwd is one of the configured repos
    fi

    local seats_here=0    # D155: the builder-seats nag only fires for a session under this checkout
    cwd_is_allowed "$cwd" "$REPO" && seats_here=1
    case "$mode" in
        session-start) mode_session_start "$stdin_json" ;;
        stop) mode_stop "$stdin_json" "$stop_hook_active" "$seats_here" "${@:2}" ;;
    esac
}

main "$@" || { warn "unexpected failure; not failing the session"; exit 0; }
