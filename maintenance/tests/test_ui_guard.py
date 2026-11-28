"""docker/base/ui_guard.py - the guard every tests/ui test must attach.

Tested with a fake page (a Playwright page is just `.on(event, fn)` here), so
this runs in the plain unit suite with no browser and no playwright install.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "docker" / "base"))

import ui_guard  # noqa: E402


class FakeRequest:
    def __init__(self, method="GET"):
        self.method = method


class FakeResponse:
    def __init__(self, status, url, method="GET"):
        self.status = status
        self.url = url
        self.request = FakeRequest(method)


class FakeFailedRequest:
    def __init__(self, url, method="GET", failure="net::ERR_ABORTED"):
        self.url = url
        self.method = method
        self.failure = failure


class FakeConsoleMessage:
    def __init__(self, type_, text):
        self.type = type_
        self.text = text


class FakePage:
    """Only the surface ui_guard.attach() touches."""

    def __init__(self):
        self.handlers = {}

    def on(self, event, fn):
        self.handlers.setdefault(event, []).append(fn)

    def emit(self, event, arg):
        for fn in self.handlers.get(event, []):
            fn(arg)


class Attach(unittest.TestCase):
    def setUp(self):
        self.page = FakePage()
        self.g = ui_guard.attach(self.page)

    def test_attach_subscribes_to_the_four_events(self):
        self.assertEqual(sorted(self.page.handlers), ["console", "pageerror", "requestfailed", "response"])

    def test_console_error_is_collected_and_warning_is_not(self):
        self.page.emit("console", FakeConsoleMessage("error", "boom"))
        self.page.emit("console", FakeConsoleMessage("warning", "meh"))
        self.page.emit("console", FakeConsoleMessage("log", "hello"))
        self.assertEqual(self.g.console_errors, ["boom"])

    def test_pageerror_is_a_console_error(self):
        self.page.emit("pageerror", ValueError("uncaught x"))
        self.assertEqual(self.g.console_errors, ["pageerror: uncaught x"])

    def test_http_400_and_up_is_a_failed_request_and_200_is_not(self):
        self.page.emit("response", FakeResponse(200, "http://127.0.0.1:1/ok.js"))
        self.page.emit("response", FakeResponse(304, "http://127.0.0.1:1/cached.js"))
        self.page.emit("response", FakeResponse(404, "http://127.0.0.1:1/static/tick.wav"))
        self.page.emit("response", FakeResponse(500, "http://127.0.0.1:1/api", method="POST"))
        self.assertEqual(self.g.failed_requests,
                         ["GET http://127.0.0.1:1/static/tick.wav 404",
                          "POST http://127.0.0.1:1/api 500"])

    def test_network_failure_is_a_failed_request(self):
        self.page.emit("requestfailed", FakeFailedRequest("http://127.0.0.1:1/gone"))
        self.assertEqual(self.g.failed_requests, ["GET http://127.0.0.1:1/gone net::ERR_ABORTED"])

    def test_reset_clears_both_lists(self):
        self.page.emit("console", FakeConsoleMessage("error", "boom"))
        self.page.emit("response", FakeResponse(404, "http://127.0.0.1:1/x"))
        self.g.reset()
        self.assertEqual((self.g.console_errors, self.g.failed_requests), ([], []))

    def test_a_second_attach_on_the_same_page_returns_the_same_collector(self):
        self.assertIs(ui_guard.attach(self.page), self.g)
        self.assertEqual(len(self.page.handlers["console"]), 1)


class AssertClean(unittest.TestCase):
    def setUp(self):
        self.page = FakePage()
        self.g = ui_guard.attach(self.page)

    def test_clean_page_passes_for_page_and_for_collector(self):
        ui_guard.assert_clean(self.page)
        ui_guard.assert_clean(self.g)

    def test_console_error_fails_and_the_message_names_it(self):
        self.page.emit("console", FakeConsoleMessage("error", "Uncaught TypeError: x"))
        with self.assertRaises(AssertionError) as cm:
            ui_guard.assert_clean(self.page)
        self.assertIn("CONSOLE ERRORS: 1", str(cm.exception))
        self.assertIn("Uncaught TypeError: x", str(cm.exception))

    def test_http_error_fails_and_the_message_names_it(self):
        self.page.emit("response", FakeResponse(404, "http://127.0.0.1:1/static/tick.wav"))
        with self.assertRaises(AssertionError) as cm:
            ui_guard.assert_clean(self.g)
        self.assertIn("FAILED REQUESTS: 1", str(cm.exception))
        self.assertIn("tick.wav 404", str(cm.exception))

    def test_allow_substrings_suppress_matching_problems_only(self):
        self.page.emit("console", FakeConsoleMessage("error", "favicon noise"))
        self.page.emit("response", FakeResponse(404, "http://127.0.0.1:1/favicon.ico"))
        ui_guard.assert_clean(self.page, allow=("favicon",))
        self.page.emit("response", FakeResponse(500, "http://127.0.0.1:1/api"))
        with self.assertRaises(AssertionError):
            ui_guard.assert_clean(self.page, allow=("favicon",))

    def test_allow_accepts_a_bare_string(self):
        self.page.emit("console", FakeConsoleMessage("error", "favicon noise"))
        ui_guard.assert_clean(self.page, allow="favicon")

    def test_an_unattached_page_is_a_usage_error(self):
        with self.assertRaises(AssertionError) as cm:
            ui_guard.assert_clean(FakePage())
        self.assertIn("ui_guard.attach(page)", str(cm.exception))

    def test_problems_lists_both_kinds_without_raising(self):
        self.page.emit("console", FakeConsoleMessage("error", "boom"))
        self.page.emit("requestfailed", FakeFailedRequest("http://127.0.0.1:1/gone"))
        self.assertEqual(len(self.g.problems()), 2)
        self.assertTrue(self.g.problems(allow=("boom", "gone")) == [])


if __name__ == "__main__":
    unittest.main()
