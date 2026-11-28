# Renovate: add `.github/renovate.json` to <repo>

Capsule decision **row 139 / M260**: Renovate on all three repos, weekly, grouped
PRs, patch automerged on green CI; minor, major, digest and lockfile updates are
PRs (D239).
capsule-corp has its own `.github/renovate.json` in the repo; the other two get
this card, because their config is a file in **their** repo and capsule-corp never
writes a sibling checkout.

## The intake line

Paste one of these under `## New work` in `Boards/Intake.md`:

```
- repo: my-app | risk: low | add .github/renovate.json (Capsule row 139 / M260)
- repo: other-app | risk: low | add .github/renovate.json (Capsule row 139 / M260)
```

## The config

Same policy as capsule-corp with **two deliberate differences**: no digest pinning
(the product repos keep readable action tags like `gitleaks/gitleaks-action@v2`;
only capsule-corp pins SHAs), and
each repo's own package manager enabled.

**my-app** (Python):

```json
{
  "$schema": "https://docs.renovatebot.com/renovate-schema.json",
  "extends": ["config:recommended"],
  "timezone": "Asia/Kolkata",
  "schedule": ["* 2-6 * * 1"],
  "prConcurrentLimit": 2,
  "prHourlyLimit": 1,
  "dependencyDashboard": true,
  "enabledManagers": ["github-actions", "pip_requirements"],
  "platformAutomerge": false,
  "packageRules": [
    {"matchUpdateTypes": ["patch"], "groupName": "weekly patch",
     "automerge": true, "automergeType": "pr", "automergeStrategy": "squash"},
    {"matchUpdateTypes": ["minor"], "groupName": "weekly minor", "automerge": false,
     "labels": ["renovate-minor"]},
    {"matchUpdateTypes": ["digest", "pin", "pinDigest", "lockFileMaintenance"],
     "automerge": false},
    {"matchUpdateTypes": ["major"], "automerge": false, "labels": ["renovate-major"]}
  ],
  "vulnerabilityAlerts": {"enabled": true, "automerge": false}
}
```

**other-app** (Node): identical, with
`"enabledManagers": ["github-actions", "npm"]`, and add
`"rangeStrategy": "bump"` so `package.json` and the lockfile move together.

## Why these four settings

- **`schedule: ["* 2-6 * * 1"]`** - one weekly batch, Monday 02:00-06:00 local,
  so a PR is waiting when the week starts and nothing arrives mid-window.
- **`platformAutomerge: false`** - GitHub's auto-merge needs branch protection,
  which was refused at HTTP 403 (row 123). With it false, Renovate merges the
  branch itself once its own status checks pass, which is what "on green CI"
  means here.
- **minor, digest and lockfile `automerge: false`** - only patch merges itself (D239).
- **majors `automerge: false` + a label** - a major arrives as a PR and goes
  through the ladder like any other change.
- **`enabledManagers` named explicitly** - so no manager is enabled by
  accident. In capsule-corp the `dockerfile` manager is off on purpose:
  the worker images' base is bumped through
  `maintenance/templates/image-rebuild-card.md`, which requires a pasted
  registry-HEAD evidence line, and a bot cannot produce that.

## Checklist

- [ ] `.github/renovate.json` added, valid JSON (`python3 -c 'import json;
      json.load(open(".github/renovate.json"))'`)
- [ ] Renovate enabled for the repo in the GitHub app UI (a human action; no
      token in this repo can do it)
- [ ] the repo's existing `verify.yml` still passes
- [ ] the repo's `verify.yml` shows the pinned `gitleaks` CLI step
