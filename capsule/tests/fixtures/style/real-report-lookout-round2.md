# Lookout shipper hardening - fix round 2 report (t_b982aacb)

status: DONE
branch: capsule/lookout-harden
worktree: /home/example/CodeBase/capsule-corp/.worktrees/lookout-harden
sha: a3a86d9 (on top of round-1 3b2ce55, which was already on origin/main's a17afa1 - no rebase needed, no conflicts)

## Starting state

Picked up mid-flight: the previous implementer (stopped by usage limit) had left round-2 work
**uncommitted** in the worktree - the I-R2-1 regex fix (`[0-9A-Z]{5}`), the I-R2-2 prices
retry-once-without-prices change, and three new guard-isolating unit tests, plus a "|"-list
extension to the fake docker fixture. I verified, finished, and committed that work rather than
redoing it.

## What I did this round

1. **I-R2-1** (already fixed by the previous implementer, verified by me): `_SQLSTATE_RE` is now
   `r"ERROR:\s+([0-9A-Z]{5}):"` (was digit-only). Added unit test
   `test_intent_letter_bearing_sqlstate_row_quarantines_and_the_rest_ships` (22P02 on one row of
   three, the rest ship, rc 0) - already present uncommitted, I verified it and left it as-is.
   Added the real-postgres case to `tests/test_migration_pg.sh` myself (`badint_pg`/`BADINT_RC`/
   `BADINT_RECOVERABLE`: a non-numeric `failure_count` triggers 22P02 via
   `jsonb_populate_record`'s own cast, before any constraint runs). Not run (no docker per
   common-rules); syntax-checked with `bash -n` - OK.

2. **I-R2-2**: each C1 guard now has its own isolating test (already present uncommitted, I
   verified and extended):
   - (a) `test_intent_one_bad_row_with_a_systemic_code_still_fails_loudly` - a mixed chunk, one
     row's own code is systemic (42P10). I added a `LOCATION:` line to the fake docker's canned
     reject output (the real two-line VERBOSITY-verbose shape) so this test - and all tests using
     `FAKE_PSQL_REJECT_SQLSTATE` - exercise the real shape, not a single-line stand-in. Fixed a
     `\s` invalid-escape `SyntaxWarning` this introduced into the fixture's module docstring.
   - (b) `test_intent_every_row_failing_the_same_way_is_never_quarantined` - every row fails the
     same recoverable code (23502), never row-shaped.
   - (c) `test_intent_prices_and_one_bad_row_only_quarantines_that_row` - prices + one bad row;
     the chunk is retried once without prices (M-R2-2) before per-row isolation finds just the
     bad row; `prices_at` stays unset.
   - The existing P1 fixture (`test_intent_systemic_sql_error_fails_loudly_with_no_quarantine`)
     was already updated to the verbose `42P10: ... \nLOCATION: ...` shape.
   - Extended `capsule/tests/fixtures/fakebin/docker`: `FAKE_PSQL_REJECT_IF_CONTAINS` now takes a
     `"|"`-separated list (lets one test trip two independent triggers - prices AND a row - in
     the same chunk), and its canned stderr always carries a `LOCATION:` line.

3. **Mutation proof** (scratch mutants on `capsule/lookout_ship.py`, restored after each, diffed
   clean against the committed original):
   - digits-only `_SQLSTATE_RE` -> `test_intent_letter_bearing_sqlstate_row_quarantines_and_the_rest_ships`: RED (`3 != 0`)
   - drop the all-rows-failed half of `if not bad or len(bad) == len(chunk_lines)` -> `test_intent_every_row_failing_the_same_way_is_never_quarantined`: RED (`0 != 3`)
   - broaden `_RECOVERABLE_CLASSES` to include `"42"` -> `test_intent_one_bad_row_with_a_systemic_code_still_fails_loudly`: RED (`0 != 3`); the P1 systemic test still passes (that one is caught by the all-rows-failed guard, confirming guard (a) is independently needed)
   - let prices ride the per-row retry (`prices=None` -> `prices=prices` in the isolation loop) -> `test_intent_prices_and_one_bad_row_only_quarantines_that_row`: RED (`3 != 0`); `test_intent_bad_prices_entry_never_gets_blamed_on_rows` still passes (confirms guard (c)'s test is the one pinning this half, not the other prices test)

   All four restores verified byte-identical to the committed file via `diff`.

4. **Z7 (optional)**: skipped - no docstring growth beyond what round 1 already trimmed; nothing
   to shrink further without weakening the "why" explanations the reviewer asked to keep.

## Gates / tests

- RED/GREEN: each guard's RED shown above under "Mutation proof"; GREEN confirmed by re-running
  the full `test_lookout_ship.py` suite after each restore (48 tests, OK) and once more at the end.
- `python3 -m unittest discover -s capsule/tests -q`: **1338 tests, OK (skipped=6)** - env
  `NOTES_VAULT=<scratchpad>/fake-vault AGENT_OPS_NO_SEND=1 SKIP_DOCKER_IMAGE_TEST=1`.
- `python3 tests/mutation_check.py --base a17afa1 --head HEAD`: **exit 0**.
- `python3 tests/size_check.py --base a17afa1 --head HEAD`: **exit 0** (`4 changed .py file(s), 141 frozen`).
- `bash -n tests/test_migration_pg.sh`: OK (not run - no docker, per common-rules and brief).
- `contracts/intent/lookout/SHA256SUMS` re-verified against `shipper.jsonl` (`sha256sum`
  matches `40bbefe9...`).

## Net lines

- Round 2 only (3b2ce55..HEAD): 6 files changed, +150/-36.
- Whole branch (origin/main a17afa1..HEAD): 9 files changed, +610/-31.

## Concerns

None. No conflicts, no rebase needed (branch was already rooted on current origin/main). No
guard was weakened to satisfy a gate; the mutation gate's own flagged mutants (revert/
assertion-weaken) all correctly FAIL, confirming the new/changed tests pin real behavior.

# Fix round 3 (after Fable review r2 of a3a86d9)

status: DONE; branch capsule/lookout-harden rebased onto c584407 (clean), sha 18d761d.
- Important 1: shipper.jsonl `bad-prices-entry-never-gets-blamed-on-rows` rewritten (rc 0, offset at end, prices_at unset); SHA256SUMS refreshed.
- Important 2: ship() prints `quarantined N row(s) to <path>`; the chunk loop prints `prices failed, rows shipped without them: <out[:300]>` (_ship_chunk now returns the first, prices-bearing failure output). Two new tests through ship(); RED first (both failed before the code), GREEN after. Two new intent examples added.
- Minor 3: comment at the all/none-failed return. Minor 4: `len(self.scripts()) == 1` pinned in the P1 systemic test; mutant (drop first `_recoverable` gate) -> RED (2 != 1), restored.
- Skipped Minors 5-6 as told.
- Gates: unit suite 1343 OK (skipped 6); mutation_check --base c584407 exit 0; size_check exit 0.
- Net lines: round 3 +45/-4 (4 files); branch vs c584407 +651/-31 (9 files).
