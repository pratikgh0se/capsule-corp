# Compose live guard

## Purpose
Stop a test or a stray shell from driving the live `capsule-corp` observability stack with the
wrong environment file (incident `inc_02b0bd3a`).

## Where it lives (files)
- `maintenance/lib_compose.sh`: `compose_live_guard`, called first by `compose_up_infisical`, `compose_up_rest` and `compose_recreate`; `bin/hoi-poi.sh` stop also goes through it.
- `bin/capsule-compose-recreate`: force-recreate one service through `compose_recreate`.
- `contracts/intent/compose_live_guard.jsonl`; `tests/test_compose_live_guard.sh`.

## Inputs and outputs
In: `OBS_ENV`, `COMPOSE_PROJECT_NAME`, the docker binary in use. Out: proceed, or `FAILED compose: refusing to run the live capsule-corp stack with a non-live OBS_ENV (...)` with rc 1 and docker never sees the command.

## State it reads and writes
None.

## Key rules and invariants
- Mutating compose (`up`, recreate, `stop` from hoi-poi) on the live project is refused unless the directory of `OBS_ENV` is the live config directory of the passwd-database home (both sides resolved with `cd -P`, so a symlink counts; `HOME` is not trusted). The live config directory is `config_dir` from `host.json` (read with `capsule host get`), else `~/.config/capsule-corp`, else an older legacy config directory when it holds `observability.env` or `ops.env` (`compose_live_config_dir`). `OBS_ENV` unset or its directory missing refuses.
- Read-only compose calls (`config`, `ps`) stay open.
- A `COMPOSE_PROJECT_NAME` other than `capsule-corp` skips the check only when the docker to run is a plain file, not a symlink, under the temp root (`TMPDIR` or `/tmp`), and that temp root is not the passwd home or inside it (the fake-docker seam). With real docker such a project is still refused. Tests that drive compose on fake docker export `COMPOSE_PROJECT_NAME=capsule-corp-test`.

## Failure modes and what happens
A refused call returns 1; `heal`'s stack step (`compose_up_rest`) then records a failed `stack` check rather than touching the live stack.

## Tests that pin it
`tests/test_compose_live_guard.sh`, `tests/test_lib_compose.sh`, `tests/test_capsule_compose_recreate.sh`.

## Related decisions and specs
Incident `inc_02b0bd3a`; commit `107dda2f`; [observability](observability.md) and [maintenance-jobs](maintenance-jobs.md).

## Status
current.
