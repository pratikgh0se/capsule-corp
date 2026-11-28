# Extending Capsule

How to add four kinds of thing: a role, a gate, an event kind and a config key. For each, this page
lists the files to touch in order and the tests that must change. All names were checked against
the code at commit `3acc797a7` (2026-11-27). Read [overview.md](overview.md) and
[glossary.md](glossary.md) first.

## TL;DR

| You add | Start in | Most likely test to change |
|---|---|---|
| A role | `contracts/<role>.md` | `capsule/tests/test_contracts.py` |
| A gate | `capsule/lander.py` (`GATES`) and a script in `tests/` | `capsule/tests/test_lander.py`, `capsule/tests/test_lander_checks_protected.py` |
| An event kind | `capsule/event_kinds.py` (`ALLOWED`) | `capsule/tests/test_event_kinds.py` |
| A config key | `capsule/config.py` (`validate`) | `capsule/tests/test_redesign_switches.py`, `capsule/tests/test_config_autonomy.py` |

Run the whole suite before you propose a change. The commands are in
[CONTRIBUTING.md](../../CONTRIBUTING.md).

## Rules every change must meet

These are the Zenkai rules ([ZENKAI.md](../ZENKAI.md)). Cite them by item and never copy them.

| Rule | What it asks of your change |
|---|---|
| Z1: intent is a separate artefact | A new script or contract ships with intent examples: real inputs, each with the outcome a person calls right, each with a named `source`. They live in `contracts/intent/<name>.jsonl`. |
| Z2: never one oracle | A new check must observe each fact through two oracles that share no code path and no data source. The check names both and its result records both. |
| Z8: simple is complex enough | No new layer, class, function or config option without two real callers or a done-when line that needs it. Prefer the standard library. The merge gate runs `tests/simplicity_check.py` and `tests/size_check.py`. |

Also relevant: Z3 (a contradiction between sources is resolved by precedence: intent rows, then
spec, then contract, then script, then code), Z4 (a bypass needs an override that expires) and
Z7 (smallest change; report `net_lines`).

**Protected paths.** A worker cannot edit `contracts/**`, `capsule/intent.py`, the lander, the
gate scripts and other judge files (`PROTECTED` in `capsule/workspace.py`). Changes to those paths
reach `main` only through a human or chief-of-staff stamp, with the pinned hashes updated in the
same change. The lander turns a hit on a protected path into a hold.

## (a) Add a role

A role is a kind of run: a contract, a schema for its report, a model route, a runner and a prompt
bundle. The optional security reviewer is the smallest worked example: see
`capsule/security_review.py` and `capsule/tests/test_security_review.py`.

Files to touch, in order:

1. **Contract.** Write `contracts/<role>.md`. The header line is `schema: <name>.v1`, then the five
   fields `gets`, `sees`, `done-when`, `reports`, `may-not`. The `may-not` line starts with `C;`
   (this expands to the shared rule line, `contracts.LINE_C`) and a second `may-not` line carries
   the Zenkai line: `never bypass a Zenkai Rule (docs/ZENKAI.md) without an override that names it.`
   Keep it under 2048 bytes and under 900 words (`contracts.MAX_BYTES`, `WORD_CAP`). If the file
   stem differs from the role name, add the case to `contracts.stem` (as `security` maps to
   `security-reviewer`). Copy the layout of `contracts/korin.md`.
2. **Schema.** If the role needs a new report shape, add `contracts/schemas/<name>.v1.json` and a
   line in `contracts/schemas/SHA256SUMS`. A schema change always means a new version file.
3. **Intent examples (Z1).** Add `contracts/intent/<role>.jsonl`, one JSON example per line with
   `id`, `given`, `expect` and `source`. Add its hash to `contracts/intent/SHA256SUMS`. Run
   `python3 -m capsule loophole contracts/intent/<role>.jsonl` to list cheap loopholes before the
   stamp.
4. **Routing.** Add the role name to `OPTIONAL_ROUTING_ROLES` (an optional role) or
   `ROUTING_ROLES` (a required one) in `capsule/config.py`. `_validate_routing` then checks the
   route fields `provider`, `account`, `model` and `turn_budget`. Add an entry under `routing` in
   `capsule/config.example.json` and `capsule/config.capsule-corp.example.json`. A required role
   makes every existing board config invalid, so prefer an optional role with a switch under
   `redesign` (see (d)).
