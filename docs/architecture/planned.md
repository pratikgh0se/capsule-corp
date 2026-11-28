# Planned: decided but not built

Each entry is a design that is decided (or in flight on a separate branch) and not part of `main`.
None of these may be described as current on a component page. Item ids (F2 to F10, R07, R13, D-numbers)
come from the 2026-10-28 overnight queue, which lives outside the repo; D-numbers there are the CoS
decision ids of that queue, not vault decision rows. When an item lands, move its text into the
relevant component page and delete it here. Re-checked against `main` at `f1f54a228` (2026-11-27); earlier at `83b6672d` (2026-11-12):
the blind path, hollow-test check, card pre-check (claim check), public edition export and the
secret-read guard block mode have landed since the first version of this page and are documented on
their component pages.

## Packs, Dragon Radar and the security pack

- **Pack loader (F6, D77, D93).** A loader for optional capability packs. No loader code on `main` (no `pack` module under `capsule/`).
- **Dragon Radar (F4, D90, D92, D94, D96).** A pack of 10 tracks that replaces the tech-radar standing job, the research intake hub and weekly scout, and the news scan part of INT-08. No code and no spec in the repo; related vault material lives outside it.
- **Security pack (F5, D93).** A pack folding in the security-engineer and pentest roles (M173, M174) and SEC-10 stage 1. The first pieces on `main` are `contracts/security-reviewer.md` with `capsule/security_review.py` ([role-security-reviewer](components/role-security-reviewer.md)), and `capsule/security.py` and `capsule/threat_model.py` (scan verdicts and the threat-model check).

## App incidents and app monitoring (F7, D79, D89, OBS-11, TEAM-27)

Partly built. App failures already become incidents (`capsule/app_events.py`, `capsule/incident_repo.py`,
`capsule/korin_checkout.py`; [incidents-korin-flow](components/incidents-korin-flow.md)). Still not
built: wider app monitoring beyond those events (OBS-11). The lookout role is contract-only
([role-lookout](components/role-lookout.md)).

## Channel plugin (F9, D88)

A channel plugin is not built; no spec in the repo. The chat plugin (F8, CLI-15) has landed as
`claude/capsule-chat/` (status line, `status` and `needs-you` commands, read-only; see
[cos-tools](components/cos-tools.md)). Notifications are outbox to Slack
([outbox-notifications](components/outbox-notifications.md)).

## Two-way Slack CoS

Built in part. The listener exists (`capsule/slack_listen.py`, D225): it acks Slack Socket Mode
envelopes and hands clicks and replies to `capsule.slack_answer`
([slack-listen-job](components/slack-listen-job.md), [outbox-notifications](components/outbox-notifications.md)).
The launchd job is install-only, so it does not run by default. Design:
docs/superpowers/specs/2026-10-29-cos-role-and-notifications-design.md (maintainer only, not in the public edition) and
docs/superpowers/specs/2026-09-10-communication-and-rhythm-design.md.

## Unwired redesign switches

`redesign.night_runner`, `memory_rules`, `advisor_cards_only` and `allocator` are accepted by
`capsule/config.py` and have no consumer in the tick (`allocator` is only named in a comment in `capsule/replay.py`) (see [tick-scheduler](components/tick-scheduler.md)).
`rhythm` is a read-only calendar that nothing in the tick consults yet
([product-board-landing](components/product-board-landing.md)).

## Scheduling the auditor and the advisor

The advisor packet, check and draft pipeline and the auditor packet builder exist
(`maintenance/process_advisor.py`, `capsule/auditor.py`), but no job runs either model weekly and
nothing files the auditor's proposals ([role-advisor](components/role-advisor.md),
[role-auditor](components/role-auditor.md)).

## Hollow-test mutation per language

The warn-only hollow-test detector is live (`capsule/hollow_tests.py`, D85). Real per-language
mutation for Swift and web tests, as `tests/mutation_check.py` does for Python, is not built
([gate-mutation-check](components/gate-mutation-check.md)).

## Automated image builder loop

`image render` and `image build` are operator commands ([image-record](components/image-record.md)).
The scripted build of a worker's image request already runs from a tick step (`capsule/image_requests.py`, called from `capsule/tick.py`). Only an agent that proposes images by itself is not built.

## Shell scripts to Python (D97)

Decision D97: new scripts are Python, not shell, and the existing non-test shell scripts are to be
ported before the public release. Several have been ported (for example `maintenance/deps_sync.py`
beside `deps_sync.sh`, `landing_prep.py`, `branch_sweep.py`, `storage_gc.py`, `disk_relief.py`);
deploy.sh (maintainer only, not in the public edition), bootstrap.sh (maintainer only, not in the public edition) and `tailnet_serve.sh` remain
private shell. Count the remaining ones with `git ls-files '*.sh'`; no spec in the repo.
