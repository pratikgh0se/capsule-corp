#!/usr/bin/env bash
# grafana_prune_folders.sh (W346) - delete the three empty Grafana folders left over from before
# the Grafana 13 rebuild ("Agent Ops", "Archive", "Capsule Corp") when each looks empty: no dashboard,
# no sub-folder (alert rules also block the delete, forceDeleteRules is never sent). Grafana's folder
# DELETE removes whatever the folder holds, so this is check-then-delete, not atomic: a dashboard added
# in the gap between the check and the delete would be deleted with the folder. Failure lines start
# "grafana prune: FAILED"; any API error leaves that folder (or all of them) untouched.
# A provisioned folder is kept, never FAILED (t_6bb2badd): "Capsule Corp" is where the file-provisioned
# alert rules live, and Grafana refuses to delete it. Provisioned means the folder list says managedBy,
# or an alert rule in it has a provenance (the folder counts API reports 0 alert rules for it, so it
# cannot be used), or the delete itself comes back with "provisioned" in its message. Any other
# delete error is still FAILED, with the HTTP status.
# Idempotent: a folder that is gone or non-empty is left alone. Admin password: the same Infisical
# read the other Grafana scripts use (capsule-secret get, identity compose); it reaches curl on stdin via
# `-K -`, never on an argv, and is never printed.
# Env (tests stub these): GRAFANA_URL, CURL_BIN, CAPSULE_SECRET_BIN.
set -u
REPO="$(cd -P "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
: "${GRAFANA_URL:=http://127.0.0.1:3000}"
: "${CURL_BIN:=curl}"
: "${CAPSULE_SECRET_BIN:=$REPO/bin/capsule-secret}"
FOLDERS=("Agent Ops" "Archive" "Capsule Corp")

pw="$(CAPSULE_IDENTITY=compose "$CAPSULE_SECRET_BIN" get capsule-corp OPS_GRAFANA_PASSWORD 2>/dev/null)" || pw=""
if [ -z "$pw" ]; then echo "grafana prune: FAILED cannot read OPS_GRAFANA_PASSWORD from Infisical"; exit 1; fi
# gcurl API_PATH [curl opts...]: the URL is built on the curl line so the egress check resolves GRAFANA_URL.
gcurl() { local p="$1"; shift; printf 'user = "admin:%s"\n' "$pw" | "$CURL_BIN" -K - -fsS -m 10 "$@" "$GRAFANA_URL/$p"; }
# gdelete UID: DELETE without curl -f so the error body is kept; prints the body, then the HTTP code.
gdelete() { printf 'user = "admin:%s"\n' "$pw" | "$CURL_BIN" -K - -sS -m 10 -X DELETE -w '\n%{http_code}' "$GRAFANA_URL/api/folders/$1"; }
jlen() { python3 -c 'import json,sys; print(len(json.load(sys.stdin)))'; }

all="$(gcurl "api/folders")" || { echo "grafana prune: FAILED cannot list folders"; exit 1; }
rc=0
for title in "${FOLDERS[@]}"; do
  uid="$(printf '%s' "$all" | python3 -c 'import json,sys; t=sys.argv[1]; print(next((f["uid"] for f in json.load(sys.stdin) if f.get("title")==t),""))' "$title")"
  if [ -z "$uid" ]; then echo "grafana prune: '$title' absent"; continue; fi
  managed="$(printf '%s' "$all" | python3 -c 'import json,sys; t=sys.argv[1]; print(next((f.get("managedBy","") for f in json.load(sys.stdin) if f.get("title")==t),""))' "$title")"
  if [ -n "$managed" ]; then echo "grafana prune: '$title' kept (provisioned): managed by $managed"; continue; fi
  case "$uid" in *[!A-Za-z0-9_-]*) echo "grafana prune: FAILED '$title' has an odd uid, left alone"; rc=1; continue ;; esac
  items="$(gcurl "api/search?folderUIDs=$uid")" || { echo "grafana prune: FAILED cannot search '$title'"; rc=1; continue; }
  subs="$(gcurl "api/folders?parentUid=$uid")" || { echo "grafana prune: FAILED cannot list sub-folders of '$title'"; rc=1; continue; }
  n="$(printf '%s' "$items" | jlen)" || n=1
  s="$(printf '%s' "$subs" | jlen)" || s=1
  if [ "$n" != 0 ] || [ "$s" != 0 ]; then echo "grafana prune: '$title' is not empty ($n items, $s sub-folders), kept"; continue; fi
  rules="$(gcurl "api/v1/provisioning/alert-rules")" || { echo "grafana prune: FAILED cannot list alert rules for '$title'"; rc=1; continue; }
  r="$(printf '%s' "$rules" | python3 -c 'import json,sys; u=sys.argv[1]; print(sum(1 for x in json.load(sys.stdin) if x.get("folderUID")==u and x.get("provenance")))' "$uid")" || r=""
  case "$r" in "" | *[!0-9]*) echo "grafana prune: FAILED cannot read alert rules for '$title'"; rc=1; continue ;; esac
  if [ "$r" != 0 ]; then echo "grafana prune: '$title' kept (provisioned): $r alert rules from files"; continue; fi
  res="$(gdelete "$uid")" || { echo "grafana prune: FAILED delete of '$title' failed (no answer)"; rc=1; continue; }
  code="${res##*$'\n'}"; body="${res%$'\n'*}"
  case "$code" in
    2??) echo "grafana prune: '$title' deleted" ;;
    *) if printf '%s' "$body" | grep -qi provisioned; then echo "grafana prune: '$title' kept (provisioned): Grafana refused the delete"
       else echo "grafana prune: FAILED delete of '$title' failed (HTTP $code)"; rc=1; fi ;;
  esac
done
exit "$rc"
