import os, sys, tempfile, unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from capsule.tests import tmpclean  # noqa: E402
from capsule.tests.public_tree import private_only  # noqa: E402
tmpclean.install()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("AGENT_OPS_NO_SEND", "1")
import policy_check as pc

BANNER = ("> **Draft. Not reviewed by a lawyer.** Study section 8 item 16 - a paid\n"
          "> Thai-lawyer review of this document - is parked by decision row 140.\n")
TODAY = date(2026, 9, 10)

# A realistic-length placeholder body, well above STUB_FLOOR (200 chars
# stripped) and nowhere near a one-line placeholder like "## Coming soon" -
# fix round 1: the original fixture ("Real content here.") stripped to ~19
# chars and was itself indistinguishable from a stub once the floor was
# corrected to reject actual placeholders.
REAL_BODY = (
    "## Body\n\n"
    "This is placeholder policy text used only by the test suite; it stands "
    "in for the real document body so the presence gate can tell a drafted "
    "policy from an empty stub. It is deliberately long enough to clear the "
    "stub floor with a healthy margin, well above the two hundred character "
    "threshold used by the real check.\n"
)


def _sec_txt(expires):
    # Padded past STUB_FLOOR (200 chars stripped) the same way the real
    # docs/policies/security.txt is - Policy/Canonical/Encryption lines, not
    # just Contact/Expires/Preferred-Languages, which alone strip to under
    # 100 chars.
    return (BANNER + "\nContact: mailto:someone@example.com\n"
            f"Expires: {expires}\nPreferred-Languages: en\n"
            "Policy: https://example.com/policies/support-policy\n"
            "Canonical: https://example.com/.well-known/security.txt\n"
            "Encryption: https://example.com/pgp-key.txt\n")


class NoDirectory(unittest.TestCase):
    """M271: "presence gate AFTER the policy files exist". Made mechanical -
    the gate ships before the files and is a no-op until they land, so the
    ordering cannot go wrong in either direction."""
    def test_a_missing_directory_is_a_skip_not_a_failure(self):
        with tempfile.TemporaryDirectory() as d:
            ok, lines = pc.check(Path(d) / "nope", today=TODAY, env={"OPS_POLICY_GATE": "1"})
        self.assertTrue(ok)
        self.assertEqual(lines, ["policies: skipped (docs/policies not created yet)"])


class WithFiles(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="policies-test-"))
        for name in pc.REQUIRED:
            if name.endswith(".txt"):
                (self.d / name).write_text(_sec_txt("2027-09-10T00:00:00.000Z"))
            else:
                (self.d / name).write_text(BANNER + "\n" + REAL_BODY)

    def test_all_five_present_is_ok(self):
        ok, lines = pc.check(self.d, today=TODAY)
        self.assertTrue(ok, lines)
        self.assertIn("policies: ok (5 files)", lines)

    def test_the_expiry_is_reported_with_days_left(self):
        ok, lines = pc.check(self.d, today=TODAY)
        self.assertTrue(any(l.startswith("security.txt: Expires 2027-09-10") and "365 days left" in l
                            for l in lines), lines)

    def test_a_missing_file_is_a_finding(self):
        (self.d / "terms.md").unlink()
        ok, lines = pc.check(self.d, today=TODAY, env={"OPS_POLICY_GATE": "1"})
        self.assertFalse(ok)
        self.assertIn("policies: FAILED - terms.md missing", lines)

    def test_a_missing_file_is_advisory_by_default(self):
        (self.d / "terms.md").unlink()
        ok, lines = pc.check(self.d, today=TODAY, env={})
        self.assertTrue(ok)
        self.assertIn("policies: advisory - terms.md missing", lines)

    def test_a_banner_only_file_does_not_satisfy_presence(self):
        (self.d / "terms.md").write_text(BANNER)
        ok, lines = pc.check(self.d, today=TODAY, env={"OPS_POLICY_GATE": "1"})
        self.assertFalse(ok)
        self.assertIn("policies: FAILED - terms.md is empty apart from the banner", lines)

    def test_an_expired_security_txt_is_a_finding(self):
        (self.d / "security.txt").write_text(_sec_txt("2026-01-01T00:00:00.000Z"))
        ok, lines = pc.check(self.d, today=TODAY, env={"OPS_POLICY_GATE": "1"})
        self.assertFalse(ok)
        self.assertTrue(any("EXPIRED" in l for l in lines), lines)

    def test_an_expiry_inside_thirty_days_warns_and_names_the_checklist(self):
        (self.d / "security.txt").write_text(_sec_txt("2026-09-25T00:00:00.000Z"))
        ok, lines = pc.check(self.d, today=TODAY)
        self.assertTrue(ok)
        self.assertTrue(any("expires in 15 days" in x and "renew" in x and "RUNBOOK" not in x
                            for x in lines), lines)

    def test_a_missing_or_unparseable_expires_is_a_finding(self):
        # Padded past STUB_FLOOR (no "## Body" heading here - this is a .txt,
        # not markdown) so this exercises the missing-Expires path, not the
        # stub path.
        (self.d / "security.txt").write_text(
            BANNER + "\nContact: mailto:x@example.com\n"
            "Preferred-Languages: en\n"
            "Policy: https://example.com/policies/support-policy\n"
            "Canonical: https://example.com/.well-known/security.txt\n"
            "Encryption: https://example.com/pgp-key.txt\n")
        ok, lines = pc.check(self.d, today=TODAY, env={"OPS_POLICY_GATE": "1"})
        self.assertFalse(ok)
        self.assertTrue(any("no parseable Expires" in l for l in lines), lines)


