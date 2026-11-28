#!/usr/bin/env bash
# Runs docker/base/ui-check against a real headless Chromium and a real
# HTTP server: a two-line page with one missing asset (404) and one
# console.error. Before C0 item 3 this printed "FAILED REQUESTS: 0" and exited
# 0 - Playwright completes a 4xx through requestfinished, and only
# requestfailed was watched.
#
# Playwright-gated, the way tests/test_fluent_bit_scrub.sh is docker-gated:
# skips (exit 77, prints `SKIP test_ui_check: <reason>`, which tests/run_shell_suites.sh counts) when the
# interpreter has no playwright, or when its Chromium is not installed - which
# is the normal case on a laptop, since ui-check's real home is the worker
# image. Serves only on 127.0.0.1 out of its own temp dir; no docker, no
# network, nothing written outside "$T".
set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

PY="${UI_CHECK_PYTHON:-}"
if [ -z "$PY" ]; then
  . "$REPO/tests/lib_playwright_python.sh"
  PY="$(playwright_python)" || PY=""
fi
[ -z "$PY" ] && { printf 'SKIP test_ui_check: no interpreter with playwright + chromium: export CAPSULE_PLAYWRIGHT_PYTHON (RUNBOOK "ui-check on the host")\n'; exit 77; }

T="$(mktemp -d)"; SRV=""
cleanup() { [ -n "$SRV" ] && kill "$SRV" 2>/dev/null; wait "$SRV" 2>/dev/null; rm -rf "$T"; }
trap cleanup EXIT

fails=0
check() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; fails=$((fails+1)); fi; }

# One missing asset (<img src="missing.png"> -> 404) and one console error.
cat >"$T/index.html" <<'HTML'
<!doctype html><title>guard fixture</title>
<body><h1>hello guard</h1><img src="missing.png" alt="gone">
<script>console.error("deliberate console error");</script></body>
HTML
cat >"$T/clean.html" <<'HTML'
<!doctype html><title>clean fixture</title><body><h1>hello clean</h1></body>
HTML

PORT="$("$PY" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"
( cd "$T" && exec "$PY" -m http.server "$PORT" --bind 127.0.0.1 >/dev/null 2>&1 ) &
SRV=$!
for _ in 1 2 3 4 5 6 7 8 9 10; do
  "$PY" -c "import socket,sys; s=socket.socket(); sys.exit(0 if s.connect_ex(('127.0.0.1',$PORT))==0 else 1)" && break
  "$PY" -c "import time; time.sleep(0.3)"
done

run() { UI_CHECK_PYTHON="$PY" "$REPO/docker/base/ui-check" "$@" >"$T/out" 2>&1; echo $?; }

RC="$(run dump "http://127.0.0.1:$PORT/index.html")"
check "dirty page exits 4" '[ "$RC" = 4 ]'
check "dirty page still dumps its text" 'grep -q "hello guard" "$T/out"'
check "the 404 is a failed request" 'grep -Eq "^FAILED REQUESTS: [1-9]" "$T/out"'
check "the 404 url is named" 'grep -q "missing.png" "$T/out"'
check "the console error is counted" 'grep -Eq "^CONSOLE ERRORS: [1-9]" "$T/out"'
check "the console error text is named" 'grep -q "deliberate console error" "$T/out"'
check "the failure line explains the exit" 'grep -q "ui-check: FAILED" "$T/out"'

RC="$(run --allow-errors dump "http://127.0.0.1:$PORT/index.html")"
check "--allow-errors exits 0" '[ "$RC" = 0 ]'
check "--allow-errors still reports" 'grep -Eq "^CONSOLE ERRORS: [1-9]" "$T/out"'
check "--allow-errors says it ignored them" 'grep -q "not failing on the above" "$T/out"'

RC="$(run dump "http://127.0.0.1:$PORT/clean.html")"
check "clean page exits 0" '[ "$RC" = 0 ]'
check "clean page reports zero of each" 'grep -q "^CONSOLE ERRORS: 0" "$T/out" && grep -q "^FAILED REQUESTS: 0" "$T/out"'

RC="$(run expect "http://127.0.0.1:$PORT/clean.html" "hello clean")"
check "expect still passes on a clean page" '[ "$RC" = 0 ] && grep -q FOUND "$T/out"'
RC="$(run expect "http://127.0.0.1:$PORT/clean.html" "not there")"
check "expect still fails with 1 on a clean page" '[ "$RC" = 1 ] && grep -q "NOT FOUND" "$T/out"'
RC="$(run expect "http://127.0.0.1:$PORT/index.html" "hello guard")"
check "a found needle on a dirty page still exits 4" '[ "$RC" = 4 ]'

RC="$(run shot "http://127.0.0.1:$PORT/clean.html" "$T/shot.png")"
check "shot still writes a png and exits 0" '[ "$RC" = 0 ] && [ -s "$T/shot.png" ]'
RC="$(run shot "http://127.0.0.1:$PORT/index.html" "$T/dirty.png")"
check "shot on a dirty page writes the png and exits 4" '[ "$RC" = 4 ] && [ -s "$T/dirty.png" ] && grep -Eq "^CONSOLE ERRORS: [1-9]" "$T/out"'

RC="$(run dump "http://example.com/")"
check "non-local host still refused with 3" '[ "$RC" = 3 ]'
RC="$(run bogus "http://127.0.0.1:$PORT/clean.html")"
check "unknown command still exits 2" '[ "$RC" = 2 ]'

[ "$fails" -eq 0 ] && echo "ALL PASS test_ui_check" || echo "$fails FAILED test_ui_check"
exit $((fails > 0))
