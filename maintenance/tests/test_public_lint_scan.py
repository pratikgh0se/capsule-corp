"""public_lint._scan: a file with a UTF-16 byte-order mark is decoded and scanned, not skipped
because its bytes hold NULs (followups.md line 26)."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("AGENT_OPS_NO_SEND", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import public_lint  # noqa: E402

RULES = [("term", "acme-secret", [])]


class Utf16(unittest.TestCase):
    def scan(self, data):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "f.txt").write_bytes(data)
            return public_lint._scan(td, ["f.txt"], rules=RULES)

    def test_utf16_with_a_bom_is_scanned(self):
        for enc in ("utf-16", "utf-16-be"):
            data = (b"\xfe\xff" if enc.endswith("be") else b"") + "x = acme-secret\n".encode(enc)
            with self.subTest(enc):
                self.assertEqual([h[:3] for h in self.scan(data)], [("f.txt", 1, "term")])

    def test_utf16_without_a_hit_is_clean(self):
        self.assertEqual(self.scan("fine\n".encode("utf-16")), [])

    def test_plain_utf8_is_still_scanned(self):
        self.assertEqual(len(self.scan(b"acme-secret")), 1)


class SharedExemptFile(unittest.TestCase):
    """The shipped maintenance/public_lint_exempt.txt is scanned like any file and may only
    carry personal-path exemptions."""
    EX = public_lint.PUBLIC_EXEMPT

    def repo(self, td, exempt_text):
        (Path(td) / "release").mkdir()
        (Path(td) / "release/private-denylist.txt").write_text("term\tacme-secret\n")
        (Path(td) / "maintenance").mkdir()
        (Path(td) / self.EX).write_text(exempt_text)

    def test_a_private_word_in_the_file_is_flagged(self):
        with tempfile.TemporaryDirectory() as td:
            self.repo(td, "# owner acme-secret\npersonal-path\t(?!)\t\ta.py=^x$\n")
            rules, line_exempt = public_lint.load_rules(td)
            hits = public_lint._scan(td, [self.EX], rules=rules, line_exempt=line_exempt)
            self.assertEqual([h[2] for h in hits], ["term"])

    def test_a_slash_users_path_in_the_file_is_not_flagged(self):
        with tempfile.TemporaryDirectory() as td:
            self.repo(td, "personal-path\t(?!)\t\ta.py=/Use" + "rs/x\n")
            rules, line_exempt = public_lint.load_rules(td)
            self.assertEqual(public_lint._scan(td, [self.EX], rules=rules,
                                               line_exempt=line_exempt), [])

    def test_a_non_personal_path_category_is_refused(self):
        for generic_only in (False, True):
            with self.subTest(generic_only=generic_only), tempfile.TemporaryDirectory() as td:
                self.repo(td, "term\t(?!)\t\ta.py=^x$\n")
                with self.assertRaisesRegex(ValueError, "only personal-path"):
                    public_lint.load_rules(td, generic_only=generic_only)


class Binary(unittest.TestCase):
    def scan(self, data, allow=()):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "img.bin").write_bytes(data)
            return public_lint._scan(td, ["img.bin"], rules=RULES,
                                     binary_allow=public_lint._binary_allowed("\n".join(allow)))

    def test_nul_file_is_refused_with_how_to_allow(self):
        hits = self.scan(b"abc\0def")
        self.assertEqual([h[:3] for h in hits], [("img.bin", 1, "binary")])
        self.assertIn("binary-allowlist.txt", hits[0][3])

    def test_non_utf8_without_nul_is_refused(self):
        self.assertEqual(len(self.scan(b"\xff\xfa\xc3(")), 1)

    def test_allowlisted_binary_passes(self):
        self.assertEqual(self.scan(b"abc\0def", ["# c", "*.bin"]), [])

    def test_utf16_bom_text_is_not_binary(self):
        self.assertEqual(self.scan("fine\n".encode("utf-16")), [])

    def test_plain_utf8_passes(self):
        self.assertEqual(self.scan("h\u00e9llo\n".encode()), [])


class Summary(unittest.TestCase):
    """--summary / --baseline: per-category counts, report mode exits 0, ratchet exits 1 above."""
    BY = {"alpha": 3, "beta": 1}

    def run_summary(self, baseline_text=None):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as td:
            bl = None
            if baseline_text is not None:
                bl = Path(td) / "bl.txt"
                bl.write_text(baseline_text)
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = public_lint._summary(self.BY, 7, str(bl) if bl else None)
        return rc, out.getvalue(), err.getvalue()

    def test_report_mode_prints_counts_and_total_and_exits_0(self):
        rc, out, _ = self.run_summary()
        self.assertEqual(rc, 0)
        self.assertEqual(out.splitlines(), ["alpha=3", "beta=1",
                                            "public_lint: 7 public files, total=4"])

    def test_at_baseline_passes_and_lower_passes(self):
        self.assertEqual(self.run_summary("# c\nalpha\t3\nbeta\t9\n")[0], 0)

    def test_one_above_baseline_fails_and_names_the_category(self):
        rc, _, err = self.run_summary("alpha\t2\nbeta\t1\n")
        self.assertEqual(rc, 1)
        self.assertIn("alpha", err)

    def test_category_missing_from_baseline_counts_as_zero(self):
        self.assertEqual(self.run_summary("alpha\t3\n")[0], 1)

    def test_bad_baseline_is_exit_2(self):
        self.assertEqual(self.run_summary("alpha\tmany\n")[0], 2)

    def test_duplicate_category_is_exit_2(self):
        self.assertEqual(self.run_summary("alpha\t3\nalpha\t9\nbeta\t1\n")[0], 2)


class MainBaseline(unittest.TestCase):
    """main(--baseline FILE): implies the summary output, the exit code passes through."""
    HITS = [("f", 1, "alpha", "x"), ("f", 2, "alpha", "y"), ("g", 1, "beta", "z")]

    def run_main(self, baseline_text):
        import contextlib
        import io
        from unittest import mock
        with tempfile.TemporaryDirectory() as td:
            bl = Path(td) / "bl.txt"
            bl.write_text(baseline_text)
            out = io.StringIO()
            with mock.patch.object(public_lint, "_run", return_value=(["f", "g"], self.HITS)), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                rc = public_lint.main(["--baseline", str(bl)])
            with mock.patch.object(public_lint, "_run", return_value=(["f", "g"], self.HITS)), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                missing = public_lint.main(["--baseline", str(Path(td) / "nope.txt")])
        return rc, out.getvalue(), missing

    def test_exit_codes_and_summary_output(self):
        rc, out, missing = self.run_main("alpha\t2\nbeta\t1\n")
        self.assertEqual(rc, 0)
        self.assertIn("alpha=2 (baseline 2)", out)
        self.assertIn("total=3", out)
        self.assertEqual(missing, 2)
        self.assertEqual(self.run_main("alpha\t1\nbeta\t1\n")[0], 1)


if __name__ == "__main__":
    unittest.main()
