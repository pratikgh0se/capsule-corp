# Egress - capsule-corp

> **Update 2026-10-23 (tick era):** The earlier prototype and its Telegram gateway are retired. Door 2 is now
> Slack, sent through `python3 -m capsule outbox-send`; the old gateway send command and `api.telegram.org` below are
> historical. See [tick-scheduler](architecture/components/tick-scheduler.md).

Spec: docs/superpowers/specs/2026-09-09-retention-egress-design.md section
5 (decision row 75).

> Data leaves this laptop through exactly four doors, each with an audit
> row (a fifth, the image build, is build-time only and fetches packages in;
> it is listed after the table): (1) git pushes: `vault_git.push()` to the
> vault remote (allowlisted paths; secrets scrubbed by `.gitignore` + gitleaks in CI), and the
> lander and keeper repo pushes to GitHub (the old `merge_gate` repo push retired 2026-11-12,
> D238: the owner's GitHub merge of the epic PR is the approval); (2) Slack messages sent by the Capsule outbox
> sender (`capsule/slack.py`); (3) the
> encrypted off-machine backup - today a manual weekly external-disk copy of
> already-encrypted ciphertext, not a code path (row 137); (4) the
> healthchecks.io dead-man ping `run.sh` sends after the `backup` job (job
> name and exit code only, row 137/M214). Model calls by Claude Code and
> the earlier prototype carry code and vault text by design and are governed by the
> subscription terms, not by this rule. Nothing else may open an outbound
> connection: workers run with `docker_network: none`; maintenance scripts
> may call only localhost services.
>
> The Mac lane (D305, `redesign.mac_runner`) has no network, like the light tier, and no
> listening socket. The profile can take `localhost:<port>` endpoint rules, but no card field or
> caller passes any yet, so today it has none; the model endpoint stays host-side.

## The doors

| Door | Code path | Audit row | May carry | May never carry | Enforced by |
|---|---|---|---|---|---|
| 1. Git push (repos + vault) | `maintenance/vault_git.py` `push()` (`maintenance/merge_gate.py` keeps only read-side git/gh calls for its check helpers since D238); the fork-sync section of `maintenance/run.sh`; deploy.sh/`backup.sh`/`restore.sh` (bundle/deploy git operations) | `events.log("job", "vault_git", ...)` (vault push failures/successes); the lander and keeper logs | Repo source, docs, specs/plans, decision notes, kanban-derived markdown | Secrets, `.env` contents, session transcripts, anything under `state/` | `test_egress.py` (git/gh file-scope check against `GIT_GH_ALLOWED_FILES`); `PUSH_ALLOW`/`PUSH_DENY` path allowlist in `vault_git.py`; `.gitignore` + gitleaks in CI |
| 2. Slack | the Capsule outbox sender (`capsule/slack.py`, `python3 -m capsule outbox-send`); the legacy gateway send command invoked from `run.sh`/`report.sh`/`heal.sh`/`board_bridge` through the gateway profile | `events.log(...)` rows written by the callers before/after send; the gateway's own send log | Status lines, digests, alerts, decision/approval prompts | Secrets, `.env` contents, session transcripts, anything under `state/` | `test_egress.py` (`api.telegram.org` is not on `ALLOWED_HOSTS`; a planted one fails) and `capsule/tests/test_telegram_retired.py`; `AGENT_OPS_NO_SEND=1` short-circuits sends in tests |
| 3. Encrypted off-machine backup | `maintenance/backup_key.sh`'s `backup_enc()` encrypts the tarball on the laptop, behind `OPS_BACKUP_ENCRYPT`, before anything could carry it off-machine; the off-machine copy itself is the owner's manual weekly whole-laptop backup to an external hard disk | the `backup` job's own `kind=job` event (the encryption step, on-machine); the off-machine copy is manual and outside any code path, so it has no event of its own | only ciphertext leaves - the AES-256-encrypted `backup.sh` tarball, nothing else | Secrets, `.env` contents, session transcripts, anything under `state/` (same exclusions `backup.sh` already applies today); the plaintext tarball itself, which is retained 0 days | there is nothing to enforce and no host to allowlist today - a manual, offline, network-touching-nothing copy is not a code path. Trigger that turns this into a real door needing a code path, an audit row, a host on `ALLOWED_HOSTS` and its own contract test: blob storage available, ~2026-11, with the planned cloud deployment (row 137, M211/M212) |
| 4. Healthchecks.io dead-man ping | `maintenance/run.sh` (`hc_ping`, called from `write_stamp`) | the `kind=job` event the same function has just written | the job name and its exit code, in a URL path and a one-line body | Log text, file paths, card or spec content, secrets, `.env` contents, anything under `state/` | `test_egress.py` (`hc-ping.com` on `ALLOWED_HOSTS`, and `test_hc_ping_lives_in_exactly_one_file` pins the single caller) |

**Door 5, the image build (R11, spec "Worker images" sections 3 and 6).** Not a door for data
leaving: it is the host-side `docker build` / `docker pull` (`docker/build-base.sh`, and the
builder loop later) fetching packages in. It is listed here because it is the one place a
network interface is used for the worker images, and it is build-time only: a running worker
still has `--network=none`, and nothing in this section widens any runtime or worker access.

| | |
|---|---|
| Code path | `docker/build-base.sh` (base, `python`, `web`); `docker/base/Dockerfile`, `docker/stacks/*/Dockerfile`; project layers rendered by `capsule image render` |
| Audit row | none yet: `docker/build-base.sh` writes no event row; the only record of a build is the row it writes: in `$CAPSULE_HOME/images.lock` (a tester's build; no git history), or in `docker/IMAGES.lock` when run with `IMAGES_LOCK=docker/IMAGES.lock` (the maintainer's; that file's git history). A build event is a follow-up |
| Host allowlist (nothing else) | `registry-1.docker.io`, `auth.docker.io`, `production.cloudflare.docker.com` (Docker Hub: `FROM`, `COPY --from` and `# syntax=` images, pinned by digest); `snapshot.debian.org` (apt, a dated snapshot); `pypi.org`, `files.pythonhosted.org` (pip, hashed locks); `registry.npmjs.org` (npm, `package-lock.json` with integrity hashes, `npm ci --ignore-scripts`) |
| Not on the list | `cdn.playwright.dev` (the web stack uses Debian's Chromium from the snapshot instead), `curl | sh`, any bare URL |
| Enforced by | `test_egress.py` `BuildDoor5`: every host named in a Dockerfile or in `package-lock.json` is on that list, the list shares no host with `ALLOWED_HOSTS`, and this section names each host; `capsule image render` refuses unlocked or URL installs; builds refuse under the disk floor (`docker/build-base.sh` refuses under 40 GB free: the 25 GB floor plus headroom). The build container's network is not technically limited to this list: the list is a review-time check on the build inputs, not a firewall |

The old all-in-one worker image (a tag-pinned third-party base) is retired: every worker image is the base, a stack and a project layer, all digest-pinned.

Localhost-only services (Grafana, Infisical status, the local Docker daemon)
are not doors - they never leave the machine - but the scanner still checks
their target hosts. `maintenance/secrets_import.py` and friends may resolve
`INFISICAL_API_URL`, `CAPSULE_SECRET_STATUS_URL`, `INFISICAL_STATUS_URL`, and
`GRAFANA_URL` from environment variables with no literal on the call line;
`test_egress.py` follows each variable back to the literal it ultimately
comes from (`_resolve_name()`, a fixed set of idioms: a direct assignment, a
`${NAME:-default}` bash default, a `x = x or DEFAULT` fallback, a one-name
f-string, a call's first argument) and requires that literal's host to be on
`ALLOWED_HOSTS` - for these four, one of `DEFAULT_LOCAL_HOSTS`
(`localhost`/`127.0.0.1`). A variable that pointed one of these at a real
host would fail the same way a hardcoded external URL would, and a name
built any other way resolves to nothing and fails as `<unresolved>`.

## How it is enforced

`maintenance/tests/test_egress.py` is the contract test. It scans
`maintenance/*.py`, `maintenance/*.sh`, `bin/*`, the two root-level scripts
deploy.sh and bootstrap.sh (`ROOT_FILES`), and
`launchd/templates/*.plist` for `urllib`/`http.client`/`requests`/`curl`/
`wget`/`nc`/`socket` calls, and for `git push`/`fetch`/`pull` or `gh`
invocations, and checks each hit against:

- **The host allowlist** (`ALLOWED_HOSTS`): `127.0.0.1`, `localhost`,
  `host.docker.internal`,
  `github.com`, `api.github.com`, `raw.githubusercontent.com`,
  `hc-ping.com` (restricted to `run.sh`). Any other literal host fails the
  test with the file:line that carries it.

  Every other watchdog in Capsule Corp - Grafana's rules, `heal.sh`'s own
  checks - is a container running on the same laptop it is watching, so the
  one night the laptop itself is asleep or off is exactly the night none of
  them can page anyone. The healthchecks.io ping is the one door that leaves
  the laptop on a schedule instead of on an event, which is what makes an
  absence of pings - not a bad reading - the alarm. It deliberately carries
  nothing about the job beyond its name and exit code: no log text, no file
  paths, no card or spec content, nothing under `state/`. The uuid in the
  ping's own URL path is a bearer capability - anyone who has it can ping (or
  silence) that check - so it is the *secret*, read fresh from Potara on
  every run and never written to a log, an event or a committed fixture; the
  `hc-ping.com` host is the opposite, a fixed literal that has to stay in the
  source for `test_egress.py`'s scanner to see it, which is what keeps this
  door enforceable at all - a host resolved only from an environment
  variable would be invisible to the same scan.
- **The git/gh door-file allowlist** (`GIT_GH_ALLOWED_FILES`):
  `merge_gate.py`, `vault_git.py`, `run.sh`, deploy.sh, `backup.sh`,
  `restore.sh`, plus bin/capsule-corp.zsh and bootstrap.sh. A
  `git push`/`fetch`/`pull` or `gh` call anywhere else fails.
  bin/capsule-corp.zsh is a member of that set, not a separate exemption, on
  the controller's ruling that it is a set of interactive shell aliases
  the owner types at their own terminal (`ops-deploy -> git pull
  --ff-only`), not unattended automation - the same reasoning this rule
  gives model calls, not a widening of the host allowlist. bootstrap.sh is
  there for the same kind of reason: it is the operator-run first-install
  script, its `git` use is `git clone` of the pinned repos onto a fresh Mac,
  and its two `gh` hits are a `command -v gh` presence check and an echoed
  "run `gh auth login`" instruction to the human.

Plus the sandbox invariant from row 108: every code-role config has
`docker_network: none`, no worker `config.yaml` mounts a secret path, no
`SOUL.md` mentions `delegate_task` (checked by
`maintenance/tests/test_contracts.py`, not `test_egress.py`).

`test_egress.py` is a line-literal scanner - a tripwire against the easy
regression, matching `test_contracts.py`'s `_kanban_argv_lines` in spirit
and in limits, not a data-flow proof or a network sandbox (R4). Comment
lines, multi-line Python docstrings, and plist `<!DOCTYPE>` boilerplate are
excluded from the scan; planted-fixture tests in the same file assert the
scanner actually fires on an unknown host, an out-of-door `git push`, and
`api.telegram.org` anywhere (Telegram is retired, D229).

## Not doors

**Disk relief (D204).** `maintenance/disk_relief.py` makes one network call: a fetch of this
repo's own origin (BatchMode, no prompts), so it can prove a worktree's branch is merged into
main before removing it. Everything else it runs (worktree list/remove, merge-base, cherry) is
local. It is not a door and is not on the door-file list; its messages avoid the literal fetch
phrase the scanner matches on, so a real new fetch elsewhere still trips the test.

**Grafana anonymous Viewer (W333).** `cc.grafana.localhost` (and `127.0.0.1:3000`) needs no login:
anonymous users are Viewer, read-only. A container on a bridge network (the researcher role) can
reach it through `host.docker.internal`, because Docker Desktop maps that name to the host's
loopback. Accepted because Viewer can read dashboards only; it cannot edit alert rules or create
tokens. Never raise it to Admin. Edits use the admin login, saved in Chrome.

**Model calls by Claude Code and the earlier prototype.** These carry code and vault text
to Anthropic by design - that is what the tools are for - and are governed
by the subscription terms, not by this rule. `test_egress.py` does not scan
for them at all.

**Docker image pulls at deploy.** deploy.sh runs `docker pull -q
"$WORKER_IMAGE"` where `WORKER_IMAGE` is resolved from `OPS_WORKER_IMAGE` or
a pinned value (typically a `ghcr.io` or Docker Hub reference). Said
honestly: this is **not covered** by `test_egress.py`. The scanner's URL
matcher only fires on an `https?://` literal, and a Docker image reference
like `ghcr.io/org/image:tag` carries no scheme, so the line never produces a
hit for `classify()` to check - `ghcr.io`/Docker Hub are not on
`ALLOWED_HOSTS` and the pull is not treated as a door or blocked as a
violation. It is a real, unguarded egress path; it is out of scope for this
build (spec §7) and is not one of the four doors the rule names.

*Worker-image pin (M051).* The only automated pull call site is that
deploy.sh line (`apply` mode only; `--check`/`--dry-run` say `WOULD pull`),
and it fires only when the `OPS_WORKER_IMAGE` pin contains a `/`, i.e. a registry
reference. The pin that decides which image runs is `docker/IMAGES.lock`
(protected path): one `<ref>@sha256:<digest>` row per layer, rows written only
by `docker/build-base.sh`, and `capsule/runners/docker.py` accepts the
`capsule-python` row only if it is `@sha256:<64 hex>` (a bare tag is refused).
Those rows are images built on this laptop, so using them pulls nothing. The
lock is always the source (`redesign.image_loop` accepts only `"record"`), and
the image order is the one in D328 (`OPS_WORKER_IMAGE` in the environment, then the board's
`capsule-proj-<board>` row, then `OPS_WORKER_IMAGE` in ops.env, then the `capsule-python` row). Drift is watched by `maintenance/obs_image_lock.py`, which
`obs_probe` prints each tick as one advisory `worker image ok|mismatch|n/a`
line (it compares the image the board would run with the lock row; it never
changes the probe's exit code). `run_record` records the image a run used
(whichever source the D328 order picked: a pin or a record row). Still
unguarded at the network level: a registry pull of a tag-pinned `OPS_WORKER_IMAGE`
is not digest-verified (`deploy.sh --check` only prints a `WOULD pin by
digest` note for a tag).

**Homebrew.** maintenance/setup-semantic.sh tells the operator to run
`brew install ollama` by hand when Ollama is missing; this is advice printed
to a human, not an automated network call the scanner needs to allow, and
`brew`'s own package-index fetches are Homebrew's business, not this repo's.

**The first install (bootstrap.sh).** Not a door either - it is the
one-time, operator-run setup of a fresh Mac, not the running pipeline - but
it *is* scanned (`ROOT_FILES`), because it is the one file in the tree that
fetches over the network from a third host: `curl -fsSL
https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh`, the
official Homebrew installer. That is why `raw.githubusercontent.com` is on
`ALLOWED_HOSTS`, and scanning bootstrap.sh is what exercises that entry
instead of leaving it there unused (`test_the_scan_is_not_vacuous` asserts
the host is actually seen). Its `git clone`s of the pinned repos and its
printed `gh auth login` instruction are why it sits in
`GIT_GH_ALLOWED_FILES`.

## Where a candidate's code runs at landing (row 632 b)

The lander never executes a candidate's code on the host. Its suites, the mutation gate's tests and
the suites re-run after a push run in the worker image with `--network=none`, no host environment,
no docker socket, and a private copy of the clone (`capsule/runners/sandbox.py`; [lander-and-keeper](architecture/components/lander-and-keeper.md)). The only host commands are the trusted checkout's text-only gate scripts and its
`deploy.sh --check`. When docker or the image is missing the check waits, never runs on the host.

## Proving zero egress from a `network: none` worker (row 127, M040)

`test_egress.py` proves the *pipeline's own code* never opens a door it
isn't allowed to. It says nothing about what a worker container could do at
the docker level if its code tried anyway - that is a separate claim, and
this section is what backs it.

**What is proved, today, by static config plus a live sample:**

- Seven of the eight code-role sandbox profiles carry `docker_network: none`
  in the earlier prototype's profile configs (removed in D198/D202; historical) - `dev`, `dev-backend`,
  `dev-frontend`, `qa`, `infra`, `lead`, `reviewer`
  (`maintenance/tests/test_contracts.py:635`,
  `Sandbox.test_sandbox_roles_have_no_docker_network`, spec
  2026-09-09-retention-egress-design.md section 5, decision row 108). Docker
  itself enforces `network: none` - no interface but loopback exists inside
  the container, so there is nothing for an escaped process to dial out on.
- `researcher` is the one role that is `docker_network: bridge`
  (the removed researcher profile of the earlier prototype) - it is the only sandbox
  profile permitted a network interface at all, by design (research needs
  to fetch pages).
- A live sample: `capsule.sandbox.containers` (how many running containers
  are the worker image, `maintenance/obs_probe.py`'s `collect_sandbox`,
  matched by `board_bridge.BRIDGE_IMAGE`'s repository since worker
  containers are ephemeral and carry generated names) and
  `capsule.sandbox.network.attached` {container} (1/0 from `docker
  inspect`'s `HostConfig.NetworkMode`, per running container) should read
  exactly zero attached, every tick, for the seven `none` roles. A container
  this probe cannot inspect reports `attached=1` - unknown is not "safe" -
  so the sample is a real alarm, not a config file nobody re-checks once
  it's merged.

**What is not proved: bytes.** None of the above says how many bytes left
(or tried to leave) a container, or to where. `docker_network: none` and a
network-attachment count of zero are a strong claim about the *interface* -
there is nothing to send bytes over - but they are not a byte-level audit
trail the way the four doors above have one. Proving bytes needs either a
packet capture on the host's docker bridge/veth, or a sidecar container with
`NET_ADMIN` sitting between a role's container and the network, logging what
crosses. That is a separate card, deliberately not built here (KISS - a
sandbox with no interface has nothing to capture), and its trigger is either
of two events: the first tick `capsule.sandbox.network.attached` reads
non-zero for a role that should be `none`, or M038 (a real fetch capability
for `researcher`) going live - at which point `researcher`'s traffic is the
one lane that needs a byte-level log, not the six `none` roles that have
none to log.

The fetch log itself - URL, domain, bytes, sha256 - belongs to the fetcher
when it exists (M038), not to this probe: it rides `researcher`'s `bridge`
lane, the one role with an interface to fetch over. A `network: none` role
cannot fetch at all, so it has no fetch log to produce - its only egress
story is "prove the interface doesn't exist," which is what
`capsule.sandbox.network.attached` does.
