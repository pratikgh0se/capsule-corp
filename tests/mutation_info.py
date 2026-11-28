"""Row 568 (card t_f51d9056): two mutation-gate heuristics report as info, not as hard fails.

Both pushed implementers to reshape or delete guard tests to satisfy the gate (t_1b26f70e,
t_f63e221f, t_d2b36dd4). They stay printed in the table for reviewers, with a reason; reviewers
keep catching weak tests. Every other hard check stays hard (a bare `@skip`, `@skipUnless`,
`@expectedFailure`, a test that passes with production reverted, ...).

  1. `@skipIf` on a new/changed test: a conditional guard (a root-uid check, a platform check)
     is a legitimate shape; the flag is `info:`-prefixed, which `_check_range_impl` already
     treats as non-failing.
  2. "pure addition explains every revert failure": the keep-and-rerun passed, so every revert
     failure came from a pure-addition symbol that was missing at base (state `symbol_kept_bad`).

Lives here, not in the frozen tests/mutation_check.py (row 514)."""
import re

#: decorator -> reason when it is info-level (None = hard flag). `@skipUnless` stays hard (row 579).
SKIP_DECOS = {
    "skip": None,
    "skipIf": ("conditional guard, not a blanket skip - the reviewer confirms the condition "
               "is environmental (root uid, platform) and the test still runs elsewhere"),
    "skipUnless": None,
    "expectedFailure": None,
}


def skip_flags(deco_window):
    """Static flags for the decorator lines just above a new/changed test's `def`."""
    flags = []
    for d, reason in SKIP_DECOS.items():
        if re.search(rf"@\w*\.?({d})\b", deco_window):
            flags.append(f"info: @{d} on a new/changed test ({reason})" if reason
                         else f"@{d} on a new/changed test")
    return flags


def kept_cell(state, why):
    """The (b) cell for a kept-and-rerun pass. `symbol_kept_info` (the weaken oracle (c)
    discriminated) is an info finding with a reason; `symbol_kept_bad` (no oracle) is hard."""
    if state == "symbol_kept_info":
        return (f"info: pure addition explains every revert failure - the rerun with the added "
                f"name(s) kept passed, so reverting production fails this file only through the "
                f"addition; the reviewer reads the test [revert: {why}]")
    return f"did not fail (BAD) [revert: {why}]"
