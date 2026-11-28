# Public portable edition

## Purpose
A stranger can install and run Capsule from a public copy of the repo without the owner's private
setup. The private repo stays the source; the public tree is an allowlisted export.

## Where it lives (files)
- `capsule/doctor.py`: `python3 -m capsule doctor`, a read-only first-run check that needs no board: one `ok` or `MISSING <what> -> <command>` line per prerequisite.
- `capsule/install.py`: `python3 -m capsule install`, the one-command setup (macOS only, idempotent, one line per step). `capsule/plists.py` fills launchd template placeholders (the Python twin of deploy.sh (maintainer only, not in the public edition)'s `render()`). `install.sh` at the repo root.
- `maintenance/public_export.py`: `public_export.py SHA DEST [--repo DIR] [--denylist FILE] [--dry-run] [--no-commit]` writes the allowlisted tree at one commit into a destination checkout and commits it as "Sync from private repo".
- `tests/personal_paths_check.py`, `contracts/intent/public_export.jsonl`, `public_lint.jsonl`, `install.jsonl`, `doctor.jsonl`.

## Inputs and outputs
In: a private commit sha, an allowlist and denylist. Out: a commit in the destination checkout and one line in a private mapping log (public commit to private sha) under the maintenance state dir.

## State it reads and writes
Reads the private repo through git plumbing; writes only to `DEST` and the mapping log, never inside the public tree or its history.

## Key rules and invariants
The export never pushes, never creates a remote or a repo, and never touches the network (Z6: publishing is a human step). Personal paths and secrets are linted out. `public_lint.py` also refuses a binary file (a NUL in the first 8 KB, or bytes that are not UTF-8 or UTF-16 with a BOM) unless its path matches release/binary-allowlist.txt (maintainer only, not in the public edition); the seed is empty.

## Failure modes and what happens
A denylisted or personal-path file in the export set refuses the export.

## Tests that pin it
`capsule/tests/test_doctor.py`, `test_install.py`, `test_plists.py`; `maintenance/tests/` for the export.

## Related decisions and specs
Spec "Public portable edition" (P07, P08, P10); `docs/RELEASE.md`; `docs/SETUP.md`.

- `capsule/host.py` reads `$CAPSULE_HOME/host.json`, the host-level settings of the public portable edition (env var, then host.json, then built-in default). `owner.name` (env `CAPSULE_OWNER_NAME`, default `owner`) is the owner's name: `host.owner_name()` feeds the decision-row matcher in `capsule/touches.py`, the hand-ledger note name and the author aliases (`human.canonical_author`: the old legacy name and the configured name both store as `owner`).

## Status
current for doctor, install and export; publishing stays manual.
