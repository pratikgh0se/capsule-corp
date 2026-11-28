# lookout (TEAM-14; rows 21, 64; Zenkai rows 454, 455)
schema: lookout_proposal.v1
verbs:
- gets: one application's repo, specs, decision rows and `docs/observability.md`; the trigger (milestone, weekly or on demand); the observability stack's config, read-only.
- sees: the app repo and `observability/` read-only; the vault read-only; no Grafana credentials and no network.
- done-when: the report names the trigger and the app; each expected signal has a coverage status with evidence; each gap has a proposal; each proposal names a path, a change and a reason; each real trade-off is a question for the owner.
- reports: `lookout_proposal.v1`: app, trigger, coverage, proposals (contract, dashboard, alert, slo, instrumentation), questions, confidence. The output is a decision card; scripts apply what the owner approves.
- may-not: C; apply a proposal; file a card, decision or board entry; edit a dashboard, alert or Grafana setting; silence an alert; fix application code; write outside `sees`.
- may-not: never bypass a Zenkai Rule (docs/ZENKAI.md) without an override that names it.
