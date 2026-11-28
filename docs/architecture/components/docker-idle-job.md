# Docker idle reaper launchd job

## Purpose
Quits the Docker engine (OrbStack by default) when nothing needs it, so the machine is not running a VM all day.

## Where it lives (files)
- `launchd/templates/__PREFIX__.capsule-corp-docker-idle.plist`: label `__PREFIX__.capsule-corp-docker-idle`; runs `maintenance/docker_idle.py --apply` every 300 s (`StartInterval`).
- `maintenance/docker_idle.py` (D195: Docker runs only while something needs it): the reaper. launchd/unloaded (maintainer only, not in the public edition) lists the label (install-only).

## Inputs and outputs
In: `docker info`, running containers, the landing queue, each board's DB (read only), build processes. Out: `docker compose -p capsule-corp stop`, then quit the Docker engine (OrbStack by default), and a quit stamp. launchd stdout and stderr go to `launchd-docker-idle.{out,err}` in the user's maintenance logs directory under the config home (see the plist), not the repo; the action log `docker_idle.jsonl` is in the state root.

## State it reads and writes
An idle clock (`docker_idle.state`) and the quit stamp `docker-idle-quit` in the state root. The stamp is read by `maintenance/heal.sh`, so Docker being down is a skip, not a page.

## Key rules and invariants
- Busy (reset the clock) if a non-stack container runs, a non-stack container event occurred in the last 10 minutes (catches short `docker run --rm` containers between polls), a landing is in progress, a board has cards waiting on a container, a running run under 2 h old (a paused board never holds Docker), or a pending Slack outbox row, a docker build runs, or a test suite process (`capsule-suite` or `run_shell_suites.sh`) runs.
- Idle for `OPS_DOCKER_IDLE_MIN` minutes (default 15) before it acts; it rechecks before quitting. A stop failure means Docker is not quit.
- Without `--apply` it only prints. It is the inverse of the Z5 detector in [detectors-z5](detectors-z5.md).

## Failure modes and what happens
A gap of over 10 minutes (sleep) restarts the idle clock. A stale landing claim still holds Docker up and says so.

## Tests that pin it
`maintenance/tests/test_docker_idle.py`.

## Related decisions and specs
D195 (keep Docker up only for landing checks and container work, then quit it); [compose-live-guard](compose-live-guard.md).

## Status
current; install-only until the owner removes the label from launchd/unloaded.
