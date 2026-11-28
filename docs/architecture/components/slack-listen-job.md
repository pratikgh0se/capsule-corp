# Slack listener launchd job

## Purpose
Keeps the host-side Slack Socket Mode listener running, so button clicks and thread replies reach
Capsule without a public endpoint.

## Where it lives (files)
- `launchd/templates/__PREFIX__.capsule-corp-slack-listen.plist`: label `__PREFIX__.capsule-corp-slack-listen`; runs `python3 -u -m capsule.slack_listen`.
- `capsule/slack_listen.py` (the listener, D225): intake only. It acks every envelope at once and hands clicks and replies to `python -m capsule.slack_answer event` as a subprocess. Answering is in [outbox-notifications](outbox-notifications.md).
- launchd/unloaded (maintainer only, not in the public edition) lists the label: deploy installs and tracks the plist but does not load it.

## Inputs and outputs
In: Slack Socket Mode envelopes. Out: subprocess calls to `slack_answer`; log `__CAPSULE_HOME__/logs/slack-listen.log`.

## State it reads and writes
Env `CAPSULE_HOME` and `CAPSULE_BOARD` (set to `capsule-corp` in the template). It keeps no board state itself.

## Key rules and invariants
- `RunAtLoad` true, `KeepAlive` with `SuccessfulExit` false, `ThrottleInterval` 30 s, `ExitTimeOut` 20 s.
- The listener is stdlib only and imports only the protected `slack_secret` from capsule; a test pins that import closure.

## Failure modes and what happens
A crash restarts it after 30 s. While the label stays in launchd/unloaded the job simply is not running.

## Tests that pin it
`capsule/tests/test_slack_listen.py`, `test_slack_listen_repair.py`, `test_sandbox_slack_protected.py`.

## Related decisions and specs
D225; this page.

## Status
current; install-only until the owner removes the label from launchd/unloaded.
