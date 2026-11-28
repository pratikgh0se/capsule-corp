# Setup on a fresh Mac

Step-by-step install of the Capsule Corp factory. Every step has a verify command. Commands are taken from this repo's scripts. A step marked **UNVERIFIED** is described from the code but has not been run end to end on a clean machine. Read it and adjust.

## 0. What you are installing

- The Capsule harness (`capsule/`, plain Python, standard library) and its tick, one launchd job per board.
- A Docker worker image the implementer runs in.
- Maintainer only, not part of a first tester's install: a dashboard stack (Postgres, Loki, Fluent
  Bit, Grafana, Caddy; section 9). `./install.sh` does not start it.

Data leaves the machine only to Anthropic (the model, through your Claude Code login), GitHub (your repositories) and, if you enable it, Slack. See `docs/EGRESS.md`.

Every change is reversible: `./install.sh --dry-run` lists what it would write, the files it writes are listed below, and `launchctl bootout` stops any job.

## Limits for first testers

- **Cards cannot install pip or npm packages yet.** The implementer container has no network, and the dependency folder it mounts read-only (`~/.local/share/capsule-corp/deps`) is empty after install: nothing in this release fills it. A repo you point a board at should use only the standard library for now, or a card that needs a package will fail.
- Landing is a dry run until you set `lander.mode` to `land` (section 8).

**Verified so far.** In CI: the install dry run on a macOS runner (section 3) and both unit test suites (section 12). On the author's machines: the same, plus `capsule doctor`.
**Not verified yet.** On a fresh macOS account: the full `./install.sh --yes`, the first OrbStack start, the worker image build (section 7), `capsule board new`, and a first card landing. See [KNOWN-ISSUES.md](KNOWN-ISSUES.md).

## 1. Prerequisites

