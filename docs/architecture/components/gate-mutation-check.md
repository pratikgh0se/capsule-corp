# Gate: mutation (test-honesty) check

## Purpose
A new or changed test that can never fail proves nothing. The gate makes each changed test file
show that it discriminates.

## Where it lives (files)
- `tests/mutation_check.py` (main script), helpers `tests/mutation_alias.py` (one-hop alias on revert), `mutation_contractdata.py` (contracts and schemas read as data), `mutation_env.py` (checks that the test interpreter can import the project's packages), `mutation_info.py` (two heuristics that report as info), `mutation_literals.py` (string masking, frozen-file gaps), `mutation_newsymbol.py` (pure-addition symbols).
- Called by the lander (`lander.GATES`, config `lander.gates`) and locally by `bin/precheck-gates.py` (working tree in a throwaway shared clone, runs mutation_check and the simplicity check).
- Intent and tests: `contracts/intent/mutation_check.jsonl`, `mutation_alias.jsonl`; the tests are in `tests/`: `test_mutation_alias.py`, `test_mutation_check.py`, `test_mutation_check_remedy.py`, `test_mutation_check_retry.py`, `test_mutation_contractdata.py`, `test_mutation_info.py`, `test_mutation_literals.py`, `test_mutation_marker_ancestor.py`, `test_mutation_marker_wrap.py`, `test_mutation_newsymbol.py`.

## Inputs and outputs
In: `--base <sha> --head <sha> [--repo .]`. Out: a markdown table; exit 0 when every changed test file discriminates, no static flag fired and the size and simplicity checks pass (`mutation_check.main` also runs `size_check.check` and `simplicity_check.check`), else 1; exit 75 on an environment error (the lander retries it, `lander.ENV_RC`).

## State it reads and writes
Scratch worktrees only; no board access.

## Key rules and invariants
- (b) Revert mutation: with the range's non-test `.py` files (and changed `contracts/intent/<role>.jsonl`) reverted to base, every changed test file must show at least one failure.
- (c) Assertion weakening: flipping the first `assertEqual` to `assertNotEqual` (or `assertTrue` to `assertFalse`, `==` to `!=`) in an unmodified head copy must make the file's changed tests fail.
- (d) Static flags on changed test functions: no assert, `@skip*` or `@expectedFailure`, a bare `except: pass`, `assertTrue(True)`.
- Opt-out for a test whose subject predates the range: a per-test `# mutation-check: targets-predate-range <sha> -- <reason> :: Class.test` marker; a marker with an id not in the changed set, or one not added in this range, is `marker_stale`; a marker whose sha is neither the base nor its ancestor is `marker_mismatch`. Both fail.
- Per-call unittest timeout `max(180, 10 * n_tests)` seconds.

## Failure modes and what happens
A red gate makes the lander check fail (`gates`), so the card goes back through the failure path. A test file whose revert phase cannot run is reported, not silently passed.

## Tests that pin it
`tests/test_mutation_check.py`, `test_mutation_check_retry.py`, `test_mutation_alias.py`, `test_mutation_contractdata.py`, `test_mutation_info.py`, `test_mutation_literals.py`, `test_mutation_newsymbol.py`, `test_precheck_gates.py`.

## Related decisions and specs
Row 459 (card t_a995978e), 526, 568; Zenkai Z1, Z2 (its own second oracle is the fixture-repo tests).

## Status
current. The warn-only hollow-test check (`capsule/hollow_tests.py`, D85) covers Swift and web tests the mutation check cannot; per-language mutation is still planned: [../planned.md](../planned.md). `maintenance/blind_mutation.py` is a separate weekly report of how many mutations a board's blind tests catch (install-only job, see [blind-path](blind-path.md)).