5. **Runner.** Edit `runner_for` in `capsule/runners/__init__.py`. A read-only role goes in the
   tuple `("reviewer", "validator", "guide", "korin", "security")` and gets `host.HostRunner`;
   a role that writes code gets `docker.StrongRunner` by default. Set the role's tools in
   `limits` in `capsule/runners/host.py`.
6. **Bundle.** Add a function to `capsule/bundles.py` (or `capsule/bundles_guide.py`) that builds the
   role's prompt from the contract (`contracts.render`). Call it from `_bundle` in
   `capsule/worker.py`.
7. **Dispatch.** A read-only role must be listed so the tick gives it no lease and counts no failure:
   `READONLY_ROLES` in `capsule/tick.py` (built from `REVIEW_ROLES` in `capsule/tick_review.py`) and,
   for a review seat, `REVIEW_PROFILES` in `capsule/seats.py`. Add the step that starts the role to
   the tick, in `capsule/tick_review.py` or its own module.
8. **Docs.** Add `docs/architecture/components/role-<role>.md` and a row in
   `docs/architecture/inventory.md`.

Tests that must change:

| Test file | What to do |
|---|---|
| `capsule/tests/test_contracts.py` | Add the role to `ROLES`. These tests then check it: `test_every_role_contract_is_within_the_word_budget`, `test_five_fields_non_empty_under_cap_with_line_c`, `test_every_contract_carries_the_zenkai_line_after_its_own`, `test_every_contract_names_an_existing_schema` (its expected schema set grows by your schema). `ReportChannel.test_every_bundle_names_the_structured_output_as_the_report` covers the bundle. |
| `capsule/tests/test_schema_contracts.py` | `test_pinned_hashes` fails until the schema's line is in `SHA256SUMS`. |
| `capsule/tests/test_intent.py` | `test_pinned_hashes_cover_every_intent_file` and `test_every_shipped_role_file_lints_clean_and_is_non_empty` cover the examples. |
| `capsule/tests/test_runners.py` | Add a case for `runner_for` with your role (see `test_read_only_roles_get_no_shell`). |
| `capsule/tests/test_bundles.py`, `capsule/tests/test_worker.py` | Add a bundle test and a `run_one` test with `StubAdapter`. |
| `capsule/tests/test_redesign_switches.py` | `test_examples_ship_every_switch_off` fails if an example config ships a new switch on. |

## (b) Add a gate

A gate is a script the lander runs on the exact commit it is about to push. A red gate refuses the
landing.

Files to touch, in order:

1. **Script.** Add `tests/<name>.py` that takes `--base {base} --head {head}` and exits 0 when the
   change is fine. The lander runs one shape only, `python3 tests/<name>.py ...`
   (`lander.GATE` is the pattern). A script that only reads text may run on the host, which means
   it is also listed in `HOST_GATES`. Any other gate script runs in the sandbox container, because it
   may run the candidate's own code. An environment failure exits with `ENV_RC` (75) so the lander
   waits instead of failing the card.
2. **Intent examples (Z1) and two oracles (Z2).** Add `contracts/intent/<name>.jsonl` and make the
   script observe its fact through two independent sources (for example a diff and a test run).
   Ship the examples with `source` fields; update `contracts/intent/SHA256SUMS`. The existing
   gate `check_gate` and `lander_*` files in `contracts/intent/` are models.
3. **Register it.** Add the command string to `GATES` in `capsule/lander.py`, and the script path to
   `HOST_GATES` if it is text-only. A board may override the list with `lander.gates` in its config
   (`lc.get("gates", GATES)` in `lander.run`).
4. **Protect it (PROTECTED).** Add the script and every local file it imports to `PROTECTED` in
   `capsule/workspace.py`. A worker must not be able to rewrite its own judge. The existing entries
   for `tests/mutation_check.py`, `tests/size_check.py` and `tests/simplicity_check.py` are the
   pattern.
5. **Baseline.** If the gate has a baseline file (the size gate has `tests/size_baseline.json`),
   protect it too, and keep it so it can only shrink.
6. **Docs.** Add `docs/architecture/components/gate-<name>.md`; list it in `inventory.md`.

Tests that must change:

