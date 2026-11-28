# Image record (worker images)

## Purpose
Say, for each board, exactly which container image the implementer runs in, by digest, and let an
operator build a project layer without giving a worker the power to build. The record replaces the
old pinned-tag file.

## Where it lives (files)
- `capsule/image_render.py`: `python3 -m capsule image render [--project DIR] [--lock FILE] [-o PATH]`. A judge, not a builder (Z2): runs no docker and no network, accepts only a short list of keys from `capsule.toml [image]`.
- `capsule/image_build.py`: `image build --project DIR --board B [--dry-run]` (a dry run runs no docker command) and `image which --board B`.
- `capsule/image_requests.py` (+ `image_ledger.py`, `image_toml.py`, `image_request_run.py`): M137, a worker's missing tool becomes an image-request card that a tick step builds (below).
- `capsule/runners/docker.py`: `worker_image`, `lock_image`, `record_image`, `project_image_name`, `missing_image_message`. `capsule/paths.py`: `image_record` (the file `$CAPSULE_HOME/images.lock`).
- `docker/base/Dockerfile`, `docker/stacks/python/Dockerfile`, `docker/stacks/web/` (Dockerfile, `package.json`, `package-lock.json`), `docker/build-base.sh`, `docker/IMAGES.lock` (maintainer record of base and stack digests), `docker/BUILD_MIN_FREE_BYTES` (the free-disk floor for a build, 40 GB today).
- `maintenance/image_gc.py` (nightly, label `capsule-layer` or `capsule=1`), `maintenance/image_sweep.py` (after a landing: dangling images and superseded tags of Capsule's own images), `maintenance/obs_image_lock.py`.

## Inputs and outputs
In: `capsule.toml [image]` (one stack, apt names, hash-pinned lockfiles, smoke lines), the machine's record rows. Out: a project Dockerfile, a `capsule-proj-<board>@sha256:<hex>` row in `$CAPSULE_HOME/images.lock`.

## State it reads and writes
`$CAPSULE_HOME/images.lock` (atomic rewrite under a lock; only the named board's row changes). `docker/IMAGES.lock` is written by `docker/build-base.sh` only.

## Key rules and invariants
- Resolution order (`worker_image`): `OPS_WORKER_IMAGE` in the environment; the board's project row; `OPS_WORKER_IMAGE` in `ops.env`; the `capsule-python` row; else nothing, and the run refuses to start. No tag fallback. A workspace whose capsule.toml declares a non-empty `[image].stacks` needs its project row: with no `OPS_WORKER_IMAGE` in the process environment (an ops.env pin does not count) and no row, `StrongRunner.start` raises `MissingImage` before any docker call, naming the stacks and the `image build` command; the tick holds the card as a `capability` block (no failure, no tick_error, no retry) until the image is built. A list that is exactly `["python"]` is exempt (the generic image is what it asked for). The `capsule-python` fallback serves only undeclared and python-only workspaces (inc_8ab8c503).
- Every edge between layers is a digest, never a tag. Python lockfiles install wheels-only with `pip --require-hashes`; web lockfiles are `package-lock.json` version 3 with sha512 integrity and install with `npm ci --ignore-scripts`.
- Every value is matched whole against a strict pattern before reaching a Dockerfile line; smoke lines run later in a `--network=none` container started from the built digest, never in the Dockerfile.
- Exit codes of `image render` and `image build`: 0 done, 1 refused or failed (record untouched), 2 bad input.
- `docker/build-base.sh` keep-tags the previous `NAME:local` as `NAME:prev-<12 hex of its id>` before it builds; `maintenance/image_sweep.py` drops that tag only when no record names the image and it is not fresh (it keeps every pinned or in-use image whole, and per repo it spares the newest rollback image: the newest `prev-*`-tagged image, else the newest untagged `capsule-layer` image (Created time, tie on id); older ones are swept), so a recorded image is never left removable (inc_3a7d6457). `capsule/image_build.py` `build` does the same for project images (probe, tag, then build; a failed tag stops the build; dry run runs no docker), and the sweep also drops unrecorded old `capsule-proj-*:prev-*` tags.
- `docker/base/**`, `docker/stacks/**`, `docker/IMAGES.lock`, `docker/build-base.sh`, `docker/BUILD_MIN_FREE_BYTES` and `capsule/image_render.py` are protected paths ([gate-protected-paths](gate-protected-paths.md)).

## Image requests (M137, a worker's missing tool)
A `block` request of kind `capability` may carry `tool` (a Debian package) and `bin` (a command). `spool` hands it to `image_requests.file`: one card per board and tool (assignee `image-builder`, which `config.validate_assignees` refuses as a key, so nothing routes it to a model and the unowned detector sees it as `unassigned`), the waiter linked under it and held as a dependency. The tick's `image_requests.step` (called in `Tick._step_pass` after `stall.step`, which runs `docker_down.step`) edits only the project's own capsule.toml (`image_toml`: `[image].apt` and `[image].smoke`, single-line arrays, re-parsed to prove nothing else moved), builds in a throwaway clone through a detached `capsule image request-run` (`image_build.build`, label `capsule.kind=test`, one build per machine, an `fcntl` lock the detached builder inherits and holds, `image_request_run.py`), and on success records the digest, pushes `image/<task>`, marks the card `done` with gates `image_render` and `image_smoke`, and promotes each waiter (`blocks.promote_one`). It never starts Docker: docker down or free disk under `docker/BUILD_MIN_FREE_BYTES` waits and counts no try. D8 limits (they bound rebuild cost) live in the host ledger `<home>/image/requests.jsonl`: 3 requests per board per day, 2 tries per card, 60 build minutes per day for the whole machine; a human `unblock` adds one extra try. A missing `[image].stacks`, a multi-line array, `OPS_WORKER_IMAGE`, or a breach of the D10 disk budget (the disk keeps 25 GB free; all images together stay under 20 GB) or growth of one image over 1 GB, all block `needs_input`.

## Failure modes and what happens
No row for the board: `capsule image which` and the runner print the build command and the start refuses, also when a `capsule-python` row exists and the workspace declares `[image].stacks`. A failed smoke line leaves the record untouched.

## Tests that pin it
`capsule/tests/test_image_render.py`, `test_image_build.py`, `test_image_requests.py`, `test_image_loop_switch.py`; `tests/test_docker_base_image.sh`; `contracts/intent/image_render.jsonl`, `image_build.jsonl`, `image_lock.jsonl`, `docker_base_image.jsonl`, `web_stack.jsonl`.

## Related decisions and specs
Worker images spec (S10a, INFRA-10, R10, R11); [worker-runner-and-images](worker-runner-and-images.md). See also [worker-runner-and-images](worker-runner-and-images.md).

## Status
current. An automated builder loop (an agent proposing images) is not built.
