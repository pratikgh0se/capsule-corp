#!/usr/bin/env bash
# maintenance/sbom.sh - SBOM (gate G3) and vulnerability scan (gate G4).
# Decision row 140.
#
# Two formats because the legal study's G3 asks for both: SPDX for CRA Annex I
# Part II(1), CycloneDX for the enterprise SBOM clauses that ask by name.
# Every tool is optional and every absence is a printed skip line - the laptop
# has none of these binaries today, and a missing tool must never look like a
# clean result. CI does not call this script: verify.yml's syft-sbom and
# vulnerabilities steps use pinned actions that install their own tools. Advisory by default: findings print and exit 0 unless
# OPS_SBOM_GATE=1. Nothing here reads a secret; syft/grype/osv-scanner see the
# checkout and their own databases, which is why they run in CI, not in a
# worker (`docker_network: none`).
set -uo pipefail
usage() { cat <<'USAGE'
usage: sbom.sh [sbom | scan | all] [outdir] | -h | --help
  sbom          write the SPDX and CycloneDX files with syft
  scan          scan the SPDX with grype and osv-scanner
  all (default) sbom, then scan
  outdir        where the files go (default: <state dir>/sbom)
  -h, --help    print this usage and exit 0, doing nothing
Any other argument prints this usage to stderr and exits 2, doing nothing.
Environment: OPS_SBOM_GATE (1 = findings fail the scan), AGENT_OPS_STATE_DIR, AGENT_OPS_NO_SEND,
  TMPDIR, HOME, PATH (syft, grype, osv-scanner)
USAGE
}
case "$#:${1:-}" in
  0:) ;;
  1:-h|1:--help) usage; exit 0 ;;
  1:sbom|1:scan|1:all|2:sbom|2:scan|2:all) ;;
  *) usage >&2; exit 2 ;;
esac
CMD="${1:-all}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
. "$REPO/maintenance/lib_state.sh"
OUT="${2:-$STATE_DIR/sbom}"
GATE="${OPS_SBOM_GATE:-0}"
SPDX="$OUT/sbom.spdx.json"; CDX="$OUT/sbom.cdx.json"
have() { command -v "$1" >/dev/null 2>&1; }

do_sbom() {
  if ! have syft; then echo "sbom: skipped (syft not on PATH)"; return 0; fi
  mkdir -p "$OUT"
  if syft "dir:$REPO" -o "spdx-json=$SPDX" -o "cyclonedx-json=$CDX" >"$OUT/syft.log" 2>&1 \
     && [ -s "$SPDX" ]; then
    echo "sbom: wrote $SPDX and $CDX (SPDX + CycloneDX)"
  else
    echo "sbom: skipped (syft failed - see $OUT/syft.log)"
  fi
  return 0
}

do_scan() {
  if [ ! -s "$SPDX" ]; then echo "scan: skipped (no sbom to scan)"; return 0; fi
  local found=0
  if have grype; then
    if grype "sbom:$SPDX" --fail-on high >"$OUT/grype.txt" 2>&1; then
      echo "grype: clean (nothing at high or above)"
    else
      echo "grype: findings at high or above - see $OUT/grype.txt"; found=1
    fi
  else
    echo "grype: skipped (grype not on PATH)"
  fi
  if have osv-scanner; then
    if osv-scanner --sbom="$SPDX" >"$OUT/osv.txt" 2>&1; then
      echo "osv: clean"
    else
      echo "osv: findings - see $OUT/osv.txt"; found=1
    fi
  else
    echo "osv: skipped (osv-scanner not on PATH)"
  fi
  if [ "$found" = 1 ]; then
    if [ "$GATE" = 1 ]; then echo "scan: FAILED (OPS_SBOM_GATE=1)"; return 1; fi
    echo "scan: advisory - findings above, OPS_SBOM_GATE=1 to block"
  fi
  return 0
}

rc=0
case "$CMD" in
  sbom) do_sbom || rc=$? ;;
  scan) do_scan || rc=$? ;;
  all)  do_sbom; do_scan || rc=$? ;;
esac
exit $rc
