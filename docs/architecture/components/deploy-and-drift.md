# Deploy, drift check, tick control, hoi-poi and park

## Purpose
Make the checkout the single source of truth for what runs on the Mac, and prove it: deploy.sh (maintainer only, not in the public edition)
writes the live locations and registers launchd jobs; `deploy.sh --check` reports drift and writes
nothing. bootstrap.sh (maintainer only, not in the public edition) builds a fresh machine. `hoi-poi` and `park` start and stop the pipeline as
a unit.

## Where it lives (files)
- deploy.sh: modes apply (default), `--check` (report drift, exit 1 if anything differs), `--dry-run` (print writes), `--move-install`. Honours `AGENT_OPS_HOME`, `OBSIDIAN_VAULT_PATH`, `AGENT_OPS_FLAVOR`, `AGENT_OPS_NO_LAUNCHCTL`, `CAPSULE_LAUNCHD_PREFIX`. Records the installed checkout in `$AGENT_OPS_HOME/.local/share/capsule-corp/install-repo` and refuses an apply from a different checkout without `--move-install`.
- bootstrap.sh, bootstrap.lock, `docs/SETUP.md`: fresh-machine stand-up.
- `launchd/templates/` (plist templates with `__PREFIX__`, `__HOME__`, `__REPO__`, `__CAPSULE_HOME__`, `__VAULT__` placeholders), launchd/unloaded (maintainer only, not in the public edition) (labels installed and tracked but not bootstrapped), launchd/disabled/ (plists deploy.sh boots out and removes).
- claude/settings.fragment.json, claude/merge_settings.py: the Claude Code settings keys and hooks deploy.sh owns in `~/.claude/settings.json`; everything else there is left as found.
- `bin/capsule-tick.sh` (`start|stop|restart|status <board> [--quiet S] [--wait S]`), `bin/lib_prefix.sh`, `bin/lib_jobs.sh` (classifies each plist as DISPATCHING or CUSTODIAL; an unclassified plist is reported), `bin/hoi-poi.sh`, `bin/capsule-park.sh`, `bin/capsule-state-migrate`, bin/capsule-corp.zsh (read-only shell shortcuts).
- Worker image in `--check`: deploy.sh resolves `OPS_WORKER_IMAGE` (environment, then `ops.env`), else a built-in fallback tag `capsule-corp-worker:1`, and prints the source. The tick itself does not use that fallback: it runs the image recorded in `$CAPSULE_HOME/images.lock` ([image-record](image-record.md)).

## Inputs and outputs
In: the checkout. Out: files at live locations, registered plists, a printed `DRIFT`/`WOULD`/`ok` report per item.

## State it reads and writes
`~/.local/share/capsule-corp/install-repo` stamp, `~/.local/bin` scripts, `~/Library/LaunchAgents` plists, the `claude` settings keys it owns.

## Key rules and invariants
- `--check` and `--dry-run` write nothing. `AGENT_OPS_NO_LAUNCHCTL=1` skips launchctl and Docker steps (tests, CI).
- A temp `OBSIDIAN_VAULT_PATH` with a real target home is refused.
- launchd/unloaded labels are installed but never bootstrapped until the line is removed and deploy.sh runs again.
- `hoi-poi up` runs preflight, converges everything in order, one heal tick, prints `heal N/6`, exits 0 only on 6/6. `hoi-poi down` boots out pipeline jobs, stops worker containers and runs compose `stop`. It never applies deploy or bootstrap (only `--check`), never uses compose teardown or `-v`, never starts Docker Desktop.
- `capsule-tick.sh restart <board> --no-update` restarts one tick and leaves the release checkout where it is. Only the tick watchdog (`capsule tick-watch --apply`) uses it: a restart by a watchdog must not deploy new code.
- `capsule-tick.sh stop <board> [--for S]` writes a stop marker (`<board dir>/tick-stop.json`, 2 h by default) so the tick watchdog does not start the tick; `start` and `restart` delete it.
- `park` boots out every DISPATCHING job, keeps CUSTODIAL jobs (heal, backup, restore-verify, cleanup and others in `bin/lib_jobs.sh`), takes one encrypted backup and prints a checklist; `unpark` reverses it.
- The lander runs `deploy.sh --check` after a push; drift holds further landings.

## Scratch home with the real launchctl (refusal)
An apply into a scratch `AGENT_OPS_HOME` while `AGENT_OPS_NO_LAUNCHCTL` is not 1 is refused when the `launchctl` on PATH is the system one (`/bin/launchctl`, or the path in `AGENT_OPS_SYSTEM_LAUNCHCTL`; symlinks count), because it would re-point live launchd jobs at the scratch plists. A scratch home with a fake `launchctl` first on PATH is allowed. The real home is read from the passwd database; if that cannot be read the apply also refuses.
- Operator sees: `deploy.sh: refusing - AGENT_OPS_HOME=... (defaults to $HOME) is not the real home but AGENT_OPS_NO_LAUNCHCTL is not 1 ...; set AGENT_OPS_NO_LAUNCHCTL=1 for a scratch home`, exit 2. The refusal is made before any write, so nothing is written and launchctl is never called.
- `--check` and `--dry-run` do not stop: they print `NOTE apply would refuse: ...` and finish (`--check` counts it as drift).
- Pinned by tests/test_deploy.sh (maintainer only, not in the public edition) ("the refused apply made zero launchctl calls and wrote nothing").

## Failure modes and what happens
Drift: `--check` exits 1 and names the item. A pull failure on apply for the worker image is a warning. A plist with no classification is reported rather than silently left running.

## Tests that pin it
tests/test_deploy.sh, tests/test_bootstrap.sh (maintainer only, not in the public edition), `tests/test_hoi_poi.sh`, `tests/test_capsule_park.sh`, `tests/test_capsule_tick_sh.sh`, `tests/test_lib_prefix.sh`, `tests/test_capsule_state_migrate.sh`, tests/test_capsule_corp_zsh.sh, maintenance/tests/test_merge_settings.py; contracts/intent/deploy.jsonl, `bootstrap.jsonl`, `capsule_tick_sh.jsonl`, `capsule_home.jsonl`, `state_root.jsonl`.

## Related decisions and specs
docs/superpowers/specs/2026-09-12-hoi-poi-design.md (maintainer only, not in the public edition); `2026-09-10-continuity-and-data-safety-design.md` section 7.6; factory-autonomy spec 4.3; `docs/SETUP.md`; this page.

## Status
current. The deploy.sh and bootstrap.sh are private, not exported to the public edition (release/public-paths.txt).
