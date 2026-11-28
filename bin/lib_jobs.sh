#!/usr/bin/env bash
# lib_jobs.sh - the plist classification lists, shared by capsule-park.sh and
# hoi-poi.sh (spec docs/superpowers/specs/2026-09-12-hoi-poi-design.md
# section 3). The caller must set REPO before sourcing this file (classify_check
# reads launchd/templates/ off it).
#
# Every label in launchd/templates/ is in exactly one of DISPATCHING/CUSTODIAL.
# A new plist that is in none of them is reported as unclassified
# rather than silently left running - "I forgot to park it" is how a month of
# unapproved specs happens.
# The emerge job was retired (its plist lives in launchd/disabled/,
# deploy.sh boots it out); a label here with no plist made hoi-poi status read
# 22/23 forever and `up` try to bootstrap a file that does not exist (row 210).
# The capsule-* labels carry the machine's launchd prefix (P03): CAPSULE_LAUNCHD_PREFIX, resolved by lib_prefix.sh.
# The notes-vault/ollama-serve jobs are the owner's overlay: their template files carry a literal
# label prefix in the file name (installed as-is by deploy.sh), so _OP is read off those files;
# with no overlay templates it is the machine prefix.
. "$(dirname "${BASH_SOURCE[0]}")/lib_prefix.sh"
CAPSULE_LAUNCHD_PREFIX="$(capsule_launchd_prefix)" || exit 1
_P="$CAPSULE_LAUNCHD_PREFIX"
_OP="$_P"; _seen=""
for _f in "$REPO"/launchd/templates/*.obsidian-*.plist; do
  [ -e "$_f" ] || continue
  _f="${_f##*/}"; _f="${_f%%.obsidian-*}"
  [ -n "$_seen" ] || _OP="$_f"
  case " $_seen " in *" $_f "*) ;; *) _seen="$_seen $_f" ;; esac
done
case "${_seen# }" in *" "*) echo "lib_jobs: WARNING overlay templates carry more than one prefix:$_seen" >&2 ;; esac
unset _f _seen
OLLAMA_LABEL="$(capsule_ollama_label "$REPO")" || exit 1
# Per-board tick labels (D269): one per board this host serves (`capsule host boards`) that has a
# launchd/templates/__PREFIX__.capsule-tick-<board>.plist; no boards served, no per-board labels.
_TICKS=()
for _b in $(cd "$REPO" && PYTHONPATH="$REPO" "${PY:-python3}" -P -m capsule host boards 2>/dev/null); do
  [ -f "$REPO/launchd/templates/__PREFIX__.capsule-tick-$_b.plist" ] && _TICKS+=("${_P}.capsule-tick-$_b")
done
unset _b
DISPATCHING=(
  ${_OP}.obsidian-sprint-plan ${_OP}.obsidian-sprint-sync
  ${_OP}.obsidian-nightly ${_OP}.obsidian-morning
  ${_OP}.obsidian-inbox ${_OP}.obsidian-architect
  ${_OP}.obsidian-weekly
  ${_OP}.obsidian-agent-review ${_OP}.obsidian-retrieval-eval
  ${_TICKS[@]+"${_TICKS[@]}"}
)
CUSTODIAL=(
  ${_P}.capsule-corp-heal ${_P}.capsule-corp-backup
  ${_P}.capsule-corp-restore-verify ${_P}.capsule-corp-cleanup
  ${_P}.capsule-corp-obs-probe ${_P}.capsule-corp-obs-inventory
  ${_P}.capsule-corp-fork-sync ${_P}.capsule-corp-live-snapshot
  ${_P}.capsule-corp-quality-nightly ${_P}.capsule-corp-deps-sync
  ${_P}.capsule-corp-quality-full
  ${_OP}.obsidian-health ${_OP}.obsidian-report ${_OP}.obsidian-close
  ${_OP}.obsidian-blocked-tonight ${_OP}.obsidian-order-book-lint ${_OP}.obsidian-remove-card
  ${_OP}.obsidian-security $OLLAMA_LABEL ${_OP}.obsidian-ops-preview
  ${_P}.capsule-corp-keeper-watch ${_P}.capsule-corp-blind-mutation
  ${_P}.capsule-corp-disk-relief ${_P}.capsule-corp-docker-idle
  ${_P}.capsule-corp-slack-listen ${_P}.capsule-corp-brainstorm
  ${_P}.capsule-corp-patrol
)

# launchd/unloaded (factory-autonomy spec 4.3, ruling Q10): a label listed there stays installed and
# classified but is never loaded - not by deploy.sh, `hoi-poi up`, or `capsule unpark`.
# classify_check still reads the full lists (ALL_CLASSIFIED).
ALL_CLASSIFIED=("${DISPATCHING[@]}" "${CUSTODIAL[@]}")
UNLOADED_LABELS=()
_uf="${CAPSULE_LAUNCHD_UNLOADED:-$REPO/launchd/unloaded}"
if [ -f "$_uf" ]; then
  while IFS= read -r _l || [ -n "$_l" ]; do
    _l="${_l#"${_l%%[![:space:]]*}"}"; _l="${_l%"${_l##*[![:space:]]}"}"   # trim, CR included (deploy.sh does the same)
    case "$_l" in ''|'#'*) ;; *) UNLOADED_LABELS+=("${_l//__PREFIX__/$_P}") ;; esac
  done <"$_uf"
fi
_loadable() {
  local x
  for x in ${UNLOADED_LABELS[@]+"${UNLOADED_LABELS[@]}"}; do [ "$x" = "$1" ] && return 1; done
  return 0
}
_kept=(); for _l in "${DISPATCHING[@]}"; do _loadable "$_l" && _kept+=("$_l"); done; DISPATCHING=(${_kept[@]+"${_kept[@]}"})
_kept=(); for _l in "${CUSTODIAL[@]}"; do _loadable "$_l" && _kept+=("$_l"); done; CUSTODIAL=(${_kept[@]+"${_kept[@]}"})
unset _kept _uf

# PIPELINE_JOBS: what Hoi-Poi loads on up and boots out on down. ollama-serve
# is a plist but an app in Hoi-Poi's eyes (verified, never stopped).
PIPELINE_JOBS=()
for _l in "${DISPATCHING[@]}" "${CUSTODIAL[@]}"; do
  [ "$_l" = "$OLLAMA_LABEL" ] || PIPELINE_JOBS+=("$_l")
done
unset _l

: "${CLASSIFY_PREFIX:=capsule-park}"
classify_check() {
  local f l unclassified=""
  for f in "$REPO"/launchd/templates/*.plist; do
    [ -f "$f" ] || continue
    l="$(basename "$f" .plist)"; l="${l//__PREFIX__/$_P}"
    printf '%s\n' "${ALL_CLASSIFIED[@]}" | grep -qxF "$l" \
      || unclassified="$unclassified $l"
  done
  [ -z "$unclassified" ] || echo "$CLASSIFY_PREFIX: WARNING unclassified plists (left running):$unclassified"
}
