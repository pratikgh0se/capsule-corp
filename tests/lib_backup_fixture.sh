# lib_backup_fixture.sh - shared fixture for test_backup.sh and
# test_restore.sh: a fake HOME with merge-gate approvals, ops
# state, a vault git repo, a fake docker(pg_dump), and BACKUP_DIR seeded with
# 9 aged tarballs for retention testing.
#
# make_backup_fixture <tmpdir>: builds the fixture under <tmpdir> (the caller
# creates and cleans up <tmpdir>, typically via mktemp -d + trap) and exports
# the env vars backup.sh (and deploy.sh, via restore.sh --verify) read: PATH,
# AGENT_OPS_HOME, the vault path variable, BACKUP_DIR, AGENT_OPS_STATE_DIR,
# BACKUP_DATE, AGENT_OPS_NO_SEND, CAPSULE_HOME. Also sets H (=<tmpdir>/home) for callers
# that need to reach into the fixture directly.
make_backup_fixture() {
  local T="$1"
  H="$T/home"; mkdir -p "$H/.local/state/capsule-corp/merge-gate/approvals/sample-app" "$T/state" "$T/vault" "$T/bin" "$H/.config/capsule-corp"
  # files backup.sh must never pick up: beside the state files it copies and inside a capsule board dir
  mkdir -p "$T/state/logs" "$T/state/skills-curated" "$H/.local/share/capsule/boards/zz/logs"
  printf 'TOKEN=secret-value\n' >"$T/state/.env"; printf 'log line\n' >"$T/state/logs/run.log"
  printf 'TOKEN=secret-value\n' >"$H/.local/share/capsule/boards/zz/.env"; printf 'log line\n' >"$H/.local/share/capsule/boards/zz/logs/run.log"
  printf '{"ts":1}\n' >"$H/.local/state/capsule-corp/merge-gate/approvals/sample-app/t_1.json"
  printf '{}\n' >"$T/state/notify-seen.json"; printf 'OPS_PG_PASSWORD=pw\n' >"$H/.config/capsule-corp/ops.env"
  printf 'ENCRYPTION_KEY=deadbeefdeadbeefdeadbeefdeadbeef\nAUTH_SECRET=x\nPOSTGRES_PASSWORD=y\n' >"$H/.config/capsule-corp/infisical.env"
  ( cd "$T/vault" && git init -q && git config user.email t@t && git config user.name t && echo note >a.md && git add a.md && git commit -qm init )
  cat >"$T/bin/docker" <<'EOF'
#!/usr/bin/env bash
# $1=exec $2=container name $3=pg_dump ...
if [ "$2" = "capsule-corp-infisical-db" ]; then
  if [ "${FAKE_INFISICAL_PG_DOWN:-}" = 1 ]; then echo "Error: No such container" >&2; exit 1; fi
  echo "-- PostgreSQL database dump"; echo "CREATE TABLE secrets (id int);"; echo "-- PostgreSQL database dump complete"
  exit 0
fi
if [ "${FAKE_PG_DOWN:-}" = 1 ]; then echo "Error: No such container" >&2; exit 1; fi
echo "-- PostgreSQL database dump"; echo "CREATE TABLE events (id int);"; echo "-- PostgreSQL database dump complete"
EOF
  chmod +x "$T/bin/docker"
  export PATH="$T/bin:$PATH" AGENT_OPS_HOME="$H" OBSIDIAN_VAULT_PATH="$T/vault" BACKUP_DIR="$T/backups" AGENT_OPS_STATE_DIR="$T/state" BACKUP_DATE=2026-09-07
  export AGENT_OPS_NO_SEND=1
  export CAPSULE_HOME="$H/.local/share/capsule"   # the fixture's own capsule home: under the marker backup.sh would else back up the shared temp home
  # M210 (row 137): flag off by default, and no ambient key material or real
  # capsule-secret in reach - a stray OPS_BACKUP_ENCRYPT/BACKUP_ENC_KEY_FILE in
  # the caller's shell must never leak into the fixture's byte-identity proof.
  unset OPS_BACKUP_ENCRYPT BACKUP_ENC_KEY_FILE
  export CAPSULE_SECRET_BIN="$T/bin/capsule-secret-not-installed"
  mkdir -p "$T/backups"; for d in 01 02 03 04 05 06 07 08 09; do f="$T/backups/2026-08-$d.tar.gz"; echo x >"$f"; touch -t "202608${d}0000" "$f"; done
}
