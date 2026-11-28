#!/usr/bin/env bash
# tests/test_sbom.sh - maintenance/sbom.sh with fake syft/grype/osv-scanner on
# PATH, with the tools absent, and over the OPS_SBOM_GATE truth table. No real
# tool is ever run and nothing leaves the machine.
set -u
R="$(cd "$(dirname "$0")/.." && pwd)"; T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
pass=0; fail=0
ok(){ echo "PASS $1"; pass=$((pass+1)); }; bad(){ echo "FAIL $1"; fail=$((fail+1)); }
OUT="$T/out"; BIN="$T/bin"; mkdir -p "$BIN"

mk_syft() {  # writes both output files wherever -o points
  cat > "$BIN/syft" <<'EOF'
#!/usr/bin/env bash
for a in "$@"; do case "$a" in *=*) f="${a#*=}"; mkdir -p "$(dirname "$f")"; echo '{"fake":"sbom"}' > "$f" ;; esac; done
EOF
  chmod +x "$BIN/syft"
}
mk_tool() { printf '#!/usr/bin/env bash\nexit %s\n' "$2" > "$BIN/$1"; chmod +x "$BIN/$1"; }

# --- tools absent: three skip lines, exit 0 -----------------------------------
out="$(PATH="$BIN:/usr/bin:/bin" OPS_SBOM_GATE=1 bash "$R/maintenance/sbom.sh" all "$OUT" 2>&1)"; rc=$?
[ $rc -eq 0 ] && ok "absent tools exit 0 even with the gate on" || bad "absent tools exited $rc"
echo "$out" | grep -q "sbom: skipped (syft not on PATH)" && ok "syft absent says so" || bad "no syft skip line"
echo "$out" | grep -q "scan: skipped (no sbom to scan)" && ok "scan skips with no sbom" || bad "scan did not skip"

# --- syft present: two formats ------------------------------------------------
mk_syft
out="$(PATH="$BIN:/usr/bin:/bin" bash "$R/maintenance/sbom.sh" sbom "$OUT" 2>&1)"
[ -f "$OUT/sbom.spdx.json" ] && ok "SPDX written" || bad "no SPDX"
[ -f "$OUT/sbom.cdx.json" ] && ok "CycloneDX written" || bad "no CycloneDX"
echo "$out" | grep -q "SPDX + CycloneDX" && ok "both formats reported" || bad "formats not reported"

# --- grype absent, osv present and clean --------------------------------------
mk_tool osv-scanner 0
out="$(PATH="$BIN:/usr/bin:/bin" bash "$R/maintenance/sbom.sh" scan "$OUT" 2>&1)"; rc=$?
echo "$out" | grep -q "grype: skipped (grype not on PATH)" && ok "grype absent says so" || bad "no grype skip"
echo "$out" | grep -q "osv: clean" && ok "clean osv reported" || bad "osv not reported clean"
[ $rc -eq 0 ] && ok "clean scan exits 0" || bad "clean scan exited $rc"

# --- grype finds something: advisory by default, blocking with the flag -------
mk_tool grype 1
out="$(PATH="$BIN:/usr/bin:/bin" bash "$R/maintenance/sbom.sh" scan "$OUT" 2>&1)"; rc=$?
echo "$out" | grep -q "grype: findings at high or above" && ok "findings reported" || bad "findings not reported"
[ $rc -eq 0 ] && ok "findings are advisory with the flag unset" || bad "advisory run exited $rc"
PATH="$BIN:/usr/bin:/bin" OPS_SBOM_GATE=0 bash "$R/maintenance/sbom.sh" scan "$OUT" >/dev/null 2>&1
[ $? -eq 0 ] && ok "OPS_SBOM_GATE=0 is advisory" || bad "flag 0 blocked"
PATH="$BIN:/usr/bin:/bin" OPS_SBOM_GATE=1 bash "$R/maintenance/sbom.sh" scan "$OUT" >/dev/null 2>&1
[ $? -eq 1 ] && ok "OPS_SBOM_GATE=1 blocks on a finding" || bad "flag 1 did not block"

# --- an unknown subcommand is a usage error, not a silent success -------------
PATH="$BIN:/usr/bin:/bin" bash "$R/maintenance/sbom.sh" nonsense "$OUT" >/dev/null 2>&1
[ $? -eq 2 ] && ok "unknown subcommand exits 2" || bad "unknown subcommand did not exit 2"

echo; if [ $fail -eq 0 ]; then echo "test_sbom: all passed ($pass)"; else echo "test_sbom: $fail failed"; exit 1; fi
