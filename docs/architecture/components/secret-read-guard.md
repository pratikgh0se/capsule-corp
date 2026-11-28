# Secret-read guard

## Purpose
A Claude Code `PreToolUse` hook on Read, Grep, Glob and Bash that flags (warn) or refuses (block) an
agent tool call that would read a secret file.

## Where it lives (files)
- bin/secret-read-guard.py (maintainer only, not in the public edition); installed by deploy.sh (maintainer only, not in the public edition) through claude/settings.fragment.json as `if [ -f "<repo>/bin/secret-read-guard.py" ]; then exec python3 ...; fi` with a 5 second timeout.
- Mode file `<state>/secret-read-guard.mode`; log `<state>/secret-read-guard.log` (`<state>` is `$AGENT_OPS_STATE_DIR` or `~/.local/state/capsule-corp/maint`).
- `contracts/intent/secret_read_guard.jsonl`; this page.

This guard covers the owner's own sessions. Card workers never load it, because `--restricted` ignores user settings (`capsule/models/claude.py`). Workers have their own allow-list guard: [read-guard](read-guard.md).

## Inputs and outputs
In: the tool call JSON on stdin. Out: exit 0 (allow) or exit 2 with a one-line reason (block mode); a log line per hit.

## State it reads and writes
Reads the mode file; appends to the log (mode 0600): UTC time, mode, tool, path as written, path resolved, each cut at 200 characters. Never file contents, never the Bash command line.

## Key rules and invariants
- Secret paths: any `.env*` or `*.env` file (templates `.env.example`, `.sample`, `.template`, `.dist`, `.tpl` pass), anything under `~/.config/capsule/`, and the jobs config dir's `ops.env` (`host.config_dir()`, default `~/.config/capsule-corp`; the `*.env` rule also catches the other `.env` files there); checked on the written and resolved spelling.
- Bash: the command is split at pipes, `&&`, `||`, `;`, `&`, parentheses and newlines; heredoc bodies are dropped; a segment hits when its command word is a reader (`cat`, `head`, `grep`, `sed`, `tar`, `cp`, `source`, ...) and one of its own arguments is a secret path, or a `< secret` redirect. Existence checks (`ls`, `test`, `find`, `git`, `echo`) never hit. Known misses: `bash -c`, `cd` then `cat *`, `python -c`, variables, `xargs cat`.
- Mode: the file holds `warn` or `block`; only the word `warn` means warn, and a missing, empty, unreadable or any other value means block (default since D215, 2026-11-11). A Glob call is a hit when its path or pattern names a secret. The hook applies to the main thread as well as subagents.
- Fails open: bad input, an unwritable log or a crash allows the call with one stderr warning; a checkout without the script never refuses.

## Failure modes and what happens
A broad Grep or `grep -r` over `~` or `~/.config` is a hit on purpose and is now refused; narrow such searches to the project folder. Rollback to warn: write `warn` to the mode file, no redeploy.

## Tests that pin it
capsule/tests/test_secret_read_guard.py (maintainer only, not in the public edition), `tests/test_no_secret_values.sh`.

## Related decisions and specs
Decision D66 (2026-10-28, warn-only start); D215 (2026-11-11, block by default). `capsule/secret_guard_ship.py` ships the log lines to ops-pg as `secret_guard` events (OBS-12 b).

## Status
current (block mode by default).
