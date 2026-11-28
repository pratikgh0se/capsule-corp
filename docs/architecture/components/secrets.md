# Secrets

## Purpose
One vendor-neutral door to secrets. Callers never learn the backend; secrets never go on argv,
into agent containers or into logs.

## Where it lives (files)
- `bin/capsule-secret` (`get`, `run`, `export` (not on the env backend), `check [--identity-all]`, `backend`, `keychain-check`), tests `tests/test_capsule_secret.sh`, `test_capsule_secret_backends.sh`, `test_capsule_secret_env_scrub.sh`.
- Backends via `CAPSULE_SECRETS_BACKEND`: `file` (`$CAPSULE_HOME/secrets.env`, refused unless mode 0600), `infisical` (the Infisical CLI; identity files under `$CAPSULE_CONFIG_DIR`, default `~/.config/capsule`), `env` (process environment, for CI and containers). Unset: `secrets.backend` in `$CAPSULE_HOME/host.json` decides if present; otherwise a machine that already has an Infisical config and no `secrets.env` keeps using Infisical; otherwise `file`; both present is ambiguous and exits 3.
- `maintenance/secrets_import.py` (idempotent bootstrap and import), `maintenance/backup_key.sh` (the one door to the backup encryption key).
- observability/infisical.env.template (maintainer only, not in the public edition), the Infisical service in `observability/docker-compose.yml`; `maintenance/lib_compose.sh` brings Infisical up before the rest of the stack.
- `capsule/slack_secret.py` fetches the Slack token through `bin/capsule-secret` (project `capsule-corp`). On the infisical backend, `get` for `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN` asks the macOS Keychain first (service `capsule-corp`, wait `CAPSULE_KEYCHAIN_WAIT_S`, default 10 s), so Slack works while Docker is down (D227); a miss falls through to Infisical. `keychain-check <KEY>` tests presence only.
- Scrubbing: `maintenance/scrub.py` and `observability/fluent-bit/scrub.lua` redact secret-shaped values before spool, Postgres or Loki.

## Inputs and outputs
In: a key name and identity. Out: the value on stdout (`get`), or a child process with the value in its environment (`run`).

## State it reads and writes
Reads the backend. Writes nothing except the import script.

## Key rules and invariants
No secret value on argv. Script-owned names carry a `_CS_` prefix and never reach a child. The implementer container gets no secrets. Agents must not read secret files (see [secret-read-guard](secret-read-guard.md)). Never put secret values in docs or tests (`tests/test_no_secret_values.sh`).

## Failure modes and what happens
Backend unreachable: heal's secrets check fails and is recorded; callers get a non-zero exit.

## Tests that pin it
`tests/test_capsule_secret.sh`, `test_capsule_secret_backends.sh`, `test_capsule_secret_env_scrub.sh`, `tests/test_no_secret_values.sh`, `tests/test_fluent_bit_scrub.sh`, `maintenance/tests/test_scrub.py`, `maintenance/tests/test_secrets_import.py`; `contracts/intent/capsule_secret.jsonl`.

## Related decisions and specs
docs/superpowers/specs/2026-09-07-secrets-server-design.md (maintainer only, not in the public edition), `2026-09-08-secrets-cutover-design.md`; `docs/EGRESS.md`; this page.

## Status
current.
