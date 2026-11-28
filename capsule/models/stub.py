"""Stub adapter: returns canned reports in order, no subprocess, no network.
Swapping `stub.StubAdapter([...])` in for `claude.ClaudeAdapter(route)` is the
one line a test or the golden runner changes (the swap test).
"""
from . import Result


class StubAdapter:
    def __init__(self, reports):
        self._reports = list(reports)

    def run(self, role, bundle, workspace, limits):
        """The next canned entry: a report document, or a whole `Result` returned as-is
        (so a test can script max_turns, rate_limit or fatal outcomes)."""
        report = self._reports.pop(0) if self._reports else None
        if isinstance(report, Result):
            return report
        return Result(report, 0, {"in": 0, "out": 0, "cache_read": 0}, 1, None, None, None, "")
