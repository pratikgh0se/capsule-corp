#!/usr/bin/env python3
"""Render observability/grafana/provisioning/alerting/policies.yaml from its
.template (deploy.sh's alert-policy section, R28). Pulled out of deploy.sh so it is
independently testable: a test can render the same template twice, outside of deploy.sh's
apply/--check install-and-compare flow, and diff the two renders directly - the property
"two consecutive renders never differ" that the live DRIFT report depends on.

Routing (D133, D229: Telegram is retired and Slack is the one channel): the root receiver is
capsule-slack-alerts, page goes there too, and notice goes to capsule-slack-activity UNMUTED
(notice-quiet mutes 00:00-24:00, which on Slack would hide every notice). The severity split
flag no longer changes the policy.

Usage: render_alert_policy.py <template path>  -> rendered file on stdout
"""
import sys


def _route(receiver, severity, extra=""):
    return (f"      - receiver: {receiver}\n"
            "        object_matchers:\n"
            f"          - [\"severity\", \"=\", \"{severity}\"]\n" + extra)


def render(text):
    routes = _route("capsule-slack-alerts", "page") + _route("capsule-slack-activity", "notice")
    return text.replace("__NOTICE_ROUTE__\n", "    routes:\n" + routes).replace(
        "__ROOT_RECEIVER__", "capsule-slack-alerts")


def main(argv):
    sys.stdout.write(render(open(argv[1]).read()))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
