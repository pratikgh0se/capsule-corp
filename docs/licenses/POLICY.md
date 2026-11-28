# Licence policy (D240, 2026-11-13, master-list row M268)

For the public release, enforced by `maintenance/notice_gen.py` (`classify`, `--deep-review`):

| Licence family | In what ships | In dev-only tools |
|---|---|---|
| GPL-*, AGPL-* (all versions, `-only`, `-or-later`, `+`) | denied | allowed |
| LGPL-*, MPL-* | allowed | allowed |
| permissive (MIT, BSD, Apache-2.0, ISC, ...) | allowed | allowed |
| unknown or empty name | denied | allowed |
| anything unclassified (EPL, CDDL, ...) | review or denied (unchanged) | allowed |

Shipped vs dev-only: `notice_gen.py --deep-review` treats a finding whose path matches a glob in
`docs/licenses/dev-only.txt` as dev-only and skips it. Every other path counts as shipped. Add a
glob only for a dev tool (test runners, linters, Playwright) that is not part of Capsule's shipped
code. Such tools may appear in the worker images as separate programs (aggregate distribution);
D240's rule is about what Capsule's own code links or vendors. Licence ids are matched whole: an
expression ("LGPL-2.1 AND GPL-2.0") or a malformed id is unclassified, hence denied.
The inventory scan (`--scan`) has no paths, so it applies the family rules only; `hypothesis`
(MPL-2.0) is therefore plain "allowed" there.
