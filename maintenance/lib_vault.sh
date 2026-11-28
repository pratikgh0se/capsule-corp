# lib_vault.sh - the one shell spelling of the notes-vault location (D254). Source it; do not exec.
# vault_resolve <capsule checkout> [python]: VAULT = $OBSIDIAN_VAULT_PATH if non-empty, else host.json
# vault.path (via the `capsule host get` reader the architect job uses, run with the caller's
# interpreter - call this only after PATH is rebuilt), else "" (vault features off). A failed host
# read prints one stderr line and counts as unset. OBSIDIAN_VAULT_PATH is exported only when a
# vault is set, and unset otherwise.
vault_resolve() {
  _VAULT_PY="${2:-python3}"      # skill_root_resolve reuses it
  VAULT="${OBSIDIAN_VAULT_PATH:-}"
  if [ -z "$VAULT" ]; then
    VAULT="$(cd "$1" 2>/dev/null && PYTHONPATH="$1" "${2:-python3}" -P -m capsule host get --only vault.path)" \
      || { echo "vault_resolve: host.json vault.path could not be read; treating the vault as unset" >&2; VAULT=""; }
  fi
  if [ -n "$VAULT" ]; then export OBSIDIAN_VAULT_PATH="$VAULT"; else unset OBSIDIAN_VAULT_PATH; fi
}

# skill_root_resolve <capsule checkout> [python] (default: the python vault_resolve was given): OSB_DIR = the notes-vault skill checkout:
# $OBSIDIAN_SKILL_ROOT if non-empty, else host.json vault.skill_root, else "" (the jobs and steps
# that need the skill then skip; there is no default directory). Same rules as vault_resolve;
# OBSIDIAN_SKILL_ROOT is exported only when a checkout is set, and unset otherwise.
skill_root_resolve() {
  OSB_DIR="${OBSIDIAN_SKILL_ROOT:-}"
  if [ -z "$OSB_DIR" ]; then
    OSB_DIR="$(cd "$1" 2>/dev/null && PYTHONPATH="$1" "${2:-${_VAULT_PY:-python3}}" -P -m capsule host get --only vault.skill_root)" \
      || { echo "skill_root_resolve: host.json vault.skill_root could not be read; treating it as unset" >&2; OSB_DIR=""; }
  fi
  if [ -n "$OSB_DIR" ]; then export OBSIDIAN_SKILL_ROOT="$OSB_DIR"; else unset OBSIDIAN_SKILL_ROOT; fi
}
