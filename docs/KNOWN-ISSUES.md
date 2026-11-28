# Known issues

Capsule Corp is a pre-alpha preview (`v0.2.0-alpha.1`). This page lists the problems that are still open or under watch.
Interfaces can change without notice.

Status terms:
- Partly fixed: some causes have a fix on `main`. At least one case has no fix yet.
- Under watch: the problem happened once or a few times, with no cause we can prove. We watch for it.
- Not tested: nobody has run this step yet.
- A fix is planned: we know the cause and have not built the fix yet.

## 1. Install not tested on a stranger's Mac

- What you see: nothing yet. The install script and its dry run have tests, but nobody has run a full install on a fresh macOS account.
- Workaround: run `./install.sh --dry-run` first and read what it will do. Then run `capsule doctor` after the install.
- Status: Not tested.

## 2. Test runs can change live services

- What you see: a test run with the real Docker engine can start a live stack.
- Workaround: stop live services before you run a test with `RUN_DOCKER_TESTS=1`.
- Status: Partly fixed. The compose guard and the launchd guard are on `main`. One reported case has no known cause yet.

## 3. Contradictions stay open with no cause recorded

- What you see: a reviewer and a guide disagree about a card. No cause is written down, and the card stays in review or triage.
- Workaround: ask the chief-of-staff agent to read the incident and rule on it.
- Status: Under watch.

## 4. Card status and card events can disagree

- What you see: the status of a card does not match its events. A card can look stuck or done when it is not.
- Workaround: read the card events before you act on the status.
- Status: Under watch.

## 5. Valid run output is rejected as an invalid document

- What you see: a run output fails the schema check, for example because one field is too long. The run counts as failed.
- Workaround: none found yet. Rerun the card.
- Status: Under watch.

## 6. A logged-out Claude account counts as a card crash

- What you see: the Claude account is logged out or refused (an API 401 or 403). The factory records a card crash and uses up the retries of that card. It does not pause the board.
- Workaround: run `claude` and log in. Then reset the card.
- Status: A fix is planned.

## 7. The Grafana and Loki feed does not ship yet

- What you see: the optional dashboards and alerts show no data. The core factory does not need them.
- Workaround: none needed. Ignore the dashboards for now.
- Status: A fix is planned.

## 8. Worker read limits

- What you see: a worker session can read only its clone and the folders Capsule adds for that run. Three checks apply: Claude Code's `--restricted` mode, its permission default, and Capsule's read guard (`capsule/read_guard.py`). A worker cannot edit outside its clone, and its shell commands run in a no-network container or a sandbox.
- Not covered: a hard link that already exists inside the clone and points at a file outside it. Workers cannot make such a link. The checks also depend on Claude Code keeping its behaviour (checked with Claude Code 2.1.326).
- Workaround: keep Claude Code current. For full separation, run Capsule under a separate macOS user account that holds no personal files or secrets. This is defence in depth.
- Status: Partly fixed. See [read-guard](architecture/components/read-guard.md).

## Report a problem

Open an issue on GitHub. For a security problem, follow [SECURITY.md](../SECURITY.md) and do not open a public issue.
