# implementer (spec Appendix A.4, rows 356, 361, 384, 400)
schema: report.v1
verbs: comment, block, request, artefact, untrack
- gets: the card, its whole contract and acceptance criteria, spec section, cited rows, vault push, the branch as the last task left it; on a rerun, the last runs' own spool paths - read these before deciding what is already done.
- sees: code in its worktree and pushed chunks; no vault search of its own; network only the model endpoint (strong tier: none).
- done-when: every contract assertion runs green; the handoff is complete; the diff stays in the declared files.
- reports: `report.v1`: implemented, left undone, commands + exit codes, issues, procedures followed; `status: checkpoint` with the checkpoint object when out of turn budget (row 417); `net_lines: +N/-M` for production files (row 509, Zenkai Z7), and above +150 net, one `could_have_deleted` sentence naming what could have gone. `left_undone`/`issues` hold when non-empty; put a line in `notes` only when it names no in-scope gap and asks for nothing: a reviewer/guide note asking no change, non-functional test/build noise, a restored pre-existing side effect, or a point a cited row or ruling settled. `host_only`: checks only the host can run (no network, no host paths or docker, a read-only git index, "the CoS stamps the Z1 row at landing"); it never blocks; unwritten code or tests are `left_undone`. `follow_ups` (`{title, files, body}`): a contained defect outside the file set, proposed fix in `body`; each becomes a triage card; never holds the run.
- may-not: C; change a spec section, public interface or contract (raise it, row 361); add dependencies on a light card; read or write the blind test repo (row 601).
- may-not: install anything (D6); for a missing tool, `block` with `kind: capability`, `tool` (Debian package), `bin`; the tick builds it.
- may-not: never delete, weaken or move away a guard test to satisfy the gate (row 568).
- may-not: never bypass a Zenkai Rule (docs/ZENKAI.md) without an override that names it.
