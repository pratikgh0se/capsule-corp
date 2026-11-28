# Release: the supply-chain free column, and its deferrals

Row 138 accepted M232's free column and ordered the deferral written down, so
the same questions are not re-litigated monthly. Three columns: taken now,
conditional on an owner decision, and deferred until a consumer asks.

## Taken now ($0, config not engineering)

- The SBOM one-liner: `syft` host-side, or
  `gh api /repos/{owner}/{repo}/dependency-graph/sbom` when `syft` is not on
  PATH - SPDX, works on private repos, zero install.
- CHANGELOG-as-release-notes - already enforced per card, and every line is
  traceable to `Ledger/<repo>/<task_id>.jsonl`.
- `SHA256SUMS` beside every artefact: `.github/workflows/release.yml` (M232,
  D222) builds the public portable edition tarball on a `v*` tag or by hand,
  writes `SHA256SUMS`, and uploads both as workflow artifacts. It creates no
  GitHub Release; publishing stays a human step.

## Conditional on M228 (public/private, per repo - the owner's decision)

- GitHub Artifact Attestations - `actions/attest-build-provenance` (already in
  `release.yml`, guarded by `!github.event.repository.private`, so it runs only
  once the repo is public), in-toto
  SLSA v1.0 provenance signed with short-lived Sigstore certificates, verified
  by `gh attestation verify` - **free on public repos, Enterprise Cloud on
  private** (2026-09-09), the single sharpest public-vs-private cost.
- **npm provenance requires a public repository** (2026-09-09) - a
  precondition, not a cost, so `my-app` cannot publish with provenance
  while private.
- macOS runner minutes at **$0.062/min against Linux's $0.006** (~10.3x,
  2026-09-09), i.e. about **200 macOS minutes a month** on GitHub Free's
  2,000, and **free on public repos** - a 2-minute release build is ~$0.12;
  unlimited Linux minutes on public repos.
- **And before any flip: a LICENSE.** No repo has one
  (`gh repo view --json licenseInfo` -> `null`, all four, 2026-09-09), so a
  public repo today would grant recipients no licence at all, and
  `my-app`'s missing `license` field makes every downstream scanner
  report `UNKNOWN`. Publishing before deciding a licence is the one ordering
  mistake available here. Update: capsule-corp now has an MIT `LICENSE`; the
  other repos named above were not rechecked.

## Verify a download

Checksums (any download from a release run):

- macOS: `shasum -a 256 -c SHA256SUMS`
- Linux: `sha256sum -c SHA256SUMS`

Provenance (works once the repo is public, M228):
`gh attestation verify <file> --repo <owner>/<repo>`

## Deferred until a consumer asks, each with its trigger

- `anchore/sbom-action` (Syft as a maintained CI scan) - **Trigger:** a
  consumer asks for CycloneDX or for a scan of the built artefact rather than
  the manifest.
- cosign / Sigstore keyless - **Trigger:** Capsule ships a container image
  someone else consumes; row 40 already parked image signing until there is a
  customer, and npm/PyPI packages have native attestations instead.
- SLSA L3 (`slsa-framework/slsa-github-generator`'s hermetic reusable
  workflows) - **Trigger:** a consumer requires L3; a hosted build plus one
  attestation action already reaches roughly L2.
- Homebrew Cask - **Trigger:** notarisation is solid; a cask must pass
  Homebrew's Gatekeeper checks, so an unnotarised app fails that bar.
- The Mac App Store - **Trigger:** none; it requires App Sandbox, which fights
  the first product's menu-bar app spawning `server.py` on loopback, so a Mac App Store build and
  a Developer ID build would be separate configurations from day one.
- `security.txt` and a vulnerability-disclosure policy (M236) - **Trigger:**
  deferred with M228; nothing is on any market, so nothing is due.
- Feature flags and canaries - **not now**, in writing (row 138/M240):
  Capsule's config-in-a-file-plus-gate flags are better governed, and at one
  instance blue/green and canary are definitionally inert.

## Release checklist (M259, row 139)

- [ ] **No card longer than a week.** Anything bigger is an epic, not a card
  (the free rule from Amp).
- [ ] **Defer support.** The support inbox, a refund policy and the
  single-maintainer disclosure are deferred explicitly to POC-5; do not build
  them before that.
- [ ] **Am I placing this on the EU market?** If yes, stop and look at the CRA,
  the GPSR and the DPO threshold first (these are open questions for any EU release; this repo is a pre-alpha preview and does not decide them).

## Not reproposed

This file exists to prevent three things from being re-proposed:

- `changesets/action`'s bot-authored Release PR - needs a PAT or App token, a
  long-lived secret outside Infisical (row 66).
- A self-hosted Actions runner - row 32, row 108, and npm trusted publishing
  does not support one.
- `semantic-release` - removes the third stamp by design.
