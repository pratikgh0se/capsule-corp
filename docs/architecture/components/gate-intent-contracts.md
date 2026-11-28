# Gate: intent contracts, schemas and SHA256SUMS

## Purpose
Every role has two independent definitions of "right" (Z1, Z2): the written contract and a file of
intent examples a human judged. The tick checks each schema-valid role document against the
examples; a disagreement is a contradiction, not a silent choice. Checksum files make the judge
files tamper-evident.

## Where it lives (files)
- `contracts/<role>.md`: role contracts (`implementer`, `reviewer`, `validator`, `guide`, `korin`, `advisor`, `auditor`, `blind-tester`, `security-reviewer`, `lookout`), plus `contracts/_style.md` (output style).
- `contracts/intent/*.jsonl`: intent examples, one per line (`id`, `given`, `expect`, `source`). Also examples for scripts and checks (for example `lander.jsonl`, `deploy.jsonl`, `mutation_check.jsonl`).
- `contracts/schemas/*.json`: boundary schemas; `contracts/shown/*.jsonl`: the document fields each role sees; `contracts/schemas/rows/`: pinned operational-row schemas.
- `capsule/intent.py` (`check`, `lint_all`, pattern language), `capsule/intent_example_schema.py`, `capsule/contracts.py` (parse, lint, render), `capsule/row_schemas.py`, `capsule/loophole.py` (`capsule loophole <file>` lists cheap loopholes before a stamp).
- `SHA256SUMS` files: `contracts/SHA256SUMS`, `contracts/intent/SHA256SUMS`, `contracts/schemas/SHA256SUMS`, `contracts/shown/SHA256SUMS`.

## Inputs and outputs
In: a role document plus the run's job inputs. Out: zero or more contradictions (each names the example id), which the tick routes to the guide for a ruling; lint errors for malformed examples.

## State it reads and writes
Read-only files. Writes happen only by a human stamp: the files are protected paths.

## Key rules and invariants
- Pattern language: a dict matches key by key (extra keys allowed); lists and scalars match by type-strict equality; `<empty>`, `<non-empty>`, `<any>`, `{"<not>": p}`, `{"<each>": p}`, `{"<some>": p}`. `$outcome` in `expect` judges the tick's own routing (`hold`, `review`, `checkpoint`).
- `lint_all` holds every field an example names to its role's schema and rejects a `given` that would match any document.
- A changed file in a directory with a `SHA256SUMS` must match its line: the lander's `protected` check (`_sums_problems`) and `capsule check` (`preflight._sums_ok`) both enforce it.
- `contracts/**` and `capsule/intent.py` are in `workspace.PROTECTED`; a worker cannot edit its own oracle. Proposed intent rows get a stamp reviewed by a human or the CoS after `capsule loophole`.
- Every contract carries a `may-not: never bypass a Zenkai Rule` line; the Zenkai brief block (`docs/briefs/zenkai-brief-block.md`) goes into every dispatch.

## Failure modes and what happens
A contradiction opens an incident of kind `contradiction` and a guide ruling card (ruling options in `bundles.RULING_OPTIONS`). A schema-valid document that breaks an example never lands silently. A checksum mismatch makes the lander red at `protected`.

## Tests that pin it
`capsule/tests/test_intent.py`, `test_intent_example_schema.py`, `test_intent_diff_base.py`, `test_intent_task11_scripts.py`, `test_contracts.py`, `test_contract_field_lint.py`, `test_schema_contracts.py`, `test_schema_amendment.py`, `test_shown_sets.py`, `test_loophole.py`, `test_loophole_false_positives.py`, `test_loophole_git_lock.py`, `test_loophole_guide_rows.py`, `test_loophole_korin.py`, `test_zenkai_mirror.py`.

## Related decisions and specs
Rows 395, 454, 455, 470, 569, 602; Zenkai Z1, Z2; Appendix A of the Capsule 2 design.

## Status
current.
