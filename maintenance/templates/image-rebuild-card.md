# Image rebuild: <what changed, e.g. "bump hypothesis to 6.x in the python stack">

## Why

<one or two sentences: what in the base, a stack or a project layer changed and why a rebuild
is needed>

## What changed

- Base image / package(s): <name@version -> name@version>
- Dockerfile / requirements diff: <link or summary>

## Evidence (required for every new or changed package)

- Registry: HEAD <url> -> <http status> (<date>)

The `HEAD` above is run by hand or in the sandbox and its result pasted here -
capsule-corp code never makes this call itself, since an outbound request from
the codebase would mean widening `test_egress.py`'s `ALLOWED_HOSTS`, a
decision this row does not take (design doc section 4.6).

## Build

Rebuild the layer with `docker/build-base.sh` (base) or the image builder (stack and project
layers). Each build writes its digest row to `docker/IMAGES.lock`; `capsule image which`
prints what a board will run.

## Check

- [ ] `capsule image which` names the new image
- [ ] tests/test_deploy.sh and `tests/test_ci_yaml.sh` still pass
