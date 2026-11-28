# Gate: simplicity (Z8) and size (Z7)

## Purpose
Keep the change small and plain. Z7 is "less code"; Z8 is "simple is complex enough". Both are
measured on the lines a branch adds or changes, by an AST pass with ruff as a second oracle (Z2).

## Where it lives (files)
- `tests/simplicity_check.py` (Z8 measured rules), `tests/size_check.py` (Z7 size oracle), `tests/size_baseline.py` and `tests/size_baseline.json` (baseline plumbing and pinned oversize entries).
- Run by the lander as gates (`lander.GATES`), and by `bin/precheck-gates.py` and `tests/mutation_check.py` (its pass calls both checks). Adjacent lander gates in `capsule/config.capsule-corp.example.json`: `tests/ruff_gate.py` (same `ruff check` selection as the CI `verify.yml`, plus a stricter tier that blocks only on lines the range adds or changes), `tests/portability_lint.py` (macOS-only constructs in shell lines the range adds).
- Reviewers also answer the five Z8 questions and "what could be deleted" in `review_verdict.v1` (`simplicity`, `could_delete`).

## Inputs and outputs
In: `--base`, `--head`. Out: findings; exit non-zero on a hard finding from either oracle.

## State it reads and writes
Reads git history; no writes.

## Key rules and invariants
- `size_check`: in a changed `.py` file, code that already existed at base may not pass 60 lines per function, 800 per file or ruff C901 complexity 15; new code gets the tighter limits 40, 400 and 10 (`NEW_FUNC_LIMIT`, `NEW_FILE_LIMIT`, `NEW_COMPLEXITY_LIMIT`). A function or file already over is pinned in the baseline and may not grow. Ruff (on PATH, else a pinned `uvx --offline`) is a required second oracle.
- `simplicity_check` limits: function line limit 100, nesting depth 3, try-block 5 lines (`LINE_LIMIT`, `DEPTH_LIMIT`, `TRY_LIMIT`); a new function or class needs at least 2 real callers (`MIN_CALLERS`); dynamic code (`eval`, `exec`, `__import__`, `import_module`) is flagged; ruff rules E501, E702, PLR1702, S102, S307, E731, E722, S110 map to weights.
- `docs/ZENKAI.md` Z7 and Z8 are the authority; this page does not restate them.
- A frozen file (`capsule/tick.py`, `tests/mutation_check.py`, ...) may not grow: new logic goes in a new module with a one-line call site.

## Failure modes and what happens
A hard finding is a red lander gate. A baseline raise needs review agreement (the chief of staff may approve a raise under 20 lines and logs it; a standing owner rule, not in code).

## Tests that pin it
`tests/test_size_check.py`, `tests/test_simplicity_check.py`, `capsule/tests/test_ruff_gate.py`, `capsule/tests/test_shell_portability.py`; `contracts/intent/size_check.jsonl`, `simplicity_check.jsonl`.

## Related decisions and specs
Row 509; docs/superpowers/specs/2026-09-29-simplicity-doctrine-design.md (maintainer only, not in the public edition); Zenkai Z7, Z8.

## Status
current.
