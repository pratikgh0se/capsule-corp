# Product principles

What Capsule Corp is for, where its edges are, and how it talks to its maintainer. Every rule below
cites the decision it comes from. "Row N" means a row in the vault note `Decisions/Capsule Corp -
System design decisions 2026-09-06`. Where a source is silent or two sources differ, this page says
"open:" and lists it under Open questions. It invents no rules.

## North star

Stated by the owner on 2026-10-18 and refined on 2026-10-20 after market research (memory note
`capsule-north-star`; focus rule row 603, 2026-10-18).

1. **Lightweight and solo-maintainer.** One chief of staff he talks to. Agents keep working when they
   do not need him; he unblocks them in short bursts. He is the bottleneck and the system exists to
   remove that. (2026-10-18)
2. **Resource-friendly.** It runs on small machines. Cheap models do well-scoped work from specs
   written by strong models. (2026-10-18; model routing row 381, 2026-09-24)
3. **Reliable first, features second.** Reliable, resource-optimised, works as designed. Fewest lines
   that do the job. Rework is a burden. (rows 341, 347, 351, 352; 2.0 design spec section 1)
4. **Commodity out.** Use what Claude Code and others already ship: scheduling, Remote Control, Slack,
   parallel agents. Build only the differentiating core: the step-ins-per-card metric, the safe
   auto-merge trust gate (hidden tests plus senior review), the alignment and simplicity reviewer,
   cheap-model routing, and one chief of staff. (2026-10-20)
5. **Model-robust.** Keep machinery around the models thin so a new model does not break it. It must
   still work, more slowly, with weak models such as Haiku doing dev work. (2026-10-20)
6. **Provider-agnostic, later.** Each role picks its runner, model and key. This waits until after M5
   (the off-legacy-runtime deployment). Prefer an existing multi-provider agent CLI over a home-built agent loop.
   (2026-10-20; 2.0 design spec header item 1, "provider-agnostic model layer")
7. **Scope discipline.** New papers and ideas are judged on merit, not chased. The chief of staff
   parks anything that does not directly serve the goal in the vault "Not now" list and says so.
   Test each proposal against items 1 to 3 first; prefer cutting weight over adding. (2026-10-20)
8. **Focus until M5.** Until the off-legacy-runtime deployment is done, tokens go to building Capsule itself.
   Product work such as a small timer app is allowed only as a deliberately named test load.
   (row 603, 2026-10-18; a timer app named as a test project 2026-10-20)

## Product boundary

- **Whole company, and its product.** Capsule Corp is the whole company: the Capsule tick factory (old runtime retired 2026-10-23), the second
  brain, observability, every agent and automation. Its product is the applications it ships for
  the owner, the first being a reliable timer app used daily. Build with enterprise discipline.
  (row 39, decision D1, 2026-09-06)
- **Own factory first.** The factory is the owner's own, built so a second operator could run it: no
  personal path, name or secret hardcoded, and a fresh machine runs it from the runbook.
  (row 389, 2026-09-24)
- **No product until one product shipped.** No work on Capsule as a product for others until Capsule
  has shipped one product end to end; the question is revisited then. (row 389, 2026-09-24; 2.0
  design spec section 10 lists it as "Not now" with that trigger)
- **Hygiene rule.** No new hardcoded absolute paths and no new "only the owner" assumptions. (row 39)
- **Not fully autonomous.** In two months the owner must be able to read the codebase and find no slop
  that causes production bugs. (row 347, 2026-09-16)
- **Open source, as a preview.** The repo is published as a pre-alpha preview (v0.2.0-alpha.1) with a README and licence. A UI that
  shows the agents comes much later. (memory note capsule-north-star item 5, 2026-10-18)

## Single pane

The rule (row 73, decision D11, 2026-09-06; study D11 at
docs/superpowers/specs/2026-09-07-capsule-corp-system-design-study.md lines 856-883, maintainer tree only; not in the public edition):

- If it needs the owner now, it is a **message** (Slack; phone-answerable).
- If it is state he might inspect, it is a **dashboard** (Grafana, row 73; since 2026-10-21 `capsule status` / `capsule board`).
- If it is a record, it is the **vault**.

One place per kind of thing. A message that carries state he keeps asking for is the signal to
revisit a dedicated Capsule web UI, reassessed after a month on Slack (row 73). The visibility spec
docs/superpowers/specs/2026-10-21-visibility-status-board-slack-design.md (maintainer tree only) section 2 (A, C, D, approved
2026-10-21) makes `capsule status` plus the `capsule board` terminal UI the place to inspect state;
Slack is for needs-you, broken things and the 09:00 summary (Slack is live, rows 452, 621). The board
terminal UI is the step taken now instead of a web UI. Do not build a web UI
before then: Slack's buttons, channels and threads already cover it (study D11 recommendation 4).

## Release-ready duty

The owner, 2026-10-20 (memory note `release-ready-as-we-go`): keep the repo release-ready continuously
so the public pre-alpha preview stays easy to keep current. He does not want big changes now.

- Keep `README.md` current; each milestone leaves it accurate. Keep STATUS.md and `docs/` current
  and easy to add to.
- Keep clearing out what is not needed. Prefer cutting over adding.
- Avoid personal paths and secrets in new code and docs (matches the row 39 hygiene rule).
- It is standing low-priority work, done in idle seats only. It never jumps ahead of getting the
  factory and harness working.

## Open questions

- open: row 39 says "not for sale now"; row 389 says the product question reopens after the first
  shipped product. Which product counts as "shipped end to end": the first timer app?
- open: what is the finished-state check for release-ready (a checklist, or only the standing duty)?
  `docs/RELEASE.md` exists; its relation to this duty is not stated in any source.
