"""vault_conf.py - the one python spelling of the notes-vault location (D254).

The vault and its skill checkout are opt-in: both come from `capsule.host` (env var
OBSIDIAN_VAULT_PATH / OBSIDIAN_SKILL_ROOT, else host.json `vault.path` / `vault.skill_root`) and
have no default directory. Unset means "None": the caller skips its vault feature and says
`NO_VAULT` (or `NO_SKILL_ROOT`) in one line. Never raises.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # `capsule`
from capsule import host  # noqa: E402

NO_VAULT = "no notes vault configured (set vault.path in host.json or OBSIDIAN_VAULT_PATH)"
NO_SKILL_ROOT = ("no notes-vault skill checkout configured "
                 "(set vault.skill_root in host.json or OBSIDIAN_SKILL_ROOT)")


def vault():
    """The configured notes vault as a Path, or None."""
    return host.vault_path()


def host_json_vault():
    """The vault host.json names, ignoring the env override: what a test must never write to
    (a test points OBSIDIAN_VAULT_PATH at a scratch dir; host.json is the real machine's)."""
    value = host.get_or_default("vault.path", env={})
    return Path(value) if value else None


def skill_root():
    """The configured notes-vault skill checkout as a Path, or None."""
    value = host.get_or_default("vault.skill_root")
    return Path(value) if value else None
