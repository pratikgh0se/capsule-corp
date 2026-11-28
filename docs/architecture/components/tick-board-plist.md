# Per-board tick launchd template

## Purpose
The generic launchd job that runs one board's tick. A new board gets a tick by rendering this
template with its board name.

## Where it lives (files)
- `launchd/board/__PREFIX__.capsule-tick-__BOARD__.plist`: label `__PREFIX__.capsule-tick-__BOARD__`; runs `python3 -u -m capsule tick --replace --board __BOARD__`.
- Rendered copies for the live boards: `launchd/templates/__PREFIX__.capsule-tick-capsule-corp.plist` and one more template for the second board. Behaviour of the tick: [tick-scheduler](tick-scheduler.md).

## Inputs and outputs
Placeholders `__PREFIX__`, `__BOARD__`, `__PYTHON__`, `__HOME__`, `__CAPSULE_HOME__` are filled by deploy.sh (maintainer only, not in the public edition). Output: the board's tick log `__CAPSULE_HOME__/logs/tick-__BOARD__.log` (stdout and stderr).

## State it reads and writes
Working directory `__CAPSULE_HOME__/harness/main`; env `CAPSULE_HOME`. It holds no state; the tick owns the board DB.

## Key rules and invariants
- `RunAtLoad` true and `KeepAlive` with `SuccessfulExit` false: a crash restarts it, a clean exit does not. `ThrottleInterval` 30 s.
- `ExitTimeOut` 900 s: launchd waits that long for a clean stop before it kills the tick.
- `--replace` takes over from an older tick of the same board.

## Failure modes and what happens
A crash loop is throttled to one start per 30 s and shows in the tick log and the incident flow ([incidents-korin-flow](incidents-korin-flow.md)).

## Tests that pin it
tests/test_launchd_label_identity.sh, tests/test_deploy.sh (maintainer only, not in the public edition) (live, run by the owner only).

## Related decisions and specs
Zenkai Z5 (detectors watch the tick); [intake](intake.md).

## Status
current.
