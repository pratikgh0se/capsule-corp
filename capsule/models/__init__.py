"""Model adapters: build the argv/env for `claude -p`, parse its output into a
`Result`, and retry transient failures a bounded number of times (spec §8.4,
row 417). The tick never calls a model directly (§2) - only adapters here do,
and only when a worker's tick asks for one.
"""
import time
from collections import namedtuple

Result = namedtuple("Result", "report exit tokens attempts error_class retry_after session_id raw stderr")
# `stderr` defaults to "": the adapter's own `raw` drops it whenever
# stdout is non-empty (never a guess to reconstruct after the fact), so a fatal exit keeps both
# separately; every existing 8-positional-arg call site is unaffected by the new trailing field.
Result.__new__.__defaults__ = ("",)


def adapter_for(route):
    """route["adapter"] (default route["provider"]) picks the adapter class."""
    name = route.get("adapter") or route.get("provider")
    if name == "claude":
        from .claude import ClaudeAdapter
        return ClaudeAdapter(route)
    if name == "stub":
        from .stub import StubAdapter
        return StubAdapter(route.get("reports", []))
    raise ValueError(f"models: unknown adapter {name!r}")


def with_retries(call, *, max_retries=3, base=2.0, sleep=time.sleep):
    """Retry only `error_class == "transient"` results, exponential backoff
    `base * 2**n` (row 417). Any other outcome (ok, rate_limit, max_turns,
    fatal) returns immediately - those are not retryable here.
    """
    attempt = 0
    while True:
        attempt += 1
        r = call()._replace(attempts=attempt)
        if r.error_class != "transient" or attempt > max_retries:
            return r
        sleep(base * 2 ** (attempt - 1))
