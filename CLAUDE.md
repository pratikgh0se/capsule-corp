# capsule-corp

## Zenkai Rules

The Zenkai Rules (reliability doctrine, decision rows 454 and 455) live in `docs/ZENKAI.md`, a
checksummed verbatim mirror of the vault note `Knowledge/Capsule Corp - Zenkai Rules.md`
(`capsule/tests/test_zenkai_mirror.py` fails on drift). Cite them by item; never copy them.

- Every agent in this repo (controller, implementer, reviewer, validator, guide): Z3 precedence,
  Z4 overrides, Z6 autonomy by reversibility.
- Anyone adding or changing a script, check, contract or schema: Z1 intent examples, Z2 two oracles, Z8 simplicity.
- The controller and the tick: Z5 detectors (canary slice after every merge, t_c9cf359b).
- Every dispatch carries `docs/briefs/zenkai-brief-block.md`; every role contract carries the
  Zenkai may-not line.
- `docs/ZENKAI.md` is a protected path (`capsule/workspace.py` PROTECTED): workers may not write it.

## Korin (incidents)

Every crash, hang, review-infra failure, contradiction, human intervention, override and tick error
opens an incident (`capsule/incidents.py`, row 456); Korin writes the post-mortem, the tick files a
blocked fix card, and the incident closes only through the gate. A repeat (same fingerprint within
30 days of a close) is the headline number. [docs/architecture/components/incidents-korin-flow.md](docs/architecture/components/incidents-korin-flow.md).