| Test file | What to do |
|---|---|
| `tests/` gate tests, for example `tests/test_size_check.py` and `tests/test_simplicity_check.py` | Add `capsule/tests/test_<name>.py` for the script, with a passing and a failing case for each oracle. |
| `capsule/tests/test_lander_checks_protected.py` | `test_every_configured_gate_script_is_protected` and `test_every_local_import_of_every_gate_script_is_protected` fail until step 4 is done. |
| `capsule/tests/test_lander.py` | Add a test next to `test_a_failing_gate_is_red_and_the_suites_still_do_not_run` and `test_gates_get_the_base_and_head_shas`. |
| `capsule/tests/test_intent.py` | `test_pinned_hashes_cover_every_intent_file` for the new examples file. |

## (c) Add an event kind

An event kind is a value of `task_events.kind`. A per-kind allowlist keeps stray fields out of the
event stream.

Files to touch, in order:

1. **Allowlist.** Add the kind and its payload keys to `ALLOWED` in `capsule/event_kinds.py`.
   Reuse the shared tuples `_BLOCK`, `_INTAKE` and `_DECISION` where they fit. `db.event` adds
   `run_id` to a payload itself when you pass it.
2. **Producer.** Write the event with `db.event(conn, task_id, "<kind>", {...})` from the code that
   makes the change. An allowlisted kind with no producer fails the tests.
3. **Hold events.** If the event starts or ends a hold on a card, also name it in the hold lists in
   `capsule/blocks.py` (`HOLD_EVENTS`). A test scans the writers for this.
4. **Consumers.** Search for code that switches on event kinds (for example the incident step and
   the Lookout) with `grep -rn '"<kind>"' capsule/`.

An unknown kind or key is not fatal at run time: `db.event` prints a warning on stderr and still
writes the row. In CI the warning is a hard failure through the tests below.

Tests that must change:

| Test | What it checks |
|---|---|
| `capsule/tests/test_event_kinds.py` `CallSites.test_every_literal_call_site_is_inside_the_allowlist` | Every literal `db.event` call uses an allowed kind and keys. |
| same file, `CallSites.test_every_allowlisted_kind_has_a_producer` | Your new kind has a real call site. |
| same file, `Allowlist` tests | Add a case for your kind if its payload is special. |
| `capsule/tests/test_blocks.py` | Only if the kind is a hold event. |

## (d) Add a config key

Board config is `boards/<board>/config.json`, loaded and checked by `capsule/config.py`. The tick
re-reads it every pass; a bad reload keeps the last good config. `validate` does not reject unknown
top-level keys, so a typo is silently ignored: that is why every key needs its own check.

Files to touch, in order:

1. **Check and default.** Add the key's validation to `capsule/config.py`. Follow the pattern:
   a `_validate_<block>(block)` function that raises `ValueError("<block>.<key>: <what is wrong>")`,
   called from `validate` (or from `_validate_quiet_lookout`, which groups the optional blocks); a
   `DEFAULTS` constant; and a small accessor that returns the value or its default. An on/off
   feature switch goes in `REDESIGN_FLAGS` (booleans) or `REDESIGN_MODES` (`off`, `report`, ...) so
   `_validate_redesign` already checks it. A new cap goes in `CAP_DEFAULTS`.
2. **Use it.** Read it through the accessor at the one place that needs it. Do not read the raw
   dict in many places (Z8: two real callers or a done-when line).
3. **Examples.** Add the key to `capsule/config.example.json` and
   `capsule/config.capsule-corp.example.json`, with the safe default (a switch ships off).
4. **Docs.** Say what the key does in the component page that owns it, and in `docs/SETUP.md` if an
   operator must set it.

Tests that must change:

| Test file | What to do |
|---|---|
| `capsule/tests/test_redesign_switches.py` | For a `redesign` switch: add to the checks next to `test_unknown_switch_refused` and `test_bad_mode_refused`; `test_examples_ship_every_switch_off` covers the example files. |
| `capsule/tests/test_config_autonomy.py` | The model for a new block: `test_defaults_are_off`, `test_validation`, `test_result_is_a_copy_not_the_module_defaults`. Add a test file for a new block in the same shape. |
| the tests of the code that reads it | One test for the key on and one for off. |

## Checklist before you open the change

- [ ] Intent examples with named sources (Z1), in `contracts/intent/`, hash updated.
- [ ] Two independent oracles for every new check (Z2).
- [ ] No new layer or option without two callers or a done-when line (Z8); report `net_lines` (Z7).
- [ ] New judge files are in `PROTECTED`.
- [ ] The component page and `inventory.md` are updated in the same change.
- [ ] `python3 -m unittest` for the files above passes, then the full suite.
