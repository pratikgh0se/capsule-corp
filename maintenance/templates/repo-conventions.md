# Repo conventions: versioning, changelog, tests, licence, release

Sibling of `image-rebuild-card.md`: paste this into a new repo so its release
path inherits row 138's rulings instead of re-deciding them (M233, row 138).

## Versioning

**0.x until an API is promised.** `0.y.z` means "anything may change", which
is exactly true of agent-written code with one user, and no agent decides on
its own that shipping means `1.0.0`. Versions are **independent** per repo
(row 113, extended from libraries to apps): no release train, nothing to
synchronise. Tags are `v<major>.<minor>.<patch>[-pre]`, and a released
version's content is immutable.

## CHANGELOG

`CHANGELOG.md` starts `# Changelog`, then `## Unreleased`. Every non-docs,
non-infra card adds one `- <what> (<task id>)` line, enforced by
`quality_gate.changelog_line_ok`. A release promotes the section under
`## <tag>` and **re-inserts an empty `## Unreleased`** above it - skip the
re-insert and the next card's quality gate fails with `changelog: FAILED`.

## Tests

Declare `OPS_TEST_CMD_<repo>` in `ops.env` if the suite is not
`python3 -m unittest discover tests` (`merge_gate.test_cmd_for`).

## Licence

An owner decision, never a template default. A private repo with no LICENSE
grants recipients nothing, and that is deliberate until he decides
(`docs/RELEASE.md`).

## Release

The docs/RUNBOOK.md `## Releases` ladder, unchanged per repo.
