# Read guard

## Purpose
Limit what the file-reading tools (Read, Glob, Grep) of a card worker can open. A worker reads only its clone and the folders Capsule adds for the run. Every other path is denied.

## Where it lives (files)
- `capsule/read_guard.py`: the guard. A stdlib-only `PreToolUse` hook script. It never imports from `capsule`.
- `capsule/models/claude.py`: `build_argv` passes `--restricted` and, for every role, `--settings` with the hook entry (matcher `Read|Glob|Grep`). The hook command is `sys.executable -I <install>/capsule/read_guard.py <workspace> <add_dirs...>`, with the absolute install path.
- `capsule/runners/docker.py` `limits()` (strong tier) and `capsule/runners/host.py` `LightRunner.limits`: `allowed` holds only `mcp__capsule__exec`. No bare `Read`, `Glob` or `Grep` allow rule.
- Intent rows: `contracts/intent/read_guard.jsonl`; this page.

## Inputs and outputs
In: the hook JSON on stdin (`tool_name`, `tool_input`) and the roots on argv. The first root is the session working directory. Out: exit 0 (no objection, empty stdout) or exit 2 with one stderr line that starts with `read guard: `. Any failure (bad JSON, missing field, exception) exits 2. The guard only denies. It never grants.

## State it reads and writes
None. It has no mode file and no log. A denial reaches the run record through the CLI's `permission_denials` (`capsule/worker.py`). The only file-system call is `realpath`.

## Key rules and invariants
- Three layers, each default deny:
  1. `--restricted` (`capsule/models/claude.py`): Claude Code confines the file tools to the working directories and `--add-dir` folders, resolves symlinks, and ignores the clone's own settings file.
  2. No bare read allow rule (`runners/docker.py`, `runners/host.py`). If layer 1 is lost, the permission default still denies paths outside the working directory and `--add-dir`.
  3. The guard hook. It uses the same two inputs as the CLI: the workspace and `limits["add_dirs"]`. One source, so the checks cannot drift.
- Fail-open note: a hook that exits 1 or times out does not block the call (the call goes on to layers 1 and 2). So the hook is never the only layer. A missing guard file makes `build_argv` raise, so no session starts.
- Guard rules: a path must resolve (symlinks followed) inside a root, compared by path segment and exact case. A path field with leading or trailing whitespace, a control character, or a leading `~` is denied (the guard does not expand `~`). A `..` segment is denied in Glob and Grep patterns. A Read path with `..` that resolves inside a root is allowed (`sub/../in.txt`). An absolute glob pattern must stay under a root. A non-leading absolute brace alternative is always denied, also under a root. Unknown tools are denied.
- Allow-list by role (root 1 is the working directory):

| Role | Root 1 | `add_dirs` |
|---|---|---|
| implementer, strong tier | the clone | `<spool>/requests`; `<spool>/prior` when earlier runs exist |
| implementer, light and mac tiers | the clone | `<spool>/requests` (and `prior`) |
| blind tester | the blind clone | none |
| reviewer | the clone | `<spool>/requests` |
| Korin | the main checkout | `<spool>/requests`, the incident note folder, the evidence folders |
| guide, validator, other review roles | the spool | `<spool>/requests` (no read tool) |

- The whole spool is not readable. `mcp.json`, `job.json` and `seatbelt.sb` stay closed. Contracts and prompts reach the session inline (`--append-system-prompt`, `--json-schema`, stdin), not by path.
- The owner's secret-read guard is a different hook for the owner's sessions. Workers never load it ([secret-read-guard](secret-read-guard.md)).

## Failure modes and what happens
- Residual: a hard link already inside the clone that points at an outside file can be read. Workers cannot make one: the strong-tier shell sees only its container, and the light-tier seatbelt refuses `ln` to outside files. Git does not store hard links.
- Residual (Glob, names only): some patterns pass the guard and can list file names outside a root, never contents: `{.,x}./outside/*` (a `..` made by brace expansion), `sub/.[.]/x`, `[l]inkdir/*` and `linkdi?/*.txt` (a wildcard that matches a symlinked folder), `**/secret.txt`, `@(/etc/passwd|x)`. Layers 1 and 2 still apply, and Read of such a file is denied.
- Residual: a symlink swapped between the check and the read is not detected.
- All three layers need the vendor CLI to keep its behaviour. Only a kernel sandbox removes that need. This is a possible later card.
- False denies are possible (case-insensitive file systems, Unicode normal form, a tab in a file name, the `/System/Volumes/Data` prefix, leading or trailing spaces, `..` in a glob). All fail closed and show in `permission_denials`.
- `exec_mcp` spills output over 64 KiB to `<spool>/spill/<n>.txt` (`capsule/runners/exec_mcp.py`). The host creates each file new, mode 0600, through a no-follow folder handle; it never follows or overwrites an entry. No role can read that folder yet.
- Sessions that do not use `build_argv` (patrol, ask panel, brainstorm, maintenance) are controller jobs and are out of scope.

## Live smoke (second oracle)
The guard's blind tests are the first oracle. The second oracle is the real CLI's `permission_denials`. It needs a login, so run it by hand at each release and after each Claude Code upgrade, not in CI.
1. Build a folder with `clone/` (working directory), `outside/canary.txt`, `spool/requests/`, `spool/mcp.txt`, a file symlink and a directory symlink in `clone/` that point to `outside/`, and a hard link of the canary in `clone/`.
2. Start `claude -p --model haiku` with the implementer argv from `build_argv` (`--restricted`, `acceptEdits`, `--allowedTools mcp__capsule__exec`, `--add-dir spool/requests`, the `--settings` guard). Ask it to Read and Grep each path above.
3. Expect denials for every path outside, both symlinks, `..`, `spool/mcp.txt`, and absolute Glob patterns. Expect the clone and `spool/requests` reads to pass. Only the hard link may show content.

## Tests that pin it
The hidden blind suite (kept in the private blind repo; it runs the script as a subprocess), `capsule/tests/test_models.py` (`--settings` shape, quoting), `test_worker_mcp_fence.py`, `test_light_runner.py`; `contracts/intent/read_guard.jsonl`.

## Related decisions and specs
Zenkai Z1 and Z2 in `docs/ZENKAI.md`; [KNOWN-ISSUES](../../KNOWN-ISSUES.md) item 8; [worker-runner-and-images](worker-runner-and-images.md); [secret-read-guard](secret-read-guard.md).

## Status
current (guard in place; hard-link residual open).
