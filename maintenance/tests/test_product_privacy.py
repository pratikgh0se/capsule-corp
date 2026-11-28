import os, sys, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("AGENT_OPS_NO_SEND", "1")
import product_privacy as pp

FIX = Path(__file__).resolve().parent / "fixtures" / "privacy"


def _fix(name):
    return (FIX / name).read_text(encoding="utf-8")


class G8Parsers(unittest.TestCase):
    """G8, the cheapest gate in C12: the sample app's `log_message` override is what
    keeps request data out of the logs, and *Breyer* (C-582/14) is why that
    matters - it is the property that keeps any future beacon data arguably
    non-personal. It has held since the file was written and has never been
    tested, so a regression would be silent."""

    def test_a_pass_only_override_suppresses_logging(self):
        self.assertTrue(pp.suppresses_request_logging(_fix("clean_server.py")))

    def test_a_body_that_writes_does_not(self):
        self.assertFalse(pp.suppresses_request_logging(_fix("logging_server.py")))

    def test_no_override_at_all_does_not(self):
        self.assertFalse(pp.suppresses_request_logging("class H:\n    def do_GET(self):\n        pass\n"))

    def test_a_docstring_in_the_body_is_still_a_suppression(self):
        self.assertTrue(pp.suppresses_request_logging(
            'class H:\n    def log_message(self, fmt, *a):\n        """quiet"""\n        pass\n'))

    def test_a_second_noisy_override_is_not_masked_by_a_first_quiet_one(self):
        # Fix round 1, Important 1: checking only the FIRST `log_message`
        # match let a later, noisy override pass silently - this is the one
        # bug that could make the gate say "clean" when it is not.
        text = (
            "class A:\n"
            "    def log_message(self, format, *args):\n"
            "        pass\n"
            "\n"
            "class B(A):\n"
            "    def log_message(self, format, *args):\n"
            "        sys.stderr.write(format % args)\n"
        )
        self.assertFalse(pp.suppresses_request_logging(text))

    def test_client_address_reads_are_reported_with_line_numbers(self):
        hits = pp.reads_client_address(_fix("client_address_server.py"))
        self.assertEqual(len(hits), 1)
        self.assertGreater(hits[0], 0)

    def test_a_clean_handler_reads_no_client_address(self):
        self.assertEqual(pp.reads_client_address(_fix("clean_server.py")), [])

    def test_a_commented_mention_is_not_a_read(self):
        self.assertEqual(pp.reads_client_address("# self.client_address is never read\n"), [])

    def test_the_forwarded_headers_count_as_a_read(self):
        self.assertEqual(len(pp.reads_client_address('ip = self.headers["X-Forwarded-For"]\n')), 1)


class G9Parsers(unittest.TestCase):
    """G9, row 63's "no third party" - which study 3.4 shows is a legal
    posture, not a preference: a hosted recipient drags in a DPA, sub-processor
    management and a named recipient in the notice."""

    def test_a_third_party_host_is_reported(self):
        self.assertEqual(pp.outbound_hosts(_fix("third_party.html")), ["api.plausible.io"])

    def test_relative_and_localhost_fetches_are_first_party(self):
        self.assertEqual(pp.outbound_hosts(_fix("first_party.html")), [])

    def test_a_send_beacon_to_a_third_party_is_reported(self):
        self.assertEqual(pp.outbound_hosts('navigator.sendBeacon("https://in.example.com/e", d)'),
                         ["in.example.com"])

    def test_the_scan_would_notice_nothing_and_say_nothing(self):
        self.assertEqual(pp.outbound_hosts(""), [])

    def test_a_bracketed_ipv6_loopback_is_first_party(self):
        # Fix round 1, Important 2: `host.split(":", 1)[0]` on `[::1]` used
        # to yield the bare "[", making the FIRST_PARTY entry unreachable
        # and reporting a loopback fetch as a violation.
        self.assertEqual(pp.outbound_hosts('fetch("https://[::1]/api")'), [])

    def test_a_bracketed_ipv6_loopback_with_a_port_is_first_party(self):
        self.assertEqual(pp.outbound_hosts('fetch("https://[::1]:8080/api")'), [])

    def test_a_non_loopback_ipv6_host_with_a_port_is_still_reported(self):
        self.assertEqual(pp.outbound_hosts('fetch("https://[2001:db8::1]:8080/x")'),
                         ["[2001:db8::1]"])

    def test_trailing_sentence_punctuation_is_not_part_of_the_host(self):
        # Fix round 1, Minor 5.
        self.assertEqual(pp.outbound_hosts("See https://example.com. for more."),
                         ["example.com"])


class SiblingSource(unittest.TestCase):
    """`sibling_source` against a scratch git repo this test builds and
    removes itself - never against a product repo - covering the present,
    absent-file, absent-repo and empty-file cases without depending on
    a product repo existing on this machine."""

    @classmethod
    def setUpClass(cls):
        import subprocess
        import tempfile
        cls._tmp = tempfile.TemporaryDirectory()
        cls.repo = Path(cls._tmp.name)
        run = lambda *args: subprocess.run(
            ["git", "-C", str(cls.repo), *args], check=True,
            capture_output=True, text=True)
        run("init", "-q")
        run("config", "user.email", "scratch@example.invalid")
        run("config", "user.name", "Scratch")
        (cls.repo / "present.py").write_text("pass\n", encoding="utf-8")
        (cls.repo / "empty.py").write_text("", encoding="utf-8")
        run("add", "present.py", "empty.py")
        run("commit", "-q", "-m", "scratch")

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_a_present_file_at_head_is_returned(self):
        self.assertEqual(pp.sibling_source(self.repo, "HEAD", "present.py"), "pass\n")

    def test_a_missing_file_at_head_is_none(self):
        self.assertIsNone(pp.sibling_source(self.repo, "HEAD", "nope.py"))

    def test_a_missing_repo_is_none(self):
        self.assertIsNone(pp.sibling_source(self.repo / "nope", "HEAD", "present.py"))

    def test_a_missing_ref_is_none(self):
        self.assertIsNone(pp.sibling_source(self.repo, "no-such-ref", "present.py"))

    def test_a_present_but_empty_file_is_none_not_an_empty_string(self):
        # Fix round 1, Minor 4: `git show` of an empty file exits 0 with
        # stdout "" - that used to come back as "" (not None), so a caller
        # would assert against it instead of skipping. Absent-or-unreadable
        # and present-but-empty must both read as "nothing to check".
        self.assertIsNone(pp.sibling_source(self.repo, "HEAD", "empty.py"))


if __name__ == "__main__":
    unittest.main()