class Expires(unittest.TestCase):
    def test_iso_forms(self):
        self.assertEqual(pc.parse_expires("Expires: 2027-09-10T00:00:00.000Z"), date(2027, 9, 10))
        self.assertEqual(pc.parse_expires("expires: 2027-09-10\n"), date(2027, 9, 10))
        self.assertIsNone(pc.parse_expires("Expires: soon"))
        self.assertIsNone(pc.parse_expires("Contact: mailto:x@example.com"))

    def test_an_out_of_range_date_is_none_not_a_raise(self):
        self.assertIsNone(pc.parse_expires("Expires: 2027-13-01T00:00:00.000Z"))

    def test_a_duplicate_expires_line_uses_the_first(self):
        text = "Expires: 2027-09-10\nExpires: 2099-01-01\n"
        self.assertEqual(pc.parse_expires(text), date(2027, 9, 10))


class Gate(unittest.TestCase):
    def test_the_flag_truth_table(self):
        self.assertFalse(pc.gate_blocking({}))
        self.assertFalse(pc.gate_blocking({"OPS_POLICY_GATE": "0"}))
        self.assertTrue(pc.gate_blocking({"OPS_POLICY_GATE": "1"}))

    def test_an_unrecognized_value_stays_advisory(self):
        """Every new flag defaults OFF: a typo'd value must not silently turn
        a blocking gate on."""
        self.assertFalse(pc.gate_blocking({"OPS_POLICY_GATE": "maybe"}))


class Main(unittest.TestCase):
    def test_the_argv_path_and_exit_code(self):
        with tempfile.TemporaryDirectory() as d:
            code = pc.main([d], env={})
        self.assertEqual(code, 0)

    def test_a_blocking_failure_exits_nonzero(self):
        d = Path(tempfile.mkdtemp(prefix="policies-main-test-"))
        # No files at all: every one of the five is a missing-file finding.
        code = pc.main([str(d)], env={"OPS_POLICY_GATE": "1"})
        self.assertEqual(code, 1)


class LiveRepo(unittest.TestCase):
    @private_only
    def test_the_real_docs_policies_directory_passes(self):
        """T7 wrote the five files; this is the assertion that they are really
        there and are not stubs."""
        root = Path(__file__).resolve().parents[2]
        ok, lines = pc.check(root / "docs" / "policies", today=date.today())
        self.assertTrue(ok, lines)
        self.assertIn("policies: ok (5 files)", lines)


if __name__ == "__main__":
    unittest.main()
