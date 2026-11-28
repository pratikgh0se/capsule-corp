#!/usr/bin/env bash
# Stub curl for tests/test_front_door.sh (W346 prune): logs argv to $PRUNE_LOG, answers by URL.
# Default: "Archive" (uid full) holds one dashboard; "Agent Ops" and "Capsule Corp" are empty.
# PRUNE_MODE=gone: the three folders no longer exist (second run). PRUNE_MODE=err: the search API fails.
# t_6bb2badd modes (all about "Capsule Corp", uid empty3): rules = it holds file-provisioned alert rules;
# managed = the folder list marks it managedBy; delete-prov / delete-real = its DELETE answers 400
# "provisioned" / 500; rules-err = the alert-rule list fails. DELETE answers "body, newline, HTTP code" (-w).
cat >/dev/null
echo "$*" >>"$PRUNE_LOG"
case "$*" in
  *"-X DELETE"*)
    case "${PRUNE_MODE:-}:$*" in
      delete-prov:*empty3*) printf '{"message":"folder is provisioned and cannot be deleted"}\n400' ;;
      delete-real:*empty3*) printf '{"message":"database is locked"}\n500' ;;
      *) printf '{}\n200' ;;
    esac ;;
  *"/api/v1/provisioning/alert-rules"*) [ "${PRUNE_MODE:-}" = rules-err ] && exit 22
    if [ "${PRUNE_MODE:-}" = rules ]; then echo '[{"uid":"r1","folderUID":"empty3","provenance":"file"}]'
    else echo '[{"uid":"r2","folderUID":"real","provenance":"file"}]'; fi ;;
  *"/api/folders?parentUid="*) echo '[]' ;;
  *"/api/search"*) [ "${PRUNE_MODE:-}" = err ] && exit 22
    case "$*" in *"folderUIDs=full"*) echo '[{"uid":"d1","title":"a dashboard"}]' ;; *) echo '[]' ;; esac ;;
  *"/api/folders")
    if [ "${PRUNE_MODE:-}" = gone ]; then echo '[{"uid":"real","title":"The Lookout"}]'
    else m=""; [ "${PRUNE_MODE:-}" = managed ] && m=',"managedBy":"classic-file-provisioning"'
      echo '[{"uid":"empty1","title":"Agent Ops"},{"uid":"full","title":"Archive"},{"uid":"empty3","title":"Capsule Corp"'"$m"'},{"uid":"real","title":"The Lookout"}]'; fi ;;
esac
