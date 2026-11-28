"""ui_guard - the console-error / HTTP-status guard every `tests/ui` test must use.

A Playwright test that only asserts on the DOM passes happily while the page
logs `Uncaught TypeError` and its `tick.wav` returns 404: nothing in the test,
and nothing in `ui-check`'s old `requestfailed`-only listener, ever looked
(C0 item 3, decision row 120). This module is that missing look, in one place
so every card's UI test spells it the same way:

    import ui_guard

    def setUp(self):
        self.page = self.browser.new_page()
        self.guard = ui_guard.attach(self.page)      # before the first goto()

    def test_thing(self):
        self.page.goto(self.base_url)
        ...
        ui_guard.assert_clean(self.page)             # last line of the test

`attach()` must run before `page.goto()` - it only sees events fired after it
subscribes. `assert_clean()` takes the page or the collector, and `allow=` is a
tuple of substrings matched against the recorded problem text, for the rare
expected error (`allow=("favicon",)`); an empty `allow` is the default and the
reviewer should ask about every entry in a non-empty one.

Stdlib only, and it never imports playwright: a "page" here is anything with
`.on(event, callback)`, which is what makes it unit-testable with a fake page.
It is baked into the `capsule-base` image on the interpreter's path (see
`docker/base/Dockerfile`), so `import ui_guard` works inside the worker
container; a repo that runs its UI tests elsewhere copies this file next to
its tests instead.
"""
import weakref

__all__ = ["attach", "assert_clean", "Collector", "HTTP_ERROR_FROM"]

HTTP_ERROR_FROM = 400
_ATTACHED = weakref.WeakKeyDictionary()


class Collector:
    """Console errors and failed requests seen on one page, in arrival order."""

    def __init__(self):
        self.console_errors = []
        self.failed_requests = []

    # -- recording -------------------------------------------------------
    def on_console(self, message):
        if _msg_type(message) == "error":
            self.console_errors.append(_text(message))

    def on_page_error(self, error):
        self.console_errors.append(f"pageerror: {error}")

    def on_response(self, response):
        status = getattr(response, "status", None)
        if isinstance(status, int) and status >= HTTP_ERROR_FROM:
            method = getattr(getattr(response, "request", None), "method", "GET") or "GET"
            self.failed_requests.append(f"{method} {getattr(response, 'url', '?')} {status}")

    def on_request_failed(self, request):
        method = getattr(request, "method", "GET") or "GET"
        self.failed_requests.append(
            f"{method} {getattr(request, 'url', '?')} {getattr(request, 'failure', None)}")

    # -- reading ---------------------------------------------------------
    def reset(self):
        """Forget everything so far - e.g. after a deliberate error case."""
        self.console_errors = []
        self.failed_requests = []

    def problems(self, allow=()):
        """[(kind, text), ...] for every recorded problem not matched by `allow`."""
        allow = _allow_tuple(allow)
        out = []
        for kind, items in (("console error", self.console_errors), ("failed request", self.failed_requests)):
            for text in items:
                if not any(a in text for a in allow):
                    out.append((kind, text))
        return out

    def summary(self, allow=()):
        """The same two counted lines `ui-check` prints, for an assertion message."""
        allow = _allow_tuple(allow)
        errs = [t for t in self.console_errors if not any(a in t for a in allow)]
        reqs = [t for t in self.failed_requests if not any(a in t for a in allow)]
        lines = [f"CONSOLE ERRORS: {len(errs)}"] + [f"   {t[:300]}" for t in errs[:20]]
        lines += [f"FAILED REQUESTS: {len(reqs)}"] + [f"   {t[:300]}" for t in reqs[:20]]
        return "\n".join(lines)


def _msg_type(message):
    t = getattr(message, "type", None)
    return t() if callable(t) else t


def _text(message):
    t = getattr(message, "text", None)
    return t() if callable(t) else str(t)


def _allow_tuple(allow):
    if allow is None:
        return ()
    return (allow,) if isinstance(allow, str) else tuple(allow)


def attach(page):
    """Subscribe to `page`'s console/pageerror/response/requestfailed events and
    return the `Collector`. Idempotent: a second call on the same page returns
    the collector already attached instead of double-counting."""
    existing = _collector_for(page)
    if existing is not None:
        return existing
    c = Collector()
    page.on("console", c.on_console)
    page.on("pageerror", c.on_page_error)
    page.on("response", c.on_response)
    page.on("requestfailed", c.on_request_failed)
    try:
        _ATTACHED[page] = c
    except TypeError:  # a page object that cannot be weak-referenced
        pass
    return c


def _collector_for(page_or_collector):
    if isinstance(page_or_collector, Collector):
        return page_or_collector
    try:
        return _ATTACHED.get(page_or_collector)
    except TypeError:
        return None


def assert_clean(page_or_collector, allow=()):
    """Raise AssertionError if the page logged a console error / uncaught page
    error or got any HTTP >= 400 or failed request since `attach()`.

    `allow` is a substring or tuple of substrings; a problem whose text contains
    one of them is ignored."""
    c = _collector_for(page_or_collector)
    if c is None:
        raise AssertionError("ui_guard.assert_clean: this page was never guarded - "
                             "call ui_guard.attach(page) before the first goto()")
    problems = c.problems(allow)
    if problems:
        raise AssertionError("ui_guard: the page is not clean\n" + c.summary(allow))
