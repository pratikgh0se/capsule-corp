"""W236: the Slack morning summary (capsule/slack_digest.py) reads the wall clock; tick tests
call silence(self) so none depends on the hour. test_slack_digest keeps its injected clock.
The once-a-day eval-check pass (eval_check.tick_step) is silenced the same way: from each Monday
00:00 local it opens a contradiction incident for the week that just closed, which broke every
incident-counting tick test on 2026-11-02. test_eval_check drives it with an injected `now`."""
from unittest import mock


def silence(case):
    for target, kw in (("capsule.slack_digest.step", {}),
                       ("capsule.eval_check.tick_step", {"return_value": []})):
        patcher = mock.patch(target, **kw)
        patcher.start()
        case.addCleanup(patcher.stop)