1. macOS 14 or newer, with Homebrew (https://brew.sh; the installer never installs Homebrew itself) and `launchctl` (ships with macOS). `install.sh` exits with "macOS only" on other systems.
2. Install OrbStack (the default Docker engine) and start it. OrbStack is free for personal use; commercial use needs a paid OrbStack licence. Docker Desktop is the alternative: set `CAPSULE_DOCKER_ENGINE=desktop` or write `desktop` to the `docker-engine` file ([worker-runner-and-images](architecture/components/worker-runner-and-images.md), "Docker engine switch").
   - Verify: `docker info` prints server details.
3. Log in to GitHub: `gh auth login`.
   - Verify: `gh auth status`.
4. git 2.40 or newer (`git --version`; the Brewfile installs it, since macOS ships an older one). Older git cannot do the protected-path check, so `capsule doctor` reports `MISSING git 2.40+` with `brew upgrade git` as the fix.
5. Python 3.11 or newer (`install.sh` finds one, or with your yes installs it through `uv`), a Claude Code login (section 5 below) and about 25 GB of free disk (40 GB until the worker image is built).
6. Check all of the above in one go (read-only; it installs and writes nothing): `python3 -m capsule doctor` from the clone, if your `python3` is 3.11 or newer. On the stock 3.9 it stops with a one-line message: run `./install.sh` (section 1b) first, which ends with this same check, and rerun it any time as `~/.local/bin/capsule doctor`.
   - It prints one line per prerequisite: `ok <what>`, or `MISSING <what> -> <fix>` with the command to run or the thing to install. Then it prints the effective `host.json` settings.
   - The worker image line says which of three it is: `present`, `not built yet, buildable here` (still `ok`: it carries the build command, section 7) or `MISSING` (the build files are gone from your clone). The free-disk line asks for 40 GB while the image is not built, 25 GB after.
   - `--offline` skips the one network check (`gh auth status`) and only confirms `gh` is installed.
   - Exit 0 means every prerequisite is met. Exit 1 means fix the `MISSING` lines, then run it again.

## 1b. The one-command install (macOS)

`install.sh` is a thin shim: it finds a `python3` of 3.11 or newer (with your yes it runs `brew install uv && uv python install 3.11` when there is none), then runs `python3 -m capsule install`. Run it from a clone; never pipe it from the network. It never builds an image or loads a launchd job.

```bash
./install.sh --dry-run      # print every step; changes nothing
./install.sh --yes          # take the defaults, ask nothing
./install.sh                # ask before each change
./install.sh --capsule-home DIR   # keep the factory's state in DIR (default ~/.local/share/capsule)
```
- `--dry-run` prints one line per step (`WOULD ...`, or `ok` when it is already done) and writes nothing. The only commands it runs are read-only checks (`brew bundle check`, `docker image inspect`).
- `--capsule-home DIR` moves `$CAPSULE_HOME`. The keeper-watch plist carries it (as `CAPSULE_HOME` in its environment and in its log paths), and the launcher keeps it unless you set `CAPSULE_HOME` yourself.
- `--yes` runs without prompts. Without it, every change asks `[y/N]` and no answer means no.
- It is idempotent: a second run changes nothing, prints no `done` and no `WOULD` line, and ends with `capsule doctor`. Lines are `ok` (already done), `done`, `WOULD`, `skip` (left alone on purpose, the line says why), `next` (run this yourself) or `FAILED`.
- The exit code is doctor's, or 1 if any step printed `FAILED`. A `--dry-run` does not run doctor: it ends with a `WOULD capsule doctor` line and exits 0 unless a step printed `FAILED`.

What it does, in order:
1. `brew bundle` of the Brewfile: `uv`, `gh`, `git`, and the `claude-code@latest` and `orbstack` casks. Nothing else, no third-party taps. A cask is left out when its tool is already installed (an existing `/Applications/OrbStack.app`, the native `claude`), so the bundle does not fail on it; the line says so. Brew output is shown as it runs, auto-update is off, and tools you already have are never upgraded (`HOMEBREW_BUNDLE_NO_UPGRADE`). It fails with a pointer if Homebrew is missing; it never installs Homebrew.
2. Creates `$CAPSULE_HOME` (default `~/.local/share/capsule`) and its `logs` folder.
3. Writes `$CAPSULE_HOME/host.json` with `launchd.label_prefix` set to `dev.capsule` (or the prefix you already set by env or file). An existing `host.json` is never overwritten.
4. Writes `~/.local/bin/capsule`, a small launcher that runs `python3 -m capsule` from this clone, with the Python 3.11+ it found (a stock Mac's `python3` is 3.9 and cannot run capsule). From here on use `~/.local/bin/capsule` wherever this guide says `capsule`; `python3 -m capsule` on a stock 3.9 stops with a one-line message that says the same. It does not edit your shell profile: if `~/.local/bin` is not on `PATH` it prints the exact line to add yourself.
5. Writes the keeper-watch plist into `~/Library/LaunchAgents` and prints the one `launchctl bootstrap` command that would load it. It does not load it. A plist that is already there but differs, or is a symlink, is never replaced, even with `--yes`: the line says `skip ... differs: move it, then rerun`.
6. Prints one `skip` line for the per-board tick plists and the harness checkout: those are set up per board (see "Not part of the install" below).
7. Checks the worker image recorded in `$CAPSULE_HOME/images.lock` (section 7 below). If it is not built, it prints `next ... run docker/build-base.sh base && docker/build-base.sh python` (needs 40 GB free and takes hours); it builds nothing itself.
8. Runs `capsule doctor`; the install exits with doctor's exit code, or 1 if any step printed `FAILED`.

The installer itself writes only under `$CAPSULE_HOME`, `~/Library/LaunchAgents` and `~/.local/bin` (the tools doctor probes may create their own files), and never reads or prints a secret. The `capsule` launcher it writes starts Python with `-P`, so a `capsule/` folder in your current directory can never shadow this clone.

**Not part of the install (you do these next):**
- A board and its tick: `~/.local/bin/capsule board tick <board>` sets up the tick plist and the release checkout under `$CAPSULE_HOME/harness/main`, then `<repo>/bin/capsule-tick.sh start <board>` (an absolute path) starts it (see "8. Create a board and start its tick" below).
- The dashboards: optional, see "9. Local UIs" below.
- OrbStack's first launch: open it once (`open -a OrbStack`) and accept its prompts.
- The `claude` and `gh` logins: `claude` then `/login`, and `gh auth login`. `capsule doctor` reports both until you do.

## Linux, manual

`install.sh` refuses to run on anything but macOS (a pointer to this section on stderr, exit 2). The harness and its unit tests are plain Python and run on Linux: `python3 -m unittest discover -s capsule/tests -t .`. There is no scheduler there yet: run the tick under a user systemd unit or a shell loop (`python3 -m capsule tick --board <board>`). **UNVERIFIED**.

## 2. Clone to the expected path

Keep the clone where it is: the `capsule` launcher and the tick plist point at it.

```bash
git clone <this repo> capsule-corp
cd capsule-corp
```
- Verify: `git status` is clean.

## 3. Dry run the install

```bash
./install.sh --dry-run
```
Prints every step and changes nothing (section 1b says what each line means).
- Verify: no `FAILED` line.

## 4. Run the install

```bash
./install.sh
```
It asks before each change (`--yes` takes the defaults) and is idempotent: re-run it after fixing anything.
- Verify: it ends with `capsule doctor`; `claude` and `gh` show as missing until you log in (step 5).

## 5. Log in to Claude Code

```bash
claude
```
Complete the login. Workers run under this login; no API key is stored in the repo.
- Verify: `~/.local/bin/capsule check --board <board>` (after step 8) reports `claude` as ready. **UNVERIFIED** on a fresh machine.

## 6. Start Docker

```bash
open -a OrbStack
```
Open OrbStack once and accept its prompts; the implementer runs in a container.
- Verify: `docker info` prints server details.

## 7. Build the worker image

You build the worker image yourself, once. There is no hosted download. It needs about 40 GB of free disk and takes hours. The build script refuses with exit 2, before it runs any docker command, when under 40 GB is free. With Docker running, from your clone:

```bash
docker/build-base.sh base && docker/build-base.sh python
```
If your `CAPSULE_HOME` is not the default `~/.local/share/capsule`, run `export CAPSULE_HOME=DIR` first (`capsule doctor` and `capsule install` print the command with it).
- Where it is recorded: the script writes the digests to `$CAPSULE_HOME/images.lock`, outside every git clone. The tick runs from `$CAPSULE_HOME/harness/main`, a clone of origin, so a file written inside your own checkout would never reach it, and the digests committed in `docker/IMAGES.lock` are the maintainer's. The `capsule-python` row is the image the workers run.
- Which board uses it: every board. The image is base, then stack, then an optional project layer (`capsule image build --project DIR --board NAME`). The tick, the landing sandbox and `capsule check` run the recorded image and nothing else, chosen in the D328 order: `OPS_WORKER_IMAGE` in the environment, then the board's own project row, then `OPS_WORKER_IMAGE` in ops.env, then the python stack row; `capsule check` says so on the `worker_image` line when an environment or ops.env pin is the image in use. A board whose image is not its own project row (the generic python image, or the machine-wide ops.env pin) gets `info worker_image_fallback: WARN ...` from `capsule check`, naming which of the two it runs and the project build command, unless the project's capsule.toml file asks for exactly `stacks = ["python"]`. A record that is missing, empty or malformed means "no image", never the wrong one: the tick treats the strong tier as down and cards wait, and they start on the next pass once the record exists.
- Verify: `~/.local/bin/capsule doctor` prints `ok worker image present (recorded in ...)`; after step 8, `~/.local/bin/capsule check --board <board>` prints `ok worker_image <digest>`. Before the build, `check` prints `FAIL worker_image no worker image recorded: build it with docker/build-base.sh base && docker/build-base.sh python (needs 40 GB free and takes hours)`, and `doctor` prints `ok worker image not built yet, buildable here: ...` and asks for 40 GB free (25 GB once the image is there).
- The web stack image also carries the python stack's tools (`ruff`, `pytest`, `hypothesis`, `mutmut`, `coverage`, `diff-cover`, `perl`, `ui_guard`), so a Python app with browser tests needs only `stacks = ["web"]`.
- Browser tests in the web stack image: the image has no Playwright-downloaded browser, only Debian's chromium and chromium-headless-shell, but `PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright` holds the folders stock Playwright looks for as symlinks to them (headless launches use the headless shell, as stock Playwright does; `headless=False` and `channel='chromium'` use the full chromium), so a plain `chromium.launch()` (node or python, no `executablePath`) works offline with no project change. Passing `executablePath: process.env.CHROMIUM_PATH` and `args: ['--no-sandbox']` still works (the image sets `CHROMIUM_PATH`; `ui-check` does this).
- Maintainers only: `IMAGES_LOCK=docker/IMAGES.lock docker/build-base.sh ...` writes the tracked lock instead of the record.

## 8. Create a board and start its tick

The harness keeps one board per directory under `$CAPSULE_HOME/boards/<board>/` (default `CAPSULE_HOME` is `~/.local/share/capsule`), with a `config.json` that names the repository, caps and per-role model routing. Steps 1 and 3 are covered by tests (a temporary home, a local git origin, a stub that fails if `launchctl` is called). **UNVERIFIED** on a real fresh Mac: the load into launchd in step 4 and everything after it.

1. Create the board: `~/.local/bin/capsule board new <name> --repo <url>` (`--check CMD` sets the board's check command). It writes `$CAPSULE_HOME/boards/<name>/config.json` (from `capsule/config.example.json`, with `slack.enabled` false; `--notify` turns it on, and then the Slack channel `#cc-<name>` (lowercase, dots become `-`) must exist and the Capsule app must be invited to it) and the board database `$CAPSULE_HOME/boards/<name>/kanban.db`, and prints the commands below.
   - `<url>` is `https://host/path` (no user or token in it), `ssh://[user@]host[:port]/path`, `user@host:path`, or an absolute path to a folder that exists.
   - It refuses, writing nothing, when: the name is not letters, digits, `.`, `_`, `-` (it must start with a letter or digit; max 64, no `..`, not `new` or `tick`); a board with that name already exists; the url is any other form (`http://`, `ext::`, a host starting with `-`, a relative path); or a local path overlaps `CAPSULE_HOME` or the Capsule checkout.
   - Then edit `routing` and `contract_commands` in that file to suit the repository.
2. Check preconditions (read-only): `~/.local/bin/capsule check --board <board>`. It covers config, docker, image and claude. The new board runs the image you built in step 7; until it is built, `worker_image` fails and names the build command. A board name with no `boards/<name>/config.json` is reported as `FAIL config: no such board '<name>'`, unless the old single-board `$CAPSULE_HOME/config.json` has `"board": "<name>"` (a board made before per-board configs): then that file is used, as before. Any other board's name never gets another board's config. Before the tick has started, `FAIL tick: no tick (lock free)` is expected, not a fault: it goes away once step 4 has started the tick. On a fresh machine `FAIL deps_mount` also shows until step 3 has made that folder.
3. Set up the tick job: `~/.local/bin/capsule board tick <board>` (add `--dry-run` to see every step first, `--yes` to skip the questions, `--capsule-home DIR` for a non-default home). It does three things and prints one line for each:
   - makes sure `$CAPSULE_HOME`, its `logs` folder and the empty `~/.local/share/capsule-corp/deps` folder (the worker container mounts it read-only) exist;
   - clones this checkout's `origin` (this checkout's own folder when it has none) into `$CAPSULE_HOME/harness/main`, the folder the tick runs from. A checkout that is already there is never pulled or reset; a folder there that is not a git checkout, or has no commit, is refused; a clone that fails is removed again, so a rerun starts clean;
   - writes `~/Library/LaunchAgents/<prefix>.capsule-tick-<board>.plist`, only once the harness is ready. The plist runs the Python that ran this command (the one the `capsule` launcher uses), never a bare `python3`: a stock Mac's is 3.9 and cannot run the tick. The label prefix comes from `CAPSULE_LAUNCHD_PREFIX`, else `host.json`, else `dev.capsule`. A file already there that differs, or a symlink, is left alone: move it, then rerun. If other Capsule jobs in that folder use a different prefix, it stops and says which: set `CAPSULE_LAUNCHD_PREFIX` to it or move them.
   - It never runs `launchctl`. A second run changes nothing. It refuses a board that has no `config.json`.
4. Start the tick (this loads the job into launchd): run the exact `<repo>/bin/capsule-tick.sh start <board>` line (an absolute path) step 3 printed (it carries `CAPSULE_LAUNCHD_PREFIX=...` when the prefix is not `dev.capsule`, and `CAPSULE_HOME=...` for a non-default home). Status: `bin/capsule-tick.sh status <board>`.
5. File a first card: `~/.local/bin/capsule intake <board> --title "Add a README badge" --body-file card.md`.
6. Watch it: `~/.local/bin/capsule status`, and `~/.local/bin/capsule card <board> <task> history` for one card's event timeline.

The tick job writes its log to `$CAPSULE_HOME/logs/tick-<board>.log` and its event trail to `~/.local/state/capsule-corp/maint/events.jsonl` (the default the tick and `capsule status` share).

Landing is a dry run by default; it pushes only when the board config sets `lander.mode` to `land`.

## 9. Local UIs

**Maintainer only.** First testers follow `./install.sh` and skip this section; the dashboards are
not part of the first tester version.

| URL | Service |
|---|---|
| `http://cc.grafana.localhost` | Grafana, read-only anonymous viewer; edit with the `admin` login |
| `http://cc.secrets.localhost` | Infisical, with its own login. Optional: only exists with the `infisical` secrets backend (compose profile `secrets`) |

Both are served by a Caddy front door on 127.0.0.1:80. Chrome resolves `*.localhost` without a hosts edit; Safari may not.
- Verify: `curl -sI http://cc.grafana.localhost | head -1`.

**Bring them up.** `./install.sh` does not start them. With Docker running, `bin/capsule-ui-up` starts the stack in `observability/docker-compose.yml`, waits for the front door and prints the URLs (`--dry-run` lists the commands). Nothing else in this guide needs the dashboards.

## 10. Secrets

Secrets never live in git. `bin/capsule-secret` is the wrapper (`get`, `run`, `export`, `check`, `backend`, `keychain-check`). `CAPSULE_SECRETS_BACKEND` picks the backend: `file` (`$CAPSULE_HOME/secrets.env`, mode 0600; the default on a new machine), `infisical` (a self-hosted server, optional) or `env` (the process environment, for CI). `bin/capsule-secret backend` prints the one in use. Nothing needs to be in the secrets file unless you turn Slack on (section 11); a fresh install can leave it empty.
- Order: `CAPSULE_SECRETS_BACKEND`, then `secrets.backend` in host.json, then autodetect.
- Verify: `bin/capsule-secret check` (exit 0 when the backend answers and, for `infisical`, the identity can read the project).
- Infisical only: creating the first project, identity and secrets is a manual step in the UI. `bin/capsule-secret --help` lists the commands. **UNVERIFIED** for a new operator.
- Never commit `.env` files. CI runs a gitleaks scan on every pull request.

**Project lists in `host.json`.** Which repos and boards the maintenance jobs walk is host config, never code. Every key is optional; the defaults name only this repo or nothing.
- `repos`: object, name to checkout path (default: this checkout as `capsule-corp`). The board bridge, the daily brief, the release gate and `deps-sync` read it when the maintainer's private registry file is absent (it is not part of the public tree, so a public install uses these keys).
- `repo_urls`: object, repo name to a plain git URL (default none; `https://host/path`, `git@host:path` or `ssh://host/path`, no credentials). Release links, first-time clones of the host's repos and `deps-sync` need it for repos that should have one. The boards a host serves come from the repos.json board fields first, then config `unblocked.boards` (`capsule host boards` prints them).
- `quality.repos`: list of repo names the nightly quality job rotates through (default `["capsule-corp"]`).
- `secrets.projects`: list of secret-store project slugs the importer creates (default `["capsule-corp"]`).
- `observability.components`: extra rows for the coverage matrix (default none).
- `config_dir`: the scheduled jobs' runtime directory: `ops.env`, `observability.env`, `infisical.env` and the `maintenance/` link the deploy step makes (default `~/.config/capsule-corp`; env `CAPSULE_JOBS_DIR` beats it). The launchd templates and prompt files carry it as the `__CONFIG__` placeholder, which the deploy step fills in. It is not the secret-store directory `CAPSULE_CONFIG_DIR` of `bin/capsule-secret`.
- `owner.copyright`: the legal name written as the copyright holder in a LICENSE that `bin/new-repo.sh` generates (default empty: `owner.name` is used, else `owner`). Same character rules as `owner.name`.
- `owner.name`: the owner's name (default `owner`; env `CAPSULE_OWNER_NAME` beats it). `capsule human` files record the author `owner` (an older install whose records use its own name keeps it, when the fixed author list allows it). It is the name the weekly report matches in decision-row text, and the name in the acts ledger note (`Capsule Corp - <name> acts ledger.md`); `waits:` and assignee values naming it count as the owner. Letters first, then letters, digits, space and `. _ ' -`; `cos` and `opus` are refused.
- `boards.default`: the board a bare `capsule tick` serves, for the live snapshot (default `capsule-corp`).
- `diligence.ledgers`: globs of decision notes that `capsule/ask_refs.py` searches (default `Decisions/*decisions*.md`). `repo`: this checkout's path (default: the clone you run from). `observability.enabled` and `tailscale.serve` are accepted by `capsule host get` but no code reads them yet.
- `maintenance.scan_repos`: repos under `~/CodeBase` the architect scan (`maintenance/run.sh`) walks (default none).
- `maintenance.mirror_repos`: repos whose `docs/` and `README.md` `docs_sync` mirrors to the vault, after capsule-corp (default none).
- Notes vault (opt-in, off by default): the vault jobs, decision-log sidecar, brainstorm notes, Korin post-mortem notes and vault pushes run only when a vault is configured, with `vault.path` or `OBSIDIAN_VAULT_PATH` (env wins). With none, each does nothing and exits 0. Tuning keys: `vault.decision_notes` (a relative glob for a top-level note in Decisions/, default `Decisions/*decisions*.md`; a glob that matches elsewhere is ignored and the default note name is used), `vault.push_allow` (folders the machinery may push, default the generic note folders such as Boards/, Decisions/, Knowledge/) and `vault.skill_root` (the vault health checker's checkout, or `OBSIDIAN_SKILL_ROOT`).
- Read from a shell with `capsule host get <key>` (lists print space-separated, `repos` prints `name<TAB>path` lines).

## 11. Optional: Slack

Set `slack.enabled` to true in the board config, create a Slack app and bot token, and store the token with `bin/capsule-secret` (whichever backend you chose). `docs/EGRESS.md` lists what is sent to Slack. **UNVERIFIED** for a new workspace.

## 12. Run the test suites

```bash
python3 -m unittest discover -s capsule/tests -t .
python3 -m unittest discover maintenance/tests
```
- Verify: both exit 0. CI (`.github/workflows/verify.yml`) runs a larger set on Linux.

## Stop and uninstall

- Stop a tick: `bin/capsule-tick.sh stop <board>`.
- Cold shutdown of everything: `bin/hoi-poi.sh down`. Cold start: `bin/hoi-poi.sh up`. Status: `bin/hoi-poi.sh status`.
- Hold all dispatch: `~/.local/bin/capsule pause --reason "<why>"` (optional `--seconds N`); lift it with `~/.local/bin/capsule unpark`.

## Do not

- Install anything the installer did not ask about without checking it.
- Edit a plist in `~/Library/LaunchAgents` by hand; rerun `~/.local/bin/capsule board tick <board>` instead (move the old file first).
- Put a secret in a file or in git.
