# Observability compose stack

## Purpose
The always-on local services behind the dashboards: a metrics database, logs, dashboards and a
secrets store, all bound to localhost. Compose project name `capsule-corp`.

## Where it lives (files)
- `observability/docker-compose.yml`: eight services. Config under `observability/{caddy,fluent-bit,grafana,loki,postgres}/`; env templates `observability/env.template` and observability/infisical.env.template (maintainer only, not in the public edition).
- Services: `postgres` (database `ops`, 127.0.0.1:5432), `loki` (3100), `fluent-bit` (log shipper), `grafana` (3000), `front-door` (Caddy, 127.0.0.1:80), the secrets-vault trio (a server on 8085 with its own Postgres and Valkey; the service is still named `infisical-redis`), which exist only under the `secrets` compose profile (`COMPOSE_PROFILES=secrets`).
- The code that feeds and reads it is described in [observability](observability.md).

## Inputs and outputs
In: logs and metric rows from the host jobs. Out: Grafana dashboards behind the Caddy front door.

## State it reads and writes
Data under `${DATA_DIR}` (Postgres volume); password from `${OPS_PG_PASSWORD}`. Every service is `restart: unless-stopped`.

## Key rules and invariants
- Ports bind to 127.0.0.1 only.
- Images are pinned by tag, except the secrets-vault server and its Valkey, which are pinned by digest; a bump is deliberate.
- Containers of project `capsule-corp` are the "always-on stack" the idle reaper ignores ([docker-idle-job](docker-idle-job.md)); recreating them goes through [compose-live-guard](compose-live-guard.md).

## Failure modes and what happens
Docker down or the stack stopped: dashboards go dark, board ticks continue. The Z5 detectors in [detectors-z5](detectors-z5.md) watch for Docker being down.

## Tests that pin it
`tests/test_compose_live_guard.sh`, `tests/test_lib_compose.sh`.

## Related decisions and specs
D195; Zenkai Z5.

## Status
current.
